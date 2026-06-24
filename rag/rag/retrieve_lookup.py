"""(检索 Layer 1) 确定性查表 — hybrid pipeline 的第一层检索.

请求里的某些字段是**字面 key**, 不该用余弦相似度去近似 — 直接 hash table 命中又快又准:

  - ``route_url``                ↔  AST 各 Endpoint 表里的 ``Method/Path`` 列;
  - payload 里"报错文案"类字段     ↔  AST "所有错误信息"族表 (substring 匹配);
  - payload 里"错误码 / Code"类   ↔  AST "ResultCode/Code" 表 (整数/编号字面);
  - payload 里"状态枚举值"类       ↔  AST "Status 枚举字典"族表 (枚举值字面相等).

后三类的 payload 字段名是**业务约定** — 不同项目里 "错误码"可能叫
``errorCode`` / ``code`` / ``resultCode``, "状态"可能叫 ``processStatus`` /
``status`` / ``messageType``. :class:`LookupIndex` 把这三组 payload key 当构造参数收,
业务侧创建时传具体的字段名列表. 启动期一次性扫所有 ``table_row`` chunk 建几个倒
排索引, 运行期 ~微秒命中.

出来的 hits 由 :mod:`.pipeline` 排在 prompt 最前面, 标 ⭐ 提示 LLM 这是权威条目;
同时它们的 chunk_id 作为 Layer 2 的 ``exclude_chunk_ids``, 避免重复.

URL 归一这个工具函数在 :mod:`.filter`, 这一层只做"匹配 + 出 hit", 不碰原始 URL 形态.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .corpus import Chunk
from .filter import normalize_url


# ---------------------------------------------------------------------------
# 公共数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LookupHit:
    """一条 deterministic 命中.

    - ``chunk``: 命中的 chunk;
    - ``layer``: ``"route"`` / ``"remark"`` / ``"errorCode"`` / ``"status"``
      — 让 prompt 能区分"是路由表命中还是错误码命中";
    - ``reason``: 人话解释命中理由, 直接打进 prompt 给 LLM 看.
    """

    chunk: Chunk
    layer: str
    reason: str


# ---------------------------------------------------------------------------
# 字符串 / 路径工具 (本文件内部用)
# ---------------------------------------------------------------------------

_BACKTICK_RE = re.compile(r"`([^`]+)`")
_PATH_IN_CELL_RE = re.compile(r"`(/[^`]+)`")
_EMOJI_MARKER_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")


def _strip_md(s: str) -> str:
    """剥掉单元格里的 markdown 装饰 (反引号 / emoji 标记 / **加粗**), 留下
    纯文本, 给 substring 比对用. 无意于完美渲染, 只要"两边都剥成同一种纯
    文本"就行."""
    if not s:
        return ""
    s = _BACKTICK_RE.sub(lambda m: m.group(1), s)
    s = _EMOJI_MARKER_RE.sub("", s)
    s = s.replace("**", "").replace("__", "")
    return s.strip()


def _extract_paths(cell_value: str) -> list[str]:
    """从一个表格单元格里抽出所有 ``/...`` 路径. 单元格里可能多路径用
    ``<br>`` / 顿号 / 逗号 隔开, 这里直接靠正则按反引号包住的路径串提取."""
    if not cell_value:
        return []
    out = [m.group(1).strip() for m in _PATH_IN_CELL_RE.finditer(cell_value)]
    # 再兜底一下没用反引号的裸路径 (新接口 / 老接口 等表里偶尔出现).
    if not out and "/" in cell_value:
        for token in re.split(r"[<,\uff0c、\s]+", cell_value):
            token = token.strip().strip("`")
            if token.startswith("/") and len(token) > 1:
                out.append(token)
    return out


def _path_matches(req: str, template: str) -> bool:
    """段落级匹配: 模板里 ``{xxx}`` 段当通配, 其他段必须字面相等.

    选择 segment-by-segment 而非整串正则:
      - O(n) 比起编译几百条正则更可控;
      - 严格按 ``/`` 切, 不会出 ``/foo/bar`` 误匹 ``/foo`` 这种.
    """
    req_segs = req.strip("/").split("/")
    tpl_segs = template.strip("/").split("/")
    if len(req_segs) != len(tpl_segs):
        return False
    for r, t in zip(req_segs, tpl_segs):
        if t.startswith("{") and t.endswith("}"):
            continue
        if r != t:
            return False
    return True


def _path_specificity(template: str) -> int:
    """模板路径"具体程度": 字面段越多越具体. 多模板同时命中时优先报最具体的."""
    segs = template.strip("/").split("/")
    return sum(1 for s in segs if not (s.startswith("{") and s.endswith("}")))


# ---------------------------------------------------------------------------
# 倒排索引: route / remark / errorCode / status
# ---------------------------------------------------------------------------

# headers 命中策略 — 用关键词命中 header 名而非死等具体表头, 适应 KB 多种
# 表格写法 (e.g. "Path" 也可能写 "路径前缀" / "路由前缀" / "URL 路径段").
_PATH_HEADER_KEYS = ("path", "路径", "url", "endpoint")
_REMARK_HEADER_KEYS = ("processremark", "message", "触发文案", "错误", "errormsg")
_CODE_HEADER_KEYS = ("code", "resultcode")


def _header_matches(headers: Iterable[str], keys: tuple[str, ...]) -> list[str]:
    """返回 ``headers`` 里命中 ``keys`` (大小写无关 substring) 的那些表头名."""
    matched: list[str] = []
    for h in headers:
        h_clean = _strip_md(h).lower()
        if any(k in h_clean for k in keys):
            matched.append(h)
    return matched


@dataclass(frozen=True)
class _RouteEntry:
    template: str            # e.g. /api/{customer}/commit
    chunk: Chunk
    method: str = ""         # 表里有 Method 列就抓出来, 没有就空


@dataclass(frozen=True)
class _RemarkEntry:
    text: str                # 已剥 markdown
    chunk: Chunk
    header: str              # 命中的表头名, prompt 里报"匹配 XXX 列"


@dataclass(frozen=True)
class _CodeEntry:
    code: str                # 字符串形式 e.g. "5013"
    chunk: Chunk


@dataclass(frozen=True)
class _StatusEntry:
    value: str               # 枚举值字面, e.g. "ReleaseToFIN"
    chunk: Chunk
    header: str              # 命中的"值"列名


class LookupIndex:
    """启动期一次性建好的几张确定性索引.

    构造参数 (业务侧定义 payload 里哪些字段对应哪类 lookup):

    - ``remark_payload_keys``: 触发 remark 类查表的 payload 字段名 (lower-case 命中);
    - ``code_payload_keys``: 触发 errorCode 类查表的 payload 字段名;
    - ``status_payload_keys``: 触发 status 类查表的 payload 字段名.

    空 tuple = 不启用对应类查表. 只想要 route lookup 的话三个都留空即可.
    """

    def __init__(
        self,
        chunks: list[Chunk],
        *,
        remark_payload_keys: tuple[str, ...] = (),
        code_payload_keys: tuple[str, ...] = (),
        status_payload_keys: tuple[str, ...] = (),
    ) -> None:
        self.routes: list[_RouteEntry] = []
        self.remarks: list[_RemarkEntry] = []
        self.codes: dict[str, list[_CodeEntry]] = {}
        self.statuses: dict[str, list[_StatusEntry]] = {}
        self._remark_payload_keys = remark_payload_keys
        self._code_payload_keys = code_payload_keys
        self._status_payload_keys = status_payload_keys
        self._build(chunks)

    # ---------- build ----------

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
        # 仅当 headers 里同时有 Code 和 Message 时才认 (区分纯 code 表 vs 路由表).
        h_code = _header_matches(c.headers, _CODE_HEADER_KEYS)
        h_msg = _header_matches(c.headers, ("message",))
        if not h_code or not h_msg:
            return
        for h in h_code:
            raw = _strip_md(str(c.row.get(h, "")))
            # ResultCode 通常是数字或短标识符, 长文本忽略 (避免把"某说明"塞进来).
            if not raw or len(raw) > 16 or " " in raw:
                continue
            self.codes.setdefault(raw, []).append(_CodeEntry(code=raw, chunk=c))

    def _index_statuses(self, c: Chunk) -> None:
        # "Enum/Name/Constant" + "值/Value" 形式的枚举表
        h_label = _header_matches(c.headers, ("enum", "name", "constant"))
        h_value = _header_matches(c.headers, ("value", "值"))
        if not h_label and not h_value:
            return
        # 实际"值"可能在 "值" 列, 也可能就在 Name 列 (BorScopeEnum 那种 Name=Value).
        cell_keys = h_value or h_label
        for h in cell_keys:
            raw = _strip_md(str(c.row.get(h, "")))
            for v in re.split(r"[,\uff0c/\s]+", raw):
                v = v.strip().strip("`")
                if not v or len(v) > 32 or len(v) < 2:
                    continue
                self.statuses.setdefault(v, []).append(
                    _StatusEntry(value=v, chunk=c, header=_strip_md(h))
                )

    # ---------- query ----------

    def find_by_route(self, route_url: str) -> list[LookupHit]:
        url = normalize_url(route_url)
        if not url:
            return []
        matches = [e for e in self.routes if _path_matches(url, e.template)]
        # 按 specificity 降序: 字面段更多的先报.
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
                    reason=f"URL `{url}` ↔ 索引路由 `{mtd}{m.template}`",
                )
            )
        return out

    def find_by_remark(self, text: str) -> list[LookupHit]:
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
            # 双向 substring: 报告的 remark 通常是模板的某个具体实例 (含变量
            # 替换), KB 里写的是模板/前缀, 反之亦然 — 任意一边包含另一边即认.
            if indexed in q or q in indexed:
                if e.chunk.chunk_id in seen:
                    continue
                seen.add(e.chunk.chunk_id)
                out.append(
                    LookupHit(
                        chunk=e.chunk,
                        layer="remark",
                        reason=f"`{e.header}` substring 命中: `{e.text}`",
                    )
                )
        return out

    def find_by_code(self, code: Any) -> list[LookupHit]:
        if code is None:
            return []
        key = str(code).strip().strip("`")
        hits = self.codes.get(key, [])
        return [
            LookupHit(chunk=e.chunk, layer="errorCode", reason=f"errorCode/ResultCode 命中: `{key}`")
            for e in hits
        ]

    def find_by_status(self, value: Any) -> list[LookupHit]:
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
                reason=f"`{e.header}` 枚举值命中: `{key}`",
            )
            for e in hits
        ]

    # ---------- 顶层 API ----------

    def find_deterministic_hits(
        self,
        route_url: str,
        payload: dict[str, Any],
    ) -> list[LookupHit]:
        """对一条请求 (URL + payload) 做一次完整的 deterministic 扫描.

        返回顺序: route → errorCode → status → remark. 这个顺序是按"诊断价值
        递减 + 误报概率递增"排的: 路由匹配几乎不可能误报; remark substring 匹配
        最容易误报 (短串撞车), 放最后.

        payload key → lookup 类的对应关系由构造时传入的 ``*_payload_keys``
        参数决定; 任一组空 tuple 时跳过对应类查表.
        """
        out: list[LookupHit] = []
        seen: set[tuple[str, str]] = set()  # (chunk_id, layer) 去重

        def _add(hits: list[LookupHit]) -> None:
            for h in hits:
                key = (h.chunk.chunk_id, h.layer)
                if key in seen:
                    continue
                seen.add(key)
                out.append(h)

        _add(self.find_by_route(route_url))
        for v in _payload_values(payload, self._code_payload_keys):
            _add(self.find_by_code(v))
        for v in _payload_values(payload, self._status_payload_keys):
            _add(self.find_by_status(v))
        for v in _payload_values(payload, self._remark_payload_keys):
            if isinstance(v, str):
                _add(self.find_by_remark(v))
        return out


def _payload_values(payload: dict[str, Any], keys: tuple[str, ...]) -> list[Any]:
    """按 lower-case 命中从 ``payload`` 取所有匹配字段的值, 去 None / 空串.

    payload 字段名风格可能各异 (``processRemark`` / ``process_remark`` /
    ``processremark``), 全部按 lower-case 比对, 容忍前端命名风格差异.
    """
    out: list[Any] = []
    if not payload:
        return out
    lower_to_orig = {str(k).lower(): k for k in payload.keys()}
    for k in keys:
        ok = lower_to_orig.get(k)
        if ok is None:
            continue
        v = payload.get(ok)
        if v in (None, ""):
            continue
        out.append(v)
    return out
