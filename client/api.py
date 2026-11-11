"""TeamFlow 端侧 SDK 的 REST 兜底通道。

WebSocket 是主通道（实时、可反向查询、支持暂停）；当长连接被代理/防火墙干掉时，
可以用这一个 REST 端点把事件补传上去，保证「先不丢，再说快」：

    POST {base_url}/api/teams/{team}/engine/edge/events
    body: {"token": "tfd_…", "envelope": {…}}

服务端行为与 WS 完全一致（同样的校验、幂等、入库与入队），返回::

    {"ok": true, "status": "stored" | "duplicate" | "rejected",
     "message": "", "event_id": 12, "job_id": 3, "issue_number": 7}

注意两个限制：

* 该端点仍受「编辑口令」与团队编辑权限约束——免登录模式下若没解锁编辑，
  会返回 401 ``edit_locked``，这不是 SDK 的问题；
* REST 通道**不支持**反向查询（服务端要读文件只能走长连接）。

::

    from client.api import RestFallback

    rest = RestFallback("https://teamflow.example.com", token="tfd_…", team="default")
    rest.post_event(envelope)          # 返回服务端结果字典
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 15.0
MAX_RESPONSE_BYTES = 1 << 20


class EdgeApiError(Exception):
    """REST 兜底调用失败（网络、鉴权、校验都有可能）。"""

    def __init__(self, message: str, *, status: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = code

    @property
    def retryable(self) -> bool:
        """网络错误/5xx 可以稍后再试；4xx（含 edit_locked）不该盲目重试。"""
        return self.status == 0 or self.status >= 500


def _json_request(
    url: str,
    payload: dict,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict | None = None,
) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "teamflow-edge-sdk/1",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            text = raw[:MAX_RESPONSE_BYTES].decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            text = exc.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")
            data = json.loads(text) if text else {}
            detail = str(data.get("error") or data.get("detail") or "")
            code = str(data.get("code") or "")
        except (ValueError, OSError):
            code = ""
        raise EdgeApiError(
            detail or f"服务端返回 HTTP {exc.code}", status=int(exc.code or 0), code=code
        ) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise EdgeApiError(f"连接服务端失败：{exc}") from exc
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EdgeApiError("服务端返回的不是 JSON") from exc
    return data if isinstance(data, dict) else {"data": data}


def _get_json(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict | None = None,
) -> dict:
    """GET 一个 JSON 端点（用于拉取策略）。"""
    request = urllib.request.Request(url, method="GET")
    for key, value in (headers or {}).items():
        request.add_header(key, str(value))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")
            data = json.loads(text) if text else {}
            detail = str(data.get("error") or data.get("detail") or "")
        except (ValueError, OSError):
            detail = ""
        raise EdgeApiError(detail or f"服务端返回 HTTP {exc.code}", status=int(exc.code or 0)) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise EdgeApiError(f"连接服务端失败：{exc}") from exc
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EdgeApiError("服务端返回的不是 JSON") from exc
    return data if isinstance(data, dict) else {"data": data}


class RestFallback:
    """把事件补传到 REST 端点；可直接作为 ``EdgeClient(rest_fallback=…)``。"""

    PATH = "/api/teams/{team}/engine/edge/events"

    def __init__(
        self,
        base_url: str,
        token: str,
        team: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        headers: dict | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url 不能为空")
        if not token:
            raise ValueError("token 不能为空")
        if not team:
            raise ValueError("team 不能为空（团队 slug）")
        self.ws_url = base_url.rstrip("/")
        # 允许直接把 ws(s):// 地址传进来：REST 兜底要的是 http(s) 根地址
        self.base_url = self.ws_to_http(self.ws_url).rstrip("/")
        self.token = token
        self.team = team
        self.timeout = float(timeout)
        self.headers = dict(headers or {})

    # -- 地址
    @property
    def url(self) -> str:
        return self.base_url + self.PATH.format(team=self.team)

    @staticmethod
    def ws_to_http(ws_url: str) -> str:
        """把 ``wss://host/api/edge/ws`` 换成 ``https://host``，省得重复配置。"""
        text = str(ws_url or "").strip()
        if text.startswith("wss://"):
            text = "https://" + text[len("wss://"):]
        elif text.startswith("ws://"):
            text = "http://" + text[len("ws://"):]
        marker = "/api/"
        index = text.find(marker)
        return text[:index] if index >= 0 else text

    # -- 调用
    def post_event(self, envelope: dict) -> dict:
        """上报一个事件包；``rejected`` 也会正常返回（看 ``status`` 字段）。"""
        data = _json_request(
            self.url,
            {"token": self.token, "envelope": envelope},
            timeout=self.timeout,
            headers=self.headers,
        )
        status = str(data.get("status") or "")
        # duplicate（重复上报）与 rejected（服务端明确拒收）都算「服务端已处理」，
        # 调用方看 status 字段决定后续动作即可。
        if not data.get("ok") and status not in {"rejected", "duplicate"}:
            raise EdgeApiError(
                str(data.get("message") or "服务端没有确认这次上报"),
                status=200,
                code=str(data.get("code") or ""),
            )
        return data

    def health(self, *, timeout: float | None = None) -> dict:
        url = self.base_url + "/api/health"
        try:
            with urllib.request.urlopen(url, timeout=timeout or self.timeout) as response:
                text = response.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise EdgeApiError(f"健康检查失败：HTTP {exc.code}", status=int(exc.code or 0)) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            raise EdgeApiError(f"健康检查失败：{exc}") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EdgeApiError("健康检查返回的不是 JSON") from exc
        return data if isinstance(data, dict) else {}

    def fetch_policy(self) -> dict:
        """拉取团队当前的采集策略（间隔时长、是否允许查看变动内容）。"""
        url = f"{self.base_url}/api/edge/policy?token={urllib.parse.quote(self.token)}"
        data = _get_json(url, timeout=self.timeout, headers=self.headers)
        if not data.get("ok"):
            raise EdgeApiError("服务端没有返回有效策略")
        return data

    def post_log(self, log: dict, envelope: dict | None = None) -> dict:
        """上报一条无人操作窗口日志；带 envelope 时服务端会顺带触发一次推断。"""
        payload: dict = {"token": self.token, "log": log}
        if envelope:
            payload["envelope"] = envelope
        data = _json_request(
            self.base_url + "/api/edge/logs",
            payload,
            timeout=self.timeout,
            headers=self.headers,
        )
        if not data.get("ok"):
            raise EdgeApiError(str(data.get("message") or "服务端没有确认这条日志"))
        return data

    def __call__(self, envelope: dict) -> dict:
        """让对象本身就能当 ``EdgeClient(rest_fallback=…)`` 的回调用。"""
        return self.post_event(envelope)


def fallback_from_config(cfg: Any) -> RestFallback | None:
    """从 ``EdgeConfig`` 造一个兜底通道；信息不全时返回 None。"""
    url = str(getattr(cfg, "url", "") or "")
    token = str(getattr(cfg, "token", "") or "")
    team = str(getattr(cfg, "team", "") or "")
    if not (url and token):
        return None
    if not team:
        # 团队 slug 缺失时从 ws 地址里拿不到，只能放弃兜底
        return None
    return RestFallback(RestFallback.ws_to_http(url), token, team)


def spool_summary(path: str | Path) -> dict:
    """给 CLI 的 ``status`` 用：读 JSONL 队列，统计条数与最早时间。"""
    target = Path(path)
    if not target.exists():
        return {"path": str(target), "exists": False, "count": 0, "oldest": "", "newest": ""}
    count = 0
    oldest = ""
    newest = ""
    try:
        with target.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                count += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = str((record.get("envelope") or {}).get("ts") or "")
                if ts:
                    oldest = ts if not oldest or ts < oldest else oldest
                    newest = ts if not newest or ts > newest else newest
    except OSError:
        return {"path": str(target), "exists": True, "count": 0, "oldest": "", "newest": ""}
    return {"path": str(target), "exists": True, "count": count, "oldest": oldest, "newest": newest}


__all__ = [
    "DEFAULT_TIMEOUT",
    "EdgeApiError",
    "RestFallback",
    "fallback_from_config",
    "spool_summary",
]
