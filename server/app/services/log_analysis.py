"""日志实时分析：规则推断打底，配置了模型就交给模型写成人话。

端侧每上报一个「无人操作窗口」，这里同步给出判断——
完成度、状态、风险、建议，2~4 句，直接显示在权限页与客户端窗口里。

设计约束：

* **不许空等**：模型超时/报错一律回退到规则文案，日志照常入库；
* **不许瞎编**：模型只拿到日志摘要与规则结果，要求它不得编造未出现的文件；
* **可追溯**：返回文本里保留规则判断的完成度与置信度，方便和洞察对账。
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from ..db.models import ClientLog
from ..services import ai as ai_service
from ..services.ai import _endpoint  # 复用「OpenAI 兼容端点」的拼装规则

log = logging.getLogger("teamflow.log_analysis")

SYSTEM_PROMPT = """你是研发团队的进度分析助手。用户会给你一段「没人操作电脑后，工具捕捉到的文件改动摘要」。
请用中文给出 2~4 句判断，必须包含：
1) 这段改动最可能在做什么；
2) 进度判断（推进中／接近完成／完成）与一句理由；
3) 需要注意的风险或下一步动作（没有就说"无明显风险"）。
只依据给定信息，不要编造未出现的文件名、人名或数字。不要用列表，直接写一段话。"""


def _content_hint(result: dict) -> str:
    """把「这六行到底是什么」说出来。

    ``log_analysis`` 原来只看 ``files`` 的路径，静默模式甚至连路径都没有，
    于是永远只能输出「N 个文件改动」。这里优先用端侧上行的结构摘要：
    新增了哪些函数 → 新增了哪几行内容（文档正文/代码骨架）→ 注释说明。
    """
    signals = result.get("signals") if isinstance(result, dict) else None
    if signals is None:
        return ""

    def names(field: str) -> list[str]:
        return [str(item) for item in (getattr(signals, field, None) or []) if str(item).strip()]

    added = names("symbols_added")
    removed = names("symbols_removed")
    lines = names("content_lines")
    notes = names("notes_added")

    bits: list[str] = []
    if added:
        bits.append("新增 " + "、".join(f"{name}()" for name in added[:5]))
    if removed:
        bits.append("移除 " + "、".join(f"{name}()" for name in removed[:3]))
    if notes:
        bits.append("改动说明「" + "；".join(notes[:2]) + "」")
    if lines and not added:
        # 没有函数级符号（文档、配置、纯文本）时，把新增的几行读出来，
        # 这是「多了六行」和「这六行写了什么」之间唯一的差别
        bits.append("新增内容「" + "；".join(lines[:4]) + "」")
    if not bits:
        return ""
    return "本轮" + "，".join(bits) + "。"


def _files_hint(files, result: dict | None = None) -> str:
    """根据改动文件的类型给一句判断：没匹配到议题时也能用。

    静默模式没有路径明细，这时退回规则层的分类计数（code/test/doc_files），
    否则「1 个源码文件 + 1 个测试文件」这种基础信息都会丢。
    """
    paths = []
    for item in files or []:
        path = getattr(item, "path", None) or (item.get("path") if isinstance(item, dict) else "")
        if path:
            paths.append(str(path).lower())

    signals = (result or {}).get("signals")
    if not paths and signals is not None:
        tests = int(getattr(signals, "test_files", 0) or 0)
        docs = int(getattr(signals, "doc_files", 0) or 0)
        code = int(getattr(signals, "code_files", 0) or 0)
        if not (tests or docs or code):
            return ""
        parts = [f"{n} 个{name}" for n, name in
                 ((code, "源码文件"), (tests, "测试文件"), (docs, "文档")) if n > 0]
        if tests and code:
            verdict = "同时在补测试，通常意味着这块功能已基本成型"
        elif tests:
            verdict = "只在补测试，更像收尾或回归验证"
        elif docs and not code:
            verdict = "只动了文档，属于整理说明而不是功能推进"
        else:
            verdict = "以源码改动为主，仍在开发过程中"
        return f"改动包含 {'、'.join(parts)}，{verdict}。"

    if not paths:
        return ""

    # 每个文件只能进一个桶：以前 tests 和 docs 会重复命中同一个路径，
    # code = 总数 - tests - docs 于是算成负数（真出现过「-1 个源码文件」）
    tests = docs = code = 0
    for path in paths:
        if any(k in path for k in ("test", "spec", "__tests__")):
            tests += 1
        elif any(k in path for k in (".md", ".txt", ".rst", "readme")):
            docs += 1
        else:
            code += 1

    parts = []
    if code:
        parts.append(f"{code} 个源码文件")
    if tests:
        parts.append(f"{tests} 个测试文件")
    if docs:
        parts.append(f"{docs} 个文档")

    if tests and code:
        verdict = "同时在补测试，通常意味着这块功能已基本成型"
    elif tests:
        verdict = "只在补测试，更像收尾或回归验证"
    elif docs and not code:
        verdict = "只动了文档，属于整理说明而不是功能推进"
    else:
        verdict = "以源码改动为主，仍在开发过程中"

    tail = "未匹配到对应议题，建议分支名带上编号（如 feat/12-order）以便自动关联"
    return f"改动包含 {'、'.join(parts)}，{verdict}；{tail}。"


def _rules_text(result: dict, row: ClientLog, files=None) -> str:
    """规则版文案：即使没有模型也能看懂。"""
    confidence_raw = float(result.get("confidence") or 0)
    content = _content_hint(result)
    if confidence_raw < 0.2:
        hint = _files_hint(files, result)
        base = f"{row.file_count} 个文件改动（+{row.additions}/-{row.deletions} 行）。"
        if content:
            # 内容线索比「几个文件」重要得多，放在最前面
            return content + base + (hint or "")
        return base + (hint or "没有足够的信号判断进度，建议补充议题关联。")

    status_map = {"in_progress": "仍在推进中", "done": "可以视为完成"}
    status = status_map.get(str(result.get("status") or ""), "仍在推进中")
    completion = int(result.get("completion") or 0)
    confidence = float(result.get("confidence") or 0)
    head = (
        f"{row.file_count} 个文件改动（+{row.additions}/-{row.deletions} 行），"
        f"规则判断：{status}，完成度约 {completion}%，置信度 {confidence:.2f}。"
    )
    summary = str(result.get("summary") or "").strip()
    # summary 里已经带了符号/内容行；这里再补一条，防止模型版摘要把细节吃掉
    detail = content if content and content not in summary else ""
    risks = result.get("risks") or []
    tail = f"风险：{'；'.join(str(r) for r in risks[:2])}" if risks else "无明显风险。"
    return " ".join(x for x in [head, detail, summary, tail] if x)


def _call_model(cfg: dict, prompt: str) -> str:
    """调用 OpenAI 兼容的 chat/completions，返回纯文本。"""
    url = _endpoint(cfg["base_url"])
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        # 推理型模型（deepseek-flash 之类）会先花 token 想，再写正文。
        # 320 的预算曾被推理全部吃掉，返回正文为空 → 静默回退成「几个文件」，
        # 看起来就像「AI 根本不会看内容」。这里必须留够正文空间。
        "max_tokens": 1200,
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {cfg['api_key']}")
    with urllib.request.urlopen(request, timeout=12) as response:
        body = json.loads(response.read().decode("utf-8", "replace"))
    choices = body.get("choices") or []
    if not choices:
        raise ValueError("模型没有返回内容")
    choice = choices[0]
    content = str((choice.get("message") or {}).get("content") or "").strip()
    if not content:
        # 推理型模型把预算耗在 reasoning 上时，content 会是空串。
        # 这种情况必须当失败处理，否则会拿一条空分析覆盖规则文案。
        raise ValueError(f"模型没有返回正文（finish_reason={choice.get('finish_reason')}）")
    if choice.get("finish_reason") == "length":
        raise ValueError("模型输出被截断，改用规则文案")
    return content


def analyze_log(session, row: ClientLog, result: dict, files=None) -> str:
    """给一条日志生成分析文本：优先模型，失败回退规则。"""
    fallback = _rules_text(result, row, files)
    cfg = ai_service.get_config(session)
    if not ai_service.is_configured(cfg):
        return fallback

    prompt = (
        f"仓库：{row.repo_label or '未命名'}　分支：{row.branch or '未命名'}\n"
        f"无人操作时长：{row.idle_minutes} 分钟\n"
        f"改动统计：{row.file_count} 个文件，+{row.additions}/-{row.deletions} 行\n"
        f"内容线索（端侧本地抽取，已脱敏）：{_content_hint(result) or '无'}\n"
        f"改动摘要：\n{row.summary or '（静默模式，未提供文件明细）'}\n\n"
        "请重点说明：这些改动具体在实现什么功能，而不只是「改了几个文件」。\n\n"
        f"规则推断（仅供参考，不要照抄数字）：完成度 {result.get('completion', 0)}%，"
        f"状态 {result.get('status', '')}，置信度 {float(result.get('confidence') or 0):.2f}，"
        f"摘要：{result.get('summary', '')}"
    )
    try:
        text = _call_model(cfg, prompt)
        # 太短的回复（例如只写了半句）不如规则文案可用
        if len(text) >= 40:
            return text
        log.warning("模型返回过短（%s 字），回退规则文案", len(text))
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
        log.warning("日志模型分析失败，回退规则文案：%s", exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("日志模型分析异常：%s", exc)
    return fallback
