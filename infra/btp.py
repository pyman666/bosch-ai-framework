"""BTP / Cloud Foundry 集成基础层 — VCAP_SERVICES 解析 + service binding helper.

定位: 跟"应用部署平台" 打交道的所有粘合逻辑都收在这里, 业务代码 (settings /
chatbot / rag) 不感知"BTP 还是裸 env". 本地 dev `.env` 文件 + BTP 服务绑定
共用同一份业务代码, 切换零摩擦.

## 数据流

BTP 在每个 instance 启动时把所有绑定的服务凭证打包成一个 JSON 字符串塞进
``VCAP_SERVICES`` env var. 形如::

    {
      "user-provided": [
        {"name": "bapee-bot-creds",
         "credentials": {"BOT_KEY": "...", "BOT_SECRET": "..."}}
      ],
      "redis-cache": [
        {"name": "bapee-redis",
         "credentials": {"hostname": "...", "port": 6379, "password": "..."}}
      ],
      "aicore": [
        {"name": "bapee-aicore",
         "credentials": {
           "clientid": "...", "clientsecret": "...",
           "url": "https://<tenant>.authentication.<region>.hana.ondemand.com",
           "serviceurls": {"AI_API_URL": "https://api.ai.<region>.hana.ondemand.com"}
         }}
      ],
      "xsuaa": [...]
    }

本模块提供三层 API, 从底到上:

1. :func:`get_vcap_services` — 拿到反序列化后的整个 dict (或 ``None``, 本地
   dev 无此 env). 业务代码很少直接调.
2. :func:`find_service_binding` — 按 service-type + 可选 name 找一条绑定的
   ``credentials`` dict. 给业务代码"我要找名为 X 的 Redis 绑定"用.
3. :func:`get_secret` — 综合 env var 和 user-provided binding, 返回字符串
   值. 给"凭证类配置" (BOT_KEY / BOT_SECRET / API key) 用, 本地 ``.env``
   和 BTP user-provided service 都能落.

## 为什么自己造而不是用 cfenv / cf-python-helper

那些库要么不维护 (cfenv 上次 release 2017), 要么把简单 dict 包装成几十个
类层次显得复杂. 我们要的就是"读个 JSON, 按 type 过滤"30 行的事, 不值得拉
依赖. AI Core / XSUAA / Redis 各自的鉴权 / 客户端构造逻辑由各自的 SDK 处
理, 这里只负责"把凭证 dict 交付给它们".

## 扩展点

未来加新服务类型 (e.g. PostgreSQL on BTP) 时, 模式是:

1. 在调用方 (e.g. ``bapee.core.llm`` / ``bapee.core.cache``) 里直接调
   ``find_service_binding("postgresql", name=...)`` 拿 credentials;
2. 不要在本模块加 ``parse_postgresql()`` 这种 type-specific wrapper —
   每个服务的字段命名差异由调用方就近处理, 本模块只做通用 lookup.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any


logger = logging.getLogger(__name__)


# 缓存解析后的 VCAP_SERVICES, 避免每次调用都重新 ``json.loads``. 这个 env
# var 在 instance 生命周期内不变 (CF 文档明确说; 服务重新绑定要 re-stage 才
# 生效), 缓存安全. Module-level 单例, 不需要锁.
_vcap_cache: dict[str, Any] | None = None
_vcap_cache_loaded: bool = False


def _load_vcap() -> dict[str, Any] | None:
    """读 + 解析 ``VCAP_SERVICES`` env var. 内部用; 业务调 :func:`get_vcap_services`.

    解析失败 (JSON 非法 / 字段类型不对) 时记 warning 然后当作"无 BTP 环境"
    继续, 不抛异常 — 那会让本地 dev 没设这个 env 的人莫名其妙起不来.
    """
    raw = os.environ.get("VCAP_SERVICES")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "VCAP_SERVICES is set but not valid JSON; ignoring. err=%s", exc
        )
        return None
    if not isinstance(parsed, dict):
        logger.warning(
            "VCAP_SERVICES is JSON but top-level is %s, expected dict; ignoring",
            type(parsed).__name__,
        )
        return None
    return parsed


def get_vcap_services() -> dict[str, Any] | None:
    """返回完整的 VCAP_SERVICES (反序列化后), 没绑服务 / 本地 dev 时返 ``None``."""
    global _vcap_cache, _vcap_cache_loaded
    if not _vcap_cache_loaded:
        _vcap_cache = _load_vcap()
        _vcap_cache_loaded = True
    return _vcap_cache


def is_running_on_btp() -> bool:
    """快速判断是否在 BTP / CF 环境里跑.

    判据是 ``VCAP_APPLICATION`` env var (CF 一定会注入, user-provided service
    可能没绑). 这个判断给"日志里打一行环境标识""调试模式只在本地启用"这类
    弱依赖场景, 不要在业务逻辑里用它做硬分支 — 业务逻辑应该看具体的 service
    binding 在不在.
    """
    return os.environ.get("VCAP_APPLICATION") is not None


def find_service_binding(
    service_type: str,
    *,
    name: str | None = None,
) -> dict[str, Any] | None:
    """在 VCAP_SERVICES 里找一条绑定, 返回它的 ``credentials`` dict.

    Args:
        service_type: VCAP_SERVICES top-level key. 常见值: ``user-provided``,
            ``redis-cache``, ``aicore``, ``xsuaa``, ``postgresql-db``.
        name: 可选 service instance 名字; 一个 type 下绑了多个时用. 留 None
            则取第一条 (一个 type 通常只绑一条, 多条没显式指定就要靠 name
            消歧).

    Returns:
        Credentials dict, 或 ``None`` (没绑该类型 / 该名字).
    """
    vcap = get_vcap_services()
    if vcap is None:
        return None
    entries = vcap.get(service_type) or []
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if name is not None and entry.get("name") != name:
            continue
        creds = entry.get("credentials")
        if isinstance(creds, dict):
            return creds
    return None


def get_secret(
    name: str,
    *,
    default: str | None = None,
    vcap_service_name: str | None = None,
) -> str | None:
    """读一条凭证类配置: env var 优先, 没有就翻 user-provided service binding.

    解析优先级 (找到立刻返, 不继续):

    1. 同名 env var (``os.environ[name]``) — 覆盖本地 ``.env`` 和 ``cf set-env``
       两种"裸 env" 路径.
    2. user-provided service 绑定的 ``credentials.<name>`` — BTP 的推荐姿势,
       凭证轮换走 ``cf update-user-provided-service`` 无需 re-push.
       ``vcap_service_name=None`` 时遍历所有 user-provided 绑定找第一个有这
       个 key 的; 传具体名字则只看那条.
    3. 都没有 → 返 ``default``.

    Args:
        name: 凭证字段名 (兼当 env var 名和 credentials dict key).
        default: 所有来源都没找到时的兜底.
        vcap_service_name: 限定只在指定名字的 user-provided binding 里找.

    Examples::

        # 本地 .env 设 BOT_KEY=xxx, 或 BTP user-provided 绑定 {"BOT_KEY": "xxx"}
        bot_key = get_secret("BOT_KEY")

        # 严格只看 bapee-bot-creds 这条 binding
        bot_key = get_secret("BOT_KEY", vcap_service_name="bapee-bot-creds")
    """
    env_val = os.environ.get(name)
    if env_val:
        return env_val

    vcap = get_vcap_services()
    if vcap is None:
        return default

    user_provided = vcap.get("user-provided") or []
    if not isinstance(user_provided, list):
        return default

    for entry in user_provided:
        if not isinstance(entry, dict):
            continue
        if vcap_service_name is not None and entry.get("name") != vcap_service_name:
            continue
        creds = entry.get("credentials")
        if not isinstance(creds, dict):
            continue
        val = creds.get(name)
        if val is not None:
            return str(val)

    return default


# ---------------------------------------------------------------------------
# Redis URL 解析 — 给 rate limiter + LiteLLM Router 共享 backend
# ---------------------------------------------------------------------------

# 在 BTP marketplace 上 Redis 至少有两种 service-type 形态, 字段命名一致但
# 走老 / 新 entitlement 时 type 名不同. 按"新 hyperscaler 优先"顺序探测;
# 都没找到才返 None.
_REDIS_SERVICE_TYPES: tuple[str, ...] = (
    "hyperscaler-option-redis",  # 新版, AWS ElastiCache / Azure / GCP 后端
    "redis-cache",                # 老 SAP 自营 Redis on BTP
)


def find_redis_url() -> str | None:
    """解析 Redis 连接 URL: env var 优先, 再翻 VCAP_SERVICES 的 redis 绑定.

    优先级:

    1. ``REDIS_URL`` env var — 本地 dev 用 Docker Redis 时直接 ``redis://localhost:6379``;
       BTP 上想跳过自动检测 (强制某个连接) 也可以 ``cf set-env`` 覆盖. 永远
       第一优先, 这样 dev 和 prod 都用同一个开关.
    2. ``VCAP_SERVICES["hyperscaler-option-redis"][0].credentials.uri`` — 新版 plan.
       BTP service binding 注入的 ``credentials.uri`` 已经是带密码 / scheme 的
       完整 URL (``redis://`` 或 ``rediss://``), 我们直接吃即可.
    3. ``VCAP_SERVICES["redis-cache"][0].credentials.uri`` — 老版 plan, 同理.
    4. 都没有 → 返 ``None``, 调用方按 in-memory fallback.

    Returns:
        Redis 连接 URL (``redis://...`` 或 ``rediss://...``), 或 ``None`` (无可用 Redis).

    Note:
        某些老 plan ``credentials`` 里**没有** ``uri`` 字段, 只有
        ``hostname`` / ``port`` / ``password``, 需要自己拼. 实践中现在
        marketplace 上的 plan 基本都给 ``uri``, 我们暂不处理这个少数派 — 真
        撞上 (启动期日志会打 "redis service bound but no 'uri' field") 再
        加 fallback 拼装.
    """
    env_url = os.environ.get("REDIS_URL")
    if env_url:
        return env_url

    for service_type in _REDIS_SERVICE_TYPES:
        creds = find_service_binding(service_type)
        if creds is None:
            continue
        uri = creds.get("uri")
        if isinstance(uri, str) and uri:
            return uri
        # 字段缺失 / 形态意外, 打个 warning 让人能定位是 binding 配错还是
        # 真没绑.
        logger.warning(
            "redis service %r bound but credentials.uri missing or non-string; "
            "keys=%s — falling back to next candidate.",
            service_type,
            list(creds.keys()),
        )
    return None
