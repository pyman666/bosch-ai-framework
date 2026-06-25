"""HTTP 鉴权 — XSUAA JWT (BTP 生产) + Basic Auth (本地 dev / 无 XSUAA fallback).

模块加载时根据 ``VCAP_SERVICES`` 里 ``xsuaa`` 绑定是否存在自动二选一:

- **有 XSUAA binding** → :class:`XSUAAJWTValidator` 验 ``Authorization: Bearer <jwt>``,
  签名走 XSUAA `/token_keys` 拿 JWKS (5 分钟缓存), aud / exp / nbf 都校验, 可
  选 scope 校验 (scope 字符串由调用方在 :data:`REQUIRED_SCOPES` 里配, 默认空
  = 只做"有效 token 即可", 不限定 scope).
- **无 XSUAA binding** (本地 dev / 未绑服务) → 回退到 ``HTTPBasic`` 用
  ``BOT_KEY`` / ``BOT_SECRET``. 保留旧 dev 路径无缝.

业务路由侧 ``Depends(_bot_auth)`` 完全无感, 不知道也不关心当前走的是 JWT 还
是 Basic — 拿到的 dependency 返回值统一是 ``None`` (Basic) 或 claims dict
(JWT), 当前路由不用读 claims 所以两种返回都不影响.

## XSUAA token 校验点 (跟 sap-xssec 等 SDK 行为对齐)

1. **签名** — JWT header 拿 ``kid``, 在 JWKS 里找对应 RSA 公钥, ``RS256`` 验签.
   JWKS 5 分钟缓存; ``kid`` 找不到时强刷一次再判 (应对密钥轮换).
2. **过期 / 起效时间** — ``exp`` / ``nbf`` 必须存在 + 60s leeway 容时.
3. **Audience** — ``aud`` claim 必须 contains ``clientid`` (XSUAA 的常规约定;
   有些场景 aud 是 ``sb-<clientid>`` 这种带前缀的形式, 这里做 substring 匹配
   兼容).
4. **Issuer (软校验)** — 不强校验, 因为 XSUAA token 的 ``iss`` 形式因 tenant
   / subdomain 而异, 严校验需要传 expected issuer 列表. 我们靠 audience +
   签名两层守住身份, iss 留作日志记录字段.
5. **Scope (可选)** — 通过 :data:`REQUIRED_SCOPES` 列表配置 (默认空). XSUAA
   的 scope 字符串带 xsappname 前缀 (e.g. ``bapee-app!t1234.bot.ask``),
   :meth:`XSUAAJWTValidator._has_scope` 会做后缀匹配, 用户配 ``"bot.ask"``
   就能命中.

## 怎么从 dev 切到 prod (BTP)

1. `cf create-service xsuaa application bapee-xsuaa -c xs-security.json` 创实例
2. `cf bind-service bapee bapee-xsuaa` 绑到 app
3. `cf restage bapee` 让 VCAP_SERVICES 重新注入
4. 本模块下次启动时自动检测到 binding, 切到 JWT 模式; 旧 Basic Auth 凭证
   不再生效, 客户端必须改成传 ``Authorization: Bearer <jwt>``.

## 安全提醒

- 别让"无 XSUAA = 回 Basic Auth"这条 fallback 路径出现在 BTP 生产环境.
  生产部署强制要求 ``xsuaa`` binding 存在, 否则启动期 :func:`_build_auth_dependency`
  能识别出 ``VCAP_APPLICATION`` 在但 ``xsuaa`` 不在, 这种"BTP 上跑但没绑
  XSUAA" 的怪状态会记一行 warning, 别忽视.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from secrets import compare_digest
from typing import Any, Awaitable, Callable

import httpx
import jwt
from fastapi import HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from infra.btp import find_service_binding, is_running_on_btp
from ..settings import get_basic_auth_credentials


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 配置: scope 校验列表 — 用户按 xs-security.json 里定义的 scope 短名填
# ---------------------------------------------------------------------------

#: 路由生效的 scope 短名列表 (xs-security.json 里 ``"name": "$XSAPPNAME.<这里>"`` 那段).
#: 留空 = 任何 XSUAA 颁发的有效 token 都通过 (默认行为); 想严格按 scope 拦截,
#: 填类似 ``("bot.ask", "bot.chat")`` 就让客户端 token 必须带这些 scope.
#:
#: 当前留空, 部署前在这里 (或通过 monkey patch 测试环境) 填上具体 scope.
REQUIRED_SCOPES: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Basic Auth (本地 dev / fallback)
# ---------------------------------------------------------------------------

_basic = HTTPBasic()


def basic_auth(
    user: bytes,
    secret: bytes,
) -> Callable[[Request], Awaitable[None]]:
    """构造一个 FastAPI dependency, 直接吃 :class:`Request` 验 Basic Auth.

    跟 :func:`jwt_auth` 签名对齐 (都是 ``async (Request) -> ...``), 这样
    :func:`_bot_auth` 的 dispatcher 不用关心当前是哪条路径 — 拿到 Request
    透传即可. 历史上这里通过 ``Depends(_basic)`` 走 FastAPI 二级依赖,
    重构后改成内部直接 ``await _basic(request)``, 减一层 dependency 注入,
    顺手让 dispatcher pattern 能成立.
    """

    async def _check(request: Request) -> None:
        # ``HTTPBasic`` 是 async callable; 缺 Authorization 头会自己抛 401,
        # 不用我们再 check None — 一致行为, 出错信息也跟 FastAPI 内置一致.
        credentials: HTTPBasicCredentials = await _basic(request)  # type: ignore[assignment]
        usr = credentials.username.encode("utf-8")
        pwd = credentials.password.encode("utf-8")
        if not (compare_digest(usr, user) and compare_digest(pwd, secret)):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Permission Denied",
                headers={"WWW-Authenticate": "Basic"},
            )

    return _check


# ---------------------------------------------------------------------------
# XSUAA JWT 验签器
# ---------------------------------------------------------------------------

class XSUAAJWTValidator:
    """从 BTP XSUAA service binding 创建的 JWT 验签器.

    生命周期: app 启动时 :func:`_build_auth_dependency` 构造一次, 全进程复用.
    内部维护 JWKS 缓存 + asyncio 锁防并发 fetch.
    """

    _JWKS_TTL_SEC: float = 300.0  # JWKS 缓存 5 分钟; XSUAA 密钥轮换是分钟到小时级.
    _CLOCK_LEEWAY_SEC: int = 60   # exp / nbf 容时, 跨服务器时钟漂移容忍.
    _JWKS_FETCH_TIMEOUT_SEC: float = 10.0

    def __init__(
        self,
        *,
        clientid: str,
        uaa_url: str,
        xsappname: str,
    ) -> None:
        self._clientid = clientid
        self._uaa_url = uaa_url.rstrip("/")
        self._xsappname = xsappname
        self._jwks_url = f"{self._uaa_url}/token_keys"
        self._jwks_cache: dict[str, dict[str, Any]] = {}
        self._jwks_fetched_at: float = 0.0
        self._jwks_lock = asyncio.Lock()
        logger.info(
            "xsuaa validator ready",
            extra={
                "uaa_url": self._uaa_url,
                "xsappname": self._xsappname,
                "jwks_url": self._jwks_url,
            },
        )

    async def _fetch_jwks(self) -> dict[str, dict[str, Any]]:
        """同步从 XSUAA `/token_keys` 拉一次 JWKS, 转成 ``{kid: jwk}`` 字典.

        失败时抛 :class:`httpx.HTTPError`, 调用方决定怎么处理 (我们用 401
        透传给客户端). 不静默吞错 — JWKS 拿不到 = 任何 token 都验不了, 静
        默 fallback 等于绕过鉴权, 危险.
        """
        async with httpx.AsyncClient(timeout=self._JWKS_FETCH_TIMEOUT_SEC) as client:
            resp = await client.get(self._jwks_url)
        resp.raise_for_status()
        data = resp.json()
        keys = data.get("keys") or []
        return {k["kid"]: k for k in keys if isinstance(k, dict) and k.get("kid")}

    async def _get_jwks(self, *, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
        """带缓存 + 锁的 JWKS 读取. ``force_refresh=True`` 用于"kid 找不到"的兜底重试."""
        now = time.monotonic()
        if (
            not force_refresh
            and self._jwks_cache
            and now - self._jwks_fetched_at < self._JWKS_TTL_SEC
        ):
            return self._jwks_cache

        # 加锁 + 双检查锁: 高并发下避免 N 个请求同时打 XSUAA `/token_keys`.
        async with self._jwks_lock:
            now = time.monotonic()
            if (
                not force_refresh
                and self._jwks_cache
                and now - self._jwks_fetched_at < self._JWKS_TTL_SEC
            ):
                return self._jwks_cache
            self._jwks_cache = await self._fetch_jwks()
            self._jwks_fetched_at = now
            return self._jwks_cache

    @staticmethod
    def _check_audience(claims: dict[str, Any], expected_clientid: str) -> bool:
        """``aud`` claim 包含 expected_clientid (或带 ``sb-`` 前缀的变体).

        XSUAA 的 aud 形态不一: 有时 ``"aud": "<clientid>"``, 有时
        ``"aud": ["<clientid>", "sb-<clientid>"]``, 业务 OAuth2 client 调用
        可能再加自家 client id. 这里用 "any of expected 子串/精确匹配在 aud
        里" 的宽松规则, 跟 sap-xssec 行为对齐.
        """
        aud = claims.get("aud")
        if isinstance(aud, str):
            aud_list = [aud]
        elif isinstance(aud, list):
            aud_list = [a for a in aud if isinstance(a, str)]
        else:
            return False
        return any(
            a == expected_clientid or a == f"sb-{expected_clientid}" for a in aud_list
        )

    def _has_required_scopes(
        self, claims: dict[str, Any], required: tuple[str, ...]
    ) -> tuple[bool, list[str]]:
        """``scope`` claim contains required scope (按短名做后缀匹配).

        XSUAA scope 字符串带 xsappname 前缀, 实际形如
        ``"bapee-app!t1234.bot.ask"``. 用户在 :data:`REQUIRED_SCOPES` 配的
        是短名 ``"bot.ask"``, 我们看 token scope 是否以 ``.bot.ask`` 结尾
        或者精确等于. 这样:

        - 用户只需写跟 xs-security.json 一致的短名, 不用关心 xsappname / tenant
          的前缀拼装;
        - 不同环境 (canary / prod) tenant ID 不同也能复用同一份代码.
        """
        if not required:
            return True, []
        token_scopes_raw = claims.get("scope") or claims.get("scopes") or []
        if isinstance(token_scopes_raw, str):
            token_scopes_raw = token_scopes_raw.split()
        token_scopes: list[str] = [s for s in token_scopes_raw if isinstance(s, str)]
        missing: list[str] = []
        for req in required:
            ok = any(
                ts == req or ts.endswith(f".{req}") for ts in token_scopes
            )
            if not ok:
                missing.append(req)
        return (not missing), missing

    async def verify(
        self, token: str, *, required_scopes: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        """完整校验流程, 返回 token claims dict.

        抛 :class:`HTTPException`: 401 = token 无效 / 缺失关键字段; 403 = scope
        不够. 业务侧无需 try/except, FastAPI 会自动给客户端返对应状态码.
        """
        # 1. 解 header 拿 kid (不验签, 只读 header)
        try:
            unverified_header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise HTTPException(
                status_code=401, detail=f"invalid token header: {exc}"
            ) from exc
        kid = unverified_header.get("kid")
        if not kid:
            raise HTTPException(status_code=401, detail="token missing kid in header")

        # 2. 拿 JWK; 命中失败强刷一次再判
        jwks = await self._get_jwks()
        jwk_dict = jwks.get(kid)
        if jwk_dict is None:
            try:
                jwks = await self._get_jwks(force_refresh=True)
            except httpx.HTTPError as exc:
                logger.warning("jwks refresh failed", extra={"err": str(exc)})
                raise HTTPException(
                    status_code=401, detail="unable to fetch signing keys"
                ) from exc
            jwk_dict = jwks.get(kid)
            if jwk_dict is None:
                raise HTTPException(
                    status_code=401, detail=f"unknown signing key kid={kid}"
                )

        # 3. JWK → 公钥对象; PyJWT 的 RSAAlgorithm.from_jwk 接受 JSON 字符串
        try:
            public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk_dict))
        except Exception as exc:
            logger.exception("failed to parse JWK", extra={"kid": kid})
            raise HTTPException(
                status_code=401, detail="malformed signing key"
            ) from exc

        # 4. 验签 + 标准 claims (exp / nbf / aud 由 PyJWT 处理); 我们手动 aud 校验
        #    因为 PyJWT 的 audience 校验对 XSUAA 的多 aud 形态不太友好.
        try:
            claims = jwt.decode(
                token,
                key=public_key,
                algorithms=["RS256"],
                audience=None,  # 跳过库内 aud 校验, 我们自己来
                leeway=self._CLOCK_LEEWAY_SEC,
                options={"require": ["exp", "iat"], "verify_aud": False},
            )
        except jwt.ExpiredSignatureError as exc:
            raise HTTPException(status_code=401, detail="token expired") from exc
        except jwt.InvalidTokenError as exc:
            raise HTTPException(
                status_code=401, detail=f"invalid token: {exc}"
            ) from exc

        # 5. Audience 校验 (手动)
        if not self._check_audience(claims, self._clientid):
            raise HTTPException(
                status_code=401,
                detail=f"token audience mismatch (expected clientid={self._clientid})",
            )

        # 6. Scope 校验 (可选)
        ok, missing = self._has_required_scopes(claims, required_scopes)
        if not ok:
            raise HTTPException(
                status_code=403,
                detail=f"missing required scopes: {missing}",
            )

        return claims


def jwt_auth(
    validator: XSUAAJWTValidator,
    required_scopes: tuple[str, ...] = (),
) -> Callable[[Request], Awaitable[dict[str, Any]]]:
    """构造一个 FastAPI dependency, 验 ``Authorization: Bearer <jwt>``.

    路由用 ``Depends(_bot_auth)``, 拿到的是 token claims dict (可读 user_id /
    scope / tenant 等). 当前业务路由不用 claims, 但保留返回值让将来扩展 (e.g.
    按 user_id 限流 / 多租户隔离) 不用改 signature.
    """

    async def _check(request: Request) -> dict[str, Any]:
        auth_header = request.headers.get("Authorization") or ""
        if not auth_header.lower().startswith("bearer "):
            raise HTTPException(
                status_code=401,
                detail="missing or invalid Authorization header (expected 'Bearer <jwt>')",
                headers={"WWW-Authenticate": 'Bearer realm="bapee"'},
            )
        token = auth_header[7:].strip()
        if not token:
            raise HTTPException(
                status_code=401,
                detail="empty bearer token",
                headers={"WWW-Authenticate": 'Bearer realm="bapee"'},
            )
        return await validator.verify(token, required_scopes=required_scopes)

    return _check


# ---------------------------------------------------------------------------
# 自动选择 auth strategy: XSUAA 绑了用 JWT, 没绑回 Basic
# ---------------------------------------------------------------------------

#: 走 XSUAA JWT 路径时, 这里持有 validator 实例 (供 :func:`install` warmup 用);
#: Basic Auth 路径下保持 ``None``. 业务代码不要直接读, 走 :func:`_bot_auth`.
_active_validator: "XSUAAJWTValidator | None" = None

#: 真正的 auth callable, 由 :func:`install` (lifespan 阶段) 设置. Module
#: import 期保持 ``None`` — :func:`_bot_auth` dispatcher 看到 ``None`` 返
#: 503, 既能让 ``Depends(_bot_auth)`` 在 routes 模块顶层捕获稳定的 callable,
#: 又把"构造 XSUAA validator / 决定走哪条路径"这种带 IO 副作用的事推到 lifespan.
_active_dep: Callable[[Request], Awaitable[Any]] | None = None


def _build_auth_dependency() -> Callable[..., Any]:
    """模块加载期决定 ``_bot_auth`` 实际是 JWT 还是 Basic.

    决策表::

        在 BTP   | XSUAA 绑    | 选择        | 备注
        --------+------------+-------------+--------------------------
        否       | -          | Basic Auth  | 本地 dev, BOT_KEY/SECRET
        是       | 是         | XSUAA JWT   | 生产正常路径
        是       | 否         | Basic Auth  | + 大声告警! 这是错误配置

    "BTP 上但没绑 XSUAA" 那行特别危险 — 等于把 Basic Auth 暴露到外网了.
    我们仍然回退 (不让应用起不来), 但会在启动日志里 warning 醒目地标记,
    监控应该对这条日志告警.
    """
    xsuaa = find_service_binding("xsuaa")
    if xsuaa is None:
        if is_running_on_btp():
            logger.warning(
                "running on BTP but xsuaa service is NOT bound; "
                "falling back to Basic Auth. "
                "This exposes BOT_KEY/BOT_SECRET-only auth in production — "
                "bind an xsuaa service ASAP (cf bind-service bapee bapee-xsuaa)."
            )
        else:
            logger.info("no xsuaa binding (local dev); using Basic Auth")
        # 走 Basic 路径才 require BOT_KEY/BOT_SECRET (lazy); XSUAA-only 部署
        # 不会到这, 那俩 env / binding 可以完全不设.
        bot_key, bot_secret = get_basic_auth_credentials()
        return basic_auth(bot_key, bot_secret)

    clientid = xsuaa.get("clientid")
    uaa_url = xsuaa.get("url")
    xsappname = xsuaa.get("xsappname") or ""
    if not clientid or not uaa_url:
        # 绑了 xsuaa 但 credentials 残缺 — 跟没绑一样硬 fallback, 大声警告
        logger.error(
            "xsuaa binding present but missing clientid/url; falling back to Basic Auth",
            extra={"clientid_present": bool(clientid), "url_present": bool(uaa_url)},
        )
        bot_key, bot_secret = get_basic_auth_credentials()
        return basic_auth(bot_key, bot_secret)

    validator = XSUAAJWTValidator(
        clientid=clientid,
        uaa_url=uaa_url,
        xsappname=xsappname,
    )
    global _active_validator
    _active_validator = validator
    logger.info(
        "auth strategy: XSUAA JWT",
        extra={"clientid": clientid, "xsappname": xsappname, "required_scopes": list(REQUIRED_SCOPES)},
    )
    return jwt_auth(validator, required_scopes=REQUIRED_SCOPES)


async def _bot_auth(request: Request) -> Any:
    """业务路由统一引用的 dispatcher dependency.

    本身没业务逻辑 — 在 :func:`install` 把真正的 auth callable 写进
    :data:`_active_dep` 之前返 503, 之后透传到 :data:`_active_dep` (基于
    Request 验 XSUAA JWT 或 Basic).

    为什么要 dispatcher 这一层: FastAPI 在 routes 模块 import 时就把
    ``Depends(_bot_auth)`` 里的 callable 捕获到路由树, 之后改不动. 直接把
    ``_bot_auth`` 模块级常量替换 (``_bot_auth = jwt_auth(...)``) 等于"路由
    树用旧 callable, 模块属性指新 callable" — 替换无效. dispatcher 模式
    让模块级 callable 始终是同一个对象, 内部 dispatch 到 ``_active_dep``
    才有效.

    503 兜底: routes 在 :func:`install` 跑完之前不该收到请求 (lifespan
    yield 之前 ASGI 不会路由流量). 但万一被强制调到 (e.g. 测试不走 lifespan
    直接调路由), 503 比 ``AttributeError`` 友好.
    """
    if _active_dep is None:
        raise HTTPException(
            status_code=503,
            detail="auth not initialized (service still starting up)",
        )
    return await _active_dep(request)


async def install() -> None:
    """Lifespan hook: 选 auth strategy + warmup, 在 :data:`_active_dep` 上挂好.

    幂等 — 多次调用结果一样 (后调的覆盖前调的). 但实际只在 lifespan 启动期
    调一次. JWKS 拉取失败时只 warn 不抛 — 启动失败比"首请求慢一点"更糟,
    真到第一个客户请求时验签路径自己会重试 fetch.

    切到 lifespan 阶段构造 (而不是模块加载期) 的两个好处:
    - 测试 ``import rag.server`` 时不会触发 XSUAA validator 构造 / VCAP 解析;
    - 副作用 (logger.info "auth strategy: ...") 落在启动日志里, 跟 pipeline
      init 的时序一致, 排查时一眼看到决策过程.
    """
    global _active_dep
    _active_dep = _build_auth_dependency()  # type: ignore[assignment]

    if _active_validator is None:
        return
    try:
        await _active_validator._get_jwks()
        logger.info("auth warmup: jwks cache primed")
    except Exception:  # noqa: BLE001
        logger.warning(
            "auth warmup: jwks prefetch failed; request path will retry",
            exc_info=True,
        )
