"""结构化输出：JSON Schema + 异步封装 + 防幻觉校验。

两个坑在这里被一次性处理掉：

1. ``orchify.structured.ask_structured`` 内部调**同步**的 ``agent.ask``，在 FastAPI
   的事件循环里直接调用会阻塞整个 loop（并且 orchify 明确禁止在编排循环线程上等）。
   所以这里统一用 ``asyncio.to_thread`` 包一层，对外暴露 ``aask_structured``。
2. 模型返回的 JSON 即使 schema 通过，也可能「证据为空却给高置信度」。
   ``check_analysis`` 做的是语义层的硬闸门（对应方案 §5.3 的 D8 三规则）。

schema 只使用 orchify 支持的子集：type / properties / required / items / enum /
description / additionalProperties。不要用 $ref、format、allOf。
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from .errors import EngineError

# ---------------------------------------------------------------- Schema

ANALYSIS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "issue_number": {"type": "integer", "description": "议题编号，不知道就填 0"},
        "completion": {"type": "integer", "description": "0-100 的完成度"},
        "status": {"type": "string", "enum": ["todo", "in_progress", "done"]},
        "confidence": {"type": "number", "description": "0-1 的置信度"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["file", "test", "commit", "issue"]},
                    "ref": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["type", "ref"],
                "additionalProperties": False,
            },
        },
        "risks": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["completion", "status", "confidence", "evidence", "summary"],
    "additionalProperties": False,
}

PREPROCESS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "issue_number": {"type": "integer"},
                    "reason": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "score": {"type": "number"},
                },
                "required": ["issue_number", "reason"],
                "additionalProperties": False,
            },
        },
        "noise": {"type": "array", "items": {"type": "string"}},
        "focus": {"type": "string"},
    },
    "required": ["matches"],
    "additionalProperties": False,
}

COORDINATE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["accept", "adjust", "hold"]},
        "adjustments": {
            "type": "object",
            "properties": {
                "completion": {"type": "integer"},
                "status": {"type": "string", "enum": ["todo", "in_progress", "done"]},
                "confidence": {"type": "number"},
            },
            "additionalProperties": False,
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict"],
    "additionalProperties": False,
}

REPORT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "suggestion": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["todo", "in_progress", "done"]},
                "plan_week": {"type": "string"},
                "comment": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "followups": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "summary"],
    "additionalProperties": False,
}

SCHEMAS = {
    "preprocess": PREPROCESS_SCHEMA,
    "analysis": ANALYSIS_SCHEMA,
    "coordinate": COORDINATE_SCHEMA,
    "report": REPORT_SCHEMA,
}

# ---------------------------------------------------------------- 调用

async def aask_structured(
    agent,  # noqa: ANN001
    prompt: str,
    schema: dict,
    *,
    retries: int = 2,
    name: str = "result",
    timeout: float | None = None,
    max_steps: int | None = None,
) -> Any:
    """异步版 ``ask_structured``：在线程池里跑同步实现，避免卡住事件循环。"""
    from orchify.structured import ask_structured

    def _call() -> Any:
        kwargs: dict = {"retries": retries, "name": name}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if max_steps is not None:
            kwargs["max_steps"] = max_steps
        return ask_structured(agent, prompt, schema, **kwargs)

    return await asyncio.to_thread(_call)


# ---------------------------------------------------------------- 防幻觉闸门

# 明显是「提示词注入」的文本模式：出现时一律当作数据处理，并记一条风险
INJECTION_PATTERNS = (
    re.compile(r"忽略(以上|之前|前面).{0,6}(指令|提示|规则)"),
    re.compile(r"(ignore|disregard)\s+(all\s+)?(the\s+)?(previous|above|prior)\s+instructions", re.I),
    re.compile(r"system\s*prompt", re.I),
    re.compile(r"你现在(是|扮演)"),
)


def looks_like_injection(text: str) -> bool:
    body = text or ""
    return any(pattern.search(body) for pattern in INJECTION_PATTERNS)


def check_analysis(value: dict, *, threshold: float = 0.6) -> list[str]:
    """对 Analyst 的 JSON 做语义闸门，返回问题列表（空列表表示通过）。

    对应方案 §5.3 的三条硬规则：
    * 证据为空却高置信度；
    * 无测试证据却直接判完成；
    * 完成度与状态自相矛盾。
    """
    problems: list[str] = []
    if not isinstance(value, dict):
        return ["返回值不是 JSON 对象"]

    completion = value.get("completion")
    confidence = value.get("confidence")
    status = value.get("status")
    evidence = value.get("evidence") or []

    if not isinstance(completion, int) or not 0 <= completion <= 100:
        problems.append("completion 必须是 0-100 的整数")
    if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        problems.append("confidence 必须是 0-1 的数")
    if status not in ("todo", "in_progress", "done"):
        problems.append("status 取值非法")

    refs = [
        str(item.get("ref") or "")
        for item in evidence
        if isinstance(item, dict) and item.get("ref")
    ]
    kinds = {
        str(item.get("type") or "")
        for item in evidence
        if isinstance(item, dict) and item.get("type")
    }

    if not refs and isinstance(confidence, (int, float)) and float(confidence) > 0.5:
        problems.append("没有证据支撑却给出高置信度")
    if status == "done" and "test" not in kinds:
        problems.append("没有测试证据却判定为已完成")
    if status == "done" and isinstance(completion, int) and completion < 90:
        problems.append("状态为已完成但完成度不足 90")
    if status == "todo" and isinstance(completion, int) and completion >= 50:
        problems.append("状态为待办但完成度过高")

    for item in evidence:
        if isinstance(item, dict) and looks_like_injection(str(item.get("note") or "")):
            problems.append("证据里出现疑似提示词注入内容")
            break

    if isinstance(confidence, (int, float)) and float(confidence) < threshold:
        # 低置信度不是「错误」，但要在上层标记为不进收件箱；这里只做提示
        pass
    return problems


def clamp_analysis(value: dict) -> dict:
    """把合法但越界的数值收敛到安全范围（用于放行前的最后一道修整）。"""
    result = dict(value or {})
    try:
        result["completion"] = max(0, min(100, int(result.get("completion", 0))))
    except (TypeError, ValueError):
        result["completion"] = 0
    try:
        result["confidence"] = max(0.0, min(1.0, float(result.get("confidence", 0.0))))
    except (TypeError, ValueError):
        result["confidence"] = 0.0
    if result.get("status") not in ("todo", "in_progress", "done"):
        result["status"] = "in_progress"
    result["evidence"] = [
        item for item in (result.get("evidence") or []) if isinstance(item, dict)
    ]
    result["risks"] = [str(item) for item in (result.get("risks") or [])][:10]
    result["summary"] = str(result.get("summary") or "")[:2000]
    return result


def require_valid(value: dict, *, threshold: float = 0.6) -> dict:
    """闸门 + 收敛；不通过就抛 ``EngineError``（由调用方降级为 draft 洞察）。"""
    problems = check_analysis(value, threshold=threshold)
    if problems:
        raise EngineError("推断结果未通过校验：" + "；".join(problems), problems=problems)
    return clamp_analysis(value)


__all__ = [
    "ANALYSIS_SCHEMA",
    "COORDINATE_SCHEMA",
    "INJECTION_PATTERNS",
    "PREPROCESS_SCHEMA",
    "REPORT_SCHEMA",
    "SCHEMAS",
    "aask_structured",
    "check_analysis",
    "clamp_analysis",
    "looks_like_injection",
    "require_valid",
]
