import os
import json
import yaml
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 鉴权模式选择. 由 ``AUTH_MODE`` env 切换, ``apdfi.auth.require_auth`` 据此分发:
#
#   - ``basic`` (default): HTTP Basic Auth, 仅认 ``BAUTH_KEY/BAUTH_SECRET``.
#                          适用本地 dev 与 Java m2m 老路径; 没设 BAUTH_* 报错.
#   - ``xsuaa``: 仅 SAP BTP XSUAA OAuth2 JWT (``Authorization: Bearer ...``).
#                凭据从 ``VCAP_SERVICES.xsuaa[0].credentials`` (BTP CF 上 cf
#                bind-service 后自动注入), 或 ``XSUAA_SERVICE_KEY`` env (整个
#                service key JSON), 或 4 件套 env (``XSUAA_CLIENT_ID`` /
#                ``XSUAA_CLIENT_SECRET`` / ``XSUAA_URL`` / ``XSUAA_UAADOMAIN``).
#                可选 ``XSUAA_REQUIRED_SCOPE`` 加 scope check.
#   - ``both``: 上面两套同时开, 按请求 ``Authorization`` header 类型自动 dispatch
#               (``Basic ...`` -> basic, ``Bearer ...`` -> xsuaa). 适合 Java
#               后端 m2m + 前端 SSO 同一服务两种调用方混用的场景.
# ---------------------------------------------------------------------------
_VALID_AUTH_MODES = ("basic", "xsuaa", "both")
_auth_mode: str = os.environ.get("AUTH_MODE", "basic").strip().lower()
if _auth_mode not in _VALID_AUTH_MODES:
    raise RuntimeError(
        f"环境变量 AUTH_MODE='{_auth_mode}' 非法, 只接受: {_VALID_AUTH_MODES}."
    )

# --- basic 凭据 ------------------------------------------------------------
# basic / both 模式必须设; xsuaa-only 模式可不设, 不会启用 basic 校验路径.
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


# --- XSUAA 凭据 ------------------------------------------------------------
def _load_xsuaa_credentials() -> dict | None:
    """按优先级解析 XSUAA service credentials.

    优先级 (first hit wins):
        1. ``VCAP_SERVICES`` 里 label=='xsuaa' 的第一个 service binding (BTP CF
           ``cf bind-service apdfi <xsuaa-svc>`` 后自动注入)
        2. ``XSUAA_SERVICE_KEY`` env 里整段 service key JSON (本地 dev 推荐)
        3. ``XSUAA_*`` 4 件套 env

    返回的 dict 至少要有 ``clientid`` / ``clientsecret`` / ``url`` /
    ``uaadomain``, 这是 ``sap-xssec.create_security_context`` 必需字段.
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
            "  - BTP CF: cf bind-service apdfi <xsuaa-svc> + cf restage\n"
            "  - 本地: 在 .env 设 XSUAA_SERVICE_KEY='<整个 service key JSON>'\n"
            "  - 本地: 在 .env 设 XSUAA_CLIENT_ID/_CLIENT_SECRET/_URL/_UAADOMAIN 4 件套"
        )


# ---------------------------------------------------------------------------
# 避免依赖工作目录, 在 gunicorn / Docker 等场景下更稳.
# ---------------------------------------------------------------------------
_default_config = Path(__file__).parent / "settings.yaml"
_config_path = Path(os.environ.get("ABI_MODEL_CONFIG", _default_config))
_cfg: dict = yaml.safe_load(_config_path.read_text(encoding="utf-8"))

DEFAULT_MODEL: str = _cfg["default_model"]
ROUTER_KWARGS: dict = _cfg.get("router", {})


def _expand_model_list(providers: list[dict]) -> list[dict]:
    """把 provider 维度配置展开成 LiteLLM 的 ``model_list``.

    输出每条 entry 形如:

    .. code-block:: python

        {
            "model_name": <用户调用时的 alias, 默认 = name>,
            "litellm_params": {
                "model": "<provider>/<name>",       # LiteLLM provider routing 用
                "api_key": "os.environ/<env_var>",  # 仅"普通" provider 有
                "api_base": ...,                    # provider.api_base 透传
                ...其他 spec 字段透传 (tpm/rpm/timeout/...)
            }
        }

    展开规则:

    - **普通 provider** (有 ``keys`` 字段, e.g. gemini / openai / anthropic):
      ``keys × models`` 笛卡尔积, 每条 entry 带 ``api_key=os.environ/<key_var>``.
      LiteLLM Router 按 ``routing_strategy`` 在多个 key 之间负载均衡.
    - **SAP BTP AI Core** (``provider: sap``, **不写 keys**): SAP 用 OAuth service
      key 鉴权, 凭据从 env (``AICORE_SERVICE_KEY`` 或 ``AICORE_*`` 4 件套) 或 BTP
      上的 ``VCAP_SERVICES`` redirect 自动解析, 不需要在 entry 里塞 ``api_key``.
      不走笛卡尔积, 一个 model 出一条 entry.
    - **OpenAI 兼容协议** (``provider: openai`` + 自定义 ``api_base``, e.g. dashscope):
      跟普通 provider 同, ``keys × models`` 展开. ``api_base`` 透传.

    单条 model spec 还支持:

    - ``name`` (必填): provider 那边的模型名 (不带前缀). 拼成 ``model``.
    - ``model_name`` (可选): LiteLLM Router 暴露给业务的 alias. 默认 = ``name``.
      显式重命名场景: 同一个 model name 既走自家 OpenAI 又走 BTP AI Core, 两条
      entry alias 区分 (e.g. ``gpt-4o`` vs ``sap-gpt-4o``).
    - ``tpm`` / ``rpm`` / ``timeout`` 等其他字段透传给 LiteLLM Router.
    """
    out = []
    for p in providers:
        prefix = p["provider"]
        api_base = p.get("api_base")
        # SAP AI Core 等 OAuth/no-key provider 不写 keys -> 用 [None] 占位走单次循环.
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


MODEL_LIST: list[dict] = _expand_model_list(_cfg["providers"])
