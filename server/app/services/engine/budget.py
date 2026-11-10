"""推理作业的预算与超时。

orchify 的 Agent 支持 ``budget_usd`` / ``budget_tokens`` / ``budget_seconds``，
与团队配置一一对应。四个 Agent 共享一个作业预算，所以每个 Agent 拿到的是
「作业预算 ÷ 阶段权重」，避免 Analyst 一个阶段把额度吃光。

超出预算时 orchify 会派 ``run:budget:exceeded`` 事件并终止 run；这里只负责
计算与解析，不接管执行。
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import EngineConfig

# 四段管线的预算权重（和为 1.0）
STAGE_WEIGHTS = {
    "preprocessor": 0.15,
    "analyst": 0.55,
    "coordinator": 0.15,
    "reporter": 0.15,
}


@dataclass(frozen=True)
class JobBudget:
    """一次作业的预算上限。"""

    usd: float
    tokens: int
    seconds: float

    def agent_weights(self, **overrides: float) -> dict:
        """按阶段切分预算（可覆盖个别阶段权重）。返回 ``worker -> JobBudget``。"""
        weights = dict(STAGE_WEIGHTS)
        for key, value in overrides.items():
            if key in weights:
                weights[key] = float(value)
        total = sum(weights.values()) or 1.0
        return {
            name: JobBudget(
                usd=max(0.0, round(self.usd * weight / total, 6)),
                tokens=max(1_000, int(self.tokens * weight / total)),
                seconds=max(5.0, round(self.seconds * weight / total, 1)),
            )
            for name, weight in weights.items()
        }

    def agent_kwargs(self) -> dict:
        """直接展开成 ``Agent(**kwargs)`` 的预算参数。"""
        return {
            "budget_usd": self.usd,
            "budget_tokens": self.tokens,
            "budget_seconds": self.seconds,
        }

    def as_dict(self) -> dict:
        return {"usd": self.usd, "tokens": self.tokens, "seconds": self.seconds}


def job_budget(cfg: EngineConfig, *, factor: float = 1.0) -> JobBudget:
    """从团队配置算作业预算；``factor`` 用于重试时放大（例如 1.5 倍）。"""
    scale = max(0.1, float(factor))
    return JobBudget(
        usd=max(0.0, float(cfg.budget_usd) * scale),
        tokens=max(1_000, int(cfg.budget_tokens * scale)),
        seconds=max(5.0, float(cfg.budget_seconds) * scale),
    )


def parse_exceeded(detail: dict | None) -> dict:
    """把 ``run:budget:exceeded`` 的 detail 归一成给前端看的结构。"""
    data = detail or {}
    kind = str(data.get("budget") or data.get("dimension") or data.get("limit_type") or "unknown")
    limit = data.get("limit")
    used = data.get("used") or data.get("spent")
    labels = {"tokens": "tokens", "usd": "费用", "seconds": "时长"}
    return {
        "kind": kind,
        "label": labels.get(kind, kind),
        "limit": limit,
        "used": used,
        "message": f"推理预算超限（{labels.get(kind, kind)}）",
    }


__all__ = ["JobBudget", "STAGE_WEIGHTS", "job_budget", "parse_exceeded"]
