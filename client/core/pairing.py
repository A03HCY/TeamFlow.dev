"""客户端接入服务端：同账号登录直接换令牌；不同身份用配对码。

两种路径对应两种身份关系：

* :func:`login_and_pair` —— 与网页同一账号，登录后给自己的机器换令牌，不需要配对码；
* :func:`pair_with_code` —— 没有账号的人，用管理员给的一次性配对码接入，
  服务端会按电脑用户名自动建一个团队成员。

两者返回结构一致，都包含 ``token`` / ``team`` / ``username`` / ``device``。
"""
from __future__ import annotations

import getpass
import http.cookiejar
import json
import platform as os_platform
import socket
import urllib.error
import urllib.parse
import urllib.request


class PairError(Exception):
    """配对过程中的可读错误，直接显示给用户。"""


def local_username() -> str:
    """本机用户名，作为「自动创建的团队成员名」的默认值。"""
    try:
        name = getpass.getuser()
    except Exception:  # noqa: BLE001 - 某些受限环境拿不到
        name = ""
    return name or socket.gethostname() or "device"


def local_device_name() -> str:
    return socket.gethostname() or local_username()


def platform_name() -> str:
    return {
        "Windows": "windows",
        "Darwin": "darwin",
        "Linux": "linux",
    }.get(os_platform.system(), os_platform.system().lower() or "unknown")


def normalize_base(raw: str) -> str:
    """接受 ``127.0.0.1:8000`` / ``http://host:8000/`` 等写法，统一成 http(s)。"""
    text = (raw or "").strip().rstrip("/")
    if not text:
        raise PairError("请填写服务端地址，例如 http://127.0.0.1:8000")
    if "://" not in text:
        text = "http://" + text
    if not text.startswith(("http://", "https://")):
        raise PairError("服务端地址要以 http:// 或 https:// 开头")
    return text


def ws_url(base: str) -> str:
    text = normalize_base(base)
    if text.startswith("https://"):
        return "wss://" + text[len("https://"):] + "/api/edge/ws"
    return "ws://" + text[len("http://"):] + "/api/edge/ws"


class _Session:
    """带 cookie 的最小 HTTP 客户端（登录后要复用会话）。"""

    def __init__(self, base: str) -> None:
        self.base = normalize_base(base)
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def call(self, method: str, path: str, body=None, *, timeout: float = 20.0) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(request, timeout=timeout) as response:
                text = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8", "replace")).get("error", "")
            except Exception:  # noqa: BLE001 - 错误体不可信
                detail = ""
            raise PairError(detail or f"服务端返回 HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise PairError(f"连不上服务端：{exc.reason}") from exc
        except TimeoutError as exc:
            raise PairError("服务端响应超时，请检查地址与网络") from exc
        try:
            return json.loads(text or "{}")
        except json.JSONDecodeError as exc:
            raise PairError("服务端返回的不是 JSON，请确认地址是否填对") from exc


def list_teams(base: str, username: str, password: str) -> dict:
    """先登录一次，把账号能进的团队列出来（不落令牌，只读）。"""
    client = _Session(base)
    client.call("POST", "/api/auth/login", {"username": username, "password": password})
    teams = client.call("GET", "/api/teams").get("teams") or []
    return {"teams": [pick_team_fields(item) for item in teams]}


def pick_team_fields(item: dict) -> dict:
    return {
        "slug": str(item.get("slug") or ""),
        "name": str(item.get("name") or item.get("slug") or ""),
    }


def login_and_pair(
    base: str,
    username: str,
    password: str,
    device_name: str = "",
    team: str = "",
) -> dict:
    """同账号接入：登录 → 选团队 → 给自己的机器换令牌。

    账号在多个团队时**绝不猜**：``team`` 为空就返回 ``need_team`` 让界面选，
    否则会像以前那样固定接第一个，用户以为上报成功、其实写进了另一个团队。
    """
    if not username or not password:
        raise PairError("请填写账号与密码")
    client = _Session(base)
    client.call("POST", "/api/auth/login", {"username": username, "password": password})
    teams = client.call("GET", "/api/teams").get("teams") or []
    if not teams:
        raise PairError("这个账号还没有加入任何团队，请先在网页里建团队")

    want = (team or "").strip().lower()
    picked = None
    if want:
        for item in teams:
            slug = str(item.get("slug") or "").strip().lower()
            name = str(item.get("name") or "").strip().lower()
            if want in (slug, name):
                picked = item
                break
        if picked is None:
            raise PairError(f"这个账号不在「{team}」团队里")
    elif len(teams) == 1:
        picked = teams[0]
    else:
        return {
            "need_team": True,
            "teams": [pick_team_fields(item) for item in teams],
            "username": username,
        }

    slug = str(picked.get("slug") or "")
    if not slug:
        raise PairError("读取不到团队信息，请刷新网页后重试")
    data = client.call(
        "POST",
        f"/api/teams/{urllib.parse.quote(slug)}/devices/self",
        {"device_name": device_name or local_device_name(), "platform": platform_name()},
    )
    data["team_name"] = picked.get("name") or data.get("team_name") or ""
    data["team"] = slug
    data["teams"] = [pick_team_fields(item) for item in teams]
    return data


def pair_with_code(base: str, code: str, username: str = "", device_name: str = "") -> dict:
    """不同身份接入：用一次性配对码换令牌，服务端按电脑用户名建成员。"""
    if not (code or "").strip():
        raise PairError("请填写配对码")
    client = _Session(base)
    return client.call(
        "POST",
        "/api/edge/pair",
        {
            "code": code.strip(),
            "username": username or local_username(),
            "device_name": device_name or local_device_name(),
            "platform": platform_name(),
        },
    )


__all__ = [
    "PairError",
    "local_username",
    "local_device_name",
    "platform_name",
    "normalize_base",
    "ws_url",
    "login_and_pair",
    "pair_with_code",
]
