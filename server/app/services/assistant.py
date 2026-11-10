"""AI 助手：常驻对话框 + 「新创项目组」草稿与落地。

复用 ``settings`` 表里的 ``ai_base_url`` / ``ai_api_key`` / ``ai_model``（OpenAI 兼容
``/chat/completions``），与「设置 → AI 助手」「日程截图导入」共用同一份配置。

设计约束：
- 只读团队数据做上下文，不让模型直接改库；写库必须经过 ``create_project`` 的显式调用。
- 模型输出一律当**数据**看：JSON 解析失败、字段越界、标题为空都会被裁掉并记入 ``warnings``。
- 失败一律抛 :class:`ApiError`，由 ``app.main`` 的统一处理器渲染成 ``{"error": ...}``。
"""
from __future__ import annotations

import json
import re
import secrets
import urllib.error
import urllib.request
from datetime import date, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..db.base import utcnow_iso
from ..db.models import AiMessage, AiSession, Issue, Label, Milestone, Team, TeamMember, User
from ..errors import ApiError
from .ai import _endpoint, get_config, is_configured
from . import activity
from . import hierarchy
from . import issues as issue_service
from . import milestones as milestone_service
from . import teams as team_service

MAX_MESSAGES = 24
MAX_MESSAGE_CHARS = 4000
MAX_CONTEXT_CHARS = 6000
MAX_MILESTONES = 8
MAX_ISSUES = 20
MAX_ISSUE_LABELS = 5

CHAT_SYSTEM = """你是 TeamFlow 的研发协作助手，帮助团队理解进度、拆分任务、准备里程碑与周计划。
要求：
1. 始终用简体中文回答，语气克制，先给结论再给依据。
2. 只用下面「团队数据快照」里的事实说话；快照里没有的信息（人数、代码、外部系统）不要编造，
   需要时明确说「需要你补充」。
3. 建议要可执行：给出具体的议题标题、验收标准或下一步动作，必要时用短列表。
4. 你不能直接修改数据；涉及写操作时，提醒用户在界面上确认或使用「引擎 → 生成提案」。
5. 回答尽量控制在 300 字以内；用户要求详细展开时再写长。"""

PROJECT_SYSTEM = """你是项目规划助手。用户会用一两句话描述一个研发项目，请你产出一个可直接落地的项目组草稿。

只输出一个 JSON 对象，不要解释文字、不要代码块标记，结构：
{"name": "项目组名称（不超过 20 字）",
 "slug": "建议的英文地址，小写字母/数字/短横线，3–24 个字符",
 "description": "一句话项目目标（不超过 80 字）",
 "milestones": [{"title": "里程碑标题", "kind": "scheduled", "start_date": "YYYY-MM-DD", "due_date": "YYYY-MM-DD",
                 "acceptance": "- [ ] 可验收的结果一\\n- [ ] 可验收的结果二"}],
 "issues": [{"title": "议题标题", "body": "要做什么、完成标准", "priority": "high|medium|low",
             "estimate_hours": 4, "milestone": 1, "labels": ["前端"]}]}

规则：
1. 里程碑 3–6 个，按交付顺序排列；start_date / due_date 必须是从今天开始的未来日期，ISO 格式；
   每两周左右一个里程碑。
2. 议题 6–14 个，每个议题只做一件事，标题以动词开头（例如「实现邮箱登录接口」）；
   milestone 写里程碑的序号（从 1 开始）。
3. 每个里程碑的 acceptance 用 2–4 条 Markdown 复选框（``- [ ] …``）写清可验收的结果。
4. estimate_hours 是 1–80 的整数；priority 只能是 high / medium / low；labels 只能从
   「前端、后端、测试、文档、设计」里选，最多 3 个，可以省略。
5. 不要编造团队已有的代码或数据；不要输出 JSON 以外的任何内容。"""


# ---------------------------------------------------------------- 配置


def check_config(cfg: dict) -> dict:
    if not is_configured(cfg):
        raise ApiError(
            "AI 助手还没有配置，请到「设置 → AI 助手」填写接口地址、模型与 API Key",
            400,
            code="ai_unconfigured",
        )
    return cfg


def status(session: Session) -> dict:
    cfg = get_config(session)
    return {
        "configured": is_configured(cfg),
        "model": cfg["model"],
        "base_url": cfg["base_url"],
    }


def _post(cfg: dict, messages: list[dict], *, temperature: float, max_tokens: int) -> str:
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    request = urllib.request.Request(
        _endpoint(cfg["base_url"]),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = (
                json.loads(exc.read().decode("utf-8", "replace"))
                .get("error", {})
                .get("message", "")
            )
        except Exception:  # noqa: BLE001 - 上游错误体不可信
            detail = ""
        raise ApiError(
            f"AI 服务返回错误（HTTP {exc.code}）：{detail or exc.reason}",
            502,
            code="ai_upstream",
        ) from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"无法连接 AI 服务：{exc.reason}", 502, code="ai_upstream") from exc
    except TimeoutError as exc:
        raise ApiError("AI 服务响应超时，请稍后重试", 504, code="ai_timeout") from exc
    try:
        content = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise ApiError("AI 服务返回的数据结构不符合预期", 502, code="ai_upstream") from exc
    return str(content)


# ---------------------------------------------------------------- 上下文


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def team_brief(session: Session, team: Team) -> str:
    counts = issue_service.counts(session, team.id)
    lines = [
        f"团队：{team.name}（/{team.slug}）",
        f"简介：{_clip(team.description, 200) or '（未填写）'}",
        "议题（只统计叶子议题）："
        f"共 {counts.get('total', 0)}，未关闭 {counts.get('open', 0)}，已关闭 {counts.get('closed', 0)}"
        f"，父议题 {counts.get('parents', 0)}",
    ]

    members: dict[int, str] = {}
    people: list[str] = []
    for member, user in session.execute(
        select(TeamMember, User)
        .join(User, User.id == TeamMember.user_id)
        .where(TeamMember.team_id == team.id)
        .order_by(TeamMember.user_id)
    ):
        name = user.display_name or user.username or f"用户{user.id}"
        members[member.user_id] = name
        skills = team_service.parse_skills(member.skills)
        names = "、".join(str(item.get("name")) for item in skills if item.get("name"))
        people.append(name + (f"[{names}]" if names else ""))

    progress = {
        milestone_id: (total, closed)
        for milestone_id, total, closed in session.execute(
            select(
                Issue.milestone_id,
                func.count(Issue.id),
                func.sum(case((Issue.state == "closed", 1), else_=0)),
            )
            .where(
                Issue.team_id == team.id,
                Issue.milestone_id.is_not(None),
                hierarchy.leaf_condition(),
            )
            .group_by(Issue.milestone_id)
        )
    }

    milestones = list(
        session.scalars(
            select(Milestone)
            .where(Milestone.team_id == team.id)
            .order_by(Milestone.number.desc())
            .limit(6)
        )
    )
    if milestones:
        lines.append("最近里程碑：")
        for milestone in reversed(milestones):
            total, closed = progress.get(milestone.id, (0, 0))
            state = "已完成" if milestone.state == "closed" else "进行中"
            percent = f"，{closed}/{total} 已完成" if total else "，还没有议题"
            due = f"，截止 {milestone.due_date}" if milestone.due_date else ""
            acceptance = _clip(milestone.acceptance, 120)
            lines.append(
                f"- #{milestone.number} {milestone.title}（{state}{due}{percent}）"
                + (f" 验收：{acceptance}" if acceptance else "")
            )

    issues = list(
        session.scalars(
            select(Issue)
            .where(Issue.team_id == team.id, Issue.state == "open")
            .order_by(Issue.updated_at.desc())
            .limit(10)
        )
    )
    if issues:
        lines.append("未关闭议题（最近更新）：")
        for issue in issues:
            owner = members.get(issue.assignee_id) if issue.assignee_id else None
            lines.append(
                f"- #{issue.number} {issue.title}（{issue.status}/{issue.priority}"
                + (f"，负责人 {owner}" if owner else "")
                + "）"
            )

    if people:
        lines.append("成员：" + "，".join(people))

    text = "\n".join(lines)
    if len(text) > MAX_CONTEXT_CHARS:
        text = text[:MAX_CONTEXT_CHARS] + "\n…（已截断）"
    return text


# ---------------------------------------------------------------- 对话


def _clean_messages(raw) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise ApiError("请先输入要问的内容", 400, code="ai_bad_request")
    cleaned = []
    for item in raw[-MAX_MESSAGES:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        if role not in ("user", "assistant"):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        cleaned.append({"role": role, "content": content[:MAX_MESSAGE_CHARS]})
    if not cleaned or cleaned[-1]["role"] != "user":
        raise ApiError("请先输入要问的内容", 400, code="ai_bad_request")
    return cleaned


def chat(session: Session, *, team: Team, messages) -> dict:
    cfg = check_config(get_config(session))
    cleaned = _clean_messages(messages)
    system = f"{CHAT_SYSTEM}\n\n团队数据快照：\n{team_brief(session, team)}"
    reply = _post(
        cfg,
        [{"role": "system", "content": system}] + cleaned,
        temperature=0.4,
        max_tokens=1200,
    ).strip()
    if not reply:
        raise ApiError("AI 没有返回内容，请稍后重试", 502, code="ai_empty")
    return {"reply": reply, "model": cfg["model"]}


# ---------------------------------------------------------------- 会话（AI 工作台）

MODES: dict[str, dict[str, str]] = {
    "ask": {"label": "问答", "hint": ""},
    "plan": {
        "label": "规划",
        "hint": "本次偏重规划：拆出里程碑与议题、给验收标准与优先级建议，明确指出还缺哪些信息。",
    },
    "review": {
        "label": "复盘",
        "hint": "本次偏重复盘：指出延期、风险、阻塞与需要拍板的事项，给可执行的下一步。",
    },
}
MAX_SESSIONS = 20
SESSION_TITLE_CHARS = 48
MAX_CONTEXT_ISSUES = 8


def mode_label(mode: str) -> str:
    return (MODES.get(mode) or MODES["ask"])["label"]


def create_session(session: Session, *, team: Team, user_id: int | None = None,
                   title: str = "", mode: str = "ask") -> AiSession:
    now = utcnow_iso()
    row = AiSession(
        team_id=team.id,
        user_id=user_id,
        title=_clip(title, SESSION_TITLE_CHARS),
        mode=mode if mode in MODES else "ask",
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def get_session_row(session: Session, *, team_id: int, session_id: int) -> AiSession:
    row = session.scalar(
        select(AiSession).where(AiSession.id == int(session_id), AiSession.team_id == team_id)
    )
    if row is None:
        raise ApiError("会话不存在或已被删除", 404, code="ai_session_missing")
    return row


def _message_dict(row: AiMessage) -> dict:
    context: list[int] = []
    try:
        parsed = json.loads(row.context or "[]")
        if isinstance(parsed, list):
            context = [int(item) for item in parsed if str(item).isdigit()]
    except (json.JSONDecodeError, TypeError, ValueError):
        context = []
    return {
        "id": row.id,
        "role": row.role,
        "content": row.content,
        "context": context,
        "created_at": row.created_at,
    }


def session_messages(session: Session, *, session_id: int, limit: int = 200) -> list[dict]:
    rows = list(
        session.scalars(
            select(AiMessage)
            .where(AiMessage.session_id == session_id)
            .order_by(AiMessage.id)
            .limit(limit)
        )
    )
    return [_message_dict(row) for row in rows]


def api_messages(messages: list[dict]) -> list[dict]:
    """只保留模型需要的 role/content，并裁掉开头多余的 assistant 消息。"""
    cleaned = [{"role": m["role"], "content": m["content"]} for m in messages
               if m["role"] in ("user", "assistant") and m["content"]]
    while cleaned and cleaned[0]["role"] != "user":
        cleaned.pop(0)
    return cleaned[-MAX_MESSAGES:]


def _append_message(session: Session, row: AiSession, role: str, content: str,
                    context: list[int] | None = None) -> AiMessage:
    now = utcnow_iso()
    message = AiMessage(
        session_id=row.id,
        role=role,
        content=str(content or "")[:MAX_MESSAGE_CHARS],
        context=json.dumps(context or [], ensure_ascii=False),
        created_at=now,
    )
    session.add(message)
    row.updated_at = now
    if not row.title and role == "user":
        row.title = _clip(content, SESSION_TITLE_CHARS) or "新的对话"
    session.flush()
    return message


def session_dict(row: AiSession, *, message_count: int = 0, last_message: str = "") -> dict:
    return {
        "id": row.id,
        "title": row.title or "新的对话",
        "mode": row.mode or "ask",
        "mode_label": mode_label(row.mode or "ask"),
        "message_count": int(message_count or 0),
        "last_message": _clip(last_message, 80),
        "user_id": row.user_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def list_sessions(session: Session, *, team_id: int, limit: int = MAX_SESSIONS) -> list[dict]:
    rows = list(
        session.scalars(
            select(AiSession)
            .where(AiSession.team_id == team_id)
            .order_by(AiSession.updated_at.desc(), AiSession.id.desc())
            .limit(max(1, min(int(limit), 60)))
        )
    )
    if not rows:
        return []
    ids = [row.id for row in rows]
    counts: dict[int, int] = {
        key: value
        for key, value in session.execute(
            select(AiMessage.session_id, func.count(AiMessage.id))
            .where(AiMessage.session_id.in_(ids))
            .group_by(AiMessage.session_id)
        )
    }
    lasts: dict[int, str] = {}
    for message in session.scalars(
        select(AiMessage).where(AiMessage.session_id.in_(ids)).order_by(AiMessage.id.desc())
    ):
        lasts.setdefault(message.session_id, message.content)
    return [
        session_dict(row, message_count=counts.get(row.id, 0), last_message=lasts.get(row.id, ""))
        for row in rows
    ]


def session_detail(session: Session, *, team_id: int, session_id: int) -> dict:
    row = get_session_row(session, team_id=team_id, session_id=session_id)
    messages = session_messages(session, session_id=row.id)
    return {"session": session_dict(row, message_count=len(messages)), "messages": messages}


def delete_session(session: Session, *, team_id: int, session_id: int) -> None:
    row = get_session_row(session, team_id=team_id, session_id=session_id)
    session.delete(row)


def context_block(session: Session, *, team: Team, numbers: list[int]) -> str:
    """把用户在输入框里 @ 的议题展开成一段上下文（正文截断，避免撑爆 prompt）。"""
    picked = []
    for number in numbers[:MAX_CONTEXT_ISSUES]:
        issue = session.scalar(
            select(Issue).where(Issue.team_id == team.id, Issue.number == int(number))
        )
        if issue is not None:
            picked.append(issue)
    if not picked:
        return ""
    lines = ["用户点名要看的议题："]
    for issue in picked:
        lines.append(
            f"- #{issue.number} {issue.title}（{issue.status}/{issue.priority}，{issue.state}）"
        )
        body = _clip(issue.body, 400)
        if body:
            lines.append(f"  正文：{body}")
    return "\n".join(lines)


def chat_turn(session: Session, *, team: Team, message: str = "", messages=None,
              session_id: int | None = None, context=None, user_id: int | None = None,
              mode: str = "ask") -> dict:
    """一轮对话：可带会话（落库）也可无会话（老接口兼容）。"""
    cfg = check_config(get_config(session))
    mode = mode if mode in MODES else "ask"
    numbers: list[int] = []
    if isinstance(context, list):
        for item in context[:MAX_CONTEXT_ISSUES]:
            text = str(item).strip().lstrip("#")
            if text.isdigit():
                numbers.append(int(text))

    row: AiSession | None = None
    history: list[dict] = []
    if session_id is not None:
        row = get_session_row(session, team_id=team.id, session_id=session_id)
        history = api_messages(session_messages(session, session_id=row.id))
        history = history[-MAX_MESSAGES:]
    elif message:
        row = create_session(session, team=team, user_id=user_id, mode=mode)

    if message:
        pending = _clean_messages([{"role": "user", "content": message}])[-1]
        cleaned = (history + [pending])[-MAX_MESSAGES:]
    elif row is not None:
        raise ApiError("请先输入要问的内容", 400, code="ai_bad_request")
    else:
        cleaned = _clean_messages(messages)

    parts = [CHAT_SYSTEM]
    hint = (MODES.get(mode) or {}).get("hint") or ""
    if hint:
        parts.append(hint)
    parts.append("团队数据快照：\n" + team_brief(session, team))
    extra = context_block(session, team=team, numbers=numbers) if numbers else ""
    if extra:
        parts.append(extra)

    reply = _post(
        cfg,
        [{"role": "system", "content": "\n\n".join(parts)}] + cleaned,
        temperature=0.4,
        max_tokens=1200,
    ).strip()
    if not reply:
        raise ApiError("AI 没有返回内容，请稍后重试", 502, code="ai_empty")

    result = {"reply": reply, "model": cfg["model"], "session_id": None, "title": "",
              "context": numbers, "mode": mode}
    if row is not None:
        _append_message(session, row, "user", cleaned[-1]["content"], numbers)
        _append_message(session, row, "assistant", reply)
        result.update(
            {
                "session_id": row.id,
                "title": row.title or "新的对话",
                "mode": row.mode or mode,
                "updated_at": row.updated_at,
            }
        )
    return result


# ---------------------------------------------------------------- 项目组草稿


def _extract_json(content: str) -> dict:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ApiError("AI 没有返回可用的项目草稿，请换个说法重试", 502, code="ai_bad_json")
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ApiError("AI 返回的项目草稿不是合法 JSON，请重试", 502, code="ai_bad_json") from exc
    if not isinstance(obj, dict):
        raise ApiError("AI 返回的项目草稿结构不正确，请重试", 502, code="ai_bad_json")
    return obj


def _clean_date(value) -> str | None:
    return milestone_service.parse_date(value)


def normalize_draft(raw: dict, *, fallback_name: str = "") -> dict:
    warnings: list[str] = []
    name = _clip(raw.get("name") or fallback_name or "新项目组", 40) or "新项目组"
    description = str(raw.get("description") or "").strip()[:300]
    slug = ""
    raw_slug = str(raw.get("slug") or "").strip()
    if raw_slug:
        try:
            slug = team_service.clean_slug(raw_slug)
        except ValueError:
            warnings.append("AI 给的标识不合法，已改为按名称自动生成")

    today = date.today()
    milestones = []
    source = raw.get("milestones") if isinstance(raw.get("milestones"), list) else []
    for index, item in enumerate(source[:MAX_MILESTONES], 1):
        if not isinstance(item, dict):
            continue
        title = _clip(item.get("title"), 120)
        if not title:
            continue
        acceptance = item.get("acceptance")
        if isinstance(acceptance, list):
            parts = []
            for part in acceptance:
                text = re.sub(r"^\s*[-*+]\s*(\[[ xX]\]\s*)?", "", str(part)).strip()
                if text:
                    parts.append(f"- [ ] {text[:160]}")
            acceptance = "\n".join(parts)
        acceptance = str(acceptance or "").strip()[:4000]
        start = _clean_date(item.get("start_date"))
        due = _clean_date(item.get("due_date"))
        if start and due and start > due:
            start, due = due, start
            warnings.append(f"里程碑「{title}」的起止日期颠倒，已自动交换")
        if not due:
            due = (today + timedelta(days=14 * index)).isoformat()
        kind = "continuous" if str(item.get("kind") or "").lower() == "continuous" else "scheduled"
        milestones.append(
            {
                "title": title,
                "acceptance": acceptance,
                "kind": kind,
                "start_date": None if kind == "continuous" else start,
                "due_date": None if kind == "continuous" else due,
            }
        )
    if not milestones:
        warnings.append("AI 没有给出里程碑，已创建一个默认里程碑")

    issues = []
    source = raw.get("issues") if isinstance(raw.get("issues"), list) else []
    for item in source[:MAX_ISSUES]:
        if not isinstance(item, dict):
            continue
        title = _clip(item.get("title"), 200)
        if not title:
            continue
        milestone_ref = item.get("milestone")
        try:
            milestone_index = int(milestone_ref)
        except (TypeError, ValueError):
            milestone_index = 0
        priority = str(item.get("priority") or "medium").lower()
        if priority not in ("high", "medium", "low"):
            priority = "medium"
        try:
            hours = float(item.get("estimate_hours"))
        except (TypeError, ValueError):
            hours = 0.0
        hours = round(min(max(hours, 0.0), 80.0), 1) or None
        labels = item.get("labels") if isinstance(item.get("labels"), list) else []
        labels = [_clip(label, 20) for label in labels if str(label).strip()][:MAX_ISSUE_LABELS]
        issues.append(
            {
                "title": title,
                "body": str(item.get("body") or "").strip()[:4000],
                "priority": priority,
                "estimate_hours": hours,
                "milestone": milestone_index if 1 <= milestone_index <= len(milestones) else 0,
                "labels": labels,
            }
        )
    if not issues:
        warnings.append("AI 没有给出议题，项目组只会包含里程碑")

    return {
        "name": name,
        "slug": slug,
        "description": description,
        "milestones": milestones,
        "issues": issues,
        "warnings": warnings,
    }


def draft_project(session: Session, *, goal: str, name_hint: str = "") -> dict:
    cfg = check_config(get_config(session))
    goal = str(goal or "").strip()
    if len(goal) < 4:
        raise ApiError("请至少用一句话描述这个项目组要做什么", 400, code="ai_bad_request")
    prompt = [
        "项目描述：",
        goal[:2000],
        "",
        f"今天是 {date.today().isoformat()}。",
    ]
    if name_hint.strip():
        prompt.insert(0, f"项目组名称参考：{name_hint.strip()[:40]}")
    content = _post(
        cfg,
        [
            {"role": "system", "content": PROJECT_SYSTEM},
            {"role": "user", "content": "\n".join(prompt)},
        ],
        temperature=0.5,
        max_tokens=2048,
    )
    draft = normalize_draft(_extract_json(content), fallback_name=name_hint)
    return {"draft": draft, "model": cfg["model"]}


# ---------------------------------------------------------------- 落地


def _label_ids(session: Session, team: Team, names: list[str]) -> list[int]:
    if not names:
        return []
    rows = {
        row.name: row.id
        for row in session.scalars(select(Label).where(Label.team_id == team.id))
    }
    ids = []
    for name in names:
        found = rows.get(name)
        if found is None:
            label = Label(team_id=team.id, name=name[:40], color="#6b7280")
            session.add(label)
            session.flush()
            rows[name] = label.id
            found = label.id
        if found not in ids:
            ids.append(found)
    return ids[:MAX_ISSUE_LABELS]


def create_project(session: Session, draft: dict, *, owner_id: int | None = None,
                   actor: tuple[int | None, str] = (None, "系统")) -> dict:
    """按草稿一次性建出团队、里程碑与议题（调用方负责事务提交）。"""
    name = _clip(draft.get("name"), 40) or "新项目组"
    description = str(draft.get("description") or "").strip()[:300]
    slug = None
    raw_slug = str(draft.get("slug") or "").strip()
    if raw_slug:
        try:
            slug = team_service.unique_slug(session, team_service.clean_slug(raw_slug))
        except ValueError:
            slug = None
    if not slug:
        # 中文项目名没法直接做地址，退化成一个短地址而不是让 /team 这种泛化 slug 占位
        fallback = f"proj-{secrets.token_hex(2)}"
        try:
            slug = team_service.unique_slug(session, team_service.clean_slug(fallback))
        except ValueError:
            slug = None
    team = team_service.create_team(
        session, name=name, slug=slug, description=description, owner_id=owner_id,
    )

    created_milestones: list[Milestone] = []
    by_index: dict[int, int] = {}
    for index, item in enumerate(draft.get("milestones") or [], 1):
        milestone = milestone_service.create(
            session,
            team,
            {
                "title": item.get("title"),
                "acceptance": item.get("acceptance") or "",
                "kind": item.get("kind") or "scheduled",
                "start_date": item.get("start_date"),
                "due_date": item.get("due_date"),
            },
        )
        by_index[index] = milestone.id
        created_milestones.append(milestone)

    created_issues = []
    for item in draft.get("issues") or []:
        number = team_service.next_issue_number(session, team)
        data = {
            "title": item.get("title"),
            "body": item.get("body") or "",
            "priority": item.get("priority") or "medium",
            "status": "todo",
            "milestone_id": by_index.get(int(item.get("milestone") or 0)),
            "estimate_hours": item.get("estimate_hours"),
            "author_id": owner_id,
            "label_ids": _label_ids(session, team, list(item.get("labels") or [])),
        }
        created_issues.append(issue_service.create_issue(session, team, data, number))

    actor_id, actor_name = actor
    activity.log(
        session, team.id, actor_id, actor_name, "created", "team", team.id, team.name,
        f"AI 生成项目组：{len(created_milestones)} 个里程碑、{len(created_issues)} 个议题",
    )
    return {
        "team": team,
        "milestones": [milestone.number for milestone in created_milestones],
        "issues": [issue.number for issue in created_issues],
        "created_at": utcnow_iso(),
    }
