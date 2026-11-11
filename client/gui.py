"""TeamFlow 客户端窗口（pywebview）。

把端侧探针放进一个桌面小窗口：左边看状态与策略，右边看最近上报，
按钮控制「开始 / 暂停 / 立即分析 / 换目录」。采集线程与窗口分离：

* 主线程跑 pywebview 的事件循环（Windows 上是 WebView2 / EdgeChromium）；
* 后台线程跑 ``FolderWatcher``，每个无人操作窗口结束就上报一次；
* 界面用 JS 每秒轮询 ``window.pywebview.api.state()``，不靠推送，简单可靠。

启动::

    python -m client.main ui --root D:\\Projects\\TeamFlow.dev
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from .api import EdgeApiError, RestFallback, fallback_from_config
from .config import load_config, save_config
from .core import pairing
from .core.network import EventInvalid, build_event, new_event_uid
from .core.watcher import FolderWatcher, WindowReport, current_branch, path_hash

UI_DIR = Path(__file__).with_name("ui")
INDEX_HTML = UI_DIR / "index.html"
#: 端侧排查日志：记录「监听了哪个目录、每次上报结果、采集异常」
LOG_PATH = Path.home() / ".teamflow" / "client.log"


def _log(message: str) -> None:
    """写一行排查日志。

    没有它，「改了代码却一直没有洞察」只能靠猜——真实踩过的一次是客户端
    一直在监听另一个同名空目录，界面上完全看不出来，只能靠日志定位。
    """
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")
    except Exception:  # noqa: BLE001 - 写日志失败不能影响采集
        pass


class WatcherService:
    """后台采集服务：跑一个 FolderWatcher，把窗口结果发给服务端。"""

    # pywebview 会把 js_api 对象的属性递归展开成 JS 函数；这个对象图很深，
    # 标记成不可序列化让它只暴露 Bridge 自己的七个方法。
    _serializable = False

    def __init__(self, cfg, root: str | Path, *, interval: float = 5.0) -> None:
        self.cfg = cfg
        self.root = Path(root).expanduser().resolve()
        self.interval = interval
        self.rest = fallback_from_config(cfg)
        self.watcher = FolderWatcher(self.root, idle_minutes=3, interval=interval)
        self.label = self.root.name
        self.branch = current_branch(self.root)
        self.repo = {"label": self.label, "path_hash": path_hash(self.root), "branch": self.branch}

        self.idle_minutes = 3
        self.allow_view = False
        self.team_name = ""
        self.device_name = ""
        self.server = str(getattr(cfg, "url", "") or "")
        self.username = str(getattr(cfg, "username", "") or "")
        self.os_user = pairing.local_username()
        self.bound = self.rest is not None
        self.notice = ""
        self.policy_updated = ""
        self.running = False
        self.paused = False
        self.uploaded = 0
        self.windows = 0
        self.skipped = 0
        self.last_signature = ""
        self.last_error = ""
        self.last_analysis = ""
        self.logs: deque[dict] = deque(maxlen=30)

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            self.running = True
            self.paused = False
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="teamflow-watch", daemon=True)
        self._thread.start()
        self.running = True
        self.paused = False

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        if self.paused:
            # 暂停期间不扫描，恢复时重建基线：否则暂停几小时的改动会一次性算进一个窗口
            try:
                self.watcher.rebase(keep_pending=True)
            except Exception as exc:  # noqa: BLE001 - 基线失败不应卡住恢复
                self.last_error = f"重建基线失败：{exc}"
        self.paused = False

    def stop(self) -> None:
        self._stop.set()
        self.running = False

    # ------------------------------------------------------------ 绑定

    def bind(self, cfg) -> None:
        """换一份端侧配置（刚绑定成功）：重建上报通道并立刻拉一次策略。"""
        with self._lock:
            self.cfg = cfg
            self.server = str(getattr(cfg, "url", "") or "")
            self.rest = fallback_from_config(cfg)
            self.bound = self.rest is not None
            self.team_name = ""
            self.device_name = ""
            self.username = str(getattr(cfg, "username", "") or "")
            self.last_error = ""
            if not self.bound:
                # 退出登录后不该再挂着「登录已失效」这类提示
                self.notice = ""
        self.refresh_policy()

    # ------------------------------------------------------------ 策略

    def refresh_policy(self) -> dict:
        if self.rest is None:
            self.last_error = ""
            self.bound = False
            return {}
        try:
            info = self.rest.fetch_policy()
        except EdgeApiError as exc:
            self.last_error = str(exc)
            # 令牌失效（团队重建、设备被吊销）：退回未绑定，让用户重新接入
            if getattr(exc, "status", 0) == 401:
                self.bound = False
                self.last_error = ""      # 用「未接入」面板表达，不必再报红
                self.team_name = ""
                self.notice = "登录已失效（设备令牌作废），请重新登录"
            return {}
        policy = info.get("policy") or {}
        with self._lock:
            self.idle_minutes = int(policy.get("idle_minutes") or 3)
            self.allow_view = bool(policy.get("allow_view"))
            self.watcher.idle_minutes = max(1, self.idle_minutes)
            if not self.username:
                # 老版本配置里没有用户名：第一次拉策略时补上，界面才显示「谁登录的」
                self.username = str(info.get("username") or "")
            self.team_name = str(info.get("team_name") or info.get("team") or "")
            self.device_name = str((info.get("device") or {}).get("name") or "")
            self.policy_updated = str(policy.get("updated_at") or "")
            self.last_error = ""
            self.bound = True
        return policy

    # ------------------------------------------------------------ 采集循环

    def _loop(self) -> None:
        self.watcher.rebase(keep_pending=True)   # 建立基线
        last_policy = 0.0
        while not self._stop.is_set():
            try:
                if time.time() - last_policy > 60:
                    self.refresh_policy()
                    last_policy = time.time()
                if not self.paused:
                    report = self.watcher.poll()
                    pending = self.watcher.pending_count
                    # 只在「待上报文件数变化」时记一行，避免每 5 秒刷屏；
                    # 有这行就能区分「没看到改动」和「看到了但没上报」
                    if pending and pending != getattr(self, "_logged_pending", -1):
                        _log(
                            f"待上报 {pending} 个文件"
                            f"（{self.watcher.seconds_until_settle()} 秒后结算）"
                        )
                    self._logged_pending = pending
                    if report is not None:
                        self._deliver(report)
            except Exception as exc:  # noqa: BLE001 - 采集线程不能因单次异常退出
                self.last_error = f"{type(exc).__name__}: {exc}"
                _log(f"采集异常：{self.last_error}")
            time.sleep(self.interval)

    def trigger(self) -> dict:
        """「立即分析」：结算当前改动并马上上报。"""
        report = self.watcher.flush()
        if report is None:
            return {"ok": False, "message": "当前没有待分析的改动"}
        outcome = self._deliver(report)
        if outcome == "duplicate":
            return {"ok": False, "message": "与上次同步的内容相同，已跳过"}
        if outcome == "error":
            return {"ok": False, "message": self.last_error or "上报失败"}
        return {"ok": True, "message": f"已上报 {report.file_count} 个文件"}

    def _deliver(self, report: WindowReport) -> str:
        """把一条窗口报告发给服务端；返回 ok / duplicate / error。"""
        if not report.changes:
            return "duplicate"
        signature = report.signature()
        if signature and signature == self.last_signature:
            # 和上次成功上报的那一批完全一样：没有新改动，不再重复提交
            with self._lock:
                self.skipped += 1
            _log("上报跳过：与上次成功提交的内容完全相同")
            return "duplicate"

        payload = report.log_payload(repo=self.label, branch=self.branch)
        if not self.allow_view:
            payload["summary"] = (
                f"{report.idle_minutes} 分钟无操作，{report.file_count} 个文件改动"
                f"（+{report.additions} / -{report.deletions} 行）"
            )
        envelope = None
        try:
            envelope = build_event(
                event_uid=new_event_uid(),
                repo=self.repo,
                files=report.event_files() if self.allow_view else [],
                # 静默模式不传路径，但文件数/增删行必须照传：
                # 否则服务端规则推断只能看到「0 个文件 +0/-0」，洞察会写成空变更
                stats={
                    "files": report.file_count,
                    "added": report.additions,
                    "removed": report.deletions,
                },
                # 符号摘要不含路径也不含代码原文，静默模式也上行：
                # 这样洞察才说得出「新增了哪些函数」，而不是只有「3 个文件变化」
                digest=report.symbol_digest(),
                kind="snapshot",
                notes=payload["summary"][:500],
                trigger="idle-window",
            )
        except EventInvalid as exc:
            self.last_error = f"事件不合法：{exc}"
            _log(f"事件构造失败：{exc}")

        if self.rest is None:
            self.last_error = "缺少 url/token，无法上报"
            _log("上报失败：缺少 url/token")
            return "error"
        try:
            result = self.rest.post_log(payload, envelope)
        except EdgeApiError as exc:
            self.last_error = str(exc)
            _log(f"上报失败：{exc}")
            return "error"

        event = result.get("event") or {}
        analysis = str(result.get("analysis") or "")
        with self._lock:
            self.last_signature = signature
            self.last_analysis = analysis
            self.uploaded += 1
            self.windows += 1
            self.last_error = ""
            self.logs.appendleft(
                {
                    "time": time.strftime("%H:%M:%S"),
                    "files": report.file_count,
                    "additions": report.additions,
                    "deletions": report.deletions,
                    "status": str(result.get("status") or ""),
                    "event_id": event.get("event_id"),
                    "job_id": event.get("job_id"),
                    "issue": event.get("issue_number"),
                    "analysis": analysis,
                }
            )
        _log(
            f"上报成功：{report.file_count} 文件 +{report.additions}/-{report.deletions}"
            f" → {result.get('status')} 事件#{event.get('event_id')} 作业#{event.get('job_id')}"
        )
        return "ok"

    # ------------------------------------------------------------ 视图数据

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self.running and not self.paused,
                "paused": self.paused,
                "root": str(self.root),
                "label": self.label,
                "branch": self.branch,
                "idle_minutes": self.idle_minutes,
                "allow_view": self.allow_view,
                "team": self.team_name,
                "device": self.device_name,
                "server": self.server,
                "bound": self.bound,
                "username": self.username,
                "notice": self.notice,
                "os_user": self.os_user,
                "platform": pairing.platform_name(),
                "policy_updated": self.policy_updated,
                "uploaded": self.uploaded,
                "skipped": self.skipped,
                "pending": self.watcher.pending_count,
                "settle_in": self.watcher.seconds_until_settle(),
                "error": self.last_error,
                "last_analysis": self.last_analysis,
                "logs": list(self.logs)[:12],
                "interval": self.interval,
            }


class Bridge:
    """暴露给页面 JS 的接口（``window.pywebview.api.*``）。"""

    def __init__(self, service: WatcherService) -> None:
        self.service = service
        # 下划线开头：pywebview 生成 JS 接口时会跳过它，避免递归扫描 .NET 对象
        self._window = None

    def state(self) -> dict:
        return self.service.snapshot()

    def toggle(self) -> dict:
        if self.service.rest is None:
            return {"ok": False, "message": "还没有接入服务端，先在下面填地址并绑定"}
        if self.service.running and not self.service.paused:
            self.service.pause()
            return {"ok": True, "running": False}
        self.service.start()
        return {"ok": True, "running": True}

    def trigger(self) -> dict:
        return self.service.trigger()

    # ------------------------------------------------------------ 绑定 / 登录

    def _apply_binding(self, base_url: str, data: dict) -> dict:
        """把服务端返回的令牌写进本机配置，并让采集线程立刻改用新通道。"""
        cfg = load_config()
        cfg.url = pairing.ws_url(base_url)
        cfg.token = str(data.get("token") or "")
        cfg.team = str(data.get("team") or cfg.team or "")
        cfg.username = str(data.get("username") or cfg.username or "")
        device = data.get("device") or {}
        cfg.device_name = str(device.get("name") or cfg.device_name or pairing.local_device_name())
        try:
            save_config(cfg)
        except Exception as exc:  # noqa: BLE001 - 落盘失败要说清楚，但不能让界面崩
            return {"ok": False, "error": f"配置保存失败：{exc}"}
        self.service.username = str(data.get("username") or "")
        self.service.notice = ""
        self.service.bind(cfg)
        self.service.start()
        return {
            "ok": True,
            "team": self.service.team_name or str(data.get("team_name") or data.get("team") or ""),
            "username": self.service.username,
            "server": cfg.url,
        }

    def bind_login(self, base_url: str, username: str, password: str, team: str = "") -> dict:
        """与网页同一账号：登录后直接绑定，不需要配对码。

        账号在多个团队里时先回 ``need_team``，由界面选一次再调回来。
        """
        try:
            data = pairing.login_and_pair(
                base_url,
                str(username or "").strip(),
                str(password or ""),
                team=str(team or "").strip(),
            )
        except pairing.PairError as exc:
            return {"ok": False, "error": str(exc)}
        if data.get("need_team"):
            return {
                "ok": False,
                "need_team": True,
                "teams": data.get("teams") or [],
                "error": "这个账号在多个团队里，请先选一个",
            }
        result = self._apply_binding(base_url, data)
        if result.get("ok"):
            result["teams"] = data.get("teams") or []
        return result

    def bind_code(self, base_url: str, code: str, username: str = "") -> dict:
        """用管理员给的配对码接入；用户名默认取本机用户名，服务端会自动建成员。"""
        try:
            data = pairing.pair_with_code(
                base_url,
                str(code or ""),
                str(username or "").strip() or self.service.os_user,
            )
        except pairing.PairError as exc:
            return {"ok": False, "error": str(exc)}
        result = self._apply_binding(base_url, data)
        if result.get("ok"):
            result["created_user"] = bool(data.get("user_created"))
        return result

    def logout(self) -> dict:
        """退出登录：停掉监听、清掉本机令牌，界面回到登录页。

        设备行留在服务端（下次同账号登录会复用同一台设备，只换令牌），
        所以退出登录不等于吊销设备——吊销仍然要在网页「引擎」里做。
        """
        cfg = load_config()
        cfg.url = ""
        cfg.token = ""
        cfg.team = ""
        cfg.username = ""
        try:
            save_config(cfg)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"配置保存失败：{exc}"}
        self.service.stop()
        self.service.bind(cfg)
        self.service.notice = "已退出登录"
        return {"ok": True}

    def refresh_policy(self) -> dict:
        policy = self.service.refresh_policy()
        return {"ok": bool(policy), "policy": policy, "error": self.service.last_error}

    def open_folder(self) -> dict:
        root = str(self.service.root)
        try:
            os.startfile(root)  # noqa: S606 - 打开资源管理器，仅本机
            return {"ok": True}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

    def choose_folder(self) -> dict:
        import webview

        result = self._window.create_file_dialog(webview.FOLDER_DIALOG) if self._window else None
        if not result:
            return {"ok": False}
        target = result[0] if isinstance(result, (list, tuple)) else str(result)
        return self.set_root(target)

    def set_root(self, path: str) -> dict:
        """按路径切换监听目录（支持直接粘贴路径）。"""
        raw = str(path or "").strip().strip('"')
        if not raw:
            return {"ok": False, "error": "请先填写目录路径"}
        target = Path(raw).expanduser()
        if not target.exists() or not target.is_dir():
            return {"ok": False, "error": f"目录不存在：{target}"}
        self.service.root = target.resolve()
        self.service.watcher = FolderWatcher(
            self.service.root,
            idle_minutes=self.service.idle_minutes,
            interval=self.service.interval,
        )
        self.service.label = self.service.root.name
        self.service.branch = current_branch(self.service.root)
        self.service.last_signature = ""
        self.service.repo = {
            "label": self.service.label,
            "path_hash": path_hash(self.service.root),
            "branch": self.service.branch,
        }
        # 换目录：旧目录的待上报内容作废，新目录从头建立基线
        self.service.watcher.rebase(keep_pending=False)
        return {"ok": True, "root": str(self.service.root)}

    def close(self) -> dict:
        """关窗前先把还没结算的改动送出去，再停采集。

        直接 ``stop()`` 会把内存里的 ``_pending`` 丢掉——用户点了右上角的 ×，
        刚写的一分钟改动就永远消失，看起来就是「我明明改了却没分析」。
        """
        try:
            report = self.service.watcher.flush()
            if report is not None:
                _log(f"关窗结算：{report.file_count} 个文件待上报")
                self.service._deliver(report)
        except Exception as exc:  # noqa: BLE001 - 关窗不能因为上报失败卡住
            _log(f"关窗前上报失败：{type(exc).__name__}: {exc}")
        self.service.stop()
        if self._window:
            self._window.destroy()
        return {"ok": True}


def run(cfg_path: str | None = None, root: str | None = None, interval: float = 5.0) -> int:
    """打开客户端窗口；返回进程退出码。"""
    import webview

    cfg = load_config(cfg_path)
    # 没绑定也允许开窗口：界面里会显示「接入服务端」面板，比直接退出友好
    try:
        cfg.ensure_ready()
    except Exception as exc:  # noqa: BLE001 - 配置不全只是不能上报，不影响开窗
        print(f"端侧尚未绑定：{exc}")

    target = Path(root).expanduser().resolve() if root else Path.cwd()
    service = WatcherService(cfg, target, interval=interval)
    if service.rest is not None:
        service.refresh_policy()
        service.start()
        try:
            files = len(service.watcher.snapshot())
            _log(
                f"采集启动 root={service.root} 文件数={files} "
                f"分支={service.branch or '(非 git)'} idle={service.idle_minutes} 分钟"
            )
        except Exception as exc:  # noqa: BLE001 - 基线扫描失败只影响日志
            _log(f"采集启动 root={target} 基线扫描失败：{type(exc).__name__}: {exc}")
    else:
        _log(f"未绑定服务端，仅开窗口 root={target}")
        service.last_error = ""

    bridge = Bridge(service)
    window = webview.create_window(
        "TeamFlow 客户端",
        url=str(INDEX_HTML),
        js_api=bridge,
        width=1040,
        height=760,
        min_size=(900, 640),
        background_color="#ffffff",
    )
    bridge._window = window
    try:
        webview.start(debug=bool(os.environ.get("TEAMFLOW_UI_DEBUG")))
    finally:
        service.stop()
    return 0
