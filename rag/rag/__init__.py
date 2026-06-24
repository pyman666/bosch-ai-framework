"""``bapee.rag`` — 通用 hybrid RAG 实现层 (无业务).

设计目标: 把 deterministic lookup + BM25 + Dense + RRF + optional rerank +
LiteLLM 调用 这套 RAG pipeline 提炼成业务无关的库, 通过 :class:`HybridPipelineConfig`
注入业务定制点. **一个新项目要接入只需要写一个 ~50 行的 binding 文件**,
不用 fork 整套 pipeline.

复用到新项目的姿势:

.. code-block:: python

    # myproject/pipeline.py — 你的 binding 层
    from rag.rag import HybridPipeline, HybridPipelineConfig, PromptTemplates
    from rag.core.llm import build_router    # 或自己用 LiteLLM Router(...) 构造

    router = build_router(model_list=[...])

    _pipeline = HybridPipeline(HybridPipelineConfig(
        docs_dir=Path("kb/"),
        outline_path=Path("kb/ast/index.json"),
        system_prompt="...你的助手身份...",
        router=router,
        default_model="gpt-4o-mini",
        url_head_to_module={"/api/foo": "foo", ...},  # 可空
        search_priority_keys=("status", "errorCode"),  # 可空
        templates=PromptTemplates(prompt_head_tmpl="..."),  # 业务话术
    ))

    async def ask(url, payload, q): return await _pipeline.ask(url, payload, q)

构造期一次性 ingest, 请求期跑五步 (filter → lookup → retrieve → organize → LLM).
现成的 BPAE binding 见 :mod:`bapee.chatbot.bpae_pipeline`, 可当模板抄.

公开 API:

- :class:`HybridPipeline`         — 入口类, ``ask`` / ``chat`` 流式或整段
- :class:`HybridPipelineConfig`   — 所有可配点
- :class:`PromptTemplates`        — prompt 措辞 (默认中性, 业务可覆盖)
- :class:`MarkdownKbConvention`   — markdown KB 目录布局约定 (默认 BPAE 风格)
- :class:`Chunk`                  — 检索 chunk 数据模型 (一般不用直接构造)
- :class:`LookupHit`              — deterministic 命中的描述

离线 ingestion 工具:

- :mod:`.tools.build_kb_ast` — markdown → ``*.chunks.jsonl`` (用 ``python -m`` 跑)

边界约束: ``rag`` 内部任何模块**不得** import ``bapee.settings`` /
``bapee.chatbot.*`` / ``bapee.core.*`` 等项目内其他模块. 想换
业务定制只能通过 config 字段, 不能写死. 这条边界让
``git filter-repo --path bapee/rag`` 可以一键拆成独立包.

特别提醒: LLM Router 由调用方构造 (e.g. 用 ``bapee.core.llm.build_router``)
后通过 ``HybridPipelineConfig.router`` 注入, ``rag`` 只消费
``router.acompletion(...)`` 接口, 不持有构造责任.
"""
from .corpus import Chunk  # noqa: F401
from .markdown_kb import (  # noqa: F401
    DEFAULT_CONVENTION,
    MarkdownKbConvention,
    classify_markdown,
    split_h2,
)
from .organize import (  # noqa: F401
    DEFAULT_KIND_DESC,
    DEFAULT_LAYER_DESC,
    DEFAULT_NO_USER_QUESTION_FALLBACK,
    DEFAULT_OUTLINE_HEADER,
    DEFAULT_PROMPT_HEAD_TMPL,
    PromptTemplates,
)
from .pipeline import HybridPipeline, HybridPipelineConfig  # noqa: F401
from .retrieve_lookup import LookupHit  # noqa: F401

