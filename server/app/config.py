"""应用配置：路径、常量、数据库 URL 解析。

配置来源有两层：**系统环境变量**，以及项目根目录的 ``.env`` 文件（可选）。
``.env`` 在下面第 20 行就被读入 ``os.environ``，因此后面所有常量都能用上它；
已存在的系统环境变量不会被覆盖（见 ``app/dotenv.py``）。

支持一个进程内通过环境变量切换数据库后端：

    TEAMFLOW_DB_URL=sqlite:///D:/Projects/TeamFlow/data.db   # 默认
    TEAMFLOW_DB_URL=postgresql://teamflow:secret@127.0.0.1:5432/teamflow
    TEAMFLOW_DB_URL=mysql://teamflow:secret@127.0.0.1:3306/teamflow
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .dotenv import load_environment

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"

# 必须放在读取任何 TEAMFLOW_* 之前：.env 里的值要参与下面所有常量的计算
ENV_FILE = load_environment(base_dir=BASE_DIR)

APP_NAME = "TeamFlow"
APP_VERSION = "2.0.0"

# 会话 / 编辑口令
SESSION_DAYS = 30
EDIT_COOKIE_HOURS = 12
PASSWORD_ITERATIONS = 180_000

# Cookie 的 Secure 标记：留空按请求协议自动判断（HTTPS 才加），
# 反向代理没有传 X-Forwarded-Proto 时可以显式写 1 / 0。
COOKIE_SECURE_MODE = os.environ.get("TEAMFLOW_COOKIE_SECURE", "").strip().lower()


def hsts_enabled() -> bool:
    """HSTS 开关。

    默认只在真正的 HTTPS 请求上下发。如果反向代理错误地始终发送
    ``X-Forwarded-Proto: https``（实际却是明文 HTTP），浏览器会被 HSTS
    强制升级到 https 而再也打不开站点；这种情况下用
    ``TEAMFLOW_HSTS=0`` 关掉即可恢复。
    """
    return os.environ.get("TEAMFLOW_HSTS", "1").strip().lower() not in ("0", "false", "off", "no")

# 响应压缩：只压缩文本类内容，低于该长度的响应不压（gzip 头开销不划算）
COMPRESS_MIN_BYTES = 1024

# 登录限流：同一 来源IP+用户名 在窗口内的失败次数上限（成功即重置）
LOGIN_MAX_ATTEMPTS = 10
LOGIN_WINDOW_SECONDS = 300.0

# 角色
ADMIN_ROLES = frozenset({"owner", "admin"})
EDITABLE_ROLES = frozenset({"owner", "admin", "member"})

# 取值白名单
VALID_STATUS = ("todo", "in_progress", "done")
VALID_PRIORITY = ("high", "medium", "low")

# 头像：单张上限 2 MiB，只接受位图
AVATAR_MAX_BYTES = 2 * 1024 * 1024


# ------------------------------------------------------------------ 数据库 URL

@dataclass(frozen=True)
class DatabaseConfig:
    url: str
    backend: str = "sqlite"          # sqlite | postgresql | mysql
    path: Path | None = None         # sqlite 文件
    memory: bool = False             # sqlite :memory:
    host: str = "127.0.0.1"
    port: int | None = None
    database: str = ""
    user: str = ""
    password: str = ""
    options: dict = field(default_factory=dict)

    def describe(self) -> str:
        """可安全打印的描述（不含口令）。"""
        if self.backend == "sqlite":
            return f"sqlite:{':memory:' if self.memory else self.path}"
        where = f"{self.host}:{self.port}/{self.database}"
        who = f"{self.user}@" if self.user else ""
        return f"{self.backend}://{who}{where}"


def _sqlite_config(url: str) -> DatabaseConfig:
    raw = url.split("://", 1)[1] if "://" in url else url
    # sqlite:///abs/path（三个斜杠）或 sqlite://relative；`sqlite:///:memory:` 也算内存库
    if raw.startswith("/"):
        raw = raw[1:]
    if raw in (":memory:", "memory", ""):
        return DatabaseConfig(url=url, path=None, memory=True)

    # Windows: sqlite:///D:/x → D:/x；Unix 绝对路径 sqlite:////abs → /abs
    if len(raw) >= 3 and raw[0].isalpha() and raw[1] == ":":
        path = Path(raw)
    elif raw.startswith("./"):
        path = (BASE_DIR / raw[2:]).resolve()
    elif not os.path.isabs(raw) and not (len(raw) > 1 and raw[1] == ":"):
        path = (BASE_DIR / raw).resolve()
    else:
        path = Path(raw)
    return DatabaseConfig(url=url, path=path)


def parse_db_url(url: str) -> DatabaseConfig:
    """把数据库 URL 解析成配置；裸路径按 sqlite 文件处理，兼容旧变量。"""
    text = (url or "").strip()
    if not text:
        raise ValueError("数据库 URL 不能为空")
    if "://" not in text:
        return _sqlite_config(text)

    scheme, rest = text.split("://", 1)
    scheme = scheme.lower()
    if scheme in ("sqlite", "sqlite3", "file"):
        return _sqlite_config(text)
    if scheme in ("postgres", "postgresql", "psycopg", "psycopg2"):
        backend = "postgresql"
        default_port = 5432
    elif scheme in ("mysql", "mysql+pymysql", "mariadb"):
        backend = "mysql"
        default_port = 3306
    else:
        raise ValueError(f"不支持的数据库协议：{scheme}")

    credentials, _, location = rest.rpartition("@")
    user, _, password = credentials.partition(":")
    hostport, _, database = location.partition("/")
    host, _, port_text = hostport.partition(":")
    options: dict = {}
    if "?" in database:
        database, _, query = database.partition("?")
        for pair in query.split("&"):
            if "=" in pair:
                key, _, value = pair.partition("=")
                options[key] = value
    return DatabaseConfig(
        url=text,
        backend=backend,
        host=host or "127.0.0.1",
        port=int(port_text) if port_text.isdigit() else default_port,
        database=database,
        user=user,
        password=password,
        options=options,
    )


def database_url_from_env() -> str:
    explicit = os.environ.get("TEAMFLOW_DB_URL")
    if explicit:
        return explicit
    generic = os.environ.get("DATABASE_URL")
    if generic:
        return generic
    legacy = os.environ.get("TMT_DB")
    if legacy:
        return legacy if "://" in legacy else f"sqlite:///{legacy}"
    return f"sqlite:///{BASE_DIR / 'data.db'}"


def database_config(url: str | None = None) -> DatabaseConfig:
    return parse_db_url(url or database_url_from_env())


# ------------------------------------------------------------------ 备份

def backup_dir() -> Path:
    return Path(
        os.environ.get("TEAMFLOW_BACKUP_DIR")
        or os.environ.get("TMT_BACKUP_DIR")
        or (BASE_DIR / "backups")
    )


def backup_enabled() -> bool:
    return os.environ.get("TEAMFLOW_BACKUP", "1").strip().lower() not in ("0", "false", "off")


def avatar_dir() -> Path:
    """头像文件放哪儿：默认 data/avatars，可通过环境变量换成本地其它目录。

    TEAMFLOW_AVATAR_DIR=D:/TeamFlow-avatars
    """
    raw = (os.environ.get("TEAMFLOW_AVATAR_DIR") or "").strip()
    return Path(raw) if raw else DATA_DIR / "avatars"


# ------------------------------------------------------------------ 推理引擎
#
# 引擎（orchify 编排的「进度推断」）属于可选能力：orchify 没装、或者
# TEAMFLOW_ENGINE=0 时，应用照常启动，只是 /api/engine/*、/api/insights 等
# 接口返回 503。团队级开关存在 team_engine.enabled（默认 0），保证灰度可控。

def engine_enabled() -> bool:
    """进程级开关：0/false/off/no 关闭。"""
    return os.environ.get("TEAMFLOW_ENGINE", "1").strip().lower() not in ("0", "false", "off", "no")


def engine_data_dir() -> Path:
    """引擎运行数据（事件/检查点/run/向量）根目录，默认 data/engine。"""
    raw = (os.environ.get("TEAMFLOW_ENGINE_DIR") or "").strip()
    return Path(raw) if raw else DATA_DIR / "engine"


# 推断结果的可信度阈值：低于它的洞察只落库不进收件箱
ENGINE_CONFIDENCE_THRESHOLD = 0.6
# 人机审批超时（秒）。orchify 默认 None=永久等待，这里强制有上限
ENGINE_APPROVAL_TIMEOUT = 3600
# 单个推理作业的预算上限（orchify Agent 预算）
ENGINE_BUDGET_USD = 0.05
ENGINE_BUDGET_TOKENS = 200_000
ENGINE_BUDGET_SECONDS = 120.0
# 同一团队同时跑的推理作业数
ENGINE_MAX_CONCURRENCY = 2
# 事件合并窗口：窗口内同一 (设备, 仓库, 分支) 的多次保存只触发一个作业
ENGINE_COALESCE_SECONDS = 5.0
# 端侧防抖与单包上限
ENGINE_DEBOUNCE_SECONDS = 2.0
ENGINE_MAX_EVENT_BYTES = 512 * 1024
ENGINE_EVENT_MAX_FILES = 400
# 原始事件保留天数（不含推断结论）
ENGINE_RETENTION_DAYS = 90
# 推理时的采样温度：进度判断要稳定
ENGINE_LLM_TEMPERATURE = 0.2
ENGINE_DEFAULT_EMBEDDING = "hashing"
ENGINE_SCHEMA_VERSION = 1

# ---- P1 端侧接入与作业调度
# 配对码（= 设备 token）的有效期：超时未接入的待配对设备需要重新生成
ENGINE_PAIR_TTL_MINUTES = 60
# token 前缀，便于日志与密钥泄漏扫描时一眼认出
ENGINE_DEVICE_TOKEN_PREFIX = "tfd_"
# 单设备事件速率上限（每分钟），超限回 slow_down，不落库
ENGINE_EVENT_RATE_LIMIT = 120
# 同一 dedupe_key 的 draft 洞察在窗口内合并（不重复刷卡片）
ENGINE_INSIGHT_MERGE_MINUTES = 30
# 规则推断的最低置信度：低于它的变更只落 code_events，不建洞察
ENGINE_RULES_MIN_CONFIDENCE = 0.2
# 作业重试上限（规则阶段几乎不会失败，留给 P2 的 LLM 超时用）
ENGINE_WORKER_MAX_ATTEMPTS = 3
# 作业轮询间隔（秒）
ENGINE_WORKER_POLL_SECONDS = 1.0
# WS 下行反向查询的超时（秒）
ENGINE_QUERY_TIMEOUT = 10.0


def inference_worker_enabled() -> bool:
    """作业 worker 是否随应用启动（测试里关掉，手动 drain 更可控）。"""
    raw = os.environ.get("TEAMFLOW_INFERENCE_WORKER", "1").strip().lower()
    return raw not in ("0", "false", "off", "no")


