"""TeamFlow 后端测试夹具：每个测试使用独立的内存数据库。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

os.environ["TEAMFLOW_BACKUP"] = "0"
os.environ["TEAMFLOW_SEED"] = "1"
os.environ["TEAMFLOW_COOKIE_SECURE"] = "0"
os.environ["TEAMFLOW_DB_URL"] = "sqlite:///:memory:"
# 推理引擎的落盘目录放到临时目录，别把 runs.db / 事件写成仓库脏文件
os.environ["TEAMFLOW_ENGINE"] = "1"
# 作业 worker 在测试里手动 drain：后台循环会和用例抢同一个内存库，结果不确定
os.environ["TEAMFLOW_INFERENCE_WORKER"] = "0"
os.environ.setdefault(
    "TEAMFLOW_ENGINE_DIR",
    str(Path(tempfile.gettempdir()) / "teamflow-engine-tests"),
)

from fastapi.testclient import TestClient  # noqa: E402

from app.db import session as dbsession  # noqa: E402


@pytest.fixture()
def client():
    dbsession.configure("sqlite:///:memory:")
    dbsession.init_db()
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def team_slug(client) -> str:
    teams = client.get("/api/teams").json()["teams"]
    assert teams, "种子数据应至少包含一个团队"
    return teams[0]["slug"]


@pytest.fixture()
def new_issue(client, team_slug):
    response = client.post(
        f"/api/teams/{team_slug}/issues",
        json={"title": "测试议题", "body": "来自 pytest", "priority": "high"},
    )
    assert response.status_code == 200, response.text
    return response.json()["issue"]
