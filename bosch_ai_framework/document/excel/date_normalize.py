"""Date format detection and normalization — deterministic, no LLM.

Java backends often expect fixed date string formats (e.g. ``yyyy/MM/dd``),
but uploaded Excel files may contain: ``datetime`` cells, text ``"2024-01-15"``,
Excel serial numbers (``45292``), or mixed formats.

This module:
    1. Converts Java-style format strings (``yyyy/MM/dd``) to Python strftime (``%Y/%m/%d``)
    2. Parses individual cells into ``datetime.date`` with permissive heuristics
    3. Normalizes entire columns, returning formatted strings + a diagnostic report
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Java ↔ Python format conversion
# ---------------------------------------------------------------------------

_JAVA_TO_PY: list[tuple[str, str]] = [
    ("yyyy", "%Y"), ("yy", "%y"),
    ("MM", "%m"), ("dd", "%d"),
    ("HH", "%H"), ("hh", "%I"), ("mm", "%M"), ("ss", "%S"),
    ("y", "%Y"), ("M", "%m"), ("d", "%d"),
    ("H", "%H"), ("h", "%I"), ("m", "%M"), ("s", "%S"),
]


def java_to_python_format(java_fmt: str) -> str:
    """Convert Java SimpleDateFormat pattern to Python strftime format.

    >>> java_to_python_format("yyyy/MM/dd HH:mm:ss")
    '%Y/%m/%d %H:%M:%S'
    """
    out: list[str] = []
    i = 0
    n = len(java_fmt)
    while i < n:
        matched = False
        for jtok, ptok in _JAVA_TO_PY:
            if java_fmt.startswith(jtok, i):
                out.append(ptok)
                i += len(jtok)
                matched = True
                break
        if not matched:
            out.append(java_fmt[i])
            i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Single-cell date parsing
# ---------------------------------------------------------------------------

_EXCEL_EPOCH: date = date(1899, 12, 30)
_EXCEL_SERIAL_MIN: int = 1       # 1900-01-01
_EXCEL_SERIAL_MAX: int = 73415   # 2100-12-31

_STRING_FORMATS: list[tuple[str, re.Pattern]] = [
    ("yyyy-MM-dd HH:mm:ss", re.compile(r"^\d{4}-\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{2}:\d{2}")),
    ("yyyy/MM/dd HH:mm:ss", re.compile(r"^\d{4}/\d{1,2}/\d{1,2}[ T]\d{1,2}:\d{2}:\d{2}")),
    ("yyyy-MM-dd", re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")),
    ("yyyy/MM/dd", re.compile(r"^\d{4}/\d{1,2}/\d{1,2}$")),
    ("yyyy.MM.dd", re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}$")),
    ("yyyyMMdd", re.compile(r"^\d{8}$")),
    ("dd/MM/yyyy", re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")),
    ("MM/dd/yyyy", re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")),
    ("yyyy年MM月dd日", re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日$")),
]


@dataclass
class _ParsedCell:
    date: date | None
    source_kind: str  # "datetime_cell" / "string:<java_fmt>" / "excel_serial" / "unparseable"


def parse_cell_date(v: object) -> _ParsedCell:
    """Parse a single cell value into a date with best-effort heuristics."""
    if v is None or v == "":
        return _ParsedCell(None, "unparseable")

    if isinstance(v, datetime):
        return _ParsedCell(v.date(), "datetime_cell")
    if isinstance(v, date):
        return _ParsedCell(v, "datetime_cell")
    if isinstance(v, bool):
        return _ParsedCell(None, "unparseable")

    if isinstance(v, int):
        if _EXCEL_SERIAL_MIN <= v <= _EXCEL_SERIAL_MAX:
            return _ParsedCell(_EXCEL_EPOCH + timedelta(days=int(v)), "excel_serial")
        return _ParsedCell(None, "unparseable")

    if isinstance(v, float):
        if v != v:  # NaN
            return _ParsedCell(None, "unparseable")
        nv = int(v)
        if _EXCEL_SERIAL_MIN <= nv <= _EXCEL_SERIAL_MAX:
            return _ParsedCell(_EXCEL_EPOCH + timedelta(days=nv), "excel_serial")
        return _ParsedCell(None, "unparseable")

    if isinstance(v, str):
        s = v.strip()
        if not s:
            return _ParsedCell(None, "unparseable")
        for java_fmt, rgx in _STRING_FORMATS:
            if not rgx.match(s):
                continue
            py_fmt = java_to_python_format(java_fmt)
            try:
                return _ParsedCell(
                    datetime.strptime(s, py_fmt).date(),
                    f"string:{java_fmt}",
                )
            except ValueError:
                continue
        return _ParsedCell(None, "unparseable")

    return _ParsedCell(None, "unparseable")


# ---------------------------------------------------------------------------
# Column-level normalization
# ---------------------------------------------------------------------------

_KIND_DESC: dict[str, str] = {
    "datetime_cell": "datetime_cell (Excel native date type)",
    "excel_serial": "excel_serial (numeric cell without date format)",
}


def _describe_kind(kind: str) -> str:
    if kind.startswith("string:"):
        return kind[len("string:"):]
    return _KIND_DESC.get(kind, kind)


class NormalizeReport(BaseModel):
    """Diagnostic report for a normalized date column."""

    date_column: str
    target_date_format: str
    source_date_format: str
    source_matches_target_format: bool
    total_rows: int
    parsed_rows: int
    unparseable_rows: int
    unparseable_samples: list[str] = []


def normalize_column(
    values: Iterable[object],
    *,
    date_column: str,
    target_date_format: str,
) -> tuple[list[str | None], NormalizeReport]:
    """Normalize a column of date values to a target format string.

    Args:
        values: Raw cell values in row order.
        date_column: Column name (for the diagnostic report).
        target_date_format: Java-style target format, e.g. ``"yyyy/MM/dd"``.

    Returns:
        (normalized strings — same length as input, None for unparseable cells, diagnostic report).
    """
    vals_list = list(values)
    py_target = java_to_python_format(target_date_format)

    parsed: list[_ParsedCell] = [parse_cell_date(v) for v in vals_list]
    out: list[str | None] = [
        p.date.strftime(py_target) if p.date is not None else None
        for p in parsed
    ]

    kind_counts = Counter(p.source_kind for p in parsed if p.date is not None)

    if not kind_counts:
        source_date_format = "unknown (no values parsable as dates)"
    elif len(kind_counts) == 1:
        kind = next(iter(kind_counts))
        source_date_format = _describe_kind(kind)
    else:
        ordered = sorted(kind_counts.items(), key=lambda kv: kv[1], reverse=True)
        names = ", ".join(_describe_kind(k) for k, _ in ordered)
        source_date_format = f"mixed ({names})"

    source_matches_target_format = (
        len(kind_counts) == 1
        and next(iter(kind_counts)) == f"string:{target_date_format}"
    )

    unparseable: list[str] = [
        repr(v) for v, p in zip(vals_list, parsed) if p.date is None
    ]

    report = NormalizeReport(
        date_column=date_column,
        target_date_format=target_date_format,
        source_date_format=source_date_format,
        source_matches_target_format=source_matches_target_format,
        total_rows=len(vals_list),
        parsed_rows=sum(1 for p in parsed if p.date is not None),
        unparseable_rows=sum(1 for p in parsed if p.date is None),
        unparseable_samples=unparseable[:5],
    )
    return out, report
