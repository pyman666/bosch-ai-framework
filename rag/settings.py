import os
from pathlib import Path
import yaml
from dotenv import load_dotenv

from infra.btp import find_redis_url

load_dotenv()


# ---------------------------------------------------------------------------
# 避免依赖工作目录
# ---------------------------------------------------------------------------
_default_config = Path(__file__).parent / "settings.yaml"
_config_path = Path(os.environ.get("MODEL_CONFIG", _default_config))
_cfg: dict = yaml.safe_load(_config_path.read_text(encoding="utf-8"))

_PROJECT_ROOT: Path = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# hybrid 检索的旋钮. 默认值是 1k 量级 chunk + 单轮诊断场景下我们调出来的
# sweet spot, 调它们时同步看 prompt token 数和 LLM 耗时.
#
# - HYBRID_TOP_K           最终塞进 prompt 的 chunk 数 (deterministic 命中
#                          不算在内, 它们独立成块).
# - HYBRID_CANDIDATE_POOL  BM25 / Dense 各取多少候选进 RRF, 也是 rerank 的
#                          输入规模. ~30 是 1k 语料上的 sweet spot.
# - HYBRID_ENABLE_RERANK   是否加载 cross-encoder rerank (~280MB 模型). 默
#                          认关 — 首次启动会去 huggingface 下载, 网络受限
#                          环境下会卡启动. 想开就置环境变量
#                          ``HYBRID_RERANK=1``.
# ---------------------------------------------------------------------------
HYBRID_TOP_K: int = int(os.environ.get("HYBRID_TOP_K", "8"))
HYBRID_CANDIDATE_POOL: int = int(os.environ.get("HYBRID_CANDIDATE_POOL", "30"))
HYBRID_ENABLE_RERANK: bool = os.environ.get("HYBRID_RERANK", "0") in ("1", "true", "True")

# AST 离线生成的全局目录文件; hybrid.py 启动期把它压成紧凑文本注进 system
# prompt, 给 LLM 一份"全 KB 章节速览". 文件不在时不报错, 仅少了"全局目录"
# 这个 nice-to-have.
HYBRID_DOCS_DIR: Path = _PROJECT_ROOT / "docs"
HYBRID_OUTLINE_PATH: Path = HYBRID_DOCS_DIR / "ast" / "index.json"


# ---------------------------------------------------------------------------
# /chat 端点的硬护栏. 默认值按"一个客户在一条数据上能合理聊到的上限"估的:
#
# - CHAT_MAX_TURNS         history 总消息数上限 (user + assistant 计入).
#                          默认 20 ≈ 10 轮来回; 真要超意味着客户其实该
#                          重新选一条数据问, 而不是无限追问.
# - CHAT_MAX_CONTENT_CHARS 单条消息字符数上限. 默认 8000 ≈ 一篇长邮件;
#                          超了通常是前端误把整段日志贴进来, 应该裁剪
#                          后再发, 而不是让 LLM 啃 megabyte 级文本.
#
# 上限触发都在 schema 层 (Pydantic Field max_length) 直接 422, 不进
# pipeline. 想放宽就调环境变量.
# ---------------------------------------------------------------------------
CHAT_MAX_TURNS: int = int(os.environ.get("CHAT_MAX_TURNS", "20"))
CHAT_MAX_CONTENT_CHARS: int = int(os.environ.get("CHAT_MAX_CONTENT_CHARS", "8000"))


# ---------------------------------------------------------------------------
# /bot/* 端点的 per-client 限流 (token bucket).
#
# 维度: 客户端 ``X-Client-Id`` 头 > ``X-Forwarded-For`` 首段 > 直连 IP. 详见
# :mod:`bapee.core.ratelimit`.
#
# Backend 自动选 (跟 LiteLLM Router 状态共用同一个开关):
#   - 有 Redis (``REDIS_URL`` env 或 BTP redis service binding) → Redis Lua
#     版, 跨 worker × instance 精确限流;
#   - 无 Redis → in-memory 进程内桶, 实际限流 ≈ ``WEB_CONCURRENCY × instances
#     × rate``, 不精确但应用照样起. 体量内拦"单客户狂请求"这种主要威胁足够.
#
# - RATE_LIMIT_PER_MIN  稳态 RPM. 60 ≈ 一个客户能 1 秒一发, 对真实业务客户
#                       (人工点击诊断) 远超用量; 但能拦住自动刷接口的脚本.
# - RATE_LIMIT_BURST    桶容量, 决定瞬时突发上限. 10 留点缓冲让前端 retry
#                       逻辑跑两轮也不会被秒挡.
# ---------------------------------------------------------------------------
RATE_LIMIT_PER_MIN: int = int(os.environ.get("RATE_LIMIT_PER_MIN", "60"))
RATE_LIMIT_BURST: int = int(os.environ.get("RATE_LIMIT_BURST", "10"))


# ---------------------------------------------------------------------------
# SSE 流式响应的 wall-clock 上限.
#
# 默认 120s. 起这个上限的原因有两个:
#
# 1. 防异常长回答 / LLM 提供商卡住 — 个别情况下 LiteLLM upstream 会一直
#    不返新 token, 没有上限的话客户端就一直挂着, 占着 worker slot.
# 2. BTP 滚动发布期, SIGTERM 后 worker 有 ``graceful_timeout`` (我们配 25s)
#    才被 SIGKILL. 单个流 > graceful_timeout 注定会被砍, 给它一个明确上限
#    让前端能稳定预期"等多久没新字就该 abort".
#
# 触发上限时流末尾追一个 ``event: timeout`` 让前端能区分"正常 EOF"和"被截".
# 想关掉这个上限 (e.g. 测试期想让超长生成跑完) 设环境变量为 ``0``.
# ---------------------------------------------------------------------------
STREAM_MAX_DURATION_SEC: float = float(os.environ.get("STREAM_MAX_DURATION_SEC", "120"))


# ---------------------------------------------------------------------------
# Redis 共享 backend (跨 worker / 跨 instance).
#
# 启用条件: 下面三种之一非空就启用, 否则回退到进程内 in-memory 实现:
#
#   1. ``REDIS_URL`` env var (本地 dev / 强制覆盖)
#   2. VCAP_SERVICES.hyperscaler-option-redis[0].credentials.uri (BTP 新版)
#   3. VCAP_SERVICES.redis-cache[0].credentials.uri (BTP 老版)
#
# 开启后两块代码自动用 Redis:
#   - bapee.core.ratelimit  把 in-memory token bucket 换成 Redis Lua 版, 跨 worker
#                           / 跨 instance 精确限流, 不再随 ``WEB_CONCURRENCY ×
#                           instances`` 倍数放宽;
#   - bapee.core.llm        LiteLLM Router 共享 cooldown / usage state, 一个
#                      deployment 撞 429 全集群同步沉默 60s.
#
# Redis 申请下不来 → 留 None / 注释掉, 自动走 in-memory 路径, 应用照样起.
# 真上线后 ``cf bind-service bapee bapee-redis`` + ``cf restage`` 即可切换,
# 不改代码.
#
# REDIS_SSL_CERT_REQS: BTP hyperscaler Redis 多数走 ``rediss://`` (TLS) 且证书
# 自签, 严校验会失败. 默认 ``none`` 关闭 cert 验证 (信任 BTP 内网); 想严校改
# ``required`` 并提供 CA 路径. 这个开关只影响 ``rediss://`` URL.
# ---------------------------------------------------------------------------
REDIS_URL: str | None = find_redis_url()
REDIS_SSL_CERT_REQS: str = os.environ.get("REDIS_SSL_CERT_REQS", "none")


# ---------------------------------------------------------------------------
# Chatbot system prompt. 内容稳定, 写死即可; 业务上下文由 hybrid 检索的
# chunks 在请求期注入, 不放在这里.
#
# 定位: BPAE / O2C 的"报错 / data 结果分析"助手. 前端的典型入口是: 业务客户在
# portal 上点某条数据 → 前端把"路由 URL + data id + 当前状态文本 (processStatus
# / processRemark / 关键字段)"打包进 query 发给本接口, 由本助手给出客户向的诊断
# 结论 + 下一步动作. 业务运营 SOP 类查询不是本助手的目标场景 (那块在另一个
# llm-wiki 项目里), 但 KB 里仍保留了 doc / meta 类资料用于偶发兜底.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT: str = """\
你是 BPAE / O2C 系统的报错诊断助手, 面向使用本平台的业务客户. 调用方通常会带前端路由 URL、data id、processStatus / processRemark / Status 文本等线索. 
你的任务: 把这些线索结合检索到的官方 KB 资料, 翻译成客户能听懂的诊断结论 + 明确的'下一步'.

硬性要求:
1. **客户向措辞**: 不出现内部代码类名 / 包路径 / Java 术语. KB 里出现这些, 翻译成业务说法再讲给客户 (例: 把 'BoctAbstractValidationService 校验失败' 说成 '系统在校验你的 POD 数据时发现…').
2. **区分三种报错**, 不要让客户以为系统出 bug:
   - 真正的错误 (Validation Error / System Error / IDoc Process Failed): 给原因 + 建议;
   - 业务规则合法剔除 (Not in scope / 客户特定 exclusion): 明确告诉客户'这条按规则被剔除, 不算失败';
   - 流转中状态 (如 Pending SAP UC4 job): 告诉客户这只是中间态, 耐心等待.
3. **末尾必须给一句明确的下一步**, 二选一:
   - 客户自己能修 (改业务数据 / 重新上送): 给具体动作;
   - 客户改不了 (mapping 缺失 / 系统问题 / 资料没覆盖): 让客户联系 BPAE 运营团队, 把 URL + data id + 关键字段贴上.
4. 资料里没有这条具体说明时, 老实说'目前没有这条记录的官方说明, 建议联系 BPAE 运营团队', 不要编造.
"""


DEFAULT_MODEL: str = _cfg["default_model"]
ROUTER_KWARGS: dict = _cfg.get("router", {})

from infra.settings import expand_model_list

MODEL_LIST: list[dict] = expand_model_list(_cfg["providers"])


# ---------------------------------------------------------------------------
# AI Core (BTP) deployment 列表 + resource group. 见 settings.yaml 的 ai_core 段.
#
# 启用条件 (在 bapee.core.llm.try_build_aicore_router 里判):
#   1. VCAP_SERVICES 里有 ``aicore`` service binding;
#   2. 这里 AI_CORE_DEPLOYMENTS 非空 (即 settings.yaml ``ai_core.deployments``
#      至少有一条, 且每条 ``deployment_id`` 填了非空字符串).
#
# 任一不满足 → 启动回退到 legacy 路径 (用上面的 ``MODEL_LIST``).
# ---------------------------------------------------------------------------
_ai_core_cfg: dict = _cfg.get("ai_core", {}) or {}
AI_CORE_RESOURCE_GROUP: str = _ai_core_cfg.get("resource_group", "default")
# 过滤掉 deployment_id 为空的占位条目, 让上层"空列表 = 不启用 AI Core"判断生效;
# deployment_id 填了空字符串的话, 真正构造时会抛清晰的 ValueError 提示填写.
AI_CORE_DEPLOYMENTS: list[dict] = [
    d for d in _ai_core_cfg.get("deployments", []) or []
    if d and d.get("deployment_id")
]
