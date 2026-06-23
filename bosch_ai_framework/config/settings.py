"""Configuration loading — YAML → LiteLLM model_list.

Usage::

    from bosch_ai_framework.config import load_config, DEFAULT_MODEL, MODEL_LIST

    load_config("settings.yaml")

Environment variables:
    ``AUTH_MODE``: auth mode (basic/xsuaa/both)
    ``BAUTH_KEY`` / ``BAUTH_SECRET``: Basic Auth credentials
    ``XSUAA_SERVICE_KEY``: XSUAA service key JSON
    ``XSUAA_CLIENT_ID`` / ``XSUAA_CLIENT_SECRET`` / ``XSUAA_URL`` / ``XSUAA_UAADOMAIN``
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
# Auth config
# ---------------------------------------------------------------------------

_VALID_AUTH_MODES = ("basic", "xsuaa", "both")

_auth_mode: str = os.environ.get("AUTH_MODE", "basic").strip().lower()
if _auth_mode not in _VALID_AUTH_MODES:
    raise RuntimeError(
        f"Invalid AUTH_MODE='{_auth_mode}', expected one of: {_VALID_AUTH_MODES}."
    )

# -- basic credentials --

_bauth_key = os.environ.get("BAUTH_KEY")
_bauth_secret = os.environ.get("BAUTH_SECRET")
if _auth_mode in ("basic", "both"):
    if not _bauth_key or not _bauth_secret:
        log.warning(
            "AUTH_MODE='%s' but BAUTH_KEY/BAUTH_SECRET not set — Basic Auth disabled. "
            "Set them in .env, or switch to AUTH_MODE=xsuaa.",
            _auth_mode,
        )
_basic_auth_user: bytes | None = _bauth_key.encode("utf-8") if _bauth_key else None
_basic_auth_secret: bytes | None = _bauth_secret.encode("utf-8") if _bauth_secret else None


# -- XSUAA credentials --

def _load_xsuaa_credentials() -> dict | None:
    """Resolve XSUAA service credentials by priority chain.

    Priority (first hit wins):
        1. ``VCAP_SERVICES`` — BTP CF service binding
        2. ``XSUAA_SERVICE_KEY`` env — full service key JSON
        3. ``XSUAA_CLIENT_ID`` + ``_CLIENT_SECRET`` + ``_URL`` + ``_UAADOMAIN``
    """
    vcap = os.environ.get("VCAP_SERVICES")
    if vcap:
        try:
            data = json.loads(vcap)
            for inst in data.get("xsuaa", []):
                creds = inst.get("credentials")
                if creds:
                    log.info("[auth] XSUAA credentials: VCAP_SERVICES.xsuaa[0]")
                    return creds
        except Exception as e:
            log.warning(f"[auth] VCAP_SERVICES parse failed (skipping): {e}")
    raw = os.environ.get("XSUAA_SERVICE_KEY")
    if raw:
        try:
            log.info("[auth] XSUAA credentials: XSUAA_SERVICE_KEY env")
            return json.loads(raw)
        except Exception as e:
            log.warning(f"[auth] XSUAA_SERVICE_KEY JSON parse failed (skipping): {e}")
    if all(os.environ.get(k) for k in (
        "XSUAA_CLIENT_ID", "XSUAA_CLIENT_SECRET", "XSUAA_URL", "XSUAA_UAADOMAIN",
    )):
        log.info("[auth] XSUAA credentials: 4-part env (XSUAA_CLIENT_ID/...)")
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
        log.warning(
            "AUTH_MODE='%s' but XSUAA credentials could not be loaded. "
            "XSUAA auth will be unavailable. Options:\n"
            "  - BTP CF: cf bind-service <app> <xsuaa-svc> + cf restage\n"
            "  - Local: set XSUAA_SERVICE_KEY='<service key JSON>' in .env\n"
            "  - Local: set XSUAA_CLIENT_ID/_CLIENT_SECRET/_URL/_UAADOMAIN in .env",
            _auth_mode,
        )


# ---------------------------------------------------------------------------
# Model config loading
# ---------------------------------------------------------------------------

# Default config path is relative to this file
_config_path = Path(__file__).parent / "settings.yaml"

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
    """Load / reload model configuration.

    Args:
        config_path: Path to YAML config file
        default_model: Override config's default_model
        router_kwargs: Override config's router params

    Returns:
        Parsed config dict
    """
    global _cfg, DEFAULT_MODEL, ROUTER_KWARGS, MODEL_LIST

    path = Path(config_path) if config_path else _config_path
    if not path.exists():
        raise FileNotFoundError(f"Model config file not found: {path}")

    _cfg = yaml.safe_load(path.read_text(encoding="utf-8"))

    DEFAULT_MODEL = default_model if default_model is not None else _cfg["default_model"]
    ROUTER_KWARGS = router_kwargs if router_kwargs is not None else _cfg.get("router", {})
    MODEL_LIST = _expand_model_list(_cfg["providers"])

    # Wire into llm module
    from bosch_ai_framework.llm.router import _configure as _configure_llm
    _configure_llm(
        model_list=MODEL_LIST,
        router_kwargs=ROUTER_KWARGS,
        default_model=DEFAULT_MODEL,
    )

    log.info("[config] Loaded: %s, %d models", path, len(MODEL_LIST))
    return _cfg


def _expand_model_list(providers: list[dict]) -> list[dict]:
    """Expand provider-level config into LiteLLM ``model_list``.

    Rules:
    - **regular provider** (has ``keys``): Cartesian product ``keys × models``
    - **SAP BTP AI Core** (``provider: sap``, no keys): one entry per model
    - **custom api_base**: same as regular provider

    Single model spec supports:
    - ``name`` (required): provider model name
    - ``model_name`` (optional): LiteLLM Router alias, defaults to ``name``
    - ``tpm`` / ``rpm`` / ``timeout`` pass through
    """
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
                params: dict[str, Any] = {"model": f"{prefix}/{name}", **spec}
                if env_var is not None:
                    params["api_key"] = f"os.environ/{env_var}"
                if api_base:
                    params["api_base"] = api_base
                out.append({"model_name": model_name, "litellm_params": params})
    return out
