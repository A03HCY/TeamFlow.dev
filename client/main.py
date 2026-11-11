"""TeamFlow 端侧 SDK 命令行入口（``python -m client.main``）。

这一层只做「看得见的 SDK 操作」，方便联调和运维排查，不含任何监听/提取逻辑：

    python -m client.main status                 # 配置、离线队列与服务端连通性
    python -m client.main set --url wss://host/api/edge/ws --token tfd_xxx --team default
    python -m client.main send --branch feat/12-login --file server/app/routers/auth.py
    python -m client.main run                    # 只连接、心跳、清空离线队列
    python -m client.main clear                  # 清空本地离线队列

``set`` 会把配置写到 ``~/.teamflow/edge.json``（可用 ``TEAMFLOW_EDGE_CONFIG`` 覆盖），
``send`` 默认先入队再尝试发送，发不出去就留在队列里等下次 ``run``。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Sequence

from .api import EdgeApiError, fallback_from_config, spool_summary
from .config import ConfigError, describe, load_config, save_config
from .core.network import (
    EdgeClient,
    EventInvalid,
    build_event,
    new_event_uid,
    platform_name,
    repo_path_hash,
    sha256_file,
)
from .core.query import QuerySandbox
from .core.watcher import FolderWatcher, WindowReport, current_branch, path_hash


# ------------------------------------------------------------------ status

def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    print(describe(cfg))
    spool = spool_summary(cfg.spool_path())
    if spool["exists"]:
        window = f"{spool['oldest']} → {spool['newest']}" if spool["oldest"] else "时间未知"
        print(f"离线队列：{spool['count']} 条（{window}）")
    else:
        print(f"离线队列：{spool['path']} 尚未创建")
    if not args.check:
        print("（加 --check 可以测试服务端连通性）")
        return 0
    gaps = cfg.missing()
    if gaps:
        print("无法连通性检查：" + "、".join(gaps))
        return 2
    rest = fallback_from_config(cfg)
    if rest is None:
        print("无法连通性检查：配置里缺少团队 slug（--team / TEAMFLOW_EDGE_TEAM）")
        return 2
    try:
        data = rest.health()
    except EdgeApiError as exc:
        print(f"服务端不可用：{exc}")
        return 1
    print(f"服务端可用：{data.get('app', 'TeamFlow')} v{data.get('version') or '?'}")
    return 0


# ------------------------------------------------------------------ set

def cmd_set(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    updates = {
        "url": args.url,
        "token": args.token,
        "team": args.team,
        "device_name": args.device_name,
        "platform": args.platform,
        "agent_version": args.agent_version,
        "spool_dir": args.spool_dir,
    }
    for name, value in updates.items():
        if value is not None:
            setattr(cfg, name, value)
    if args.heartbeat is not None:
        cfg.heartbeat = args.heartbeat
    if args.queue_limit is not None:
        cfg.queue_limit = args.queue_limit
    if args.window is not None:
        cfg.window = args.window
    if args.no_validate:
        cfg.validate = False
    target = save_config(cfg, args.config)
    print(f"已写入 {target}")
    print(describe(cfg))
    return 0


# ------------------------------------------------------------------ send

def _parse_file_arg(raw: str, status: str) -> dict:
    """``path`` 或 ``path:added:removed``（从右侧切两段，路径里的冒号不会被误伤）。"""
    text = str(raw).strip()
    added = removed = 0
    parts = text.rsplit(":", 2)
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        text, added, removed = parts[0], int(parts[1]), int(parts[2])
    return {"path": text, "status": status, "added": added, "removed": removed}


def _file_entries(args: argparse.Namespace) -> list[dict]:
    items: list[dict] = []
    for raw in args.file or []:
        items.append(_parse_file_arg(raw, "modified"))
    for raw in args.added_file or []:
        items.append(_parse_file_arg(raw, "added"))
    for raw in args.deleted_file or []:
        items.append(_parse_file_arg(raw, "deleted"))
    return items


def cmd_send(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    gaps = cfg.missing()
    if gaps:
        print("配置不完整：" + "、".join(gaps), file=sys.stderr)
        return 2
    files = _file_entries(args)
    if not files:
        print("至少给一个 --file / --added-file / --deleted-file", file=sys.stderr)
        return 2
    repo_path = args.repo_path or "."
    envelope = build_event(
        event_uid=args.uid or new_event_uid(),
        repo={
            "label": args.label or Path(repo_path).name or "repo",
            "path_hash": args.path_hash or repo_path_hash(repo_path),
            "branch": args.branch or "",
            "head": args.head or "",
            "unpushed": bool(args.unpushed),
        },
        files=files,
        kind=args.kind,
        device=cfg.device(),
        notes=args.notes or "",
        trigger="manual",
    )
    client = EdgeClient(
        cfg.url,
        cfg.token,
        **cfg.client_kwargs(),
        rest_fallback=fallback_from_config(cfg),
    )
    accepted = client.enqueue(envelope)
    if accepted:
        print(f"事件 {envelope['event_uid']} 已入队（{len(client.queue)} 条待发）")
    else:
        print(f"事件 {envelope['event_uid']} 已在队列中，跳过")
    if args.offline:
        print("按 --offline 要求只入队、不发送")
        return 0
    return asyncio.run(_send_and_wait(client, timeout=args.timeout))


async def _send_and_wait(client: EdgeClient, *, timeout: float) -> int:
    runner = asyncio.create_task(client.run_forever())
    idle = False
    try:
        idle = await client.wait_idle(timeout)
    finally:
        client.stop()
        try:
            await asyncio.wait_for(runner, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            runner.cancel()
    stats = client.stats()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if stats.get("fatal"):
        print(f"连接被终止：{stats['fatal'].get('reason')}", file=sys.stderr)
        return 1
    return 0 if idle or not stats.get("queued") else 1


# ------------------------------------------------------------------ run / clear

def _query_handler(root: str, *, allow_content: bool = False):
    """反向查询：服务端点名要哪些文件，这里在 ``--repo-root`` 沙箱内回答。

    默认只回元信息（大小 / sha256 / 行数 / 语言 / 修改时间），
    只有显式传 ``--allow-content`` 才允许回文本内容（并且先脱敏、按字节截断）。
    """
    sandbox = QuerySandbox(root, allow_content=allow_content)
    return sandbox


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    try:
        cfg.ensure_ready()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    client = EdgeClient(
        cfg.url,
        cfg.token,
        **cfg.client_kwargs(),
        rest_fallback=fallback_from_config(cfg),
        on_error=lambda kind, message: print(f"[{kind}] {message}", file=sys.stderr),
    )
    client.on_query_request = (
        _query_handler(args.repo_root, allow_content=bool(getattr(args, "allow_content", False)))
        if args.repo_root
        else None
    )
    print(f"连接 {cfg.url}（设备 {cfg.device().get('name')}，队列 {len(client.queue)} 条）")
    print("按 Ctrl+C 退出")
    try:
        client.run_blocking()
    except KeyboardInterrupt:  # pragma: no cover - 交互式退出
        pass
    stats = client.stats()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0 if not stats.get("fatal") else 1


def cmd_watch(args: argparse.Namespace) -> int:
    """无人操作窗口探针：捕捉文件夹变动 → 生成日志 → 上报（可选触发推断）。"""
    cfg = load_config(args.config)
    try:
        cfg.ensure_ready()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    rest = fallback_from_config(cfg)
    if rest is None:
        print("缺少 url / token，先执行 set 命令完成配对", file=sys.stderr)
        return 2

    # 账号与团队匹配：用设备令牌换策略，顺带把团队 slug 补进本地配置
    policy = {"idle_minutes": 3, "allow_view": False}
    try:
        info = rest.fetch_policy()
        policy = info.get("policy") or policy
        team_slug = str(info.get("team") or "")
        if team_slug and not cfg.team:
            cfg.team = team_slug
            save_config(cfg, args.config)
            print(f"已根据配对信息补全团队：{team_slug}")
        print(f"已匹配团队：{info.get('team_name') or team_slug}　设备：{(info.get('device') or {}).get('name', '')}")
    except EdgeApiError as exc:
        print(f"策略拉取失败，按默认值继续：{exc}", file=sys.stderr)

    idle = int(args.idle_minutes or policy.get("idle_minutes") or 3)
    allow_view = bool(policy.get("allow_view"))

    watcher = FolderWatcher(
        args.root,
        idle_minutes=idle,
        interval=args.interval,
    )
    root = Path(args.root).expanduser().resolve()
    label = args.label or root.name
    branch = args.branch or current_branch(root)
    repo = {"label": label, "path_hash": path_hash(root), "branch": branch}

    mode = "可见（显示变动内容与进度）" if allow_view else "静默（只上传，不展示内容）"
    print(f"开始监听：{root}")
    print(f"窗口：{idle} 分钟无操作后上报；模式：{mode}")
    print("按 Ctrl+C 退出\n")

    def on_window(report: "WindowReport") -> None:
        nonlocal idle
        # 每轮都重新拉一次策略：管理员在网页改了间隔，下一轮即生效
        try:
            latest = rest.fetch_policy().get("policy") or {}
            if latest.get("idle_minutes"):
                idle = int(latest["idle_minutes"])
                watcher.idle_minutes = idle
            if args.idle_minutes:
                watcher.idle_minutes = int(args.idle_minutes)
        except EdgeApiError:
            pass

        payload = report.log_payload(repo=label, branch=branch)
        if not allow_view:
            # 静默模式：只上报统计量，日志里不带具体文件路径
            payload["summary"] = (
                f"{report.idle_minutes} 分钟无操作，{report.file_count} 个文件改动"
                f"（+{report.additions} / -{report.deletions} 行）"
            )
            payload["files"] = []
        envelope = build_event(
            event_uid=new_event_uid(),
            repo=repo,
            files=report.event_files() if allow_view else [],
            # 统计量与符号摘要任何模式都上行，否则服务端只能看到「0 个文件」
            stats={
                "files": report.file_count,
                "added": report.additions,
                "removed": report.deletions,
            },
            digest=report.symbol_digest(),
            kind="snapshot",
            notes=payload["summary"][:500],
            trigger="idle-window",
        )
        try:
            result = rest.post_log(payload, envelope)
        except EdgeApiError as exc:
            print(f"[上报失败] {exc}", file=sys.stderr)
            return

        stamp = time.strftime("%H:%M:%S")
        if allow_view:
            print(f"[{stamp}] {payload['summary']}")
        else:
            print(f"[{stamp}] 已静默上报：{report.file_count} 个文件，+{report.additions}/-{report.deletions} 行")
        event = result.get("event") or {}
        if event:
            print(f"          日志 #{result.get('log_id')} · 事件 #{event.get('event_id')} · 作业 #{event.get('job_id')} · 议题 #{event.get('issue_number')}")

    try:
        watcher.run(on_window)
    except KeyboardInterrupt:
        print("\n已停止监听")
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    """打开桌面客户端窗口（pywebview）：状态、策略与手动控制都在窗口里。"""
    try:
        from .gui import run as run_ui
    except ImportError:
        print("需要先安装：pip install pywebview", file=sys.stderr)
        return 2
    return run_ui(args.config, args.root, args.interval)


def cmd_clear(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    target = Path(cfg.spool_path())
    if not target.exists():
        print(f"{target} 不存在，无需清理")
        return 0
    backup = target.with_suffix(target.suffix + f".{int(time.time())}.bak")
    target.replace(backup)
    print(f"已把离线队列移到 {backup}")
    return 0


# ------------------------------------------------------------------ CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m client.main",
        description="TeamFlow 端侧 SDK 命令行（通信层，不含探针）",
    )
    parser.add_argument("--config", help="配置文件路径（默认 ~/.teamflow/edge.json）")
    parser.add_argument("--version", action="store_true", help="显示端侧 SDK 版本")
    sub = parser.add_subparsers(dest="command")

    status = sub.add_parser("status", help="查看配置、离线队列与连通性")
    status.add_argument("--check", action="store_true", help="请求服务端 /api/health")
    status.set_defaults(func=cmd_status)

    setter = sub.add_parser("set", help="写入配置")
    setter.add_argument("--url", help="wss://host/api/edge/ws")
    setter.add_argument("--token", help="配对码（tfd_…）")
    setter.add_argument("--team", help="团队 slug")
    setter.add_argument("--device-name", dest="device_name")
    setter.add_argument("--platform", choices=["darwin", "windows", "linux"])
    setter.add_argument("--agent-version", dest="agent_version")
    setter.add_argument("--spool-dir", dest="spool_dir", help="离线队列目录（默认 ~/.teamflow）")
    setter.add_argument("--heartbeat", type=float, help="心跳秒数（5–600）")
    setter.add_argument("--queue-limit", dest="queue_limit", type=int,
                        help="离线队列上限（1–10000）")
    setter.add_argument("--window", type=int, help="在途事件数（1–32）")
    setter.add_argument("--no-validate", dest="no_validate", action="store_true",
                        help="关闭本地事件自检（不建议）")
    setter.set_defaults(func=cmd_set)

    send = sub.add_parser("send", help="构造并发送一个事件（联调用）")
    send.add_argument("--branch")
    send.add_argument("--head", help="HEAD commit")
    send.add_argument("--label", help="仓库显示名")
    send.add_argument("--path-hash", dest="path_hash",
                      help="仓库路径指纹（默认按 --repo-path 计算）")
    send.add_argument("--repo-path", dest="repo_path", help="仓库根目录（用于计算指纹）")
    send.add_argument("--file", action="append", help="修改的文件：path 或 path:added:removed")
    send.add_argument("--added-file", dest="added_file", action="append", help="新增文件")
    send.add_argument("--deleted-file", dest="deleted_file", action="append", help="删除文件")
    send.add_argument("--kind", default="save",
                      choices=["save", "snapshot", "commit", "branch-switch", "manual"])
    send.add_argument("--notes", help="给服务端的备注（≤4000 字）")
    send.add_argument("--uid", help="自定义 event_uid（默认自动生成）")
    send.add_argument("--unpushed", action="store_true", help="标记本地提交尚未 push")
    send.add_argument("--offline", action="store_true", help="只入队、不发送")
    send.add_argument("--timeout", type=float, default=20.0, help="等队列发完的秒数")
    send.set_defaults(func=cmd_send)

    runner = sub.add_parser("run", help="连接网关并持续运行（清空离线队列）")
    runner.add_argument("--repo-root", dest="repo_root", help="允许反向查询的仓库根目录")
    runner.add_argument(
        "--allow-content",
        dest="allow_content",
        action="store_true",
        help="反向查询允许回文本内容（默认只回大小/摘要，内容会先脱敏并截断）",
    )
    runner.set_defaults(func=cmd_run)

    clearer = sub.add_parser("clear", help="把离线队列移到备份文件")
    clearer.set_defaults(func=cmd_clear)

    watcher = sub.add_parser("watch", help="监听文件夹：无操作一段时间后生成日志并上报")
    watcher.add_argument("--root", required=True, help="要监听的文件夹")
    watcher.add_argument("--idle-minutes", dest="idle_minutes", type=int,
                         help="覆盖服务端下发的间隔时长（默认用权限页配置）")
    watcher.add_argument("--interval", type=float, default=5.0, help="扫描间隔秒数（默认 5）")
    watcher.add_argument("--label", help="仓库显示名（默认取文件夹名）")
    watcher.add_argument("--branch", help="分支名（默认读 .git/HEAD）")
    watcher.set_defaults(func=cmd_watch)

    gui = sub.add_parser("ui", help="打开客户端窗口（pywebview）")
    gui.add_argument("--root", help="要监听的文件夹（默认当前目录）")
    gui.add_argument("--interval", type=float, default=5.0, help="扫描间隔秒数（默认 5）")
    gui.set_defaults(func=cmd_ui)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if getattr(args, "version", False):
        from . import __version__

        print(f"teamflow-edge-sdk {__version__}")
        return 0
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args))
    except EventInvalid as exc:
        print(f"事件不合法（{exc.field or 'field'}）：{exc}", file=sys.stderr)
        return 2
    except (ConfigError, EdgeApiError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
