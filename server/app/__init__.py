"""TeamFlow —— 团队协作进度跟踪（里程碑 / 路线图 / Issue / 日程 / 云盘）。

后台是 FastAPI 应用，按「路由 → 服务 → 模型/持久化」分层：

    app/routers/        HTTP 层，只做参数校验、权限判断与响应组装
    app/services/       业务规则（权限、校验、序列化、ICS、会议信令、云盘、签名）
    app/db/             SQLAlchemy 模型、引擎与会话、自动迁移、演示数据
    app/deps.py         FastAPI 依赖（请求上下文、团队上下文）
    app/config.py       常量与配置（读取系统环境变量和 .env）

启动入口有两个：`python server.py` 与 `uvicorn app.main:app`。
"""
from __future__ import annotations

__version__ = "2.0.0"
