"""端侧接入层（P1）：设备配对、WS 网关、事件入库。

端侧（``client/``）只负责「捕获 + 脱敏 + 上报」，这里负责接收、去重、排队。
两边的帧协议以 ``client/core/network.py`` 与 ``gateway.py`` 的常量名对齐：
``hello`` / ``hello_ok`` / ``event`` / ``ack`` / ``heartbeat`` / ``query`` /
``query_result`` / ``pause`` / ``resume`` / ``ping`` / ``error``。
"""
from __future__ import annotations

from .devices import (
    claim,
    device_dict,
    device_state,
    find_by_token,
    list_devices,
    new_token,
    pair_device,
    require_usable,
    revoke,
    touch,
)
from .gateway import Connection, ConnectionRegistry, registry
from .ingest import IngestResult, event_dict, ingest, recent_events

__all__ = [
    "Connection",
    "ConnectionRegistry",
    "IngestResult",
    "claim",
    "device_dict",
    "device_state",
    "event_dict",
    "find_by_token",
    "ingest",
    "list_devices",
    "new_token",
    "pair_device",
    "recent_events",
    "registry",
    "require_usable",
    "revoke",
    "touch",
]
