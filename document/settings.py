import os
from pathlib import Path
import yaml
from dotenv import load_dotenv

load_dotenv()


# 所有业务 router (PDF / Excel) 共用同一对 basic auth 凭证, 都从 .env 读:
#   - ``BAUTH_KEY``    -> 用户名 (basic auth username)
#   - ``BAUTH_SECRET`` -> 密码 (basic auth password)
# 之前 username 在 ``apdfi/auth.py`` 里 hardcode 成 ``apdfi-api``, secret 走
# ``APDFI_SECRET`` / ``APDFI_PDF_SECRET`` 环境变量, 现在统一搬到 ``BAUTH_*``,
# 部署时改 .env 一处即可换 user/secret, 不用碰代码.
_bauth_key = os.environ.get("BAUTH_KEY")
_bauth_secret = os.environ.get("BAUTH_SECRET")
if not _bauth_key or not _bauth_secret:
    raise RuntimeError(
        "环境变量 BAUTH_KEY 和 BAUTH_SECRET 必须都设置 (检查 .env)."
    )
_basic_auth_user: bytes = _bauth_key.encode("utf-8")
_basic_auth_secret: bytes = _bauth_secret.encode("utf-8")


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
