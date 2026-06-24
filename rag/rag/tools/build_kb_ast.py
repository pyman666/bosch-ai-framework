"""离线脚本: markdown → ``<module>.chunks.jsonl`` (+ ``<module>.ast.json``).

输出格式是 :func:`bapee.rag.corpus.load_chunks` 的消费契约 — 字段集就
是本脚本 :func:`emit_chunks` 函数里写的那些, 一一对应 :meth:`Chunk.from_ast_dict`.
没有额外的 schema 校验层, 字段错了 loader 也只是读出空字段, 想严格点的话
看一眼 :func:`emit_chunks` 函数的输出就是真源.

非 markdown 源数据 (PDF / Confluence / CSV / wiki) 推荐姿势: 先尽量转
markdown (pdftotext / pandoc / 自己写个 dump), 然后扔给本脚本. 实在转不
了的奇怪源, 直接让 LLM 在 Cursor / Claude 里照 :func:`emit_chunks` 的输出
字段 (id / module / type / level / heading / path / breadcrumb / anchor /
source / content / has_tables / has_code) 直接产 jsonl 也行 — 验证就是
``corpus.load_chunks`` 跑通 + 检索召回正常.

CLI:

.. code-block:: bash

    python -m bapee.rag.tools.build_kb_ast \\
        --input  docs/ \\
        --output docs/ast/ \\
        --glob   '*-knowledge-base.md' \\
        --strip-suffix '-knowledge-base'

参数都有默认值, 跟仓库现有约定一致, 没改 CLI 也能跑.

输出文件:

- ``<output>/<module>.chunks.jsonl``  每行一个 chunk dict, RAG 直消费
- ``<output>/<module>.ast.json``       完整 section 树 + 表/代码块, 调试/可视化用
- ``<output>/index.json``              所有 module 的目录 + outline, 给 prompt 拼"目录大纲"用

零外部依赖 (纯标准库). 解析器是 BPAE knowledge-base 风格 markdown 专用 (干净
的 GFM: ``#`` 标题, ``\`\`\`fence`` 代码块, ``|...|`` pipe 表), 不是通用解析器.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# Anchor / id helpers
# ---------------------------------------------------------------------------

_STRIP_PUNCT = re.compile(r"[^\w\u4e00-\u9fff\- ]+", re.UNICODE)


def slugify(text: str) -> str:
    """小写化, 去标点, 空格转 ``-``. 不追求跟 GFM 字节级一致, 只要稳定唯一即可."""
    text = text.strip().lower()
    text = _STRIP_PUNCT.sub("", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "section"


def module_name_from_path(path: Path, *, strip_suffix: str = "") -> str:
    """``docs/jitcall-knowledge-base.md`` + ``strip_suffix='-knowledge-base'`` → ``jitcall``.

    ``strip_suffix`` 为空时直接返回 stem; 这样新项目用任意命名约定都能跑.
    """
    stem = path.stem
    if strip_suffix and stem.endswith(strip_suffix):
        return stem[: -len(strip_suffix)] or stem
    return stem


# ---------------------------------------------------------------------------
# AST data classes
# ---------------------------------------------------------------------------


@dataclass
class Table:
    id: str
    headers: list[str]
    rows: list[dict[str, str]]
    raw: str

    def to_dict(self) -> dict:
        return {"id": self.id, "headers": self.headers, "rows": self.rows, "raw": self.raw}


@dataclass
class CodeBlock:
    id: str
    language: str
    content: str

    def to_dict(self) -> dict:
        return {"id": self.id, "language": self.language, "content": self.content}


@dataclass
class Section:
    id: str
    heading: str
    level: int
    path: list[str]
    anchor: str
    text: str = ""
    tables: list[Table] = field(default_factory=list)
    code_blocks: list[CodeBlock] = field(default_factory=list)
    children: list["Section"] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "heading": self.heading,
            "level": self.level,
            "path": self.path,
            "anchor": self.anchor,
            "text": self.text,
            "tables": [t.to_dict() for t in self.tables],
            "code_blocks": [c.to_dict() for c in self.code_blocks],
            "children": [c.to_dict() for c in self.children],
        }

    def walk(self) -> Iterable["Section"]:
        yield self
        for child in self.children:
            yield from child.walk()


# ---------------------------------------------------------------------------
# Parser (GFM 子集)
# ---------------------------------------------------------------------------

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
CODE_FENCE_RE = re.compile(r"^```(\w*)\s*$")
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")


def _looks_like_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") or "|" in stripped


def _split_pipe_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


@dataclass
class _ParseState:
    lines: list[str]
    i: int = 0

    def eof(self) -> bool:
        return self.i >= len(self.lines)


def parse_markdown(md_text: str, module: str, doc_title: str) -> tuple[Section, str]:
    """返回 ``(root_section, leading_summary)``.

    ``root_section`` 是合成的 level-1 wrapper, 装文档真正的顶层 section
    (通常是 ``## 1. ...``, ``## 2. ...``).
    ``leading_summary`` 是 H1 标题和第一个 H2 之间的内容 (一般是 ``>``
    引用块说明定位).
    """
    lines = md_text.splitlines()
    state = _ParseState(lines=lines)

    summary_buf: list[str] = []
    h1_seen = False
    while not state.eof():
        line = state.lines[state.i]
        m = HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            if level == 1 and not h1_seen:
                h1_seen = True
                state.i += 1
                continue
            if level >= 2:
                break
        summary_buf.append(line)
        state.i += 1

    summary = "\n".join(summary_buf).strip("\n")

    root = Section(
        id=module,
        heading=doc_title,
        level=1,
        path=[doc_title],
        anchor="",
    )

    stack: list[Section] = [root]
    used_ids: set[str] = {module}
    body_buf: list[str] = []

    def flush_body_into_current() -> None:
        if not body_buf:
            return
        current = stack[-1]
        text, tables, code_blocks = _split_body(
            "\n".join(body_buf),
            section_id=current.id,
            existing_tables=len(current.tables),
            existing_code=len(current.code_blocks),
        )
        if current.text:
            current.text = current.text + "\n\n" + text if text else current.text
        else:
            current.text = text
        current.tables.extend(tables)
        current.code_blocks.extend(code_blocks)
        body_buf.clear()

    while not state.eof():
        line = state.lines[state.i]

        fence_m = CODE_FENCE_RE.match(line)
        if fence_m:
            body_buf.append(line)
            state.i += 1
            while not state.eof():
                body_buf.append(state.lines[state.i])
                if CODE_FENCE_RE.match(state.lines[state.i]):
                    state.i += 1
                    break
                state.i += 1
            continue

        h_m = HEADING_RE.match(line)
        if h_m:
            level = len(h_m.group(1))
            heading_text = h_m.group(2).strip()

            flush_body_into_current()

            while stack and stack[-1].level >= level:
                stack.pop()
            parent = stack[-1] if stack else root

            section_id = _make_unique_id(f"{parent.id}/{slugify(heading_text)}", used_ids)
            new_section = Section(
                id=section_id,
                heading=heading_text,
                level=level,
                path=parent.path + [heading_text],
                anchor=f"#{slugify(heading_text)}",
            )
            parent.children.append(new_section)
            stack.append(new_section)
            state.i += 1
            continue

        body_buf.append(line)
        state.i += 1

    flush_body_into_current()
    return root, summary


def _make_unique_id(base: str, used: set[str]) -> str:
    candidate = base
    n = 1
    while candidate in used:
        n += 1
        candidate = f"{base}-{n}"
    used.add(candidate)
    return candidate


# ---------------------------------------------------------------------------
# Body splitter (从 section 正文里抽 tables / code blocks)
# ---------------------------------------------------------------------------


def _split_body(
    body: str,
    section_id: str,
    existing_tables: int,
    existing_code: int,
) -> tuple[str, list[Table], list[CodeBlock]]:
    lines = body.splitlines()
    out_lines: list[str] = []
    tables: list[Table] = []
    code_blocks: list[CodeBlock] = []

    i = 0
    while i < len(lines):
        line = lines[i]

        fence_m = CODE_FENCE_RE.match(line)
        if fence_m:
            language = fence_m.group(1) or ""
            i += 1
            buf: list[str] = []
            while i < len(lines) and not CODE_FENCE_RE.match(lines[i]):
                buf.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            code_id = f"{section_id}#code-{existing_code + len(code_blocks)}"
            code_blocks.append(
                CodeBlock(id=code_id, language=language, content="\n".join(buf))
            )
            out_lines.append(f"[CODE_BLOCK:{code_id}]")
            continue

        if (
            _looks_like_table_row(line)
            and i + 1 < len(lines)
            and TABLE_SEP_RE.match(lines[i + 1])
        ):
            header_row = _split_pipe_row(line)
            i += 2  # skip header + separator
            data_rows: list[list[str]] = []
            raw_buf = [line, lines[i - 1]]
            while i < len(lines) and _looks_like_table_row(lines[i]) and lines[i].strip():
                raw_buf.append(lines[i])
                data_rows.append(_split_pipe_row(lines[i]))
                i += 1
            normalized: list[dict[str, str]] = []
            for cells in data_rows:
                if len(cells) < len(header_row):
                    cells = cells + [""] * (len(header_row) - len(cells))
                elif len(cells) > len(header_row):
                    cells = cells[: len(header_row) - 1] + [
                        " | ".join(cells[len(header_row) - 1 :])
                    ]
                normalized.append({h: c for h, c in zip(header_row, cells)})
            table_id = f"{section_id}#table-{existing_tables + len(tables)}"
            tables.append(
                Table(
                    id=table_id,
                    headers=header_row,
                    rows=normalized,
                    raw="\n".join(raw_buf),
                )
            )
            out_lines.append(f"[TABLE:{table_id}]")
            continue

        out_lines.append(line)
        i += 1

    return "\n".join(out_lines).strip("\n"), tables, code_blocks


# ---------------------------------------------------------------------------
# Chunk emitter — 输出字段就是 corpus.load_chunks 的消费契约的真源
# ---------------------------------------------------------------------------


def _section_chunk_text(section: Section) -> str:
    """把 ``[TABLE:...]`` / ``[CODE_BLOCK:...]`` 占位还原成原始 markdown.

    chunks.jsonl 的 ``content`` 字段要"能直接喂 LLM 的可读 markdown",
    所以这里把抽出去的表格/代码块塞回来.
    """
    text = section.text or ""
    tbl_by_id = {t.id: t for t in section.tables}
    code_by_id = {c.id: c for c in section.code_blocks}

    def _replace(match: re.Match) -> str:
        kind, ident = match.group(1), match.group(2)
        if kind == "TABLE" and ident in tbl_by_id:
            return tbl_by_id[ident].raw
        if kind == "CODE_BLOCK" and ident in code_by_id:
            cb = code_by_id[ident]
            return f"```{cb.language}\n{cb.content}\n```"
        return match.group(0)

    return re.sub(r"\[(TABLE|CODE_BLOCK):([^\]]+)\]", _replace, text)


def emit_chunks(module: str, root: Section, source_rel: str) -> list[dict]:
    """生成扁平 chunk dict 列表. **本函数的输出字段就是 ``corpus.load_chunks`` 的消费契约**.

    每个 H2+ section 一条 ``section`` chunk; 该 section 内每张表的每一行再
    各一条 ``table_row`` chunk (用于 deterministic lookup 命中"具体记录").
    """
    chunks: list[dict] = []
    for section in root.walk():
        if section is root:
            continue
        breadcrumb = " > ".join(section.path)
        section_text = _section_chunk_text(section)
        chunks.append(
            {
                "id": f"{module}::{section.id}",
                "module": module,
                "type": "section",
                "level": section.level,
                "heading": section.heading,
                "path": section.path,
                "breadcrumb": breadcrumb,
                "anchor": section.anchor,
                "source": source_rel,
                "content": section_text,
                "has_tables": bool(section.tables),
                "has_code": bool(section.code_blocks),
            }
        )

        for table in section.tables:
            for idx, row in enumerate(table.rows):
                row_text = " | ".join(
                    f"{h}: {row.get(h, '')}" for h in table.headers if row.get(h, "")
                )
                chunks.append(
                    {
                        "id": f"{module}::{table.id}::row-{idx}",
                        "module": module,
                        "type": "table_row",
                        "level": section.level + 1,
                        "heading": section.heading,
                        "path": section.path,
                        "breadcrumb": breadcrumb,
                        "anchor": section.anchor,
                        "source": source_rel,
                        "table_id": table.id,
                        "row_index": idx,
                        "headers": table.headers,
                        "row": row,
                        "content": row_text,
                    }
                )

    return chunks


def build_outline(root: Section) -> list[dict]:
    """生成 ``index.json`` 用的轻量 TOC. ``rag/organize.py`` 拼"目录大纲" 时直消费."""
    outline: list[dict] = []
    for section in root.walk():
        if section is root:
            continue
        outline.append(
            {
                "id": section.id,
                "level": section.level,
                "heading": section.heading,
                "path": section.path,
                "anchor": section.anchor,
            }
        )
    return outline


# ---------------------------------------------------------------------------
# Driver (公开 API)
# ---------------------------------------------------------------------------


def extract_doc_title(md_text: str, fallback: str) -> str:
    for line in md_text.splitlines():
        m = HEADING_RE.match(line)
        if m and len(m.group(1)) == 1:
            return m.group(2).strip()
    return fallback


def build_for_file(
    md_path: Path,
    *,
    input_root: Path,
    output_dir: Path,
    strip_suffix: str,
) -> dict:
    """处理单个 markdown 文件 -> 写 ``<module>.ast.json`` + ``<module>.chunks.jsonl``.

    返回值是 ``index.json`` 里这一项的 metadata.
    """
    md_text = md_path.read_text(encoding="utf-8")
    module = module_name_from_path(md_path, strip_suffix=strip_suffix)
    doc_title = extract_doc_title(md_text, fallback=module)
    root, summary = parse_markdown(md_text, module=module, doc_title=doc_title)

    source_rel = md_path.resolve().relative_to(input_root.resolve().parent).as_posix()

    ast_payload = {
        "module": module,
        "title": doc_title,
        "source": source_rel,
        "summary": summary,
        "sections": [c.to_dict() for c in root.children],
    }

    chunks = emit_chunks(module, root, source_rel=source_rel)
    outline = build_outline(root)

    output_dir.mkdir(parents=True, exist_ok=True)
    ast_file = output_dir / f"{module}.ast.json"
    chunks_file = output_dir / f"{module}.chunks.jsonl"

    ast_file.write_text(
        json.dumps(ast_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with chunks_file.open("w", encoding="utf-8", newline="\n") as fh:
        for chunk in chunks:
            fh.write(json.dumps(chunk, ensure_ascii=False))
            fh.write("\n")

    return {
        "module": module,
        "title": doc_title,
        "source": source_rel,
        "ast_file": f"{output_dir.name}/{ast_file.name}",
        "chunks_file": f"{output_dir.name}/{chunks_file.name}",
        "section_count": sum(1 for _ in root.walk()) - 1,
        "chunk_count": len(chunks),
        "outline": outline,
    }


def build_ast(
    input_dir: Path,
    output_dir: Path,
    *,
    glob: str = "*-knowledge-base.md",
    strip_suffix: str = "-knowledge-base",
) -> list[dict]:
    """程序入口: 扫描 ``input_dir/<glob>``, 输出到 ``output_dir/``, 返回 catalog.

    被 :func:`main` 包成 CLI; 也可以直接被测试 / 其他脚本 import 调用.
    """
    files = sorted(input_dir.glob(glob))
    if not files:
        return []

    output_dir.mkdir(parents=True, exist_ok=True)
    catalog: list[dict] = []
    for path in files:
        print(f"[build_kb_ast] processing {path.name} ...")
        catalog.append(
            build_for_file(
                path,
                input_root=input_dir,
                output_dir=output_dir,
                strip_suffix=strip_suffix,
            )
        )

    index_payload = {
        "generated_by": f"bapee.rag.tools.build_kb_ast (from {input_dir})",
        "modules": catalog,
    }
    (output_dir / "index.json").write_text(
        json.dumps(index_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return catalog


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bapee.rag.tools.build_kb_ast",
        description="Build AST + chunks.jsonl from a directory of markdown files.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("docs"),
        help="Markdown 输入目录 (默认 ./docs).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/ast"),
        help="输出目录, 默认 ./docs/ast/ (跟 corpus.load_chunks 默认对齐).",
    )
    parser.add_argument(
        "--glob",
        default="*-knowledge-base.md",
        help="输入目录里的匹配模式 (默认 '*-knowledge-base.md').",
    )
    parser.add_argument(
        "--strip-suffix",
        default="-knowledge-base",
        help="从文件 stem 里去掉的后缀, 用于推断 module 名 "
        "(默认 '-knowledge-base'; 设空字符串可关掉).",
    )
    args = parser.parse_args(argv)

    catalog = build_ast(
        args.input,
        args.output,
        glob=args.glob,
        strip_suffix=args.strip_suffix,
    )
    if not catalog:
        print(f"[build_kb_ast] no markdown matched {args.glob!r} under {args.input}")
        return 1

    total_chunks = sum(m["chunk_count"] for m in catalog)
    print(
        f"[build_kb_ast] done. {len(catalog)} modules, "
        f"{total_chunks} chunks total -> {args.output}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
