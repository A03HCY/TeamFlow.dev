"""演示数据：第一次打开就有内容可看（``TEAMFLOW_SEED=0`` 时只建空团队）。"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..security import hash_password
from .base import utcnow_iso
from .models import Activity, Comment, Issue, Label, Milestone, TeamMember, User
from ..services.settings import set_setting
from ..services.teams import create_team

DEMO_USERS = [
    ("linwei", "林薇", "产品负责人"),
    ("chenmo", "陈默", "前端工程"),
    ("suxiao", "苏晓", "交互设计"),
    ("zhouyuan", "周原", "后端工程"),
]
DEMO_PASSWORD = "demo1234"


def seed_enabled() -> bool:
    return os.environ.get("TEAMFLOW_SEED", "1").strip().lower() not in ("0", "false", "off", "no")


def create_default_team(session: Session):
    team = create_team(
        session, "产品研发团队", "default",
        "跨职能产品研发小组，负责核心产品的规划与交付。",
    )
    set_setting(session, "default_team_id", team.id)
    return team


def _ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def seed_demo(session: Session) -> None:
    """写入一份接近真实的演示工作区。"""
    team = create_default_team(session)
    today = date.today()

    users: list[User] = []
    for username, display, title in DEMO_USERS:
        user = User(
            username=username,
            display_name=display,
            title=title,
            email=f"{username}@example.com",
            password_hash=hash_password(DEMO_PASSWORD),
            created_at=utcnow_iso(),
        )
        session.add(user)
        session.flush()
        users.append(user)
        session.add(TeamMember(
            team_id=team.id,
            user_id=user.id,
            role="owner" if username == "linwei" else "member",
            joined_at=utcnow_iso(),
        ))

    # 第二个团队用于演示多团队切换（开启登录后可见）
    infra = create_team(session, "基础设施组", "infra", "负责 CI/CD、可观测性与稳定性。", users[0].id)
    session.add(TeamMember(
        team_id=infra.id, user_id=users[3].id, role="member", joined_at=utcnow_iso(),
    ))

    labels = list(session.scalars(select(Label).where(Label.team_id == team.id).order_by(Label.id)))

    milestones: list[Milestone] = []
    demos = [
        ("Alpha 内测", "完成核心闭环，邀请 20 家种子用户试用。", -35, 12, 0),
        ("Beta 公测", "开放注册，打磨引导流程与性能。", 8, 55, 1),
        ("v1.0 正式发布", "付费能力上线，对外发布。", 50, 110, 2),
        ("下一阶段规划", "规划企业版与开放 API。", 100, 175, 3),
    ]
    for index, (title, desc, start_offset, due_offset, position) in enumerate(demos, start=1):
        ms = Milestone(
            team_id=team.id,
            number=index,
            title=title,
            description=desc,
            state="open",
            start_date=(today + timedelta(days=start_offset)).isoformat(),
            due_date=(today + timedelta(days=due_offset)).isoformat(),
            position=position,
            created_at=utcnow_iso(),
        )
        session.add(ms)
        milestones.append(ms)
    closed_ms = Milestone(
        team_id=team.id,
        number=len(demos) + 1,
        title="项目启动",
        description="立项、团队组建与技术选型。",
        state="closed",
        start_date=(today + timedelta(days=-80)).isoformat(),
        due_date=(today + timedelta(days=-45)).isoformat(),
        position=-1,
        created_at=utcnow_iso(),
        closed_at=utcnow_iso(),
    )
    session.add(closed_ms)
    milestones.append(closed_ms)
    session.flush()

    issues = [
        ("登录态与团队切换偶尔丢失",
         "切换团队后刷新页面会回到默认团队，疑似 session 与 active team 不同步。\n\n复现步骤：\n1. 在 A 团队点击切换\n2. 刷新页面\n3. 回到默认团队",
         "in_progress", "high", 0, [0, 1], 1, 6),
        ("Issue 列表需要支持标签筛选",
         "顶部筛选栏目前只有状态，需要增加标签与负责人的多选筛选。",
         "todo", "medium", 0, [1], 2, 9),
        ("里程碑进度条计算口径错误",
         "已关闭但未设置 closed_at 的 issue 没有计入进度，导致进度偏小。",
         "done", "high", 0, [0], 3, 15),
        ("补充新人引导文档",
         "写一份 10 分钟上手指南，覆盖团队创建、里程碑、路线图。",
         "done", "low", 0, [4], 0, 18),
        ("注册流程接入邮箱验证", "公测前必须完成邮箱验证，避免垃圾账号。",
         "todo", "high", 1, [1], 3, 4),
        ("看板视图支持拖拽改变状态",
         "拖拽 issue 卡片在 todo / in_progress / done 之间移动，写回 status 字段。",
         "in_progress", "medium", 1, [1], 1, 7),
        ("首屏加载时间优化到 1s 内",
         "当前首屏 2.4s，主要是列表接口一次返回过多字段。",
         "in_progress", "medium", 1, [3], 3, 11),
        ("路线图时间轴在窄屏错位", "宽度小于 900px 时月份刻度与条形不对齐。",
         "todo", "low", 1, [0, 2], 2, 3),
        ("支付模块接入", "支持订阅制与按席位计费，先做月付。",
         "todo", "high", 2, [1], 3, 2),
        ("设计系统整理成组件库", "把按钮、表单、卡片抽成可复用组件，统一间距与圆角。",
         "in_progress", "medium", 2, [2], 2, 5),
        ("对外公开页面需要 SEO 元信息", "公开的路线图页面加上 title / description / og 标签。",
         "todo", "low", 2, [1, 4], 0, 1),
        ("数据库备份脚本", "每日自动备份 sqlite 文件到对象存储，保留 30 天。",
         "done", "medium", 3, [3], 3, 22),
        ("企业版权限模型调研", "整理 SSO、审计日志、细粒度角色的需求。",
         "todo", "low", 3, [1], 0, 12),
        ("开放 API 设计草案", "先出 OpenAPI 文档，覆盖 issue 与里程碑的读写。",
         "todo", "low", 3, [4], 3, 14),
        ("移动端适配评审", "核心三个页面在 375px 宽度下的可用性走查。",
         "done", "medium", 4, [2], 2, 60),
        ("技术选型：前后端分离方案", "对比 SSR 与 SPA 两种方案的取舍。",
         "done", "high", 4, [1], 1, 70),
    ]

    for number, (title, body, status, priority, ms_index, label_indexes, who, age) in enumerate(issues, start=1):
        created = _ago(age)
        state = "closed" if status == "done" else "open"
        issue = Issue(
            team_id=team.id,
            number=number,
            title=title,
            body=body,
            state=state,
            status=status,
            priority=priority,
            milestone_id=milestones[ms_index].id,
            assignee_id=users[who].id,
            author_id=users[0].id,
            created_at=created,
            updated_at=created,
            closed_at=created if state == "closed" else None,
        )
        issue.labels = [labels[i] for i in label_indexes]
        session.add(issue)
        session.flush()
        session.add(Activity(
            team_id=team.id,
            actor_id=users[0].id,
            actor_name=DEMO_USERS[0][1],
            verb="created",
            subject_type="issue",
            subject_id=issue.id,
            subject_text=title,
            detail=f"#{number}",
            created_at=created,
        ))

    team.issue_seq = len(issues)

    # 部分 issue 设截止日期：正数是还剩几天，负数是已逾期
    for number, offset in {1: 3, 5: -2, 6: 0, 9: 12, 11: 1, 13: -5, 14: 20}.items():
        issue = session.scalar(
            select(Issue).where(Issue.team_id == team.id, Issue.number == number)
        )
        if issue is not None:
            issue.due_date = (today + timedelta(days=offset)).isoformat()

    comments = [
        (1, 2, "我这边也能稳定复现，先看下 cookie 的 SameSite 设置。", 5),
        (1, 3, "已定位：切换团队时只更新了内存状态，没有持久化。今天提 MR。", 4),
        (3, 3, "修复已合入，进度计算改成以 closed_at 为准。", 10),
        (6, 1, "拖拽交互参考了 GitHub Projects 的卡片排序，先做列内移动。", 2),
    ]
    for number, user_index, body, days in comments:
        issue_id = session.scalar(
            select(Issue.id).where(Issue.team_id == team.id, Issue.number == number)
        )
        if issue_id is None:
            continue
        session.add(Comment(
            issue_id=issue_id,
            user_id=users[user_index].id,
            author_name=DEMO_USERS[user_index][1],
            body=body,
            created_at=_ago(days),
        ))

    set_setting(session, "default_team_id", team.id)
    set_setting(session, "demo_seeded", "1")
    session.flush()


def has_teams(session: Session) -> bool:
    return bool(session.scalar(select(func.count()).select_from(TeamMember).limit(1)) or
                session.scalar(select(func.count()).select_from(Label).limit(1)))
