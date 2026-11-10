"""TeamFlow 服务入口（server/）。

    python server/main.py                     # 默认 http://127.0.0.1:8000
    python server/main.py --port 9000         # 换端口
    python server/main.py --host 0.0.0.0      # 允许局域网/公网访问（注意安全）
    python server/main.py --env-file prod.env # 指定配置文件（默认读 server/.env）
    uvicorn app.main:app --reload             # 开发模式（在 server/ 目录下执行）

数据库由 ``TEAMFLOW_DB_URL`` 指定（默认 ``sqlite:///server/data.db``），也可以写在
``.env`` 里，详见 README.md。
"""
from __future__ import annotations

import argparse
import os
import socket
import sys


def lan_addresses() -> list[str]:
    """枚举本机的局域网 IPv4 地址（拿不到就返回空列表）。"""
    found: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            address = info[4][0]
            if address and not address.startswith("127.") and address not in found:
                found.append(address)
    except OSError:
        pass
    return found


def browser_urls(host: str, port: int) -> list[str]:
    """浏览器真正能打开的地址。

    ``0.0.0.0`` / ``::`` 只是监听用的通配地址，不能直接粘进浏览器
    （多数浏览器会报 ERR_CONNECTION_REFUSED / ERR_ADDRESS_INVALID），
    所以对外展示时换成 127.0.0.1 与本机网卡地址。
    """
    if host in ("0.0.0.0", "::", ""):
        return [f"http://127.0.0.1:{port}"] + [
            f"http://{address}:{port}" for address in lan_addresses()
        ]
    if host == "127.0.0.1":
        return [f"http://127.0.0.1:{port}", f"http://localhost:{port}"]
    return [f"http://{host}:{port}"]


def port_in_use(host: str, port: int) -> bool:
    """端口是否已被别的进程占用（只做一次轻量探测，不占用端口）。"""
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((probe_host, port)) == 0


def main(argv: list[str] | None = None) -> None:
    # 无论从哪里启动，都把 server/ 放进 sys.path，`app` 包才导得进来
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)

    parser = argparse.ArgumentParser(description="TeamFlow 服务")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址（默认 127.0.0.1；局域网访问用 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    parser.add_argument("--reload", action="store_true", help="代码变更后自动重启（开发用）")
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--env-file", default="",
                        help="配置文件路径（默认读 server/.env；不存在则跳过）")
    args = parser.parse_args(argv)

    if args.env_file:
        os.environ["TEAMFLOW_ENV_FILE"] = args.env_file

    # 在这里导入：确保上面的 TEAMFLOW_ENV_FILE 先生效，config 才会读对文件
    from app.config import ENV_FILE

    if ENV_FILE.existed:
        names = ", ".join(sorted(ENV_FILE.applied))
        more = "" if len(names) <= 160 else f" 等 {len(ENV_FILE.applied)} 项"
        print(f"已读取配置：{ENV_FILE.path}（{len(ENV_FILE.applied)} 项生效）")
        if names:
            print(f"  {names[:160]}{more}")
        if ENV_FILE.skipped:
            print(f"  有 {len(ENV_FILE.skipped)} 行无法解析，已跳过", file=sys.stderr)
    elif args.env_file:
        print(f"警告：配置文件不存在，已忽略：{ENV_FILE.path}", file=sys.stderr)

    if port_in_use(args.host, args.port):
        print(f"无法启动：端口 {args.port} 已被占用（可能是上一次没关掉的 TeamFlow）。\n"
              f"  换一个端口：python server/main.py --port {args.port + 1}\n"
              f"  或先结束占用它的进程：netstat -ano | findstr :{args.port}",
              file=sys.stderr)
        raise SystemExit(1)

    urls = browser_urls(args.host, args.port)
    print(f"TeamFlow 已启动：{urls[0]}")
    for extra in urls[1:]:
        print(f"  同一服务也可用：{extra}")
    if args.host in ("0.0.0.0", "::"):
        print("  （正在监听所有网卡：0.0.0.0 只是监听地址，浏览器里请用上面这些地址）")

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
