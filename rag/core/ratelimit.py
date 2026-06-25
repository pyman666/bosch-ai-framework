"""Per-client token-bucket rate limiter middleware — 防单一客户打爆共享 LLM 配额.

设计取舍 (重要, 部署前看一下):

1. **限流维度**: 默认按 ``X-Client-Id`` (前端 / 调用方主动声明的身份) →
   ``X-Forwarded-For`` (反代携带的真实 IP) → ``request.client.host`` (直连
   IP) 的优先级取一个 key. Basic auth 的 ``BOT_KEY`` 是全员共用一把, 用它
   做 key 等于"全公司一个桶", 没意义; 所以 key 函数压根不看 auth.

2. **存储 (两种 backend, 自动选)**:

   - :class:`InMemoryTokenBucketRateLimiter` — 进程内字典 + 线程锁. 多 worker
     / 多 instance 部署下每个 worker 各持一个桶, 实际限流 ≈
     ``WEB_CONCURRENCY × instances × rate``. 简单, 但不精确.
   - :class:`RedisTokenBucketRateLimiter` — 走 Redis Lua 原子脚本, 跨 worker
     × instance 一致. 需要 Redis 后端 (本地 Docker / BTP service binding).

   :func:`build_rate_limiter` 是工厂入口: 传 ``redis_url`` 就给 Redis 版,
   没传 (``None``) 就给 in-memory 版. :class:`RateLimitMiddleware` 只看
   ``RateLimiter`` 的 ``async acquire`` 接口, 不知道也不关心后面是哪种.

3. **算法**: 经典 token bucket — ``rate_per_min`` 决定补桶速率, ``burst``
   决定瞬时上限. 跟 sliding window 比好处是允许"长时间空闲后一次突发" (用
   户偶尔来一波), 这种模式比稳态高并发更符合业务客户实际使用画像.

4. **作用域**: 通过 ``paths=("/bot/",)`` 只拦 LLM-burning 端点; ``/healthz``
   / ``/docs`` / openapi 这些不烧 token 的不限流, 也不污染 token 桶.

5. **错误响应**: 429 + ``Retry-After`` 头 + 跟其他错误一致的
   ``{"detail":..., "request_id":...}`` body. ``Retry-After`` 给智能客户端
   做指数退避用; 简单客户端无视它也无所谓, 反正下次还会 429.

6. **内存增长 (in-memory 版)**: 永不淘汰桶. Redis 版用 ``EXPIRE`` 自动清.
   本地短生命周期进程下 in-memory 也无所谓; 长 uptime 进程 + 高 cardinality
   key (e.g. ``X-Client-Id`` 客户随便填) 该上 Redis 或加 LRU.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable, Iterable, Protocol

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from infra.observability import get_request_id


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rate limiter protocol — duck-typed, 两种实现都满足
# ---------------------------------------------------------------------------

class RateLimiter(Protocol):
    """Token bucket rate limiter 接口. 任何带这个 ``acquire`` 方法的对象都行.

    异步是为了 Redis 版要走网络; in-memory 版没异步成本但也实现成 ``async``
    保持调用方一致 (middleware 只 ``await limiter.acquire(key)``).
    """

    async def acquire(self, key: str) -> tuple[bool, float]:  # pragma: no cover
        ...


# ---------------------------------------------------------------------------
# In-memory 版
# ---------------------------------------------------------------------------

@dataclass
class _Bucket:
    """单个 key 的桶状态. 仅在 :class:`InMemoryTokenBucketRateLimiter` 内部使用."""

    tokens: float
    last_refill: float


class InMemoryTokenBucketRateLimiter:
    """进程内 token bucket. 不跨 worker / 不跨 instance.

    线程安全靠一把全局锁 — 在 token 检查这种纳秒级操作上锁开销可忽略, 比起
    分桶细粒度锁的复杂度更划算. async 场景下 :class:`threading.Lock` 也安全
    (协程 await 时其他协程也跑不到锁内, 因为这段没有 await).

    **内存上限**: ``max_buckets`` 控制最多保留多少个活跃桶, 用 :class:`OrderedDict`
    做 LRU — 满了就 evict 最久没用过的 key. 这是 :class:`X-Client-Id` 是客户
    端可任填字符串带来的真实威胁: 恶意调用方循环填随机 UUID 会让内存里桶数
    无限增长. 默认 10k 上限对应几 MB RAM, 远低于会有任何影响的水位.
    """

    #: LRU 上限. 选 10k 因为 (a) 远超合理客户数 (b) 单桶 ~150B, 总占用 ~1.5MB
    #: 完全可忽略 (c) 攻击者填到这个数也只能在打满桶后让自己被驱逐, 等价于
    #: 自废武功. 内部用了再调.
    DEFAULT_MAX_BUCKETS: int = 10_000

    def __init__(
        self,
        rate_per_min: float,
        burst: int,
        *,
        max_buckets: int = DEFAULT_MAX_BUCKETS,
    ) -> None:
        if rate_per_min <= 0 or burst <= 0:
            raise ValueError(
                f"rate_per_min and burst must be > 0, got "
                f"rate_per_min={rate_per_min!r}, burst={burst!r}"
            )
        if max_buckets <= 0:
            raise ValueError(f"max_buckets must be > 0, got {max_buckets!r}")
        self._rate_per_sec: float = rate_per_min / 60.0
        self._capacity: float = float(burst)
        self._max_buckets: int = max_buckets
        # ``OrderedDict`` + ``move_to_end`` 实现 LRU: 每次访问把 key 挪到尾部,
        # 满时 popitem(last=False) 弹出最久没动的头部. 摊销 O(1).
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._lock = Lock()

    async def acquire(self, key: str) -> tuple[bool, float]:
        """尝试为 ``key`` 扣一个 token; 返回 ``(allowed, retry_after_seconds)``.

        允许时 ``retry_after_seconds = 0``; 拒绝时给一个粗略的"再过多久能扣
        到 token"估值, 用于客户端 ``Retry-After`` 头. 不要太精确, 客户端通
        常加抖动二次重试, 多 1s 少 1s 不影响.
        """
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                # 新 key 一上来就给满桶 = 允许"新客户的第一波 burst", 避免
                # 冷启动客户被秒挡. 想严格点改成 ``tokens=0`` 让他从零攒.
                bucket = _Bucket(tokens=self._capacity, last_refill=now)
                self._buckets[key] = bucket
                # 满了驱逐 LRU 头. 被驱逐的 key 下次再来会被当新客户给满桶,
                # 等价于"忘记你之前打过", 这是 LRU 的固有近似. 想保护"老客户
                # 不被恶意填爆挤掉" 得换 Redis 版.
                if len(self._buckets) > self._max_buckets:
                    self._buckets.popitem(last=False)
            else:
                self._buckets.move_to_end(key)
            elapsed = now - bucket.last_refill
            bucket.tokens = min(
                self._capacity, bucket.tokens + elapsed * self._rate_per_sec
            )
            bucket.last_refill = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0.0
            # 还需要多少 token 才能放过 (1 - 当前 token), 除以速率即秒数.
            retry_after = (1.0 - bucket.tokens) / self._rate_per_sec
            return False, retry_after


# 历史名 — 已有代码可能 ``from rag.core.ratelimit import TokenBucketRateLimiter``.
# 保留别名 (= in-memory 版), 切到 :func:`build_rate_limiter` 后这条不再被新
# 代码引用, 但兼容旧调用方.
TokenBucketRateLimiter = InMemoryTokenBucketRateLimiter


# ---------------------------------------------------------------------------
# Redis 版 — 跨 worker / 跨 instance 精确
# ---------------------------------------------------------------------------

# Lua 脚本: 原子完成 "refill + check + 扣 token + 续期". 必须在 server 侧
# 一把跑完, 不能拆成两次 Redis 调用 (那样两个 worker 同时跑会算重).
#
# KEYS[1] = bucket key (e.g. ``bapee:rl:cid:abc``)
# ARGV[1] = now (seconds, float)
# ARGV[2] = capacity (burst)
# ARGV[3] = rate_per_sec
# ARGV[4] = ttl_sec  (Redis EXPIRE 用; 空闲超过这个的 key 自动清, 防内存涨)
#
# 返回: {allowed (1|0), retry_after_str}. 用字符串包 retry_after 是因为 Lua
# 不直接吐 float, 客户端解析回 float 即可.
_LUA_TOKEN_BUCKET = """
local tokens_str = redis.call('HGET', KEYS[1], 'tokens')
local last_str   = redis.call('HGET', KEYS[1], 'last')
local now        = tonumber(ARGV[1])
local cap        = tonumber(ARGV[2])
local rate       = tonumber(ARGV[3])
local ttl        = tonumber(ARGV[4])

local tokens
local last
if tokens_str then
    tokens = tonumber(tokens_str)
    last   = tonumber(last_str)
else
    tokens = cap
    last   = now
end

local elapsed = now - last
if elapsed < 0 then elapsed = 0 end
tokens = math.min(cap, tokens + elapsed * rate)

local allowed = 0
local retry = 0.0
if tokens >= 1.0 then
    tokens = tokens - 1.0
    allowed = 1
else
    retry = (1.0 - tokens) / rate
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'last', now)
redis.call('EXPIRE', KEYS[1], ttl)
return {allowed, tostring(retry)}
"""


class RedisTokenBucketRateLimiter:
    """走 Redis 的 token bucket, 跨 worker / 跨 instance 精确.

    Lua 脚本在 server 侧原子完成 refill + 扣 token + EXPIRE 续期, 三步并发
    安全. 连接走 redis-py 5.x 内置的 async client (``redis.asyncio``), 跟
    FastAPI / Starlette 一致用 asyncio.

    错配置 (Redis 连不上, 网络抖动等) 怎么办: ``acquire`` 出错时**放行** (返
    ``(True, 0)``) 而不是拒绝. 理由: rate limit 是防护手段不是核心功能, Redis
    挂了让所有请求 503 显然 worse — 退化到"没限流"比退化到"完全拒服务"好.
    错误会 ``logger.warning`` 留痕, 监控应该对 ``rate_limit.backend_error``
    告警.

    Key 格式: ``bapee:rl:<key_fn 的返回>``, 例如 ``bapee:rl:cid:my-client-123``.
    前缀让你能在 Redis 里 ``SCAN`` / ``DEL`` 仅 rate limit 数据, 不误删别的.

    TTL: 桶在最后一次访问后 1 小时清掉 — 远小于 burst 攒满的时长 (capacity /
    rate), 不会误清还在用的 key; 空闲客户不会占内存. 想久点改 ``ttl_sec``
    参数.
    """

    _KEY_PREFIX: str = "bapee:rl:"
    _DEFAULT_TTL_SEC: int = 3600

    def __init__(
        self,
        *,
        redis_client: Any,  # redis.asyncio.Redis instance — duck-typed 避免 import 死链
        rate_per_min: float,
        burst: int,
        ttl_sec: int = _DEFAULT_TTL_SEC,
    ) -> None:
        if rate_per_min <= 0 or burst <= 0:
            raise ValueError(
                f"rate_per_min and burst must be > 0, got "
                f"rate_per_min={rate_per_min!r}, burst={burst!r}"
            )
        self._redis = redis_client
        self._rate_per_sec = rate_per_min / 60.0
        self._capacity = float(burst)
        self._ttl_sec = ttl_sec
        # 先注册 Lua, 拿到 SHA1; 之后 EVALSHA 比 EVAL 省带宽 (脚本体不重传).
        # 真正注册靠 redis-py 的 ``register_script``, 它内部自动处理 NOSCRIPT
        # 错误时的 fallback (重传脚本).
        self._script = self._redis.register_script(_LUA_TOKEN_BUCKET)

    async def acquire(self, key: str) -> tuple[bool, float]:
        full_key = f"{self._KEY_PREFIX}{key}"
        now = time.time()  # time.monotonic() 跨进程不可比, 必须用 wall clock
        try:
            result = await self._script(
                keys=[full_key],
                args=[now, self._capacity, self._rate_per_sec, self._ttl_sec],
            )
        except Exception as exc:  # noqa: BLE001
            # Redis 抖动 / 连不上 → 放行 + 留痕. 故意不抛, 见 class docstring.
            logger.warning(
                "rate_limit.backend_error",
                extra={"err": str(exc), "key": full_key},
            )
            return True, 0.0
        # result 是 ``[1, "0"]`` 或 ``[0, "12.345"]``
        allowed_int, retry_str = result[0], result[1]
        if isinstance(retry_str, bytes):
            retry_str = retry_str.decode("ascii")
        return bool(allowed_int), float(retry_str)


# ---------------------------------------------------------------------------
# 工厂 — 自动选 backend
# ---------------------------------------------------------------------------

def build_rate_limiter(
    *,
    rate_per_min: float,
    burst: int,
    redis_url: str | None = None,
    redis_ssl_cert_reqs: str = "none",
) -> RateLimiter:
    """根据是否有 ``redis_url`` 自动选 backend.

    Args:
        rate_per_min: 稳态 RPM.
        burst: 桶容量.
        redis_url: Redis 连接 URL (``redis://...`` 或 ``rediss://...``). 传
            ``None`` (或空字符串) 就走 in-memory 版.
        redis_ssl_cert_reqs: ``rediss://`` (TLS) 时证书校验等级. 默认
            ``"none"`` 关闭校验 (BTP 内网常用自签); ``"required"`` 严校验,
            但需要 CA bundle 配置. 这个参数对非 TLS URL 无效.

    Returns:
        实现了 :class:`RateLimiter` 协议的对象. 调用方 ``await limiter.acquire(key)``
        即可, 不知道也不需要知道下面是哪种.
    """
    if not redis_url:
        logger.info(
            "rate limiter: in-memory (no REDIS_URL / no redis service binding)",
            extra={"rate_per_min": rate_per_min, "burst": burst},
        )
        return InMemoryTokenBucketRateLimiter(rate_per_min=rate_per_min, burst=burst)

    # 延迟 import — 不用 Redis 时不需要装 redis-py (虽然 requirements.txt 已
    # 经把它列了, 留这个延迟 import 是为了让 "没装 redis 但也不需要" 的极端
    # 配置 (e.g. 用户手动剔除 dep) 仍能跑 in-memory 路径.
    from redis.asyncio import Redis

    client = Redis.from_url(
        redis_url,
        decode_responses=False,  # Lua 返回的整数 / 字符串我们自己处理, 不全局 decode
        ssl_cert_reqs=redis_ssl_cert_reqs if redis_url.startswith("rediss://") else None,
    )
    logger.info(
        "rate limiter: redis-backed",
        extra={
            "rate_per_min": rate_per_min,
            "burst": burst,
            # 安全: URL 含 password, 别整行 dump; 只露 host:port + scheme.
            "redis_scheme": redis_url.split("://", 1)[0],
        },
    )
    return RedisTokenBucketRateLimiter(
        redis_client=client,
        rate_per_min=rate_per_min,
        burst=burst,
    )


# ---------------------------------------------------------------------------
# Key 提取函数 — 决定限流维度
# ---------------------------------------------------------------------------

def default_client_key(request: Request) -> str:
    """从请求里抽出"谁在打" 的 key. 优先级:

    1. ``X-Client-Id`` 头 — 调用方主动声明的身份. 适合多个内部服务共用一
       个 ``BOT_KEY`` 但又想分别计费 / 限流的场景, 自觉的调用方会带上.
    2. ``X-Forwarded-For`` 首段 — 反向代理 (BTP gateway / nginx) 携带的
       客户端 IP. 跟代理直连时 ``request.client.host`` 永远是代理 IP, 必
       须看这个头才知道真实来源.
    3. ``request.client.host`` — 兜底, 用 TCP 对端地址. 公司内 NAT 下可能
       一个 IP 后面挂上百号人, 会造成误伤; 真业务建议让客户端带 ``X-Client-Id``.

    返回值带前缀 (``cid:`` / ``ip:``) 是为了让日志看一眼分清来源类型,
    顺手避免 IP 和 client id 撞 key.
    """
    cid = request.headers.get("X-Client-Id")
    if cid:
        return f"cid:{cid.strip()}"
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return f"ip:{xff.split(',')[0].strip()}"
    if request.client is not None:
        return f"ip:{request.client.host}"
    return "ip:unknown"


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class RateLimitMiddleware(BaseHTTPMiddleware):
    """ASGI 中间件 — 给 ``paths`` 前缀里的请求做 per-key 限流.

    挂载顺序提示: 必须挂在 :class:`bapee.core.observability.RequestIDMiddleware`
    **里面** (添加顺序: 先 RateLimit 后 RequestID), 这样:

    - RequestID 是外层, 拦到任何请求都先生成 ``X-Request-ID``, 写进 contextvar;
    - RateLimit 是内层, 限流被命中时回 429, ``get_request_id()`` 能拿到 ID,
      响应回到 RequestID 那层时它再把 ``X-Request-ID`` 头贴到响应上.

    顺序错了 (RequestID 在内): 429 响应的 ``request_id`` 字段会变成 ``"-"``,
    客户报障 grep 不出来.
    """

    def __init__(
        self,
        app,
        *,
        paths: Iterable[str],
        limiter: RateLimiter,
        key_fn: Callable[[Request], str] = default_client_key,
    ) -> None:
        super().__init__(app)
        # 用 ``tuple`` 是因为 ``str.startswith`` 直接吃 tuple, 比逐个比快.
        self._paths: tuple[str, ...] = tuple(paths)
        self._limiter = limiter
        self._key_fn = key_fn

    async def dispatch(self, request: Request, call_next) -> Response:
        if not request.url.path.startswith(self._paths):
            return await call_next(request)

        key = self._key_fn(request)
        allowed, retry_after = await self._limiter.acquire(key)
        if allowed:
            return await call_next(request)

        # 命中限流: warn 级别一行结构化日志 (好聚合告警), 然后 429.
        logger.warning(
            "rate_limit.blocked",
            extra={
                "rate_limit_key": key,
                "path": request.url.path,
                "method": request.method,
                "retry_after_sec": round(retry_after, 2),
            },
        )
        return JSONResponse(
            status_code=429,
            content={
                "detail": "rate limit exceeded; slow down or set a unique X-Client-Id",
                "request_id": get_request_id(),
            },
            headers={
                # +1 保证最小是 1 秒, 避免客户端 ``Retry-After: 0`` 立刻重试.
                "Retry-After": str(int(retry_after) + 1),
            },
        )
