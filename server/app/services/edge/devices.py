"""端侧设备的配对、鉴权与状态（P1）。

配对流程刻意做成「管理员生成即批准」（方案 §13 C7）：

1. 管理员在「设置 → 推理引擎」点「生成配对码」→ 服务端建一行 ``devices``，
   ``token_hash`` 存 sha256，明文 token 只在响应里出现这一次；
2. 探针带着 token 连 WS 发 ``hello`` → 服务端校验 token、绑定名字/平台、
   写 ``last_seen_at``，从此这条连接就算「在线」；
3. 撤销 = 写 ``revoked_at``：连接被立即关闭，之后的握手/上报一律 401。

**待接入**与**离线**的区分全靠 ``last_seen_at``：生成后从未连过是「待接入」，
连过但当前不在线是「离线」。这样不需要额外的状态列。
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import ENGINE_DEVICE_TOKEN_PREFIX, ENGINE_PAIR_TTL_MINUTES
from ...db.base import utcnow_iso
from ...db.engine_models import Device
from ...errors import ApiError

MAX_NAME = 120
MAX_PLATFORM = 40
MAX_AGENT_VERSION = 40


def hash_token(token: str) -> str:
    """token 只以 sha256 落库（唯一索引建在它上面）。"""
    return hashlib.sha256((token or "").strip().encode("utf-8")).hexdigest()


def new_token() -> str:
    """一次性配对码：``tfd_`` + 32 字节随机（够长，等价于设备口令）。"""
    return ENGINE_DEVICE_TOKEN_PREFIX + secrets.token_urlsafe(24)


def _expired(device: Device, *, now: datetime | None = None) -> bool:
    """从未连过、且生成时间超过有效期 → 配对码作废（需要重新生成）。"""
    if device.last_seen_at or device.revoked_at:
        return False
    moment = now or datetime.now(timezone.utc)
    try:
        created = datetime.strptime(device.created_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return False
    return moment - created > timedelta(minutes=ENGINE_PAIR_TTL_MINUTES)


def pair_device(
    session: Session,
    *,
    team_id: int,
    name: str = "",
    platform: str = "",
    user_id: int | None = None,
) -> tuple[Device, str]:
    """生成一台「待接入」设备，返回 ``(device, 明文 token)``。"""
    token = new_token()
    now = utcnow_iso()
    device = Device(
        team_id=team_id,
        user_id=user_id,
        name=(name or "")[:MAX_NAME],
        platform=(platform or "")[:MAX_PLATFORM],
        agent_version="",
        token_hash=hash_token(token),
        meta="{}",
        paired_at=now,
        last_seen_at=None,
        revoked_at=None,
        created_at=now,
    )
    session.add(device)
    session.flush()
    return device, token


def upsert_device(session: Session, *, team_id: int, name: str = "", platform: str = "",
                  user_id: int | None = None) -> tuple[Device, str]:
    """同一台机器重复绑定时复用同一行设备，只换令牌，避免设备列表里堆重复项。"""
    wanted = (name or "")[:MAX_NAME]
    row = None
    if wanted:
        row = session.execute(
            select(Device).where(
                Device.team_id == team_id,
                Device.user_id == user_id,
                Device.name == wanted,
                Device.revoked_at.is_(None),
            )
        ).scalars().first()
    if row is None:
        return pair_device(session, team_id=team_id, name=name, platform=platform, user_id=user_id)
    token = new_token()
    row.token_hash = hash_token(token)
    if platform:
        row.platform = platform[:MAX_PLATFORM]
    row.paired_at = utcnow_iso()
    session.flush()
    return row, token


def find_by_token(session: Session, token: str | None) -> Device | None:
    """按明文 token 找设备（不判断可用性，调用方要自己看 ``revoked_at``）。"""
    text = (token or "").strip()
    if not text:
        return None
    return session.execute(
        select(Device).where(Device.token_hash == hash_token(text))
    ).scalar_one_or_none()


def is_usable(device: Device | None) -> bool:
    return device is not None and not device.revoked_at and not _expired(device)


def claim(
    session: Session,
    device: Device,
    *,
    name: str = "",
    platform: str = "",
    agent_version: str = "",
    meta: dict | None = None,
) -> Device:
    """握手时把端侧自报的信息绑上，并刷新 ``last_seen_at``。"""
    import json

    if name:
        device.name = name[:MAX_NAME]
    if platform:
        device.platform = platform[:MAX_PLATFORM]
    if agent_version:
        device.agent_version = agent_version[:MAX_AGENT_VERSION]
    if isinstance(meta, dict) and meta:
        device.meta = json.dumps(meta, ensure_ascii=False)[:4000]
    if not device.paired_at:
        device.paired_at = utcnow_iso()
    device.last_seen_at = utcnow_iso()
    session.flush()
    return device


def touch(session: Session, device: Device) -> None:
    """心跳/事件上报时刷新在线时间（同一秒内不重复写库）。"""
    now = utcnow_iso()
    if device.last_seen_at != now:
        device.last_seen_at = now
        session.flush()


def revoke(session: Session, device: Device) -> Device:
    if not device.revoked_at:
        device.revoked_at = utcnow_iso()
        session.flush()
    return device


def list_devices(session: Session, team_id: int, *, limit: int = 100) -> list[Device]:
    return list(
        session.execute(
            select(Device)
            .where(Device.team_id == team_id)
            .order_by(Device.revoked_at.is_not(None), Device.id.desc())
            .limit(limit)
        ).scalars()
    )


def require_usable(device: Device | None) -> Device:
    """握手/上报前的统一断言，给 REST 兜底与 WS 网关共用。"""
    if device is None:
        raise ApiError("配对码无效，请重新生成", 401, code="bad_device_token")
    if device.revoked_at:
        raise ApiError("该设备已被吊销", 401, code="device_revoked")
    if _expired(device):
        raise ApiError("配对码已过期，请重新生成", 401, code="pair_expired")
    return device


def seen_within(device: Device, seconds: int = 600) -> bool:
    """十分钟内有过心跳就算在线。

    端侧除了 WS 也有纯 REST 上报（客户端窗口就是这种），没有长连接，
    只按「连接是否存在」判断会一直显示离线。这里用心跳时间兜底。
    """
    seen = getattr(device, "last_seen_at", "") or ""
    if not seen:
        return False
    try:
        moment = datetime.strptime(seen, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    return (datetime.now(timezone.utc) - moment).total_seconds() <= seconds


def device_state(device: Device, *, online: bool = False) -> str:
    if device.revoked_at:
        return "revoked"
    if online:
        return "online"
    if not device.last_seen_at:
        return "pending"
    return "offline"


def device_dict(device: Device, *, online: bool = False, event_count: int = 0) -> dict:
    """给前端与 SDK 自检用的设备视图（不含 token）。"""
    return {
        "id": device.id,
        "team_id": device.team_id,
        "name": device.name or "",
        "platform": device.platform or "",
        "agent_version": device.agent_version or "",
        "state": device_state(device, online=online),
        "online": bool(online and not device.revoked_at),
        "user_id": device.user_id,
        "paired_at": device.paired_at or "",
        "last_seen_at": device.last_seen_at or "",
        "revoked_at": device.revoked_at or "",
        "created_at": device.created_at,
        "event_count": int(event_count),
    }


__all__ = [
    "claim",
    "device_dict",
    "device_state",
    "find_by_token",
    "hash_token",
    "is_usable",
    "list_devices",
    "new_token",
    "pair_device",
    "upsert_device",
    "require_usable",
    "revoke",
    "touch",
]
