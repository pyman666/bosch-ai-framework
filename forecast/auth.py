"""HTTP Basic Auth + SAP BTP XSUAA 双轨鉴权入口."""
from __future__ import annotations

import base64
import binascii
import logging
from secrets import compare_digest
from typing import Any

from fastapi import HTTPException, Request, status

from .settings import (
    _auth_mode,
    _basic_auth_secret,
    _basic_auth_user,
    _xsuaa_credentials,
    _xsuaa_required_scope,
)

log = logging.getLogger(__name__)

_WWW_AUTH = {
    "basic": 'Basic realm="fcst"',
    "xsuaa": "Bearer",
    "both": 'Basic realm="fcst", Bearer',
}[_auth_mode]


def _check_basic(authorization: str) -> bool:
    if _basic_auth_user is None or _basic_auth_secret is None:
        return False
    try:
        token = authorization.split(" ", 1)[1].strip()
        decoded = base64.b64decode(token, validate=True).decode("utf-8")
    except (IndexError, binascii.Error, UnicodeDecodeError):
        return False
    user, sep, secret = decoded.partition(":")
    if not sep:
        return False
    return (
        compare_digest(user.encode("utf-8"), _basic_auth_user)
        and compare_digest(secret.encode("utf-8"), _basic_auth_secret)
    )


_xssec = None


def _xssec_module() -> Any:
    global _xssec
    if _xssec is None:
        try:
            from sap import xssec as _x
        except ImportError as e:
            raise RuntimeError(
                "AUTH_MODE 启用 xsuaa 但 sap-xssec 包没装. "
                "pip install sap-xssec (已在 requirements.txt 里)."
            ) from e
        _xssec = _x
    return _xssec


def _check_xsuaa(authorization: str) -> bool:
    if _xsuaa_credentials is None:
        log.error("[auth] AUTH_MODE 含 xsuaa 但凭据为空, 拒绝所有 Bearer 请求")
        return False
    try:
        token = authorization.split(" ", 1)[1].strip()
    except IndexError:
        return False
    if not token:
        return False
    try:
        ctx = _xssec_module().create_security_context(token, _xsuaa_credentials)
    except Exception as e:
        log.warning(f"[auth] XSUAA token 验证失败: {type(e).__name__}: {e}")
        return False
    if _xsuaa_required_scope:
        try:
            if not ctx.check_scope(_xsuaa_required_scope):
                log.warning(
                    f"[auth] XSUAA token scope check 失败: 缺 {_xsuaa_required_scope!r}"
                )
                return False
        except Exception as e:
            log.warning(f"[auth] XSUAA scope check 异常: {type(e).__name__}: {e}")
            return False
    return True


async def require_auth(request: Request) -> None:
    auth = request.headers.get("authorization") or ""
    scheme = auth.split(" ", 1)[0].lower() if auth else ""

    if _auth_mode == "basic":
        ok = scheme == "basic" and _check_basic(auth)
    elif _auth_mode == "xsuaa":
        ok = scheme == "bearer" and _check_xsuaa(auth)
    else:  # both
        if scheme == "basic":
            ok = _check_basic(auth)
        elif scheme == "bearer":
            ok = _check_xsuaa(auth)
        else:
            ok = False

    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"鉴权失败 (AUTH_MODE={_auth_mode}): 凭据缺失或无效",
            headers={"WWW-Authenticate": _WWW_AUTH},
        )
