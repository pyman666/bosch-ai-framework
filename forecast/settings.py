import os
import json
import yaml
import logging
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import litellm  # noqa: E402 — load_dotenv() must run before litellm import
litellm.drop_params = True

log = logging.getLogger(__name__)


_default_config = Path(__file__).parent / "settings.yaml"
_config_path = Path(os.environ.get("FCST_CONFIG", _default_config))
_cfg: dict[str, Any] = yaml.safe_load(_config_path.read_text(encoding="utf-8"))

DEFAULT_MODEL: str = _cfg["default_model"]
ROUTER_KWARGS: dict[str, Any] = _cfg.get("router", {})


def _expand_model_list(providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for p in providers:
        prefix = p["provider"]
        api_base = p.get("api_base")
        keys = p.get("keys") or [None]
        for env_var in keys:
            for m in p.get("models", []):
                spec = {"name": m} if isinstance(m, str) else dict(m)
                name = spec.pop("name")
                model_name = spec.pop("model_name", name)
                params = {"model": f"{prefix}/{name}", **spec}
                if env_var is not None:
                    params["api_key"] = f"os.environ/{env_var}"
                if api_base:
                    params["api_base"] = api_base
                out.append({"model_name": model_name, "litellm_params": params})
    return out


MODEL_LIST: list[dict[str, Any]] = _expand_model_list(_cfg["providers"])


_VALID_AUTH_MODES = ("basic", "xsuaa", "both")
_auth_mode: str = os.environ.get("AUTH_MODE", "basic").strip().lower()
if _auth_mode not in _VALID_AUTH_MODES:
    raise RuntimeError(
        f"环境变量 AUTH_MODE='{_auth_mode}' 非法, 只接受: {_VALID_AUTH_MODES}."
    )


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


def _load_xsuaa_credentials() -> dict[str, Any] | None:
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


_xsuaa_credentials: dict[str, Any] | None = None
_xsuaa_required_scope: str | None = os.environ.get("XSUAA_REQUIRED_SCOPE") or None
if _auth_mode in ("xsuaa", "both"):
    _xsuaa_credentials = _load_xsuaa_credentials()
    if _xsuaa_credentials is None:
        raise RuntimeError(
            f"AUTH_MODE='{_auth_mode}' 但 XSUAA 凭据 load 失败. 三选一:\n"
            "  - BTP CF: cf bind-service fcst <xsuaa-svc> + cf restage\n"
            "  - 本地: 在 .env 设 XSUAA_SERVICE_KEY='<整个 service key JSON>'\n"
            "  - 本地: 在 .env 设 XSUAA_CLIENT_ID/_CLIENT_SECRET/_URL/_UAADOMAIN 4 件套"
        )
