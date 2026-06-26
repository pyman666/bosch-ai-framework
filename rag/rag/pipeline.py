"""(编排) 通用 hybrid RAG pipeline 的对外入口.

业务侧的典型用法:

    from infra.llm import get_router
    from rag.rag import HybridPipeline, HybridPipelineConfig, PromptTemplates

    router = get_router()

    config = HybridPipelineConfig(
        docs_dir=Path("docs"),
        outline_path=Path("docs/ast/index.json"),
        system_prompt="...你的业务身份描述...",
        router=router,                         # 由调用方构造好
        default_model="gpt-4o-mini",
        url_head_to_module={"billing": "billing", ...},        # 业务路由前缀映射
        search_priority_keys=("processRemark", "processStatus", ...),
        lookup_remark_payload_keys=("processremark", "remark", ...),
        lookup_code_payload_keys=("errorcode", "code", ...),
        lookup_status_payload_keys=("processstatus", "status", ...),
        templates=PromptTemplates(...),        # 可选, 用业务语境的措辞覆盖默认
    )
    pipeline = HybridPipeline(config)

    async for chunk in pipeline.ask("/foo/bar", {"errorCode": "5013"}, "为啥失败"):
        print(chunk, end="")

构造期 (``HybridPipeline(config)``) 一次性建好三块:
  - chunks: 从 ``docs_dir`` 装载
  - LookupIndex: deterministic 查表 (Layer 1)
  - HybridRetriever: BM25 + Dense + 可选 rerank (Layer 2)
  - outline + system prompt 拼成 _system_prompt_full

请求期 (``pipeline.ask/chat/...``) 跑五步: filter → lookup → retrieve →
organize → LLM 调用. 五步逻辑全部不依赖业务, 业务定制点 100% 通过 config 注入.

LLM Router 由调用方传入 — pipeline **消费** ``router.acompletion(...)`` 接口
但不持有"怎么构造 Router"的责任. 这条边界让 ``rag`` 跟 LiteLLM 解耦得更
干净, 真到独立成包那天, 调用方用任何 LiteLLM-compatible 对象 (含 mock) 都行.

只读 + 线程安全: 单实例可被多 async request handler 并发调用. 想热加载 (改了
docs/ 之后不重启就生效) 见 README 的 "hot reload" 一节, 通用层不内置.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from litellm import Router

from .corpus import Chunk, load_chunks
from .filter import build_search_query, infer_module_from_url
from .markdown_kb import DEFAULT_CONVENTION, MarkdownKbConvention
from .organize import PromptTemplates, build_outline_text, render_user_message
from .retrieve_hybrid import HybridRetriever
from .retrieve_lookup import LookupIndex


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class HybridPipelineConfig:
    """通用 hybrid RAG pipeline 的所有可配点 — 业务在这里注入特定逻辑.

    分四组:

    1. **数据 / LLM / 身份**: ``docs_dir`` / ``outline_path`` / ``system_prompt`` /
       ``router`` / ``default_model`` — 必填项, 没有通用默认值. ``router`` 由
       调用方自己构造好传进来 (e.g. 用 :func:`bapee.core.llm.build_router`), pipeline
       只消费 ``router.acompletion(...)`` 接口.
    2. **检索旋钮**: ``top_k`` / ``candidate_pool`` / ``enable_rerank`` /
       ``embed_model_name`` / ``reranker_name`` — 有合理默认, 大多数业务不用动.
    3. **业务字段映射**: ``url_head_to_module`` /  ``search_priority_keys`` /
       ``lookup_*_payload_keys`` — 全是业务约定 (URL 前缀 / payload 字段命名),
       不传 = 不启用对应特性, pipeline 仍能跑, 只是退化成"全模块兜底 +
       仅路由 lookup + 自然顺序 query".
    4. **Prompt 措辞**: ``templates`` — 默认是中性版本, 业务可以换成自己业务
       语境的写法 (e.g. "BPAE 知识库" / "联系运营团队" 等)
    """

    # --- 必填: 数据 / LLM / 身份 ---
    docs_dir: Path
    system_prompt: str
    default_model: str
    router: Router

    # --- 可选: 数据补充 ---
    outline_path: Path | None = None  # 没有就跳过 outline

    # --- 检索旋钮 (有合理默认) ---
    top_k: int = 8
    candidate_pool: int = 30
    enable_rerank: bool = False
    embed_model_name: str = "all-MiniLM-L6-v2"
    reranker_name: str = "BAAI/bge-reranker-base"

    # --- 业务字段映射 (空 = 不启用) ---
    url_head_to_module: dict[str, str] = field(default_factory=dict)
    search_priority_keys: tuple[str, ...] = ()
    lookup_remark_payload_keys: tuple[str, ...] = ()
    lookup_code_payload_keys: tuple[str, ...] = ()
    lookup_status_payload_keys: tuple[str, ...] = ()

    # --- markdown KB 目录布局约定 (新项目要换 doc/ table/ 命名时改这个) ---
    markdown_convention: MarkdownKbConvention = field(default_factory=lambda: DEFAULT_CONVENTION)

    # --- Prompt 措辞 (默认中性) ---
    templates: PromptTemplates = field(default_factory=PromptTemplates)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class HybridPipeline:
    """启动期一次性 ingest, 请求期跑五步 + LLM 调用.

    构造一个实例就是构造一个完整的 RAG 服务后端. 想给同一个进程接多个 KB /
    多种业务语境, 各 new 一个 :class:`HybridPipeline` 即可, 彼此完全隔离.
    """

    def __init__(self, config: HybridPipelineConfig) -> None:
        self._config = config

        # ---- 启动期一次性构建 (数据 + 两层检索索引 + system outline) ----
        self._chunks: list[Chunk] = load_chunks(
            config.docs_dir, convention=config.markdown_convention
        )
        if not self._chunks:
            raise RuntimeError(
                f"hybrid pipeline 启动失败: {config.docs_dir} 下没有可加载的 chunk. "
                "确认 docs/ast/*.chunks.jsonl 存在 (或在 docs/doc/, docs/table/ 下放 markdown)."
            )

        self._lookup = LookupIndex(
            self._chunks,
            remark_payload_keys=config.lookup_remark_payload_keys,
            code_payload_keys=config.lookup_code_payload_keys,
            status_payload_keys=config.lookup_status_payload_keys,
        )
        self._retriever = HybridRetriever(
            self._chunks,
            embed_model_name=config.embed_model_name,
            enable_rerank=config.enable_rerank,
            reranker_name=config.reranker_name,
        )

        outline_text = ""
        if config.outline_path is not None:
            outline_text = build_outline_text(
                config.outline_path,
                header=config.templates.outline_header,
            )
        self._system_prompt_full: str = (
            config.system_prompt + ("\n\n" + outline_text if outline_text else "")
        )

        self._router = config.router

        logger.info(
            "hybrid pipeline ready: %d chunks (lookup: %d routes / %d remarks / "
            "%d codes / %d statuses); rerank=%s",
            len(self._chunks),
            len(self._lookup.routes),
            len(self._lookup.remarks),
            len(self._lookup.codes),
            len(self._lookup.statuses),
            config.enable_rerank,
        )

    # -----------------------------------------------------------------
    # 请求期: 过滤 → 检索 Layer 1 → 检索 Layer 2 → 组织 prompt
    # -----------------------------------------------------------------

    def _build_messages(
        self,
        route_url: str,
        payload: dict[str, Any],
        user_question: str | None,
    ) -> list[dict[str, str]]:
        """串完整个五步管线, 返回最终给 LLM 的 messages (单轮形态).

        ``ask`` (流式) 和 ``ask_text`` (整段) 共用本方法, 保证两条路径检索/拼接
        逻辑严格一致, 只在 LLM 调用 stream 与否上分叉.

        检索阶段总耗时记到 ``rag.retrieve.done`` 一条 info 日志 (``retrieve_ms``
        字段), 配合上层 routes 的 ``latency_ms`` (= retrieve + LLM 总和), 监
        控就能分别看 "检索慢了" vs "LLM 慢了". 跨语言聚合用 ``request_id``
        关联到 ``ask.done`` / ``chat.done``.
        """
        payload = payload or {}
        cfg = self._config
        started = time.monotonic()

        # 过滤: URL + payload → query string + module hint
        search_query = build_search_query(
            route_url,
            payload,
            user_question,
            priority_keys=cfg.search_priority_keys,
        )
        module_filter = infer_module_from_url(route_url, cfg.url_head_to_module)

        # Layer 1: deterministic lookup
        det_hits = self._lookup.find_deterministic_hits(route_url, payload)
        excluded_ids = {h.chunk.chunk_id for h in det_hits}

        # Layer 2: hybrid retrieve
        retrieved = self._retriever.retrieve(
            search_query,
            top_k=cfg.top_k,
            candidate_pool=cfg.candidate_pool,
            module_filter=module_filter,
            exclude_chunk_ids=excluded_ids,
        )

        # 组织
        user_msg = render_user_message(
            det_hits,
            retrieved,
            route_url,
            payload,
            user_question,
            templates=cfg.templates,
        )

        logger.info(
            "rag.retrieve.done",
            extra={
                "retrieve_ms": int((time.monotonic() - started) * 1000),
                "deterministic_hits": len(det_hits),
                "retrieved_count": len(retrieved),
                "module_filter": module_filter or "-",
                "user_msg_chars": len(user_msg),
            },
        )

        return [
            {"role": "system", "content": self._system_prompt_full},
            {"role": "user", "content": user_msg},
        ]

    def _build_chat_messages(
        self,
        route_url: str,
        payload: dict[str, Any],
        history: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """多轮版本的 :meth:`_build_messages`.

        ``history`` 是调用方整段传入的对话历史 (user / assistant 交替, 末条必
        须是 user). 取末条 user 文本作为"当前这一问", 走完整 RAG 渲染成带
        检索结果的 user message; 之前轮次原样塞到 system 和这条渲染后的 user
        之间. 历史轮次**不重做检索 / 不重新注入当时的 RAG 结果**, 因为:

        - 每轮重检索会让 history 里 user 消息体积爆炸, prefix cache 失效;
        - 历史 assistant 是基于"当时那次检索"答的, 此时再换一份检索结果硬塞,
          可能跟历史回答自相矛盾, 反而更难纠错.

        协议: **历史保持纯文本回放, 只在末轮注入 fresh RAG**.
        """
        if not history:
            raise ValueError("history 不能为空")
        if history[-1].get("role") != "user":
            raise ValueError("history 末条必须是 role=user")

        latest_user_text = history[-1].get("content") or ""
        prior = history[:-1]

        base = self._build_messages(
            route_url,
            payload,
            latest_user_text if latest_user_text else None,
        )
        return [base[0], *prior, base[1]]

    # -----------------------------------------------------------------
    # LLM 调用 — 流式 / 整段
    # -----------------------------------------------------------------

    async def _stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None,
    ) -> AsyncIterator[str]:
        """对 ``router.acompletion(stream=True)`` 的薄封装, 只 yield 文本增量.

        记两条性能日志, 拆开 LLM 调用各阶段供监控:

        - ``llm.request.done`` (info) — 发出 ``acompletion`` 到拿到 *第一片*
          的耗时 (``llm_request_ms``). 这一段是 "Post 请求 + 上游 LLM 排队
          + 上游开始返第一 token" 总和, 跟流式 UX 的 "首字符响应" 强相关.
        - ``llm.stream.done`` (info) — 第一片到流自然结束的总耗时 (``llm_stream_ms``)
          + token / 字符级吞吐. 流被外层主动取消 (客户掉线 / shutdown / timeout)
          时这条日志不会打, 是预期 — 外层 ``routes.py:_logged_stream`` 自己有
          收尾日志, 这里不重复.
        """
        chosen_model = model or self._config.default_model
        request_started = time.monotonic()
        resp = await self._router.acompletion(
            model=chosen_model,
            messages=messages,
            stream=True,
        )
        first_chunk_at: float | None = None
        chunk_count = 0
        total_chars = 0
        async for chunk in resp:
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            # LiteLLM 兼容 OpenAI chunk schema: ``choices[0].delta.content`` 是本片
            # 增量文本; 流末尾的 finish_reason chunk 没有 content, 直接跳过.
            content = getattr(delta, "content", None) if delta is not None else None
            if content:
                if first_chunk_at is None:
                    first_chunk_at = time.monotonic()
                    logger.info(
                        "llm.request.done",
                        extra={
                            "llm_request_ms": int(
                                (first_chunk_at - request_started) * 1000
                            ),
                            "model": chosen_model,
                        },
                    )
                chunk_count += 1
                total_chars += len(content)
                yield content
        # 流自然走完 (没被外层 break / aclose). 异常 / 客户取消 / shutdown
        # 都会跳过这条 — 那是预期, 外层 :func:`_logged_stream` 已经打了三态
        # 收尾日志.
        if first_chunk_at is not None:
            logger.info(
                "llm.stream.done",
                extra={
                    "llm_stream_ms": int((time.monotonic() - first_chunk_at) * 1000),
                    "chunk_count": chunk_count,
                    "total_chars": total_chars,
                    "model": chosen_model,
                },
            )

    # -----------------------------------------------------------------
    # 公开 API: 单轮 ask / 多轮 chat — 各两个 (流式 + 整段)
    # -----------------------------------------------------------------

    async def ask(
        self,
        route_url: str,
        payload: dict[str, Any],
        user_question: str | None = None,
        *,
        model: str | None = None,
    ) -> AsyncIterator[str]:
        """单轮入口 — 流式. 不带对话历史, 想要多轮请用 :meth:`chat`."""
        messages = self._build_messages(route_url, payload, user_question)
        async for c in self._stream(messages, model=model):
            yield c

    async def ask_text(
        self,
        route_url: str,
        payload: dict[str, Any],
        user_question: str | None = None,
        *,
        model: str | None = None,
    ) -> str:
        """非流式版本 — 一次性拿整段回答 (单轮)."""
        parts: list[str] = []
        async for c in self.ask(route_url, payload, user_question, model=model):
            parts.append(c)
        return "".join(parts)

    async def chat(
        self,
        route_url: str,
        payload: dict[str, Any],
        history: list[dict[str, str]],
        *,
        model: str | None = None,
    ) -> AsyncIterator[str]:
        """多轮入口 — 流式. 服务端 stateless, 每轮只对末条 user 重新检索 + 渲染."""
        messages = self._build_chat_messages(route_url, payload, history)
        # 监控真实使用形态 (轮次分布 / 长尾客户); 数 user 而不是数 history
        # 长度, 是因为后者会被 prior + 当前 user 混在一起, user 数才是真正的
        # "问了几个问题".
        user_turn_count = sum(1 for m in history if m.get("role") == "user")
        logger.info(
            "hybrid_pipeline.chat turn=%d (history_len=%d, route_url=%s)",
            user_turn_count,
            len(history),
            route_url,
        )
        async for c in self._stream(messages, model=model):
            yield c

    async def chat_text(
        self,
        route_url: str,
        payload: dict[str, Any],
        history: list[dict[str, str]],
        *,
        model: str | None = None,
    ) -> str:
        """非流式版本 — 一次性拿整段回答 (多轮)."""
        parts: list[str] = []
        async for c in self.chat(route_url, payload, history, model=model):
            parts.append(c)
        return "".join(parts)
