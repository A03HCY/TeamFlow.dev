"""LLM 后端构造（OpenAI 兼容端点：云端 / Ollama / vLLM 都走这一条）。

orchify 的 ``OpenAICompat`` 只依赖 httpx，base_url 指向哪里由团队配置决定，
所以「私有化」不需要改代码，只要把 base_url 换成内网地址。

``orchify`` 本身是可选依赖：这里所有导入都发生在函数内部，没装时抛
``EngineUnavailable``（503），而不是让整个应用 import 失败。
"""
from __future__ import annotations

from .config import EngineConfig
from .errors import EngineNotConfigured, EngineUnavailable


def orchify_module():
    """惰性导入 orchify；缺失时给出可执行的安装提示。"""
    try:
        import orchify  # noqa: F401 - 仅探测可导入性
    except ModuleNotFoundError as exc:  # pragma: no cover - 取决于部署环境
        raise EngineUnavailable(
            "未安装推理引擎依赖，请执行：pip install orchify  （或 pip install -e D:/Projects/orchify）"
        ) from exc
    return orchify


def build_llm(cfg: EngineConfig):
    """按团队配置构造 LLM 后端（同步构造，不发请求）。"""
    cfg.require_configured()
    orchify_module()
    from orchify.llm.openai import OpenAICompat

    return OpenAICompat(api_key=cfg.api_key, base_url=cfg.base_url)


def build_embeddings(cfg: EngineConfig):
    """向量检索用的 embedding 后端；默认 ``HashingEmbeddings``（零依赖，可离线）。"""
    orchify_module()
    from orchify.embeddings import HashingEmbeddings, OpenAIEmbeddings

    name = (cfg.embedding_model or "hashing").strip().lower()
    if name in ("hashing", "hash", "local", ""):
        return HashingEmbeddings(512)
    cfg.require_configured()
    return OpenAIEmbeddings(model=cfg.embedding_model, api_key=cfg.api_key, base_url=cfg.base_url)


async def aprobe(cfg: EngineConfig, *, timeout: float = 8.0) -> dict:
    """探活：``GET {base_url}/models``，用于「设置 → 测试连接」。

    不抛异常，统一返回 ``{"ok": bool, "models": [...], "error": str}``，
    这样前端可以就地显示结果。
    """
    import httpx

    if not cfg.base_url:
        return {"ok": False, "models": [], "error": "尚未填写接口地址"}
    url = cfg.base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers)
    except Exception as exc:  # noqa: BLE001 - 网络错误统一转成可展示文本
        return {"ok": False, "models": [], "error": f"连接失败：{exc}"}
    if response.status_code >= 400:
        detail = response.text.strip().replace("\n", " ")[:200]
        return {
            "ok": False,
            "models": [],
            "error": f"HTTP {response.status_code}：{detail or response.reason_phrase}",
        }
    models: list[str] = []
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    for item in (payload.get("data") or payload.get("models") or []):
        if isinstance(item, dict):
            name = item.get("id") or item.get("name")
            if isinstance(name, str):
                models.append(name)
        elif isinstance(item, str):
            models.append(item)
    return {"ok": True, "models": models[:50], "error": ""}


def ensure_ready(cfg: EngineConfig) -> EngineConfig:
    """管线入口的前置检查：装了 orchify、配置齐全、团队开关打开。"""
    orchify_module()
    cfg.require_enabled()
    return cfg.require_configured()


__all__ = [
    "EngineNotConfigured",
    "aprobe",
    "build_embeddings",
    "build_llm",
    "ensure_ready",
    "orchify_module",
]
