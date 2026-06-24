"""(组织) 把检索结果 + 请求场景 + 全局目录 渲染成给 LLM 的 prompt 文本.

负责两件互相独立的渲染:

1. **启动期**: :func:`build_outline_text` 读 AST ``index.json``, 压成 ~5KB 的全
   模块章节速览, 由 :mod:`.pipeline` 拼到 system prompt 后面. outline 注 system
   prompt 而不是每次请求重发是因为它**不随 query 变**, 让 LLM provider 端的
   prefix cache 一直命中, token 成本几乎为零.

2. **每次请求**: :func:`render_user_message` 把 Layer 1 hits + Layer 2 retrieved
   chunks + 请求场景串成一条 user message. 拼装顺序固定:
     - ⭐ 确定性命中 (按 route / errorCode / status / remark 分块)
     - 🔍 hybrid 检索 (按 kind=code/doc/meta 分组)
     - 请求场景 (URL + payload + 用户问题)

   每块带一段"使用说明"的元描述, 让 LLM 一眼看清"哪部分是权威, 哪部分是
   近似, 哪部分该优先用".

所有 prompt 措辞 (模板 / 来源描述 / 兜底文案) 都集中在 :class:`PromptTemplates`
里. 通用层只提供"占位 placeholder 形状", 业务层在自己的项目里实例化
``PromptTemplates`` 时把措辞调成自己业务语境的写法.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .corpus import Chunk
from .retrieve_lookup import LookupHit


# ---------------------------------------------------------------------------
# Prompt 模板 — 业务通过 PromptTemplates 注入
# ---------------------------------------------------------------------------

#: ``prompt_head_tmpl`` 必须含 ``{docs}`` placeholder, 那里会被填入渲染好的
#: 检索结果. 默认模板是中性版本; 业务可以在自己的项目里换成有业务上下文的版本.
DEFAULT_PROMPT_HEAD_TMPL = """\
以下是从知识库中检索到的相关片段, 分两类:

1. **⭐ 确定性命中**: 请求里的字面 key 直接命中 KB 里对应索引项. 这是当前请求
   **最权威的事实依据**, 优先采用.
2. **🔍 hybrid 检索**: 用 query 做 BM25 + 语义检索拿到的相关章节, 按来源类型分组.
   各类型在回答中的角色见各组开头说明.

{docs}

注意:

1. 基于上面的片段事实作答; 资料里没有这条具体说明时, **明确告诉用户"目前
   没有这条记录的官方说明"**, 不要编造.
2. **不要在回答里引用知识库的内部标题或来源标签**, 用户看不懂.
"""

#: 用户没补充提问时, 用这段填到 prompt 里替代空字符串, 给 LLM 一个明确指令.
DEFAULT_NO_USER_QUESTION_FALLBACK = (
    "(用户没有补充提问. 请基于上面的接口和 payload 主动给出诊断结论 + 下一步.)"
)

#: outline 段头部说明, 接在 ``build_outline_text`` 返回值前. 描述 outline 在
#: prompt 里扮演的角色.
DEFAULT_OUTLINE_HEADER = (
    "# 知识库全局目录\n"
    "下面列了所有可检索章节的标题. 这只是目录, **正文内容由每次请求时通过"
    " hybrid 检索单独给出**. 用户问的事在某节标题下但本次检索没给到正文时, "
    "明确告诉用户'这条信息应该在 <模块> 的 <章节> 里, 我这次没检索到具体"
    "内容', 不要编造."
)

#: 各 chunk ``kind`` 在 prompt 里的角色说明. 通用三分法 ``code`` / ``doc`` /
#: ``meta``, 业务可以替换具体描述文字. 不在表里的 kind 会回落到"无描述".
DEFAULT_KIND_DESC: dict[str, str] = {
    "code": "**回答的核心依据** — 主代码 KB, 权威事实. 客户问'为什么'/'是什么'时优先以这部分为准.",
    "doc": "运营 SOP — 偶发兜底, 通常不用于事实诊断. 仅当用户明确在问操作步骤时再引用.",
    "meta": "项目级元信息 (项目介绍 / 调度任务时间表 / 上线日期表等) — 极少用到, 仅当用户问元事实时使用.",
}

#: deterministic lookup 各 layer 的来源说明. 这四个 layer (route / remark /
#: errorCode / status) 是 LookupIndex 写死的, 业务一般只需要替换描述文字.
DEFAULT_LAYER_DESC: dict[str, str] = {
    "route": "URL 命中索引路由表 (Method+Path) — 这是请求精确指向的接口规格",
    "remark": "payload 里的报错文案命中 KB 的 Message / processRemark 表 — 这是这条报错的官方解释",
    "errorCode": "payload 里的错误码命中 KB 的 ResultCode / Code 表 — 这是该 code 的标准说明",
    "status": "payload 里的状态值命中 KB 的 Status 枚举字典 — 这是该枚举值的标准含义",
}


@dataclass
class PromptTemplates:
    """所有 prompt 渲染相关的可配字符串.

    通用层不直接读 system_prompt — system_prompt 由 :mod:`.pipeline` 那一层
    单独管 (因为它是业务身份的核心, 跟 prompt 拼装方式正交).
    这里只管 user message 这一侧 + outline header.
    """

    prompt_head_tmpl: str = DEFAULT_PROMPT_HEAD_TMPL
    no_user_question_fallback: str = DEFAULT_NO_USER_QUESTION_FALLBACK
    outline_header: str = DEFAULT_OUTLINE_HEADER
    kind_desc: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_KIND_DESC))
    layer_desc: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_LAYER_DESC))


# ---------------------------------------------------------------------------
# 全局目录 — 启动期一次性渲染, 注进 system prompt
# ---------------------------------------------------------------------------

_MAX_LEVEL_IN_OUTLINE = 3


def _module_outline_block(module_obj: dict) -> str:
    """单个模块的 outline 块.

    输出形如:
        ## <module> — <module title>
          - 1. 整体业务流程
          - 2. 子模块速查
              - 2.1 ...
    """
    name = module_obj.get("module", "?")
    title = module_obj.get("title", "")
    lines = [f"## {name} — {title}"]
    for entry in module_obj.get("outline", []):
        level = entry.get("level", 2)
        if level > _MAX_LEVEL_IN_OUTLINE:
            continue
        heading = entry.get("heading", "").strip()
        if not heading or heading in ("目录", "Table of Contents", "TOC"):
            continue
        indent = "  " * (level - 1)
        lines.append(f"{indent}- {heading}")
    return "\n".join(lines)


def build_outline_text(
    index_json_path: Path,
    *,
    header: str = DEFAULT_OUTLINE_HEADER,
) -> str:
    """加载 AST ``index.json`` 并渲染成给 system prompt 用的目录字符串.

    为什么要塞进 system prompt:
      hybrid 检索 top-K 给的是"碎片", LLM 看不到全图. 当用户问的事检索召回不
      全时, LLM 看了这份目录至少知道"X 模块里有 'Y 章节'", 不容易胡编"我没有
      这条记录"就草草结束 — 能更准确地说"这个问题应该在 X 模块的 Y 节里, 但
      我这次没检索到具体内容".

    裁剪策略: 只保留 level 2 + 3, 丢 level 4+ (那些通常是子接口 / 子表, 进系
    统 prompt 太碎). 整张表压完估计 ~3-5KB, 性价比合适.

    文件不存在 (e.g. 还没跑过 AST 构建脚本) 时返回空串, 调用方该正常工作,
    只是少了"全局目录"这个 nice-to-have.
    """
    if not index_json_path.is_file():
        return ""
    idx = json.loads(index_json_path.read_text(encoding="utf-8"))
    parts: list[str] = []
    for m in idx.get("modules", []):
        parts.append(_module_outline_block(m))
    if not parts:
        return ""
    return header + "\n\n" + "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 请求期 — 把 Layer 1 + Layer 2 + 请求场景 渲染成 user message
# ---------------------------------------------------------------------------

def _format_chunk(c: Chunk) -> str:
    return f"[{c.module} ({c.kind}) > {c.section}]\n{c.body}"


def _format_deterministic(
    hits: list[LookupHit],
    layer_desc: dict[str, str],
) -> str:
    """Layer 1 命中独立成块 — 最显眼地排在 hybrid 检索结果之前.

    每条命中除了 chunk body, 还把"命中理由"写出来 (e.g. ``URL ↔ 索引路由 ...``),
    让 LLM 一眼看清"这是请求里的字段直接对上的权威条目, 不是模糊检索拍脑袋".
    """
    if not hits:
        return ""
    by_layer: dict[str, list[LookupHit]] = {}
    for h in hits:
        by_layer.setdefault(h.layer, []).append(h)

    blocks: list[str] = []
    for layer in ("route", "errorCode", "status", "remark"):
        items = by_layer.get(layer)
        if not items:
            continue
        header = f"### ⭐ 确定性命中 — {layer}\n_{layer_desc.get(layer, '')}_"
        body_lines: list[str] = []
        for h in items:
            body_lines.append(f"**命中理由**: {h.reason}\n\n{_format_chunk(h.chunk)}")
        body = "\n\n---\n\n".join(body_lines)
        blocks.append(f"{header}\n\n{body}")
    return "\n\n".join(blocks)


def _format_grouped(
    chunks: list[Chunk],
    kind_desc: dict[str, str],
) -> str:
    """hybrid 检索结果按 kind 分组渲染 — code / doc / meta 各一段."""
    by_kind: dict[str, list[Chunk]] = {"code": [], "doc": [], "meta": []}
    for c in chunks:
        by_kind.setdefault(c.kind, []).append(c)

    blocks: list[str] = []
    for kind in ("code", "doc", "meta"):
        group = by_kind.get(kind) or []
        if not group:
            continue
        header = f"### 🔍 hybrid 检索 — 来源类型: {kind}\n_{kind_desc.get(kind, '')}_"
        body = "\n\n---\n\n".join(_format_chunk(c) for c in group)
        blocks.append(f"{header}\n\n{body}")
    return "\n\n".join(blocks) or "(未检索到相关片段)"


def _render_scene(
    route_url: str,
    payload: dict[str, Any],
    user_question: str | None,
    fallback: str,
) -> str:
    """把"请求场景"段落 (URL + payload + 用户问题) 渲染给 LLM."""
    payload_text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    user_q = (
        user_question.strip()
        if user_question and user_question.strip()
        else fallback
    )
    return (
        "---\n\n"
        "本次请求场景:\n\n"
        f"- 前端命中接口: `{route_url}`\n"
        "- 当前数据 payload (前端传来的字段快照):\n\n"
        f"```json\n{payload_text}\n```\n\n"
        f"- 用户补充问题: {user_q}\n"
    )


def render_user_message(
    det_hits: list[LookupHit],
    retrieved: list[Chunk],
    route_url: str,
    payload: dict[str, Any],
    user_question: str | None,
    *,
    templates: PromptTemplates,
) -> str:
    """把 (Layer 1 hits + Layer 2 retrieved + 请求场景) 渲染成一条 user message.

    跟 :mod:`.pipeline` 的编排逻辑解耦: 编排层只管"先检索什么再检索什么",
    具体怎么排版/怎么向 LLM 解释每块的角色, 全部归到这里.
    """
    docs_block_parts: list[str] = []
    det_block = _format_deterministic(det_hits, templates.layer_desc)
    if det_block:
        docs_block_parts.append(det_block)
    docs_block_parts.append(_format_grouped(retrieved, templates.kind_desc))
    docs_block = "\n\n".join(docs_block_parts)

    return (
        templates.prompt_head_tmpl.format(docs=docs_block)
        + "\n"
        + _render_scene(route_url, payload, user_question, templates.no_user_question_fallback)
    )
