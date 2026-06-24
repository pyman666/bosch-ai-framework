"""人工撰写 markdown SOP 的目录布局 + 切分约定 — corpus.py 共用真源.

只描述"loader 怎么分类 / 切 chunk"这一件事:

- ``docs/doc/<module>-doc.md``      → ``(module, kind="doc")``
- ``docs/table/<name>.md``          → ``(name, kind="meta")``
- ``docs/project-overview.md``      → ``(stem, kind="meta")``
- 其他散落的 *.md                   → ``(stem.split("-",1)[0], kind="meta")``
- H1 = 文档标题, H2 = chunk 切分边界
- 某些 H2 标题 (``目录`` / ``Table of Contents`` / ``TOC``) 直接跳过

为什么单独抽一个模块: 让 :class:`HybridPipelineConfig` 能让其他项目通过传
不同的 :class:`MarkdownKbConvention` 来用自家命名约定 (e.g. ``-sop`` 后缀,
``howto/`` 子目录), 不用 fork :mod:`.corpus`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class MarkdownKbConvention:
    """SOP markdown 目录布局 / 切 chunk 的约定参数化.

    构造一个新实例传给 :func:`bapee.rag.corpus.load_chunks` (或
    :class:`HybridPipelineConfig.convention`), 可以让其他项目用自家的目录布局
    + 文件命名约定, 而不用动 ``rag`` 代码.

    .. code-block:: python

        convention = MarkdownKbConvention(
            doc_subdir="howto",
            doc_file_suffix="-procedure",
            project_overview_filename="readme.md",
        )
    """

    doc_subdir: str = "doc"
    table_subdir: str = "table"
    project_overview_filename: str = "project-overview.md"
    doc_file_suffix: str = "-doc"
    kind_doc: str = "doc"
    kind_meta: str = "meta"
    skip_h2_titles: frozenset[str] = field(
        default_factory=lambda: frozenset({"目录", "Table of Contents", "TOC"})
    )


DEFAULT_CONVENTION = MarkdownKbConvention()


_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_H2_SPLIT_RE = re.compile(r"^##\s+", re.MULTILINE)


def classify_markdown(
    path: Path,
    docs_root: Path,
    *,
    convention: MarkdownKbConvention = DEFAULT_CONVENTION,
) -> tuple[str, str]:
    """根据文件相对 ``docs_root`` 的位置 / 命名推断 ``(module, kind)``.

    主代码 KB (``docs/ast/*.chunks.jsonl``) 不走 markdown 路径, 不进本函数.
    """
    stem = path.stem
    rel = path.relative_to(docs_root).as_posix()
    if rel.startswith(f"{convention.doc_subdir}/") and stem.endswith(
        convention.doc_file_suffix
    ):
        return stem[: -len(convention.doc_file_suffix)], convention.kind_doc
    if rel.startswith(f"{convention.table_subdir}/"):
        return stem, convention.kind_meta
    if rel == convention.project_overview_filename:
        return stem, convention.kind_meta
    return stem.split("-", 1)[0] or "misc", convention.kind_meta


def split_h2(
    text: str,
    *,
    convention: MarkdownKbConvention = DEFAULT_CONVENTION,
) -> tuple[str, list[tuple[str, str]]]:
    """切 markdown: 返回 ``(h1, [(h2_title, h2_body), ...])``.

    - H1 之前 / 第一个 H2 之前的引言段 **丢掉** (一般是文档自己的定位说明, 进检索是噪声).
    - ``convention.skip_h2_titles`` 里的标题直接跳过.
    - body 没内容也保留 ``(title, "")``.
    """
    h1_m = _H1_RE.search(text)
    h1 = h1_m.group(1).strip() if h1_m else ""
    parts = _H2_SPLIT_RE.split(text)
    sections: list[tuple[str, str]] = []
    for part in parts[1:]:
        nl = part.find("\n")
        if nl == -1:
            title = part.strip()
            body = ""
        else:
            title = part[:nl].strip()
            body = part[nl + 1 :].rstrip()
        if not title or title in convention.skip_h2_titles:
            continue
        sections.append((title, body))
    return h1, sections
