"""路由汇总：按注册顺序挂到 app.main。"""
from __future__ import annotations

from . import (
    ai,
    auth,
    avatars,
    client_policy,
    edge,
    issues,
    labels,
    milestones,
    schedules,
    settings as settings_router,
    system,
    teams,
    week,
)

routers = [
    system.router,
    auth.router,
    settings_router.router,
    issues.router,
    milestones.router,
    labels.router,
    schedules.router,
    week.router,
    ai.router,
    ai.team_router,
    teams.router,
    edge.router,
    edge.ws_router,
    client_policy.router,
    avatars.public_router,
    avatars.router,
]

__all__ = ["routers"]
