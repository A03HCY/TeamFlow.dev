"""极简 ``.env`` 读取器（不依赖 python-dotenv）。

支持的写法（与 python-dotenv 的习惯保持一致）::

    # 注释
    TEAMFLOW_DB_URL=sqlite:///./data.db
    export TEAMFLOW_PORT=8000
    TEAMFLOW_TURN_URL="turn:turn.example.com:3478?transport=udp"
    TEAMFLOW_TURN_CREDENTIAL='p@ss$word'        # 单引号：原样保留，不转义
    TEAMFLOW_STORAGE_ORIGINS="https://a.com, https://b.com"   # 双引号：支持 \\n \\t \\" 等转义
    TEAMFLOW_BACKUP_DIR=/srv/teamflow/backups   # 未加引号时，行尾 " # 注释" 会被丢掉
    TEAMFLOW_DB_URL=${TEAMFLOW_DATA_DIR}/data.db              # 引用已加载的变量

两条重要规则：

* **系统环境变量优先**。已经存在的 ``os.environ`` 不会被 ``.env`` 覆盖，
  这样 systemd / Docker / 命令行临时导出的值才是最终生效的；确实要覆盖时
  设 ``TEAMFLOW_ENV_OVERRIDE=1``。**空字符串按「未设置」处理**（``KEY=`` 常被
  用来表示「走默认值」），所以 ``.env`` 里的非空值仍然会生效。
* 文件不存在不算错误（开发机上大多数人没有 ``.env``）。想指定别的路径用
  ``TEAMFLOW_ENV_FILE``，或 ``python server.py --env-file prod.env``。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

ENV_FILE_VAR = "TEAMFLOW_ENV_FILE"
OVERRIDE_VAR = "TEAMFLOW_ENV_OVERRIDE"
_TRUTHY = ("1", "true", "yes", "on")
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EXPAND_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_UNESCAPE = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "$": "$"}


@dataclass
class EnvFile:
    """一次读取的结果，方便启动横幅打印「读了几项 / 哪个文件」。"""

    path: Path
    existed: bool = False
    applied: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


def _closing(body: str, quote: str) -> int | None:
    """返回引号结束的位置；双引号要跳过被转义的引号。"""
    if quote == "'":
        return body.find("'")
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            return index
        index += 1
    return None


def _unescape(text: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            nxt = text[index + 1]
            out.append(_UNESCAPE.get(nxt, nxt))
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def parse(text: str) -> tuple[dict[str, str], list[str]]:
    """把 ``.env`` 文本解析成字典；返回 ``(键值对, 无法解析的行)``。"""
    data: dict[str, str] = {}
    skipped: list[str] = []
    lines = text.lstrip("\ufeff").splitlines()
    index = 0
    while index < len(lines):
        raw = lines[index]
        index += 1
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            skipped.append(raw.strip())
            continue
        key, _, rest = line.partition("=")
        key = key.strip()
        if not _KEY_RE.match(key):
            skipped.append(raw.strip())
            continue
        rest = rest.strip()
        if rest[:1] in ('"', "'") and rest:
            quote = rest[0]
            body = rest[1:]
            consumed = 0
            end = _closing(body, quote)
            while end is None and index + consumed < len(lines):
                body += "\n" + lines[index + consumed]
                consumed += 1
                end = _closing(body, quote)
            if end is None:  # 引号没有闭合：整行当普通值
                data[key] = rest
                continue
            value = body[:end]
            index += consumed
            data[key] = _unescape(value) if quote == '"' else value
            continue
        data[key] = re.split(r"\s+#", rest, maxsplit=1)[0].rstrip()
    return data, skipped


def _expand(value: str, env: dict[str, str]) -> str:
    """展开 ``$VAR`` / ``${VAR}``；查不到的保持原样，避免把密码吃掉。"""

    def repl(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return env.get(name, match.group(0))

    return _EXPAND_RE.sub(repl, value) if "$" in value else value


def load_environment(path: str | Path | None = None, *, override: bool | None = None,
                     base_dir: str | Path | None = None) -> EnvFile:
    """读取 ``.env`` 并写进 ``os.environ``。

    路径优先级：``path`` 参数 > 环境变量 ``TEAMFLOW_ENV_FILE`` > ``base_dir/.env``。
    """
    explicit = str(path or os.environ.get(ENV_FILE_VAR) or "").strip()
    if explicit:
        target = Path(explicit).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
    else:
        target = Path(base_dir or Path.cwd()) / ".env"
    if override is None:
        override = os.environ.get(OVERRIDE_VAR, "").strip().lower() in _TRUTHY

    result = EnvFile(path=target)
    if not target.is_file():
        return result
    result.existed = True
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:  # 权限不足等：当作没有配置，不要拦住启动
        return result

    parsed, skipped = parse(text)
    result.skipped = skipped

    def configured(key: str) -> bool:
        """环境里已存在且非空才算「用户真的设过」。"""
        return bool(os.environ.get(key, "").strip())

    chain = dict(os.environ)
    for key, value in parsed.items():
        value = _expand(value, chain)
        if override or not configured(key):
            os.environ[key] = value
            result.applied[key] = value
            chain[key] = value
    return result
