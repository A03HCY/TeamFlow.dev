"""TeamFlow 端侧 SDK（``client``）。

这里只包含与服务端推理网关通信所需的最小依赖层，**不含探针**——文件监听、
diff 提取、脱敏、spool 落盘策略与桌面 UI 由端侧另一侧实现，调用本包即可：

* ``client.core.network``：组包（``build_event`` / ``validate_event``）、离线队列
  （``SpoolQueue``）、长连接客户端（``EdgeClient``）；
* ``client.core.query``：反向查询沙箱（``QuerySandbox``）——服务端点名要哪些文件，
  端侧在允许的根目录内回元信息，``mode="read"`` 才回脱敏后的内容；
* ``client.config``：``~/.teamflow/edge.json`` + ``TEAMFLOW_EDGE_*`` 环境变量；
* ``client.api``：REST 兜底通道（长连接被代理掐断时补传）；
* ``client.main``：``python -m client.main status|set|send|run|clear``。

::

    from client import EdgeClient, QuerySandbox, build_event, new_event_uid, repo_path_hash

    client = EdgeClient("wss://host/api/edge/ws", "tfd_…", device={"name": "linwei-mbp"})
    client.on_query_request = QuerySandbox("/code/teamflow", allow_content=False)
    client.enqueue(build_event(event_uid=new_event_uid(),
                               repo={"path_hash": repo_path_hash("/code/teamflow")},
                               files=[{"path": "server/app/main.py", "status": "modified"}]))
    client.run_blocking()
"""
from __future__ import annotations

__version__ = "0.3.0"

from .api import EdgeApiError, RestFallback, fallback_from_config, spool_summary
from .config import ConfigError, EdgeConfig, config_path, describe, load_config, save_config
from .core.network import (
    ACK_DUPLICATE,
    ACK_REJECTED,
    ACK_STORED,
    EdgeClient,
    EdgeError,
    EdgeSession,
    EventInvalid,
    FatalClose,
    QueueItem,
    SpoolQueue,
    build_event,
    event_frame,
    heartbeat_frame,
    hello_frame,
    is_test_path,
    lang_for,
    new_event_uid,
    now_iso,
    platform_name,
    query_result_frame,
    repo_path_hash,
    sha256_file,
    summarize_files,
    validate_event,
)
from .core.query import (
    QueryRequest,
    QuerySandbox,
    file_fingerprint,
    redact_text,
)

__all__ = [
    "ACK_DUPLICATE",
    "ACK_REJECTED",
    "ACK_STORED",
    "ConfigError",
    "EdgeApiError",
    "EdgeClient",
    "EdgeConfig",
    "EdgeError",
    "EdgeSession",
    "EventInvalid",
    "FatalClose",
    "QueryRequest",
    "QuerySandbox",
    "QueueItem",
    "RestFallback",
    "SpoolQueue",
    "__version__",
    "build_event",
    "config_path",
    "describe",
    "event_frame",
    "fallback_from_config",
    "file_fingerprint",
    "heartbeat_frame",
    "hello_frame",
    "is_test_path",
    "lang_for",
    "load_config",
    "new_event_uid",
    "now_iso",
    "platform_name",
    "query_result_frame",
    "redact_text",
    "repo_path_hash",
    "save_config",
    "sha256_file",
    "spool_summary",
    "summarize_files",
    "validate_event",
]
