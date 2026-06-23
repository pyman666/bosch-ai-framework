"""Authentication — Basic Auth + SAP BTP XSUAA dual-mode.

FastAPI dependency usage::

    from fastapi import Depends
    from bosch_ai_framework.auth import require_auth

    app.include_router(my_router, dependencies=[Depends(require_auth)])

Auth mode controlled by ``AUTH_MODE`` env var:
- ``basic`` (default): HTTP Basic, checks ``BAUTH_KEY``/``BAUTH_SECRET``
- ``xsuaa``: SAP BTP XSUAA OAuth2 JWT (``Authorization: Bearer ...``)
- ``both``: dispatch by ``Authorization`` header scheme
"""

from __future__ import annotations

import base64
import binascii
import logging
from secrets import compare_digest

from fastapi import HTTPException, Request, status

from bosch_ai_framework.config.settings import (
    _auth_mode,
    _basic_auth_secret,
    _basic_auth_user,
    _xsuaa_credentials,
    _xsuaa_required_scope,
)

log = logging.getLogger(__name__)

_WWW_AUTH = {
    "basic": 'Basic realm="api"',
    "xsuaa": "Bearer",
    "both": 'Basic realm="api", Bearer',
}[_auth_mode]


def _check_basic(authorization: str) -> bool:
    """Validate ``Authorization: Basic <base64>`` against BAUTH_KEY/BAUTH_SECRET."""
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


# Lazy import sap-xssec (basic-only deployments don't need it)
_xssec: object | None = None


def _xssec_module():
    global _xssec
    if _xssec is None:
        try:
            from sap import xssec as _x
        except ImportError as e:
            raise RuntimeError(
                "AUTH_MODE requires xsuaa but sap-xssec is not installed. "
                "Install with: pip install bosch-ai-framework[xsuaa]"
            ) from e
        _xssec = _x
    return _xssec


def _check_xsuaa(authorization: str) -> bool:
    """Validate ``Authorization: Bearer <jwt>`` via sap-xssec."""
    if _xsuaa_credentials is None:
        log.error("[auth] AUTH_MODE requires xsuaa but credentials are empty")
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
        log.warning(f"[auth] XSUAA token validation failed: {type(e).__name__}: {e}")
        return False
    if _xsuaa_required_scope:
        try:
            if not ctx.check_scope(_xsuaa_required_scope):
                log.warning(
                    f"[auth] XSUAA token scope check failed: missing {_xsuaa_required_scope!r}"
                )
                return False
        except Exception as e:
            log.warning(f"[auth] XSUAA scope check error: {type(e).__name__}: {e}")
            return False
    return True


async def require_auth(request: Request) -> None:
    """FastAPI dependency. Returns 401 if and only if all applicable checks fail."""
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
            detail=f"Authentication failed (AUTH_MODE={_auth_mode})",
            headers={"WWW-Authenticate": _WWW_AUTH},
        )


__all__ = ["require_auth"]
