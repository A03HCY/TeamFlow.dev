"""OpenAI 兼容多模态接口：把日程截图解析成结构化草稿。

配置存 settings 表：``ai_base_url`` / ``ai_api_key`` / ``ai_model``。
只用标准库 urllib 发请求（配了 ``httpx`` 也只是给 TestClient 用）。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from sqlalchemy.orm import Session

from .settings import get_setting

MAX_IMAGES = 6
MAX_IMAGE_CHARS = 5_000_000        # 单张 base64 上限（约 3.7MB 原图）
MAX_ENTRIES = 80

SYSTEM_PROMPT = """你是日程录入助手。用户会上传一张或多张日程安排、计划表的截图，请把它们解析成结构化的日程列表。

只输出 JSON，不要任何解释文字或代码块标记，格式：
{"entries": [{"title": "...", "kind": "recurring|once", "weekday": 0, "date": "YYYY-MM-DD", "start_time": "HH:MM", "end_time": "HH:MM", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "note": "", "confidence": "high|low"}], "questions": ["..."]}

字段规则：
1. kind=recurring 表示每周重复（weekday 必填，0=周一 … 6=周日）；kind=once 表示单日事项（date 必填）。
2. 如果截图里标注了这组安排的起止日期（生效范围），必须填 start_date / end_date。
3. 【严禁编造】截图里看不清或没有的信息一律填 null，绝不推测、绝不补全：
   - 表格里没有具体时间 → start_time / end_time 填 null；
   - 分不清是每周循环还是单日事项 → 按表头判断；判断不了就按 recurring 处理并在 questions 里说明；
   - 日期残缺或格式混乱时尝试归一化为 YYYY-MM-DD，归一化不了就填 null。
4. 所有不确定、看不清、互相矛盾、需要用户确认的信息，用中文写进 questions 数组（每条一个问题，例如"表格里没有标注每项安排的起止时间，请补充"）。没有疑问则为空数组。
5. 多张图片属于同一份安排时合并解析并去重；不属于同一份时也要全部解析出来。
6. note 里可以放原文中的补充信息（地点、教师、周次等），没有就留空字符串。"""


def get_config(session: Session) -> dict:
    return {
        "base_url": (get_setting(session, "ai_base_url") or "").strip(),
        "api_key": (get_setting(session, "ai_api_key") or "").strip(),
        "model": (get_setting(session, "ai_model") or "").strip(),
    }


def is_configured(cfg: dict) -> bool:
    return bool(cfg["base_url"] and cfg["api_key"] and cfg["model"])


def _endpoint(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if not re.search(r"/v\d+(?:[a-z]+)?$", base):
        base += "/v1"
    return base + "/chat/completions"


def _validate_images(images) -> list[str]:
    if not isinstance(images, list) or not images:
        raise ValueError("请先选择至少一张图片")
    if len(images) > MAX_IMAGES:
        raise ValueError(f"一次最多上传 {MAX_IMAGES} 张图片")
    out = []
    for i, img in enumerate(images, 1):
        if not isinstance(img, str) or not img.startswith("data:image/"):
            raise ValueError(f"第 {i} 张图片格式不正确，请重新选择")
        if len(img) > MAX_IMAGE_CHARS:
            raise ValueError(f"第 {i} 张图片太大，请压缩后重试")
        out.append(img)
    return out


def _build_user_content(images: list[str], answers: str, previous) -> list:
    parts = [{"type": "text", "text": "请解析这些截图里的日程安排。"}]
    if previous:
        parts[0]["text"] += (
            "\n\n上一轮解析草稿（JSON，供参考；请结合用户的回答修正后重新输出完整结果）：\n"
            + json.dumps(previous, ensure_ascii=False)
        )
    if answers:
        parts[0]["text"] += "\n\n用户对上一轮问题的回答：\n" + answers
    for img in images:
        parts.append({"type": "image_url", "image_url": {"url": img}})
    return parts


def _request(cfg: dict, images: list[str], answers: str, previous, strict: bool) -> str:
    system = SYSTEM_PROMPT
    if strict:
        system += "\n再次强调：只输出一个 JSON 对象，以 { 开头、以 } 结尾，不要输出任何其他内容。"
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": _build_user_content(images, answers, previous)},
        ],
        "temperature": 0,
        "max_tokens": 4096,
    }
    req = urllib.request.Request(
        _endpoint(cfg["base_url"]),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8", "replace")).get("error", {}).get("message", "")
        except Exception:
            pass
        raise ValueError(f"AI 服务返回错误（HTTP {e.code}）：{detail or e.reason}") from e
    except urllib.error.URLError as e:
        raise ValueError(f"无法连接 AI 服务：{e.reason}") from e
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError("AI 服务返回的数据结构不符合预期") from e


def _extract_json(content: str) -> dict:
    s = content.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("AI 没有返回 JSON 内容")
    try:
        obj = json.loads(s[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError("AI 返回的内容无法解析为 JSON") from e
    if not isinstance(obj, dict):
        raise ValueError("AI 返回的 JSON 结构不正确")
    return obj


_WEEKDAY_CN = {"周一": 0, "星期一": 0, "周二": 1, "星期二": 1, "周三": 2, "星期三": 2,
               "周四": 3, "星期四": 3, "周五": 4, "星期五": 4, "周六": 5, "星期六": 5,
               "周日": 6, "星期日": 6, "星期天": 6, "周天": 6}
_TIME_RE = re.compile(r"([01]?\d|2[0-3])[:：]([0-5]\d)")
_TIME_COMPACT_RE = re.compile(r"^([01]?\d|2[0-3])([0-5]\d)$")
_DATE_RE = re.compile(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})")


def _coerce_time(v):
    if v in (None, "", "null"):
        return None
    s = str(v).strip()
    m = _TIME_RE.search(s)
    if not m:
        m = _TIME_COMPACT_RE.match(s.replace(":", "").replace("：", ""))
        if not m:
            return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _coerce_date(v):
    if v in (None, "", "null"):
        return None
    m = _DATE_RE.search(str(v))
    if not m:
        return None
    import datetime

    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def _coerce_weekday(v):
    if v in (None, "", "null") or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        n = int(v)
        if 0 <= n <= 6:
            return n
        return 6 if n == 7 else None
    s = str(v).strip()
    if s.isdigit():
        n = int(s)
        return n - 1 if 1 <= n <= 7 else None
    for key, index in _WEEKDAY_CN.items():
        if key in s:
            return index
    return None


def clean_entry(entry) -> dict:
    """宽容地清洗单条 AI 输出：能救的字段救回来，救不了的置 null。"""
    title = str(entry.get("title") or "").strip()[:200]
    kind = entry.get("kind")
    weekday = _coerce_weekday(entry.get("weekday"))
    date_value = _coerce_date(entry.get("date"))
    if kind not in ("recurring", "once"):
        kind = "recurring" if (weekday is not None and date_value is None) else "once"
    if kind == "recurring" and weekday is None and date_value is not None:
        kind = "once"
    if kind == "once" and date_value is None and weekday is not None:
        kind = "recurring"
    return {
        "title": title,
        "kind": kind,
        "weekday": weekday if kind == "recurring" else None,
        "date": date_value if kind == "once" else None,
        "start_time": _coerce_time(entry.get("start_time")),
        "end_time": _coerce_time(entry.get("end_time")),
        "start_date": _coerce_date(entry.get("start_date")),
        "end_date": _coerce_date(entry.get("end_date")),
        "note": str(entry.get("note") or "").strip()[:500],
        "confidence": "low" if entry.get("confidence") == "low" else "high",
    }


def parse(session: Session, images, answers: str = "", previous=None) -> dict:
    """解析截图；解析失败自动重试一次（附加更强约束）。"""
    cfg = get_config(session)
    if not is_configured(cfg):
        raise ValueError("请先在设置里配置 AI 服务")
    imgs = _validate_images(images)
    if previous is not None and not isinstance(previous, list):
        previous = None
    last_err = None
    for strict in (False, True):
        content = _request(cfg, imgs, answers, previous, strict)
        try:
            obj = _extract_json(content)
        except ValueError as e:
            last_err = e
            continue
        raw_entries = obj.get("entries")
        entries = []
        if isinstance(raw_entries, list):
            for entry in raw_entries[:MAX_ENTRIES]:
                if isinstance(entry, dict):
                    cleaned = clean_entry(entry)
                    if cleaned["title"]:
                        entries.append(cleaned)
        questions = obj.get("questions")
        return {
            "entries": entries,
            "questions": [str(q).strip() for q in questions if str(q).strip()][:10]
            if isinstance(questions, list) else [],
            "model": cfg["model"],
        }
    raise last_err or ValueError("AI 返回内容无法解析")
