"""BPAE 项目对通用 :mod:`bapee.rag` 的业务黏合层.

定位: 把 BPAE 的"报错 / data 诊断"语境 (URL 前缀映射 / payload 字段命名 / prompt
措辞) 通过 :class:`~bapee.rag.HybridPipelineConfig` 注入到通用 pipeline,
对外暴露 ``ask_bot`` / ``chat_bot`` (及其 ``*_text`` 版本) 四个函数, 给
:mod:`bapee.chatbot.routes` 用.

这文件是 BPAE 业务跟 rag 唯一的接合点 — 改 BPAE 业务定制 (e.g. 新加一种
payload 字段触发 lookup, 或者调 prompt 措辞) 只动这里; 改通用 pipeline (e.g.
换 embedding 模型) 只动 ``rag/``.

历史: 2026-05 之前所有逻辑都在 ``bapee/chatbot/pipeline/hybrid.py`` 等几个文件
里, 业务跟通用 RAG 实现混在一起. 拆出 ``bapee/rag/`` 后这里只剩 ~80 行黏
合, 整体可测可换可重用. README 里有完整的拆分理由.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from ..rag import (
    HybridPipeline,
    HybridPipelineConfig,
    PromptTemplates,
)
from ..settings import (
    DEFAULT_MODEL,
    HYBRID_CANDIDATE_POOL,
    HYBRID_DOCS_DIR,
    HYBRID_ENABLE_RERANK,
    HYBRID_OUTLINE_PATH,
    HYBRID_TOP_K,
    SYSTEM_PROMPT,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BPAE 业务定制 — URL / payload 字段命名 + prompt 措辞
# ---------------------------------------------------------------------------

# URL 第一段 → 模块名 (用于 hybrid retrieval 的同模块软加权 boost ×1.5).
# 不在表里的前缀 (``/jit/...`` 之外的非标准前缀) 回 None, 让检索器全模块兜底.
_URL_HEAD_TO_MODULE: dict[str, str] = {
    "billing": "billing",
    "boct": "boct",
    "titletransfer": "boct",
    "forecast": "forecast",
    "newforecast": "forecast",
    "jit": "jitcall",
    "jitcall": "jitcall",
    "mmsl": "mmsl",
}

# build_search_query 时优先放最前的强信号字段, 顺序按"诊断价值"递减.
_SEARCH_PRIORITY_KEYS: tuple[str, ...] = (
    "processRemark",
    "processStatus",
    "messageType",
    "errorCode",
    "remark",
    "status",
)

# deterministic lookup: payload 里哪些字段触发哪类查表. 全 lower-case,
# rag 那一侧会做大小写无关命中.
_LOOKUP_REMARK_PAYLOAD_KEYS: tuple[str, ...] = (
    "processremark", "remark", "errormsg", "errormessage", "msg",
)
_LOOKUP_CODE_PAYLOAD_KEYS: tuple[str, ...] = (
    "errorcode", "code", "resultcode",
)
_LOOKUP_STATUS_PAYLOAD_KEYS: tuple[str, ...] = (
    "processstatus", "status", "matchstatus", "releasestatus",
    "podstatus", "messagetype",
)


# BPAE 业务语境的 prompt 措辞 — 跟通用 rag 的中性默认相比, 加了:
# - "BPAE 知识库" 等具体语境词
# - "客户向措辞 / 三态分流" 这种业务硬性要求
# - "联系 BPAE 运营团队" 等兜底动作
_BPAE_TEMPLATES = PromptTemplates(
    prompt_head_tmpl="""\
以下是从 BPAE 知识库中检索到的相关片段, 分两类:

1. **⭐ 确定性命中**: 请求里的 URL / processRemark / errorCode / Status 等字段直接命中
   KB 里对应索引项. 这是当前请求**最权威的事实依据**, 优先采用.
2. **🔍 hybrid 检索**: 用 query 做 BM25 + 语义检索拿到的相关章节, 按来源类型分组. 各类型在
   报错诊断中的角色见各组开头说明; 诊断时**以 `code` 类为准**, `doc` / `meta` 仅在客户
   明确问运营操作 / 元事实时使用.

3. 严格执行 system prompt 里的"客户向措辞 / 三态分流 / 末尾下一步"硬性要求.
""",
    no_user_question_fallback=(
        "(客户没有补充提问. 请基于上面的接口和 payload 主动诊断: 解释当前状态/"
        "报错原因, 并按 system prompt 的'三态分流 + 下一步'给客户向回复.)"
    ),
    outline_header=(
        "# 知识库全局目录 (供你判断'这个问题该去哪本 KB 哪一节找')\n"
        "下面列了所有可检索章节的标题. 这只是目录, **正文内容由每次请求时"
        "hybrid 检索后单独给出**. 看到客户问的事在某节标题下但本次检索没给到"
        "正文, 说'这条信息应该在 <模块> 的 <章节> 里, 我这次没检索到具体"
        "内容, 建议联系运营核对'."
    ),
    kind_desc={
        "code": (
            "**报错诊断的核心依据** — 后端 validation 逻辑 / 前端路由 / payload / "
            "错误码 / processRemark / Status 枚举. 客户问'为什么报错' / "
            "'这条数据为什么是这个状态'时, 优先以这部分为准."
        ),
        "doc": (
            "业务运营 SOP (各 OEM 怎么下载 / 拿到原始数据怎么清洗去重) — 偶发兜底, "
            "通常**不用于报错诊断**. 仅当客户明确在问运营操作步骤时再引用."
        ),
        "meta": (
            "项目级元信息 (项目介绍 / 上线日期表 / 调度任务时间表) — 极少用到, "
            "仅当客户问'这个客户什么时候上线' / '调度几点跑'这类元事实时使用."
        ),
    },
    layer_desc={
        "route": "URL 命中索引路由表 (Method+Path, 来自 AST) — 这是请求精确指向的接口规格",
        "remark": "payload 里的报错文案命中 KB 的 processRemark / Message 表 — 这是这条报错的官方解释",
        "errorCode": "payload 里的错误码命中 KB 的 ResultCode 表 — 这是该 code 的标准说明",
        "status": "payload 里的状态值命中 KB 的 Status 枚举字典 — 这是该枚举值的标准含义",
    },
)


# ---------------------------------------------------------------------------
# 启动期: 构造单例 pipeline (lazy)
# ---------------------------------------------------------------------------
#
# 历史上这里是模块级直接构造的 (``_pipeline = HybridPipeline(...)``), 后果是
# 任何 import (含 pytest collect / sphinx-build / mypy) 都会触发:
#   - 加载 ~80MB sentence-transformers 模型 (首次还要从 HuggingFace 下载)
#   - 构建 FAISS index (秒级 CPU)
#   - 解析全量 AST jsonl
# 多 worker 部署时构造时间又乘以 worker 数, 任何一步炸了 traceback 还埋在
# import 链里. 改成 :func:`init` 显式调用 + FastAPI ``lifespan`` 在启动期触发:
# 启动失败立刻看见, 测试 / 工具脚本 import 时零成本.

_pipeline: HybridPipeline | None = None


def _build_router_auto():
    """构造 LLM Router — 统一走 infra.llm."""
    from infra.llm import get_router
    logger.info("llm router: infra.llm")
    return get_router()


def init() -> HybridPipeline:
    """构造 (或返回已构造的) BPAE pipeline 单例.

    幂等 — 重复调用第二次起秒回. 由 :mod:`bapee.server` 的 ``lifespan`` 在
    应用启动期调一次; 测试里可直接 ``monkeypatch`` 模块级 ``_pipeline`` 跳过.
    """
    global _pipeline
    if _pipeline is not None:
        return _pipeline
    router = _build_router_auto()
    _pipeline = HybridPipeline(
        HybridPipelineConfig(
            docs_dir=HYBRID_DOCS_DIR,
            outline_path=HYBRID_OUTLINE_PATH,
            system_prompt=SYSTEM_PROMPT,
            default_model=DEFAULT_MODEL,
            router=router,
            top_k=HYBRID_TOP_K,
            candidate_pool=HYBRID_CANDIDATE_POOL,
            enable_rerank=HYBRID_ENABLE_RERANK,
            url_head_to_module=_URL_HEAD_TO_MODULE,
            search_priority_keys=_SEARCH_PRIORITY_KEYS,
            lookup_remark_payload_keys=_LOOKUP_REMARK_PAYLOAD_KEYS,
            lookup_code_payload_keys=_LOOKUP_CODE_PAYLOAD_KEYS,
            lookup_status_payload_keys=_LOOKUP_STATUS_PAYLOAD_KEYS,
            templates=_BPAE_TEMPLATES,
        )
    )
    logger.info(
        "pipeline constructed",
        extra={
            "docs_dir": str(HYBRID_DOCS_DIR),
            "top_k": HYBRID_TOP_K,
            "candidate_pool": HYBRID_CANDIDATE_POOL,
            "default_model": DEFAULT_MODEL,
        },
    )
    return _pipeline


def is_ready() -> bool:
    """pipeline 是否已构造 — 给 readiness probe 用."""
    return _pipeline is not None


# ---------------------------------------------------------------------------
# 公开 API — 薄 wrapper, 把 BPAE 的 settings 跟 pipeline 缝在一起
# ---------------------------------------------------------------------------

async def ask_bot(
    route_url: str,
    payload: dict[str, Any],
    user_question: str = "",
    *,
    model: str | None = None,
) -> AsyncIterator[str]:
    pipeline = _pipeline
    if pipeline is None:
        raise RuntimeError("pipeline not initialized — call init() first")
    async for chunk in pipeline.ask(route_url, payload, user_question, model=model):
        yield chunk


async def ask_bot_text(
    route_url: str,
    payload: dict[str, Any],
    user_question: str = "",
    *,
    model: str | None = None,
) -> str:
    pipeline = _pipeline
    if pipeline is None:
        raise RuntimeError("pipeline not initialized — call init() first")
    return await pipeline.ask_text(route_url, payload, user_question, model=model)


async def chat_bot(
    route_url: str,
    payload: dict[str, Any],
    history: list[dict[str, str]],
    *,
    model: str | None = None,
) -> AsyncIterator[str]:
    pipeline = _pipeline
    if pipeline is None:
        raise RuntimeError("pipeline not initialized — call init() first")
    async for chunk in pipeline.chat(route_url, payload, history, model=model):
        yield chunk


async def chat_bot_text(
    route_url: str,
    payload: dict[str, Any],
    history: list[dict[str, str]],
    *,
    model: str | None = None,
) -> str:
    pipeline = _pipeline
    if pipeline is None:
        raise RuntimeError("pipeline not initialized — call init() first")
    return await pipeline.chat_text(route_url, payload, history, model=model)
