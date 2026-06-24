"""日期格式探测 + 规范化的"无业务"基础件.

很多 Java 后端会写死期望的日期字符串格式 (e.g. ``yyyy/MM/dd``), 但客户上传的 Excel
里日期列可能是: ``datetime`` cell / 文本 ``"2024-01-15"`` / Excel 内部序列号 ``45292``
/ 五花八门的混合... 这一层负责:

    1. 把 Java 风格的格式串 (``yyyy/MM/dd`` / ``yyyy-MM-dd`` / ``yyyy.MM.dd HH:mm:ss``)
       转成 Python ``strftime`` 格式.
    2. 对单个 cell 做尽量宽松的解析, 还原成 ``datetime.date`` (或 ``datetime``).
    3. 对一整列做"格式探测 + 全部规范化", 返回规范化后的字符串列表 + 一份诊断
       ``NormalizeReport``, 上层 (业务路由) 拼"给客户的情绪价值文案"用.

设计取舍:
    - 不走 LLM. 日期格式探测是一个**确定性**问题, 拿规则跑就够了; 走 LLM 反而引入
      不确定性 + 延迟 + token 成本.
    - 探测的"源格式"用 Java 风格字符串描述 (而不是 Python ``%Y-%m-%d``), 因为
      这层报告最终是要给业务方 / Java 后端看的, 表达上跟约定一致更省心.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Java <-> Python 格式串互转
#
# Java SimpleDateFormat 的字符串里, ``M`` = 月, ``m`` = 分钟 (大小写敏感, 跟 Python
# 反着来), 所以**不能简单做 str.replace**, 必须按 token 切. 用一组按长度从大到小
# 排序的 token, 一次切一段, 没匹配上的字符 (``/`` / ``-`` / ``.`` / 空格 / ``T``)
# 原样保留作为分隔符.

_JAVA_TO_PY: list[tuple[str, str]] = [
    # 4 位
    ("yyyy", "%Y"),
    # 2 位
    ("yy", "%y"),
    ("MM", "%m"),
    ("dd", "%d"),
    ("HH", "%H"),
    ("hh", "%I"),
    ("mm", "%M"),
    ("ss", "%S"),
    # 1 位 (容错: Java 里 ``y`` / ``M`` / ``d`` 等价于 yyyy/MM/dd 不补零, Python
    # 没有"不补零"的 strftime 指示符, 一律按补零的版本走 -- 输出的字符串可能比
    # Java 端原始定义多一位 0, 业务上一般不影响).
    ("y", "%Y"),
    ("M", "%m"),
    ("d", "%d"),
    ("H", "%H"),
    ("h", "%I"),
    ("m", "%M"),
    ("s", "%S"),
]


def java_to_python_format(java_fmt: str) -> str:
    """``yyyy/MM/dd HH:mm:ss`` -> ``%Y/%m/%d %H:%M:%S``.

    按 token 切, 保留分隔符. 未识别字符原样保留.
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
# 单 cell -> date

# Excel 把日期存成"自 1899-12-30 起的天数" (1900 闰年 bug 决定了起点不是 1900-01-01).
# openpyxl 读到日期 cell 时会自动给我们 ``datetime`` 对象, 但如果用户**忘了给单元格
# 设日期格式**, openpyxl 就只能给个裸 int -- 这种 case 我们用这个 epoch 还原.
_EXCEL_EPOCH: date = date(1899, 12, 30)

# Excel 序列号合理范围: 1900-01-01 之后, 2100 年之前. 不在这个区间的小整数 (e.g.
# 12 / 100) 我们认为它是数量而不是日期, 不做转换.
_EXCEL_SERIAL_MIN: int = 1   # 1900-01-01
_EXCEL_SERIAL_MAX: int = 73415  # 2100-12-31

# 文本日期常见格式, 按经验排好优先级. 探测时按这个顺序逐个 try.
# 每一项是 (java_pattern, regex_anchor) -- regex 用于"快速否决", 不匹配就跳过
# strptime, 省一次 ValueError.
_STRING_FORMATS: list[tuple[str, re.Pattern]] = [
    ("yyyy-MM-dd HH:mm:ss", re.compile(r"^\d{4}-\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{2}:\d{2}")),
    ("yyyy/MM/dd HH:mm:ss", re.compile(r"^\d{4}/\d{1,2}/\d{1,2}[ T]\d{1,2}:\d{2}:\d{2}")),
    ("yyyy-MM-dd", re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")),
    ("yyyy/MM/dd", re.compile(r"^\d{4}/\d{1,2}/\d{1,2}$")),
    ("yyyy.MM.dd", re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}$")),
    ("yyyyMMdd", re.compile(r"^\d{8}$")),
    ("dd/MM/yyyy", re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")),  # 优先级低: 跟 MM/dd/yyyy 冲突
    ("MM/dd/yyyy", re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")),
    ("yyyy年MM月dd日", re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日$")),
]


@dataclass
class _ParsedCell:
    """单 cell 解析结果. ``date`` 字段非空就算成功, ``source_kind`` 是给 column-level
    探测拿去推断"这一列整体什么格式"用的."""
    date: date | None
    source_kind: str  # "datetime_cell" / "string:<java_fmt>" / "excel_serial" / "unparseable"


def parse_cell_date(v: object) -> _ParsedCell:
    """尽量宽松地把一个 cell 还原成 ``date``. 失败 -> ``date=None``.

    源类型识别用于上层做"这一列整体什么格式"的统计, 不影响转换结果.
    """
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
        # 浮点形式的 Excel 序列号 (带小数表示小时), 取整数部分当日期.
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
# 列级别探测与规范化

class NormalizeReport(BaseModel):
    """一列日期规范化完后的诊断信息, 给业务方拼 notice 文案用."""

    date_column: str
    target_date_format: str
    source_date_format: str
    """探测出的源格式 (Java 风格描述). 比如 ``"yyyy-MM-dd"`` / ``"datetime_cell (Excel 单元格自带日期类型)"``
    / ``"excel_serial (单元格存的是数字, 没设日期格式)"`` / ``"mixed (列里混着多种格式)"`` / ``"unknown"``."""
    source_matches_target_format: bool
    """``True`` 表示列里全是字符串且就是 ``target_date_format`` (省去转换); ``False``
    表示有 datetime_cell / excel_serial / 字符串但格式漂移等情况, 通用层已经帮业务
    方转成 ``target_date_format`` 字符串了."""
    total_rows: int
    parsed_rows: int
    unparseable_rows: int
    unparseable_samples: list[str] = []
    """无法识别的原始值前 5 条 sample, 便于业务方排查."""


# 源类型 -> 给业务方看的描述字符串.
_KIND_DESC: dict[str, str] = {
    "datetime_cell": "datetime_cell (Excel 单元格自带日期类型)",
    "excel_serial": "excel_serial (单元格存的是数字, 没设日期格式)",
}


def _describe_kind(kind: str) -> str:
    """单个 kind -> 给业务方看的描述. ``string:yyyy-MM-dd`` -> ``yyyy-MM-dd``."""
    if kind.startswith("string:"):
        return kind[len("string:"):]
    return _KIND_DESC.get(kind, kind)


def normalize_column(
    values: Iterable[object],
    *,
    date_column: str,
    target_date_format: str,
) -> tuple[list[str | None], NormalizeReport]:
    """把一列日期值规范化成 ``target_date_format`` (Java 风格) 的字符串.

    Args:
        values: 整列原始 cell 值 (按行序).
        date_column: 日期列名, 仅用于诊断报告.
        target_date_format: Java 风格目标格式, e.g. ``"yyyy/MM/dd"``.

    Returns:
        (规范化后字符串列表 (跟输入等长, 无法识别的位置是 ``None``), 诊断报告).
    """
    vals_list = list(values)
    py_target = java_to_python_format(target_date_format)

    parsed: list[_ParsedCell] = [parse_cell_date(v) for v in vals_list]

    out: list[str | None] = [
        p.date.strftime(py_target) if p.date is not None else None
        for p in parsed
    ]

    # 统计各 kind 出现次数, 决定 source_date_format 描述.
    kind_counts = Counter(p.source_kind for p in parsed if p.date is not None)

    if not kind_counts:
        source_date_format = "unknown (没有任何值能解析为日期)"
    elif len(kind_counts) == 1:
        kind = next(iter(kind_counts))
        source_date_format = _describe_kind(kind)
    else:
        # 多种 kind 共存 -> 列出排序后的描述.
        ordered = sorted(kind_counts.items(), key=lambda kv: kv[1], reverse=True)
        names = ", ".join(_describe_kind(k) for k, _ in ordered)
        source_date_format = f"mixed ({names})"

    # source_matches_target_format: 列里**全部**都是 string:<target_date_format> 才算
    # "完全匹配", 这样 notice 文案才能放心说 "跟约定一致, 没改". datetime_cell /
    # excel_serial 即使能解析出对的日期, 也算 "我帮您转过格式了", 因为客户原文件里
    # 看到的不是字符串.
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
