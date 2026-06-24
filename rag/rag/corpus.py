"""(数据) 把 ``docs/`` 下的知识库装载成统一的 ``Chunk`` 列表 — 给 hybrid 检索的 corpus.

本模块**业务无关**: 不知道任何具体业务字段 / 客户名 / 报错码格式. 只认识三种来源
形态 (按 ``docs/`` 子目录划分):

    docs/ast/*.chunks.jsonl   *主代码 KB* — 由离线脚本生成, 已切好的 section +
                              table_row 级 chunk, 带 breadcrumb / module / anchor
                              等元数据. ``kind = "code"``.

    docs/doc/*-doc.md         运营 SOP — 按 H2 切, ``kind = "doc"``.
                              文件名约定 ``<module>-doc.md`` -> ``module`` 字段.

    docs/table/*.md           扁平 meta 表 (调度时间表 / 上线日期表). 按 H2 切,
                              ``kind = "meta"``.

    docs/project-overview.md  项目自我介绍, 按 H2 切, ``kind = "meta"``.

``code`` / ``doc`` / ``meta`` 的三分法是通用文档系统的常见划分, 不绑死业务:
``code`` 表示"权威事实级 KB", ``doc`` 表示"运营/操作 SOP", ``meta`` 表示"项目级
元信息". 具体每类在 prompt 里怎么用, 由 :mod:`.organize` 的 ``PromptTemplates``
决定.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .markdown_kb import (
    DEFAULT_CONVENTION,
    MarkdownKbConvention,
    classify_markdown,
    split_h2,
)


@dataclass(frozen=True)
class Chunk:
    """一段检索 chunk, 是 hybrid 检索 / 拼 prompt 的基本单元 — runtime 模型.

    - ``chunk_id``: 全局稳定的 id, 用于 精确去重 / RRF 合并 / 后续 anchor 引用.
    - ``title``: 给 dense embedding 编码的高密度摘要, 用 breadcrumb 而非裸标题,
      区分度更高.
    - ``body``: 拼进 LLM prompt 的完整文本, 顶部带上下文 header.
    - ``module / kind / section``: prompt 分组 + 来源标注用.
    - ``chunk_type``: ``"section"`` (整节 markdown) / ``"table_row"`` (表里
      的一行, 带结构化 ``row``) / ``"meta_section"`` (meta/doc 的 H2 段).
      用来在 prompt 里让 LLM 一眼分清"这是叙述段还是表里的一条记录".
    - ``row``: 仅 ``table_row`` 才有, 是结构化字段 dict, 给 deterministic
      lookup 拿来建索引用.
    - ``anchor``: markdown 锚点 (来自 AST), 给后续做"客户向引用"留个口子.
    - ``headers``: 仅 ``table_row`` 才有, 是该表的列名顺序, 给 lookup 索引
      判断"这是路由表 / 错误码表 / 枚举表"用.
    """

    chunk_id: str
    title: str
    body: str
    module: str
    kind: str
    section: str
    chunk_type: str
    source: str
    anchor: str = ""
    row: dict[str, Any] | None = None
    headers: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_ast_dict(cls, obj: dict[str, Any]) -> "Chunk":
        """从 AST jsonl row dict 构造 Chunk.

        字段约定 (由 :mod:`.tools.build_kb_ast` 产出, ``corpus.load_chunks`` 消费):

        - ``id`` → ``chunk_id``
        - ``breadcrumb`` → ``title`` (dense embedding 用高密度摘要)
        - ``content`` → ``body`` (table_row 用 ``key: v | key: v`` 形式)
        - ``heading`` → ``section``
        - ``type`` ('section'/'table_row') → ``chunk_type``
        - ``kind`` 固定为 ``"code"`` (AST 来源都是"主代码 KB")
        """
        t = obj.get("type", "section")
        body: str
        if t == "table_row":
            body = _render_ast_table_row_body(obj)
        else:
            body = _render_ast_section_body(obj)
        return cls(
            chunk_id=obj.get("id", ""),
            title=obj.get("breadcrumb") or obj.get("heading", ""),
            body=body,
            module=obj.get("module", ""),
            kind="code",
            section=obj.get("heading", ""),
            chunk_type="section" if t == "section" else "table_row",
            source=obj.get("source", ""),
            anchor=obj.get("anchor", ""),
            row=obj.get("row"),
            headers=tuple(obj.get("headers", []) or ()),
        )


# ---------------------------------------------------------------------------
# AST chunks (主源 — docs/ast/*.chunks.jsonl)
# ---------------------------------------------------------------------------

def _render_ast_section_body(obj: dict[str, Any]) -> str:
    """把 AST section 渲染成给 LLM 看的 markdown.

    AST 里 section 的 ``content`` 已经是 markdown 文本, 我们只在最前面贴一行
    breadcrumb 让 LLM 立刻知道"这是哪本 KB 的哪一节". 章节本身没有内容
    (level 2 标题下直接是子标题) 时也保留, prompt 里至少能看到上下文路径.
    """
    breadcrumb = obj.get("breadcrumb", "")
    heading = obj.get("heading", "")
    content = obj.get("content", "") or "(本节没有正文, 仅作上下文路径)"
    header = f"_{breadcrumb}_" if breadcrumb else ""
    return f"{header}\n\n## {heading}\n\n{content}".strip()


def _render_ast_table_row_body(obj: dict[str, Any]) -> str:
    """把 AST 表格行渲染成 ``key: value`` 多行形式, 比拼字符串更易读.

    ``content`` 字段在 AST 里是 ``"key: v | key: v"`` 这种紧凑写法, 给 dense
    embedding 用没问题, 但塞进 LLM prompt 时换行多列展开更友好.
    """
    breadcrumb = obj.get("breadcrumb", "")
    row = obj.get("row", {}) or {}
    lines = [f"_{breadcrumb}_  (table row)"] if breadcrumb else ["(table row)"]
    for k, v in row.items():
        lines.append(f"- **{k}**: {v}")
    return "\n".join(lines)


def _module_header_for_ast(obj: dict[str, Any]) -> str:
    """生成 dense embedding 用的标题串 — 用 breadcrumb 而不是裸标题.

    breadcrumb 自带上下文 (e.g. ``"billing > 6 错误信息 > 6.1 通用 BN"``),
    比 ``"6.1 通用 BN"`` 这种孤立标题区分度高得多.
    """
    return obj.get("breadcrumb") or obj.get("heading", "")


def _load_ast_chunks(ast_dir: Path) -> list[Chunk]:
    """读取 ``ast_dir/*.chunks.jsonl``, 用 :meth:`Chunk.from_ast_dict` 转成 runtime chunks.

    ``module`` / ``source`` 字段允许 jsonl 行缺省, 用文件名做兜底 — 老版本
    jsonl 没强制要求这两个字段, 留点向后兼容空间.
    """
    out: list[Chunk] = []
    for jsonl in sorted(ast_dir.glob("*.chunks.jsonl")):
        fallback_module = jsonl.stem.split(".")[0]
        fallback_source = str(jsonl)
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            obj.setdefault("module", fallback_module)
            obj.setdefault("source", fallback_source)
            out.append(Chunk.from_ast_dict(obj))
    return out


# ---------------------------------------------------------------------------
# 兜底 markdown (docs/doc/*-doc.md, docs/table/*.md, docs/project-overview.md)
#
# 这条路径的所有"结构约定" (目录布局 / 文件命名 / H1+H2 切分规则) 都集中在
# :mod:`.markdown_kb` 里, 本节只负责"把 markdown 切完往 Chunk 里塞". 想改
# 约定 (e.g. 加新子目录 / 换文件后缀) 改那边, 这里不动.
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")


def _slug(s: str) -> str:
    return _SLUG_RE.sub("-", s.lower()).strip("-")


def _md_chunks(
    path: Path,
    *,
    module: str,
    kind: str,
    id_prefix: str,
    convention: MarkdownKbConvention,
) -> list[Chunk]:
    """把一份 markdown 按 H2 切成 chunk. 给 doc / table / project-overview 用.

    AST 路径外的 markdown 都用这个函数, 切分粒度跟 ``docs/ast`` 的 section
    chunk 对齐 (都是 H2). 由 ``id_prefix`` 保证 chunk_id 全局唯一.
    """
    text = path.read_text(encoding="utf-8")
    h1, sections = split_h2(text, convention=convention)
    out: list[Chunk] = []
    for h2_title, h2_body in sections:
        body_parts = [f"# {h1}" if h1 else "", f"## {h2_title}"]
        if h2_body:
            body_parts.append(h2_body)
        body = "\n\n".join(p for p in body_parts if p)
        breadcrumb = f"{h1} > {h2_title}" if h1 else h2_title
        out.append(
            Chunk(
                chunk_id=f"{id_prefix}::{_slug(h2_title) or 'section'}",
                title=breadcrumb,
                body=body,
                module=module,
                kind=kind,
                section=h2_title,
                chunk_type="meta_section",
                source=str(path),
            )
        )
    return out


def _load_meta_and_doc_chunks(
    docs_dir: Path,
    *,
    convention: MarkdownKbConvention = DEFAULT_CONVENTION,
) -> list[Chunk]:
    out: list[Chunk] = []
    candidates: list[Path] = []
    for sub in (convention.doc_subdir, convention.table_subdir):
        candidates.extend((docs_dir / sub).glob("*.md"))
    overview = docs_dir / convention.project_overview_filename
    if overview.is_file():
        candidates.append(overview)
    for md in sorted(candidates, key=lambda p: p.relative_to(docs_dir).as_posix()):
        module, kind = classify_markdown(md, docs_dir, convention=convention)
        id_prefix = md.stem
        out.extend(
            _md_chunks(
                md,
                module=module,
                kind=kind,
                id_prefix=id_prefix,
                convention=convention,
            )
        )
    return out


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def load_chunks(
    docs_dir: Path,
    *,
    convention: MarkdownKbConvention = DEFAULT_CONVENTION,
) -> list[Chunk]:
    """加载 ``docs_dir`` 下所有可索引内容并返回统一的 chunk 列表.

    顺序: AST chunks (按 jsonl 文件名 + 行序) → meta/doc markdown chunks
    (按相对路径). 实际检索由 retriever 决定, 这里的顺序只影响"分数完全打平
    时的稳定性".

    ``convention`` 控制 markdown 路径的目录布局 / 命名约定 (见
    :class:`MarkdownKbConvention`). 新项目想用不同布局 (e.g. ``howto/`` 替代
    ``doc/``), 构造一个 ``MarkdownKbConvention(...)`` 实例传入即可,
    不用 fork 本模块.
    """
    ast_dir = docs_dir / "ast"
    chunks: list[Chunk] = []
    if ast_dir.is_dir():
        chunks.extend(_load_ast_chunks(ast_dir))
    chunks.extend(_load_meta_and_doc_chunks(docs_dir, convention=convention))
    return chunks
