import os
from pathlib import Path
import yaml
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# 鉴权配置 (对齐 infra/settings.py 的模式, 非鉴权场景设 AUTH_MODE=none 跳过)
# ---------------------------------------------------------------------------

_VALID_AUTH_MODES = ("none", "basic", "xsuaa", "both")

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
            "(检查 .env). 想完全禁掉鉴权, 设 AUTH_MODE=none."
        )
_basic_auth_user: bytes | None = _bauth_key.encode("utf-8") if _bauth_key else None
_basic_auth_secret: bytes | None = _bauth_secret.encode("utf-8") if _bauth_secret else None


# --- XSUAA 凭据 ---------------


# ---------------------------------------------------------------------------
# 避免依赖工作目录, 在 gunicorn / Docker 等场景下更稳.
# ---------------------------------------------------------------------------
_default_config = Path(__file__).parent / "settings.yaml"
_config_path = Path(os.environ.get("APDFI_MODEL_CONFIG", _default_config))
_cfg: dict = yaml.safe_load(_config_path.read_text(encoding="utf-8"))

DEFAULT_MODEL: str = _cfg["default_model"]
ROUTER_KWARGS: dict = _cfg.get("router", {})


def _expand_model_list(providers: list[dict]) -> list[dict]:
    """把 provider 维度配置展开成 LiteLLM 的 model_list (key × model 笛卡尔积)."""
    out = []
    for p in providers:
        prefix = p["provider"]
        api_base = p.get("api_base")
        for env_var in p.get("keys", []):
            for m in p.get("models", []):
                spec = {"name": m} if isinstance(m, str) else dict(m)
                name = spec.pop("name")
                params = {
                    "model": f"{prefix}/{name}",
                    "api_key": f"os.environ/{env_var}",
                    **spec,
                }
                if api_base:
                    params["api_base"] = api_base
                out.append({"model_name": name, "litellm_params": params})
    return out


MODEL_LIST: list[dict] = _expand_model_list(_cfg["providers"])
