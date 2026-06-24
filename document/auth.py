"""HTTP Basic Auth 单一入口.

PDF 跟 Excel 端点共用同一 username + secret, 都从 ``.env`` 读 (``BAUTH_KEY`` /
``BAUTH_SECRET``, 见 ``apdfi.settings``). 所有业务 router 共享 ``require_auth``
这一个 dependency, 在 ``apdfi.server`` 的
``app.include_router(..., dependencies=[Depends(require_auth)])`` 一处注入, 域
router (``apdfi.pdf.routes`` / ``apdfi.excel.routes``) 自己不挂 auth, 也不必
``import`` 本模块. 加新业务域时只要在 ``server.py`` 多挂一行 ``include_router``
就自动带上 auth, 漏挂一眼能看出来.

静态 ``/mock`` 目录有意**不**走这个 dep -- 浏览器打开 mock 不弹 basic auth 框,
mock 页面内部 fetch 业务端点时自己带 Authorization header.
"""
from secrets import compare_digest
from typing import Awaitable, Callable
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from .settings import _basic_auth_user, _basic_auth_secret


_basic = HTTPBasic()


def basic_auth(user: bytes, secret: bytes) -> Callable[..., Awaitable[None]]:
    async def _check(credentials: HTTPBasicCredentials = Depends(_basic)) -> None:
        usr = credentials.username.encode("utf-8")
        pwd = credentials.password.encode("utf-8")
        if not (compare_digest(usr, user) and compare_digest(pwd, secret)):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="鉴权失败: 用户名或密码不正确",
                headers={"WWW-Authenticate": "Basic"},
            )
    return _check


require_auth = basic_auth(_basic_auth_user, _basic_auth_secret)
