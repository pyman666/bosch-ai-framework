"""配置加载 — 从 YAML 读取模型配置, 展开成 LiteLLM Router 所需的 model_list.

用法::

    from infra.config.settings import DEFAULT_MODEL, MODEL_LIST, ROUTER_KWARGS

环境变量:
    - ``<PREFIX>_MODEL_CONFIG``: 自定义配置文件路径 (默认: 模块同级 settings.yaml)
    - ``AUTH_MODE``: 鉴权模式 (basic/xsuaa/both)
    - ``BAUTH_KEY`` / ``BAUTH_SECRET``: Basic Auth 凭据
    - ``XSUAA_SERVICE_KEY``: XSUAA service key JSON
    - ``XSUAA_CLIENT_ID`` / ``XSUAA_CLIENT_SECRET`` / ``XSUAA_URL`` / ``XSUAA_UAADOMAIN``
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)

load_dotenv()


# ---------------------------------------------------------------------------
# 鉴权配置
# ---------------------------------------------------------------------------

_VALID_AUTH_MODES = ("basic", "xsuaa", "both")

_auth_mode: str = os.environ.get("AUTH_MODE", "basic").strip().lower()
if _auth_mode not in _VALID_AUTH_MODES:
    raise RuntimeError(
        f"环境变量 AUTH_MODE='{_auth_mode}' 非法, 只接受: {_VALID_AUTH_MODES}."
    )

# --- basic 凭据 ---------------

_bauth_key = os.environ.get("BAUTH_KEY")
_bauth_secret = os.environ.get("BAUTH_SECRET")
if _auth_mode in ("basic", "both"):
    if not _bauth_key or not _bauth_secret:
        raise RuntimeError(
            f"AUTH_MODE='{_auth_mode}' 需要 BAUTH_KEY 和 BAUTH_SECRET 都设置 "
            "(检查 .env). 想完全禁掉 basic 走 SSO, 设 AUTH_MODE=xsuaa."
        )
_basic_auth_user: bytes | None = _bauth_key.encode("utf-8") if _bauth_key else None
_basic_auth_secret: bytes | None = _bauth_secret.encode("utf-8") if _bauth_secret else None


# --- XSUAA 凭据 ---------------

def _load_xsuaa_credentials() -> dict | None:
    """按优先级解析 XSUAA service credentials.

    优先级 (first hit wins):
        1. ``VCAP_SERVICES`` 里 label=='xsuaa' 的第一个 service binding
        2. ``XSUAA_SERVICE_KEY`` env 里整段 service key JSON
        3. ``XSUAA_*`` 4 件套 env
    """
    vcap = os.environ.get("VCAP_SERVICES")
    if vcap:
        try:
            data = json.loads(vcap)
            for inst in data.get("xsuaa", []):
                creds = inst.get("credentials")
                if creds:
                    log.info("[auth] XSUAA 凭据: VCAP_SERVICES.xsuaa[0]")
                    return creds
        except Exception as e:
            log.warning(f"[auth] VCAP_SERVICES 解析失败 (跳过): {e}")
    raw = os.environ.get("XSUAA_SERVICE_KEY")
    if raw:
        try:
            log.info("[auth] XSUAA 凭据: XSUAA_SERVICE_KEY env")
            return json.loads(raw)
        except Exception as e:
            log.warning(f"[auth] XSUAA_SERVICE_KEY JSON 解析失败 (跳过): {e}")
    if all(os.environ.get(k) for k in (
        "XSUAA_CLIENT_ID", "XSUAA_CLIENT_SECRET", "XSUAA_URL", "XSUAA_UAADOMAIN",
    )):
        log.info("[auth] XSUAA 凭据: 4 件套 env (XSUAA_CLIENT_ID/...)")
        return {
            "clientid": os.environ["XSUAA_CLIENT_ID"],
            "clientsecret": os.environ["XSUAA_CLIENT_SECRET"],
            "url": os.environ["XSUAA_URL"],
            "uaadomain": os.environ["XSUAA_UAADOMAIN"],
        }
    return None


_xsuaa_credentials: dict | None = None
_xsuaa_required_scope: str | None = os.environ.get("XSUAA_REQUIRED_SCOPE") or None
if _auth_mode in ("xsuaa", "both"):
    _xsuaa_credentials = _load_xsuaa_credentials()
    if _xsuaa_credentials is None:
        raise RuntimeError(
            f"AUTH_MODE='{_auth_mode}' 但 XSUAA 凭据 load 失败. 三选一:\n"
            "  - BTP CF: cf bind-service <app> <xsuaa-svc> + cf restage\n"
            "  - 本地: 在 .env 设 XSUAA_SERVICE_KEY='<整个 service key JSON>'\n"
            "  - 本地: 在 .env 设 XSUAA_CLIENT_ID/_CLIENT_SECRET/_URL/_UAADOMAIN 4 件套"
        )


# ---------------------------------------------------------------------------
# 模型配置加载
# ---------------------------------------------------------------------------

_config_path = Path(__file__).parent / "settings.yaml"

# 延迟加载配置 (避免模块导入时 IO)
_cfg: dict | None = None
DEFAULT_MODEL: str = ""
ROUTER_KWARGS: dict = {}
MODEL_LIST: list[dict] = []


def load_config(
    config_path: Path | str | None = None,
    *,
    default_model: str | None = None,
    router_kwargs: dict | None = None,
) -> dict:
    """加载/重载模型配置.

    Args:
        config_path: 配置文件路径, 默认从环境变量或自动查找
        default_model: 覆盖配置文件中的 default_model
        router_kwargs: 覆盖配置文件中的 router 参数

    Returns:
        解析后的配置 dict
    """
    global _cfg, DEFAULT_MODEL, ROUTER_KWARGS, MODEL_LIST

    path = Path(config_path) if config_path else _config_path
    if not path.exists():
        raise FileNotFoundError(f"找不到模型配置文件: {path}")

    _cfg = yaml.safe_load(path.read_text(encoding="utf-8"))

    DEFAULT_MODEL = default_model if default_model is not None else _cfg["default_model"]
    ROUTER_KWARGS = router_kwargs if router_kwargs is not None else _cfg.get("router", {})
    MODEL_LIST = _expand_model_list(_cfg["providers"])

    # 同时配置 llm 模块
    from infra.llm import _configure as _configure_llm
    _configure_llm(
        model_list=MODEL_LIST,
        router_kwargs=ROUTER_KWARGS,
        default_model=DEFAULT_MODEL,
    )

    log.info("[settings] 配置加载完成: %s, %d models", path, len(MODEL_LIST))
    return _cfg


def _expand_model_list(providers: list[dict]) -> list[dict]:
    """把 provider 维度配置展开成 LiteLLM 的 ``model_list``.

    展开规则:
    - **普通 provider** (有 ``keys`` 字段): ``keys × models`` 笛卡尔积
    - **SAP BTP AI Core** (``provider: sap``, 不写 keys): 一个 model 一条 entry
    - **OpenAI 兼容协议** (自定义 ``api_base``): 同普通 provider

    单条 model spec 支持:
    - ``name`` (必填): provider 模型名
    - ``model_name`` (可选): LiteLLM Router alias, 默认 = ``name``
    - ``tpm`` / ``rpm`` / ``timeout`` 等字段透传
    """
    out = []
    for p in providers:
        prefix = p["provider"]
        api_base = p.get("api_base")
        # SAP AI Core 等 OAuth/no-key provider 不写 keys -> 用 [None] 占位走单次循环
        keys = p.get("keys") or [None]
        for env_var in keys:
            for m in p.get("models", []):
                spec = {"name": m} if isinstance(m, str) else dict(m)
                name = spec.pop("name")
                model_name = spec.pop("model_name", name)
                params: dict[str, Any] = {"model": f"{prefix}/{name}", **spec}
                if env_var is not None:
                    params["api_key"] = f"os.environ/{env_var}"
                if api_base:
                    params["api_base"] = api_base
                out.append({"model_name": model_name, "litellm_params": params})
    return out


# 自动加载配置 (如果配置文件存在)
if _config_path.exists():
    try:
        load_config()
    except Exception as e:
        log.warning("[settings] 自动加载配置失败 (将在首次使用时重试): %s", e)
