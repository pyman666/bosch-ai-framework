"""项目级 LLM 基础设施 — LiteLLM Router 工厂 + AI Core 集成层.

跟 :mod:`bapee.settings` 里的 ``MODEL_LIST`` / ``ROUTER_KWARGS`` /
``AI_CORE_DEPLOYMENTS`` 配对使用: settings 那边声明"我们用哪些 model × provider
× key (本地)" 或者 "我们用哪些 AI Core deployment (BTP)", 本模块负责把它
装进一个可调 Router 实例, 业务侧 (e.g. :mod:`bapee.chatbot.bpae_pipeline`)
拿着这个实例喂给 :class:`bapee.rag.HybridPipelineConfig.router`.

放在外层 ``bapee/`` 而不是 ``bapee/rag/`` 是有意为之: ``rag`` 是通用 hybrid
检索库, **消费** Router 接口但不该 **持有** "怎么构造 Router" 的责任 — 那
是 LLM 基础设施 / 项目装配的事. 真到 rag 作为独立包发布那天, 它不会带
LiteLLM 依赖, 由调用方自己塞 Router-shaped 对象进来即可.

``litellm.drop_params = True`` 是模块级 side effect: import 本模块即生效.
不同 provider 对 ``temperature`` / ``top_p`` 等参数的容忍度不一致 (e.g.
o1 不支持 temperature), drop_params=True 让 Router 自动跳过 provider 不
认的参数, 而不是抛 400 — 对多 provider 路由几乎必开.

## 两条路径

1. **Legacy** — 本地 dev / 自管 API key. 走 :func:`build_router`, 配置来自
   ``settings.yaml`` 的 ``providers`` 段, key 走环境变量 (``OPENAI_API_KEY``
   等). 这是历史路径, 保留.
2. **BTP AI Core** — 生产路径. 走 :func:`try_build_aicore_router`, 自动检测
   ``aicore`` service binding, 拿 ``clientid`` / ``clientsecret`` 去 XSUAA
   token endpoint 换 bearer (带缓存 + 提前 5 分钟刷), 每次 LLM 调用注入
   ``Authorization: Bearer <token>`` + ``AI-Resource-Group: <rg>`` 头. 模型
   列表来自 ``settings.yaml`` 的 ``ai_core.deployments`` 段, 你需要把每个
   deployment 的 ``deployment_id`` 填上 (从 SAP AI Launchpad / AI Core API
   ``GET /v2/lm/deployments`` 拿).

业务装配层 (:mod:`bapee.chatbot.bpae_pipeline`) 自己选 path: AI Core 绑了
就用 AI Core, 没绑回 legacy. 业务路由完全不感知, 只看到一个有 ``acompletion``
方法的对象.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Iterable

import httpx
import litellm
from litellm import Router

from .btp import find_service_binding


logger = logging.getLogger(__name__)


litellm.drop_params = True


# ===========================================================================
# Router 共享 backend (Redis) — 跨 worker / 跨 instance 同步 cooldown / 用量状态
# ===========================================================================

def with_redis_backend(
    router_kwargs: dict[str, Any],
    *,
    redis_url: str | None,
) -> dict[str, Any]:
    """如果 ``redis_url`` 非空, 给 ``router_kwargs`` 注入 ``redis_url`` 字段.

    LiteLLM Router 看到 ``redis_url`` (5.x) 就会自动把 cooldown / 用量 / rate
    limit state 都挪到 Redis 上, 实现:

    - **集群一致 cooldown**: A 实例上某个 deployment 撞 429 时, 全集群同步沉
      默 60s 不再打它, 不会变成"挨个实例去撞一遍才进 cooldown";
    - **集群一致 usage**: ``routing_strategy="usage-based-routing-v2"`` 看的
      是全集群 token / RPM 用量, 不再每个 worker 只看自己那份, 选 deployment
      更准确.

    没 ``redis_url`` 时直接返原 dict, LiteLLM Router 走默认 in-memory 状态.
    多 worker 部署下 cooldown / usage 各算各的, 配额浪费一点但不影响正确性.

    本函数是纯函数 (不就地改 ``router_kwargs``), 方便单元测试.
    """
    if not redis_url:
        return dict(router_kwargs)
    merged = dict(router_kwargs)
    merged["redis_url"] = redis_url
    logger.info(
        "litellm router: using redis shared state",
        extra={"redis_scheme": redis_url.split("://", 1)[0]},
    )
    return merged


# ===========================================================================
# Legacy: 通用 LiteLLM Router 工厂 (本地 dev / 自管 API key)
# ===========================================================================

def build_router(
    model_list: list[dict],
    **router_kwargs: Any,
) -> Router:
    """构造一个 LiteLLM Router (legacy / 本地 dev 路径).

    ``model_list`` 是 LiteLLM 的标准形状 (``[{"model_name": ...,
    "litellm_params": {...}}, ...]``), 通常已经在 :mod:`bapee.settings` 把
    ``provider × api_key × model`` 笛卡尔积展开好.

    ``router_kwargs`` 透传给 ``Router(...)`` — e.g. ``routing_strategy=
    "usage-based-routing-v2"`` / 自定义超时 / fallback 表. 业务装配层若
    希望 cooldown / usage 跨集群共享, 在调用前用 :func:`with_redis_backend`
    给 ``router_kwargs`` 注入 ``redis_url``.
    """
    return Router(model_list=model_list, **router_kwargs)


# ===========================================================================
# AI Core: OAuth2 token provider + Router 包装层
# ===========================================================================

class AICoreTokenProvider:
    """从 BTP AI Core service binding 拿 OAuth2 bearer token, 带缓存 + 提前刷.

    AI Core 走 XSUAA 颁的 client_credentials token, 有效期一般 12h. 我们:

    - 进程内缓存 token + 失效时间;
    - 距失效 ``EARLY_REFRESH_SEC`` 之内被视为"快过期", 下一次 :meth:`get_token`
      会主动刷 — 不在边界附近, 避免某个长 LLM 调用刚开始就过期;
    - 用 asyncio 锁 + 双检查锁防并发刷.

    本类只生产 token, **不** 持有调用 AI Core 的责任 — 那是
    :class:`AICoreRouter` 的事.
    """

    #: 距过期多少秒内被视为"快过期", 主动刷. 默认 5 分钟, 留足 token 调一次
    #: 后还能撑过 1 分钟级的 LLM 调用 + 网络抖动.
    EARLY_REFRESH_SEC: int = 300

    #: token endpoint 拉 token 的超时. BTP XSUAA 一般亚秒返, 10s 已经很宽.
    FETCH_TIMEOUT_SEC: float = 10.0

    def __init__(
        self,
        *,
        clientid: str,
        clientsecret: str,
        token_url: str,
    ) -> None:
        # token_url 是 XSUAA 的 base URL (e.g.
        # ``https://<subdomain>.authentication.<region>.hana.ondemand.com``);
        # 实际 POST 走 ``/oauth/token``.
        self._clientid = clientid
        self._clientsecret = clientsecret
        self._token_url = token_url.rstrip("/") + "/oauth/token"
        self._token: str | None = None
        self._expires_at: float = 0.0  # monotonic 时间; 0 表示未取过
        self._lock = asyncio.Lock()
        logger.info(
            "ai-core token provider ready",
            extra={"token_url": self._token_url, "clientid": clientid},
        )

    def _is_fresh(self) -> bool:
        return self._token is not None and time.monotonic() < self._expires_at - self.EARLY_REFRESH_SEC

    async def _refresh(self) -> None:
        """同步去 XSUAA 换一份新 token, 更新缓存. 失败直接抛."""
        # XSUAA 推荐 Basic Auth (HTTP header) 而不是把 clientsecret 放 body,
        # 减少 secret 进 access log 的概率.
        async with httpx.AsyncClient(timeout=self.FETCH_TIMEOUT_SEC) as client:
            resp = await client.post(
                self._token_url,
                data={"grant_type": "client_credentials"},
                auth=(self._clientid, self._clientsecret),
                headers={"Accept": "application/json"},
            )
        resp.raise_for_status()
        body = resp.json()
        token = body.get("access_token")
        expires_in = body.get("expires_in")
        if not token or not isinstance(expires_in, (int, float)):
            raise RuntimeError(
                f"ai-core token endpoint returned malformed body: keys={list(body.keys())}"
            )
        self._token = token
        self._expires_at = time.monotonic() + float(expires_in)
        logger.info(
            "ai-core token refreshed",
            extra={"expires_in_sec": int(expires_in)},
        )

    async def get_token(self) -> str:
        """返回当前有效的 bearer; 必要时阻塞刷新.

        高并发下多个协程同时 ``get_token()`` 时, 只有第一个进入锁的会去
        XSUAA, 其余等着复用刷完的结果 — 不会把 XSUAA token endpoint 打爆.
        """
        if self._is_fresh():
            return self._token  # type: ignore[return-value]
        async with self._lock:
            if self._is_fresh():
                return self._token  # type: ignore[return-value]
            await self._refresh()
            assert self._token is not None
            return self._token


class AICoreRouter:
    """LiteLLM ``Router`` 的薄包装, 每次 ``acompletion`` 注入 AI Core 鉴权头.

    Duck-typed: 暴露 ``acompletion(*, model, messages, stream, **kwargs)`` 方
    法, :class:`bapee.rag.HybridPipeline` 看不出来里面是真 Router 还是我们这
    个包装 — 所以 ``rag/`` 不需要任何 AI Core 特定代码.

    每次 LLM 调用前:

    1. :class:`AICoreTokenProvider` 拿 bearer (命中缓存 = 不出网, 否则去
       XSUAA);
    2. 把 ``Authorization: Bearer <token>`` + ``AI-Resource-Group: <rg>`` 注
       入 LiteLLM 的 ``extra_headers``;
    3. 透传给底层 Router, 让 LiteLLM 走 OpenAI-compatible 路径打 AI Core
       deployment endpoint.

    业务调用方传的 ``extra_headers`` (e.g. 自定义 tracing 头) 会跟我们的合
    并, 业务的不会覆盖我们的鉴权头 (鉴权头加在合并后, 不让业务误覆).
    """

    def __init__(
        self,
        inner: Router,
        *,
        token_provider: AICoreTokenProvider,
        resource_group: str,
    ) -> None:
        self._inner = inner
        self._token = token_provider
        self._resource_group = resource_group

    async def acompletion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool = False,
        **kwargs: Any,
    ) -> Any:
        token = await self._token.get_token()
        headers = dict(kwargs.pop("extra_headers", {}) or {})
        # 我方的鉴权头放最后写入, 防业务侧 extra_headers 同名 key 误覆盖
        # (覆盖了 LLM 调用必失败, 但失败原因可能很难追).
        headers["Authorization"] = f"Bearer {token}"
        headers["AI-Resource-Group"] = self._resource_group
        return await self._inner.acompletion(
            model=model,
            messages=messages,
            stream=stream,
            extra_headers=headers,
            **kwargs,
        )


# ===========================================================================
# AI Core: 装配入口
# ===========================================================================

def _build_aicore_model_list(
    deployments: Iterable[dict[str, Any]],
    api_base: str,
) -> list[dict[str, Any]]:
    """把 ``settings.yaml`` 的 ``ai_core.deployments`` 列表转成 LiteLLM ``model_list``.

    AI Core 是 OpenAI-compatible: 同一个 endpoint URL 跟 OpenAI ``/chat/completions``
    一样形状. LiteLLM 走 ``openai/`` 前缀的 model 标识让它选 OpenAI parser,
    实际 model 名 (deployment 底下挂的具体模型, e.g. ``gpt-4o-mini``) 跟着
    ``openai/`` 后面走, ``api_base`` 指向 deployment URL.

    Args:
        deployments: 形如
            ``[{"alias": "gpt-4o-mini", "deployment_id": "d123", "model": "gpt-4o-mini"}, ...]``.
            ``alias`` 是业务代码用的内部 model 名, ``deployment_id`` 是 AI
            Core 的部署 ID, ``model`` 是 deployment 底下的实际模型名 (给
            LiteLLM 选 parser 用, AI Core 真不真用这个名做 routing 取决于
            deployment 的 generative-ai-hub config).
        api_base: AI Core API 根 URL (binding ``serviceurls.AI_API_URL``).

    Returns:
        LiteLLM Router 直接能吃的 ``model_list``. ``api_key`` 设成 ``"dummy"``
        是合规需要 (LiteLLM 验非空), 实际鉴权走我们注入的 ``Authorization``
        头.

    Raises:
        ValueError: 某条 deployment 缺 ``deployment_id`` — 配置未填完, 当
            场让启动失败比上线后客户报错好.
    """
    api_base = api_base.rstrip("/")
    out: list[dict[str, Any]] = []
    for idx, d in enumerate(deployments):
        alias = d.get("alias") or d.get("model")
        deployment_id = d.get("deployment_id")
        underlying_model = d.get("model") or alias
        if not alias:
            raise ValueError(
                f"ai_core.deployments[{idx}]: missing 'alias' or 'model'"
            )
        if not deployment_id:
            raise ValueError(
                f"ai_core.deployments[{idx}] (alias={alias!r}): "
                f"deployment_id is empty — fill it in settings.yaml under ai_core.deployments."
            )
        out.append(
            {
                "model_name": alias,
                "litellm_params": {
                    "model": f"openai/{underlying_model}",
                    "api_base": f"{api_base}/v2/lm/deployments/{deployment_id}",
                    "api_key": "dummy",  # 真鉴权走 AICoreRouter 注入的 Bearer
                },
            }
        )
    return out


def try_build_aicore_router(
    deployments: Iterable[dict[str, Any]],
    *,
    resource_group: str = "default",
    binding_name: str | None = None,
    **router_kwargs: Any,
) -> AICoreRouter | None:
    """如果 VCAP_SERVICES 里有 ``aicore`` 绑定就构造 :class:`AICoreRouter`, 否则返 ``None``.

    业务装配层模式::

        from rag.core.llm import try_build_aicore_router, build_router
        from rag.settings import AI_CORE_DEPLOYMENTS, AI_CORE_RESOURCE_GROUP, MODEL_LIST, ROUTER_KWARGS

        router = try_build_aicore_router(
            AI_CORE_DEPLOYMENTS,
            resource_group=AI_CORE_RESOURCE_GROUP,
            **ROUTER_KWARGS,
        )
        if router is None:
            router = build_router(MODEL_LIST, **ROUTER_KWARGS)

    Args:
        deployments: ``settings.yaml`` 解出的 ``ai_core.deployments`` 列表.
            空列表会返 ``None`` — 即使绑了 ai_core service 但没配 deployment,
            走 legacy 路径才是合理回退.
        resource_group: ``AI-Resource-Group`` 头值. 默认 AI Core 内置的
            ``default`` resource group; 自建 RG 时改这.
        binding_name: 多个 ai_core binding 时指定用哪一个; 单 binding 留 None.
        **router_kwargs: 透传给底层 ``Router(...)`` 构造 (timeouts /
            ``routing_strategy`` / fallback 等).
    """
    deployments_list = list(deployments)
    if not deployments_list:
        return None

    aicore_creds = find_service_binding("aicore", name=binding_name)
    if aicore_creds is None:
        return None

    # 字段定位 — AI Core service key 标准 shape
    try:
        clientid = aicore_creds["clientid"]
        clientsecret = aicore_creds["clientsecret"]
        token_url = aicore_creds["url"]
        api_base = aicore_creds["serviceurls"]["AI_API_URL"]
    except KeyError as exc:
        # 绑了但字段不全 — 这是配置错误, 让启动失败而不是默默 fallback,
        # 不然你会以为在用 AI Core 实际跑 legacy.
        raise RuntimeError(
            f"aicore service binding is missing expected field {exc}; "
            f"binding keys = {list(aicore_creds.keys())}"
        )

    model_list = _build_aicore_model_list(deployments_list, api_base)
    inner = Router(model_list=model_list, **router_kwargs)
    token_provider = AICoreTokenProvider(
        clientid=clientid,
        clientsecret=clientsecret,
        token_url=token_url,
    )
    logger.info(
        "ai-core router ready",
        extra={
            "api_base": api_base,
            "resource_group": resource_group,
            "deployments": [
                {"alias": d["model_name"], "url": d["litellm_params"]["api_base"]}
                for d in model_list
            ],
        },
    )
    return AICoreRouter(
        inner,
        token_provider=token_provider,
        resource_group=resource_group,
    )
