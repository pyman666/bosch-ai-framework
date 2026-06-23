"""Deterministic lookup index — rule-based first-pass retrieval.

Builds inverted indices from table_row chunks at startup, enabling
~microsecond exact-match / substring-match lookups. This is the
"deterministic layer" that runs before hybrid semantic search.

Indexed dimensions:
    - **route**: URL path pattern matching (``{param}`` as wildcard)
    - **remark**: bidirectional substring match on error/message text
    - **code**: exact match on error codes / result codes
    - **status**: exact match on status enum values
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from bosch_ai_framework.rag.corpus import Chunk


# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LookupHit:
    """A deterministic lookup hit.

    - ``chunk``: the matched chunk
    - ``layer``: ``"route"`` / ``"remark"`` / ``"code"`` / ``"status"``
    - ``reason``: human-readable match reason for prompt injection
    """

    chunk: Chunk
    layer: str
    reason: str


# ---------------------------------------------------------------------------
# URL normalization and path matching
# ---------------------------------------------------------------------------

_VARIABLE_SEGMENT_RE = re.compile(r"\{[^}]+\}")


def normalize_url(url: str) -> str:
    """Normalize a URL for route matching: lowercase path, strip query/fragment.

    >>> normalize_url("/Billing/API/SD/CA/retrieve?x=1")
    '/billing/api/sd/ca/retrieve'
    """
    if not url:
        return ""
    url = url.split("?")[0].split("#")[0].strip().lower()
    return url.rstrip("/") or "/"


def _path_matches(req: str, template: str) -> bool:
    """Segment-level path matching: ``{param}`` segments act as wildcards.

    Segments are split by ``/``; non-wildcard segments must match exactly.
    """
    req_segs = req.strip("/").split("/")
    tpl_segs = template.strip("/").split("/")
    if len(req_segs) != len(tpl_segs):
        return False
    for r, t in zip(req_segs, tpl_segs):
        if _VARIABLE_SEGMENT_RE.fullmatch(t):
            continue
        if r != t:
            return False
    return True


def _path_specificity(template: str) -> int:
    """Count literal (non-wildcard) segments — more = more specific."""
    segs = template.strip("/").split("/")
    return sum(1 for s in segs if not _VARIABLE_SEGMENT_RE.fullmatch(s))


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

_BACKTICK_RE = re.compile(r"`([^`]+)`")
_PATH_IN_CELL_RE = re.compile(r"`(/[^`]+)`")
_EMOJI_MARKER_RE = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]")


def _strip_md(s: str) -> str:
    """Strip markdown decoration from cell values for clean text comparison."""
    if not s:
        return ""
    s = _BACKTICK_RE.sub(lambda m: m.group(1), s)
    s = _EMOJI_MARKER_RE.sub("", s)
    s = s.replace("**", "").replace("__", "")
    return s.strip()


def _extract_paths(cell_value: str) -> list[str]:
    """Extract ``/path/...`` patterns from a table cell."""
    if not cell_value:
        return []
    out = [m.group(1).strip() for m in _PATH_IN_CELL_RE.finditer(cell_value)]
    if not out and "/" in cell_value:
        for token in re.split(r"[<,，、\s]+", cell_value):
            token = token.strip().strip("`")
            if token.startswith("/") and len(token) > 1:
                out.append(token)
    return out


# ---------------------------------------------------------------------------
# Header detection — keyword-based, adapts to different table schemas
# ---------------------------------------------------------------------------

_PATH_HEADER_KEYS = ("path", "路径", "url", "endpoint")
_REMARK_HEADER_KEYS = ("processremark", "message", "触发文案", "错误", "errormsg")
_CODE_HEADER_KEYS = ("code", "resultcode")
_ENUM_LABEL_KEYS = ("enum", "name", "constant")
_ENUM_VALUE_KEYS = ("value", "值")


def _header_matches(headers: Iterable[str], keys: tuple[str, ...]) -> list[str]:
    """Return header names that match any of the given keywords (case-insensitive substring)."""
    matched: list[str] = []
    for h in headers:
        h_clean = _strip_md(h).lower()
        if any(k in h_clean for k in keys):
            matched.append(h)
    return matched


# ---------------------------------------------------------------------------
# Internal index entry types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _RouteEntry:
    template: str
    chunk: Chunk
    method: str = ""


@dataclass(frozen=True)
class _RemarkEntry:
    text: str
    chunk: Chunk
    header: str


@dataclass(frozen=True)
class _CodeEntry:
    code: str
    chunk: Chunk


@dataclass(frozen=True)
class _StatusEntry:
    value: str
    chunk: Chunk
    header: str


# ---------------------------------------------------------------------------
# LookupIndex
# ---------------------------------------------------------------------------

class LookupIndex:
    """Deterministic lookup indices built once at startup.

    Usage::

        index = LookupIndex(chunks)
        hits = index.find_by_route("/billing/api/sd/CA/retrieve")
        hits += index.find_by_code("5013")
        hits += index.find_by_status("ReleaseToFIN")
    """

    def __init__(self, chunks: list[Chunk]) -> None:
        self.routes: list[_RouteEntry] = []
        self.remarks: list[_RemarkEntry] = []
        self.codes: dict[str, list[_CodeEntry]] = {}
        self.statuses: dict[str, list[_StatusEntry]] = {}
        self._build(chunks)

    # -- build ---------------------------------------------------------------

    def _build(self, chunks: list[Chunk]) -> None:
        for c in chunks:
            if c.chunk_type != "table_row" or not c.row:
                continue
            self._index_routes(c)
            self._index_remarks(c)
            self._index_codes(c)
            self._index_statuses(c)

    def _index_routes(self, c: Chunk) -> None:
        path_headers = _header_matches(c.headers, _PATH_HEADER_KEYS)
        if not path_headers:
            return
        method = ""
        for h in c.headers:
            if _strip_md(h).lower() == "method":
                method = _strip_md(str(c.row.get(h, "")))
                break
        for h in path_headers:
            for p in _extract_paths(str(c.row.get(h, ""))):
                self.routes.append(_RouteEntry(template=p, chunk=c, method=method))

    def _index_remarks(self, c: Chunk) -> None:
        for h in _header_matches(c.headers, _REMARK_HEADER_KEYS):
            text = _strip_md(str(c.row.get(h, "")))
            if not text or len(text) < 4:
                continue
            self.remarks.append(_RemarkEntry(text=text, chunk=c, header=_strip_md(h)))

    def _index_codes(self, c: Chunk) -> None:
        h_code = _header_matches(c.headers, _CODE_HEADER_KEYS)
        h_msg = _header_matches(c.headers, ("message",))
        if not h_code or not h_msg:
            return
        for h in h_code:
            raw = _strip_md(str(c.row.get(h, "")))
            if not raw or len(raw) > 16 or " " in raw:
                continue
            self.codes.setdefault(raw, []).append(_CodeEntry(code=raw, chunk=c))

    def _index_statuses(self, c: Chunk) -> None:
        h_label = _header_matches(c.headers, _ENUM_LABEL_KEYS)
        h_value = _header_matches(c.headers, _ENUM_VALUE_KEYS)
        if not h_label and not h_value:
            return
        cell_keys = h_value or h_label
        for h in cell_keys:
            raw = _strip_md(str(c.row.get(h, "")))
            for v in re.split(r"[,，/\s]+", raw):
                v = v.strip().strip("`")
                if not v or len(v) > 32 or len(v) < 2:
                    continue
                self.statuses.setdefault(v, []).append(
                    _StatusEntry(value=v, chunk=c, header=_strip_md(h))
                )

    # -- query ---------------------------------------------------------------

    def find_by_route(self, route_url: str) -> list[LookupHit]:
        """Find chunks matching the given route URL pattern."""
        url = normalize_url(route_url)
        if not url:
            return []
        matches = [e for e in self.routes if _path_matches(url, e.template)]
        matches.sort(key=lambda e: -_path_specificity(e.template))
        seen: set[str] = set()
        out: list[LookupHit] = []
        for m in matches:
            if m.chunk.chunk_id in seen:
                continue
            seen.add(m.chunk.chunk_id)
            mtd = f"{m.method} " if m.method else ""
            out.append(
                LookupHit(
                    chunk=m.chunk,
                    layer="route",
                    reason=f"URL `{url}` ↔ indexed route `{mtd}{m.template}`",
                )
            )
        return out

    def find_by_remark(self, text: str) -> list[LookupHit]:
        """Find chunks by bidirectional substring match on remark text."""
        if not text:
            return []
        q = _strip_md(str(text)).lower()
        if len(q) < 3:
            return []
        out: list[LookupHit] = []
        seen: set[str] = set()
        for e in self.remarks:
            indexed = e.text.lower()
            if not indexed:
                continue
            if indexed in q or q in indexed:
                if e.chunk.chunk_id in seen:
                    continue
                seen.add(e.chunk.chunk_id)
                out.append(
                    LookupHit(
                        chunk=e.chunk,
                        layer="remark",
                        reason=f"`{e.header}` substring match: `{e.text}`",
                    )
                )
        return out

    def find_by_code(self, code: Any) -> list[LookupHit]:
        """Find chunks by exact error code / result code."""
        if code is None:
            return []
        key = str(code).strip().strip("`")
        hits = self.codes.get(key, [])
        return [
            LookupHit(
                chunk=e.chunk,
                layer="code",
                reason=f"Code match: `{key}`",
            )
            for e in hits
        ]

    def find_by_status(self, value: Any) -> list[LookupHit]:
        """Find chunks by exact status enum value."""
        if value is None:
            return []
        key = str(value).strip().strip("`")
        if not key:
            return []
        hits = self.statuses.get(key, [])
        return [
            LookupHit(
                chunk=e.chunk,
                layer="status",
                reason=f"`{e.header}` enum match: `{key}`",
            )
            for e in hits
        ]

    # -- convenience: scan payload dict --------------------------------------

    def find_from_payload(
        self,
        payload: dict[str, Any],
        *,
        route_url: str = "",
        remark_fields: tuple[str, ...] = ("processRemark", "remark", "errorMsg", "errorMessage", "msg"),
        code_fields: tuple[str, ...] = ("errorCode", "code", "resultCode"),
        status_fields: tuple[str, ...] = ("processStatus", "status", "matchStatus", "releaseStatus", "messageType"),
    ) -> list[LookupHit]:
        """Scan a request payload for deterministic hits.

        Returns hits in order: route → code → status → remark
        (descending diagnostic value, ascending false-positive risk).
        """
        out: list[LookupHit] = []
        seen: set[tuple[str, str]] = set()

        def _add(hits: list[LookupHit]) -> None:
            for h in hits:
                key = (h.chunk.chunk_id, h.layer)
                if key in seen:
                    continue
                seen.add(key)
                out.append(h)

        # Route
        if route_url:
            _add(self.find_by_route(route_url))

        # Code
        lower_to_orig = {str(k).lower(): k for k in payload.keys()}
        for field in code_fields:
            v = payload.get(lower_to_orig.get(field, field))
            if v not in (None, ""):
                _add(self.find_by_code(v))

        # Status
        for field in status_fields:
            v = payload.get(lower_to_orig.get(field, field))
            if v not in (None, ""):
                _add(self.find_by_status(v))

        # Remark (substring)
        for field in remark_fields:
            v = payload.get(lower_to_orig.get(field, field))
            if isinstance(v, str) and v:
                _add(self.find_by_remark(v))

        return out
