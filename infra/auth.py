"""HTTP Basic Auth + SAP BTP XSUAA 双轨鉴权入口.

FastAPI dependency 用法::

    from fastapi import Depends, FastAPI
    from infra.auth import require_auth

    app = FastAPI()
    app.include_router(my_router, dependencies=[Depends(require_auth)])

鉴权模式由 ``AUTH_MODE`` env 切换:
- ``basic`` (default): HTTP Basic, 认 ``BAUTH_KEY``/``BAUTH_SECRET``
- ``xsuaa``: 仅 SAP BTP XSUAA OAuth2 JWT (``Authorization: Bearer ...``)
- ``both``: 按 ``Authorization`` header scheme 自动 dispatch

XSUAA 凭据来源见 ``infra.settings`` 注释.
"""

from __future__ import annotations

import base64
import binascii
import logging
from secrets import compare_digest

from fastapi import HTTPException, Request, status

from infra.settings import (
    _auth_mode,
    _basic_auth_secret,
    _basic_auth_user,
    _xsuaa_credentials,
    _xsuaa_required_scope,
)

log = logging.getLogger(__name__)

# 401 时回给客户端的 ``WWW-Authenticate`` 提示, 跟 mode 对齐.
_WWW_AUTH = {
    "none": "",
    "basic": 'Basic realm="api"',
    "xsuaa": "Bearer",
    "both": 'Basic realm="api", Bearer',
}[_auth_mode]


def _check_basic(authorization: str) -> bool:
    """``Authorization: Basic <base64>`` -> 比 BAUTH_KEY/BAUTH_SECRET."""
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


# sap-xssec 模块惰性 import, basic-only 部署不必装这个包.
_xssec = None


def _xssec_module():
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
    """``Authorization: Bearer <jwt>`` -> 调 sap-xssec 验."""
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
    """FastAPI dependency. 401 当且仅当所有适用的校验都失败."""
    auth = request.headers.get("authorization") or ""
    scheme = auth.split(" ", 1)[0].lower() if auth else ""

    if _auth_mode == "none":
        return  # 跳过鉴权
    elif _auth_mode == "basic":
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
