"""通用"宽表" Excel 解析引擎: **Python 优先, LLM 兜底** + ``register_wide_excel`` 接入面.

# Excel 引擎三分法 (按"数据规整度"选档)

| 维度 | wide | simple | complex |
|---|---|---|---|
| 客户表的差异 | 模板**完全固定** (供应商定的标准模板, 列名/位置/group 标签都不会随机飘) | 列名/日期 format/banner 偶尔漂 | 同类数据多段散布, 行号每月浮动, 段标题位置不固定 |
| LLM 角色 | 失败兜底 (Python first, 模板没飘的时候 0 LLM) | 失败兜底 (Python first) | 全程规划 (LLM first) |
| 输出形态 | 业务自定义 ``<Row>`` (id 字段平铺 + ``data: list[<Data>]``) | 通用 ``SimpleExcelResp{rows, notice}`` | 业务自定义 ``list[<Row>]`` |
| 调用形态 | M2M task (POST + GET) | M2M task | M2M task + chat 5-state 状态机 |

# 设计契约 (业务客户接入)
业务客户**只写两个文件** (``clients/<name>/`` 子包):

    1. ``schemas.py``: ``<Customer>Row(BaseModel, extra="allow")`` ── id 字段平铺 +
       ``data: list[<CustomerData>]``. ``<CustomerData>`` 至少有 ``var_name`` /
       ``value_name`` 两个字段 (默认 ``date`` / ``qty``), 多 header 客户可加更多键.
    2. ``__init__.py``: 拼一份 ``WideExcelConfig`` 实例, 然后一行
       ``register_wide_excel(config, router)`` 挂上 POST + GET.

通用层吃掉: task wrapper / phase 上报 / 路由 boilerplate / PyXL/PyExcel 配 filter /
隐藏行列处理 / Python 解析失败时的 LLM repair plan + 重跑.

# Python-first 路径 (90%+ 请求走这里, 0 LLM)

1. ``PyXL`` 读 sheet -> ``PyExcel`` 应用 ``ExcelConfig.row_filter`` / ``col_filter``
   过滤 + 隐藏行列剔除 -> 拿到 "clean matrix" (numpy ndarray, 2D, dtype=object).
2. 在 ``ExcelConfig.header_config.row`` 那一行精确匹配 ``id_map`` keys 找 id 列;
   表头其它非 id cell 用 ``var_pattern`` regex 过滤出动态列.
3. 按行 unpivot: 每个数据行 + 每个动态列 -> 一条 ``{var_name, value_name}`` 记录.

# LLM 兜底路径 (Python 任何阶段失败触发)

触发条件:
- ``id_map`` 任何一个 key 在表头行找不到 (供应商悄咪咪改了列名).
- ``var_pattern`` 0 列匹配 (动态列前缀飘了, 例如 "需求合计" 改成 "需求总计").
- unpivot 后 0 数据行 (id 列对了, 但 qty 列全空).
- header_row 越界 (顶部多/少了几行 banner).

LLM 看 clean matrix 骨架 + 失败原因, 输出 ``WideExcelRepairPlan`` (header_row_1b
+ id_columns + dynamic_columns + summary), 引擎重跑 manual unpivot. 再失败 5xx.

# 配置写作要点

- ``excel_config.header_config.row`` 是 **clean matrix 内的 1-based 行号** (隐藏剔除 +
  row/col filter 应用之后), 不是原始 sheet 的行号. e.g. 原 sheet R5 是表头, 但 R1-R3
  全隐藏, 那么 clean matrix 里它在 row 2.
- ``col_filter`` 跟 ``var_pattern`` 配合: 模板里既有 "需求合计" group 又有 "结余" /
  "累计消耗" 等其它 group 时, 用 ``col_filter.blocks`` 引用 group 行 + ``mode="use"``
  + ``positions=[id 列 1-based 位置...]`` 一次砍剩 id 列 + 该 group 列, 然后
  ``var_pattern`` 在 clean matrix 表头行上正则匹配真正的动态列 (e.g. ISO 日期).
- ``read_hidden=False`` 是默认, geely 这种供应商表往往有 1000+ 隐藏行, 必须跳.

"""
from __future__ import annotations

import io
import re
from collections import Counter
from dataclasses import dataclass, replace
from typing import Generic, Literal, TypeVar

import numpy as np
import pandas as pd
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException
from litellm.exceptions import APIError
from pydantic import BaseModel, Field

from ._common import excel_upload, _is_nullish
from .core import ExcelConfig, PyExcel, PyXL
from infra.llm import aclient
from ..chat import ChatPlan
from infra.settings import DEFAULT_MODEL
from infra.task import TaskResult, TaskStatus, create_task, get_task, set_phase
from infra.utils import exception_detail


# ---------------------------------------------------------------------------
# Client config (业务子包接入唯一要填的 dataclass)

@dataclass
class WideExcelConfig:
    """单个客户接入 wide-excel parser 所需的全部配置.

    设计跟 ``SimpleExcelConfig`` / ``ComplexExcelConfig`` 对称: 业务方只填这些字段,
    通用层吃掉 task wrapper / phase 上报 / 路由 boilerplate / 异常 -> error / LLM repair.

    Args:
        label: URL-safe identifier (e.g. ``"geely-ms"``). 用作默认 path prefix +
            OpenAPI summary 标签. **必须 URL-safe** (不含空格 / 中文 / ``/``).
        id_map: ``{Excel 列名 -> JSON 字段名}``. 通用层根据 keys 在表头行精确匹配;
            values 是输出 dict 的字段名. e.g.
            ``{"物料号": "partNo", "供应商编码": "supplierCode"}``.
        row_schema: 单条解析结果的 pydantic 类型, 用作 OpenAPI response_model 收窄.
            必须含 ``data: list[<DataPoint>]`` 字段; id 字段按 ``id_map`` values 命名.
            推荐 ``model_config = ConfigDict(extra="allow")`` 让 LLM 兜底路径能多塞
            字段而不报错.
        excel_config: 通用过滤 + 表头识别配置 (``ExcelConfig`` from
            ``apdfi.excel.core``). 主要用 ``row_filter`` 跳噪音行 / ``col_filter``
            配合 R<n> 上的 group 标签限制动态列范围 / ``header_config.row`` 指定
            真正的表头行 (注意: 这里的 row 是 **clean matrix 内坐标**, 即隐藏行/列
            剔除 + filter 应用之后的 row 编号).
        var_pattern: regex 字符串, 匹配动态列的列名. 在 clean matrix 表头行上对每个
            "非 id" cell 做 ``re.search`` 测试, 命中即视为动态列. 例:
            ``r"^20\\d{2}-"`` 匹配 ISO 日期; ``r"^需求合计"`` 匹配前缀.
        var_name: unpivot 时给 var 列的字段名 (默认 ``"date"``). 出现在每条 ``data``
            记录里, 跟 ``row_schema`` 的 Data class 字段名对齐.
        value_name: unpivot 时给 value 列的字段名 (默认 ``"qty"``).
        var_header_rows: clean matrix 内 1-based 行号列表, 用来**拼合 var_value**
            (e.g. 多层表头里 group + date 一起描述一个动态列). 默认 ``None`` ->
            退化成 ``[excel_config.header_config.row]`` (即跟 id_map 同一行, 单层).
            场景: yy 客户的动态列是 (车型, 日期) 二维, 车型在 R1, 日期在 R3, 设
            ``var_header_rows=[1, 3]`` 让 ``var_value`` 自动拼成 ``"CX11 A3 L/2025-12-30"``.
            id 列的检测 (跟 ``id_map`` 精确匹配) 仍只用 ``header_config.row``;
            ``var_pattern`` 也只在 ``header_config.row`` 那一行做 regex 测试.
        primary_id: ``id_map`` 的某个 **key** (Excel 列名), 标记"主 id 列" -- 该列
            cell 为空 (None / NaN / 空白) 的行会被视作噪音行直接跳过 (e.g. 模板顶部
            的 banner / 公式 ``#REF!`` 错误行 / 阶段标题行往往只填了部分 id 单元格,
            主 id 一般是空的). 默认 ``None`` -> 取 ``id_map`` 的**第一个 key**, 一般
            刚好是业务最主键 (e.g. ``"物料号"`` / ``"零件号"``); 主键不在 id_map 第一
            位的客户显式传一下.
        description: 一句中文 prose 描述本次客户的报表是什么 (业务方写, 通用层不猜),
            会塞到 ``WideExcelTaskAck.headline`` 给前端 render 'AI Tip' 标题用. 例:
            ``"吉利 hzw3 缺口信息表 (零件号 + 当前库存 + 未来若干天的缺口数)"``. 留空
            就走默认 ``f"按 {label} 的固定模板"``, 也能跑, 只是 headline 没业务感.
        repair_prompt: LLM 兜底用的 system prompt 模板. 占位符 ``{id_map}`` /
            ``{var_pattern}`` / ``{var_name}`` / ``{value_name}`` 会在调 LLM 前
            ``str.replace`` 进去. 留空走 ``_DEFAULT_REPAIR_PROMPT``.
        sheet: 默认 sheet 选择器 (1-based int 或 sheet name). 路由层会用 form
            上传的 sheet 覆盖此默认.
        read_hidden: 是否读隐藏行/列, 默认 ``False``. geely 这种供应商表往往有
            1000+ 隐藏行, 必须跳.
    """

    label: str
    id_map: dict[str, str]
    row_schema: type[BaseModel]
    excel_config: ExcelConfig
    var_pattern: str
    var_name: str = "date"
    value_name: str = "qty"
    var_header_rows: list[int] | None = None
    primary_id: str | None = None
    description: str = ""
    repair_prompt: str = ""
    sheet: int | str = 1
    read_hidden: bool = False

    def __post_init__(self):
        if not self.label:
            raise ValueError("WideExcelConfig.label 不能为空")
        if "/" in self.label or " " in self.label:
            raise ValueError(
                f"WideExcelConfig.label={self.label!r} 必须 URL-safe (不含空格 / '/')"
            )
        if not self.id_map:
            raise ValueError(
                "WideExcelConfig.id_map 不能为空, 业务方至少要约定 1 列 id"
            )
        dup_values = [v for v, c in Counter(self.id_map.values()).items() if c > 1]
        if dup_values:
            raise ValueError(
                f"WideExcelConfig.id_map values (JSON 字段名) 不允许重复, 重复值: {dup_values}"
            )
        if self.primary_id is None:
            self.primary_id = next(iter(self.id_map))
        elif self.primary_id not in self.id_map:
            raise ValueError(
                f"WideExcelConfig.primary_id={self.primary_id!r} 必须是 id_map 的 key 之一. "
                f"实际 id_map keys: {list(self.id_map)}"
            )
        try:
            self._var_re = re.compile(self.var_pattern)
        except re.error as e:
            raise ValueError(
                f"WideExcelConfig.var_pattern 不是合法 regex: {self.var_pattern!r} ({e})"
            )
        if self.var_header_rows is not None:
            if not self.var_header_rows:
                raise ValueError(
                    "WideExcelConfig.var_header_rows 显式传 None 走默认; 传空 list 不合法"
                )
            for r in self.var_header_rows:
                if not isinstance(r, int) or r < 1:
                    raise ValueError(
                        f"WideExcelConfig.var_header_rows 元素必须是 >=1 的 int, 当前: {self.var_header_rows!r}"
                    )


# ---------------------------------------------------------------------------
# Top-level response (parallel to SimpleExcelResp; generic over client Row schema)


T = TypeVar("T", bound=BaseModel)


class WideExcelResp(BaseModel, Generic[T]):
    """Wide-excel 解析的最终结果. ``rows`` 类型在 register 时由客户的 ``row_schema`` 收窄."""

    notice: str = Field(
        ...,
        description="给业务方看的中文文案 (情绪价值). Python 路径走模板, LLM 兜底走 LLM 写好的 summary.",
    )
    rows: list[T] = Field(
        ...,
        description="unpivot 后的行级结果, 每行 = id 字段平铺 + ``data: list[{var_name, value_name}]``.",
    )
    via: Literal["python", "llm_repair"] = Field(
        ...,
        description="走的哪条路径: 纯 Python / LLM 兜底修复. Java 端做监控时能看到 LLM 调用率.",
    )


class WideExcelMappingPreview(BaseModel):
    """单条 ``id_map`` 映射的列表化形式 (前端 render '字段映射' 表格用).

    跟原始 ``id_map: dict[str, str]`` 等价, 只是变成 list of struct, 前端 ``map``
    成 ``<tr>`` / chip 更直观. 不带次序语义; 跟 ``id_map`` 的插入顺序一致.
    """
    excel_column: str = Field(..., description="Excel 中的列含义, 例: '物料号'")
    json_field: str = Field(..., description="输出 JSON 字段名, 例: 'partNo'")


class WideExcelTaskAck(BaseModel):
    """POST 建任务时立刻返回的 ACK: ``task_id`` + **当前请求实际用的预设 preview** +
    **给前端的情绪价值字段** (headline / processing_steps / id_field_preview /
    dynamic_strategy).

    设计跟 ``SimpleExcelTaskAck`` / ``XpengZqIntent`` 一脉相承: 让前端拿到响应即可立刻
    渲染 "AI Tip" / "我会按这些规则解析您的文件" 占位 UI, 不必等任务跑完, 也不必前端
    拼业务相关的中文 prose. 模板**完全固定**意味着 sheet 还没 parse 我们就知道会做啥
    -- 整个预览全是从 ``WideExcelConfig`` 推导出来的, 0 LLM 调用, 0 等待.

    M2M 调用方 (Java 后端) 想要纯结构化的字段也都在, prose 字段 (headline /
    processing_steps / dynamic_strategy) 不需要可以无视, 不影响 ``id_map`` /
    ``var_pattern`` 等程序化字段的可用性.
    """

    task_id: str = Field(..., description="任务 id, 用来 GET 轮询拿结果.")
    file_name: str = Field(..., description="客户上传的原始文件名 (可能是 ``'(未命名)'``).")
    label: str = Field(..., description="客户标识 (e.g. ``'geely-ms'``).")

    # ---- 情绪价值层 (前端拿来直接 render, 不必猜业务上下文) ----
    headline: str = Field(
        ...,
        description="一句中文 prose 描述本次任务: 文件名 + 哪个客户模板 + 大概会抽什么. 给前端 'AI Tip' 标题区域用.",
    )
    processing_steps: list[str] = Field(
        ...,
        description="处理步骤列表, 给前端 ``<ol>`` render. 每条一个动作 (跳隐藏行 / 找 id 列 / 匹配动态列 / unpivot / LLM 兜底).",
    )
    id_field_preview: list[WideExcelMappingPreview] = Field(
        ...,
        description="字段映射预设, 给前端 mapping 区域 render 表格. 跟 ``id_map`` 等价, 列表化方便 v-for.",
    )
    dynamic_strategy: str = Field(
        ...,
        description="动态列识别策略一句话描述. 单层 header 时是 'var 取该列表头 cell', 多级 header 时说明拼合规则 (例: 'R1+R3 拼成 var=车型/日期').",
    )

    # ---- 原始配置快照 (Java 后端转发 / 程序化访问) ----
    id_map: dict[str, str] = Field(..., description="本次任务用的 Excel 列名 -> JSON 字段名 map (跟 ``id_field_preview`` 等价的 dict 形式).")
    var_pattern: str = Field(..., description="本次任务用的动态列 regex.")
    var_name: str = Field(..., description="unpivot 时 var 字段名 (e.g. ``'date'``).")
    value_name: str = Field(..., description="unpivot 时 value 字段名 (e.g. ``'qty'``).")
    var_header_rows: list[int] | None = Field(
        None,
        description=(
            "拼合 var_value 用的 clean matrix 1-based 行号列表. ``None`` = 单层 header "
            "(默认). 多级 header 客户传 ``[1, 3]`` 之类."
        ),
    )


# ---------------------------------------------------------------------------
# LLM repair plan schemas


class ColumnLocation(BaseModel):
    """LLM 兜底时, 一条 'id 列定位' 记录."""

    expected_name: str = Field(..., description="``config.id_map`` 的 key (一字不差).")
    actual_name: str = Field(..., description="该列在 sheet 实际表头里的中文名 (跟 expected_name 可能漂移).")
    col_index_1b: int = Field(..., ge=1, description="该列在 clean matrix 中的 1-based 列号.")


class WideExcelDynamicLocation(BaseModel):
    """LLM 兜底时, 一条 '动态列定位 + 该列对应的 var 值' 记录."""

    col_index_1b: int = Field(..., ge=1, description="该动态列在 clean matrix 中的 1-based 列号.")
    var_value: str = Field(
        ...,
        description=(
            "该动态列对应的 var 值, 一般是该列表头的 cell 值. 多层表头 (group + date) "
            "压扁成 'group/date' 字符串. 单元格是 datetime 时取 ISO 字符串."
        ),
    )


class WideExcelRepairPlan(ChatPlan):
    """Python 失败时 LLM 给出的修复计划. ``summary`` 是给业务方看的 prose (继承自 ``ChatPlan``)."""

    header_row_1b: int = Field(
        ...,
        ge=1,
        description="clean matrix 中真正的表头所在 1-based 行号.",
    )
    id_columns: list[ColumnLocation] = Field(
        ...,
        description=(
            "id 列的实际定位. ``expected_name`` 集合**必须**跟 ``config.id_map`` keys "
            "完全相等 (不多不少, 一字不差); 服务端会 post-validate, 对不上直接 5xx."
        ),
    )
    dynamic_columns: list[WideExcelDynamicLocation] = Field(
        ...,
        description="动态列的实际定位. 至少 1 项, 不能空.",
    )


# ---------------------------------------------------------------------------
# Internal: failure type + cell helpers


class _PythonParseFailure(Exception):
    """Python 路径任何阶段失败时抛, 触发 LLM repair, 不出 route."""

    def __init__(self, reason: str, ctx: dict | None = None):
        self.reason = reason
        self.ctx = ctx or {}
        super().__init__(reason)



def _to_number(v: object) -> int | float | None:
    """cell -> number (int 优先, 否则 float). 空 / 非数字 -> None."""
    if _is_nullish(v):
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v != v:
            return None
        if isinstance(v, float) and v.is_integer():
            return int(v)
        return v
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            try:
                return float(s)
            except ValueError:
                return None
    return None


def _to_id_value(v: object) -> object:
    """id 字段的 cell 清洗: NaN/NaT/空白 -> None, 数字 -> str (物料号等常是数字格式).
    
    id 字段在业务 schema 里往往定义为 str，但 Excel 里可能是数字列（无前导零）。
    这里统一转成字符串确保 pydantic 校验通过。
    """
    if _is_nullish(v):
        return None
    # 数字转字符串
    if isinstance(v, bool):
        return None  # bool 不当 id 值处理
    if isinstance(v, float):
        if v != v:  # NaN
            return None
        if v.is_integer():
            return str(int(v))
        return str(v)
    if isinstance(v, int):
        return str(v)
    # 其它情况原样返回（字符串等）
    return v


def _header_text(v: object) -> str | None:
    """表头 cell 标准化: 空 -> None; datetime/date ISO 化 (时间全零自动退化成 ``YYYY-MM-DD``); 其它 strip 后返回.

    日期表头 cell 实际类型可能是 ``datetime`` (带 ``T00:00:00`` 后缀) 或纯 ``date``,
    给 LLM 看跟做 var_pattern 匹配时统一成最简洁的 ISO 字符串, 避免业务方前端拿到
    ``"2025-12-28T00:00:00"`` 还得自己 split. ``var_value`` 输出也用同一份归一化结果.
    """
    if _is_nullish(v):
        return None
    if hasattr(v, "isoformat"):
        try:
            iso = v.isoformat()
            if isinstance(iso, str) and iso.endswith("T00:00:00"):
                return iso[:10]
            return iso
        except Exception:
            pass
    s = str(v).strip()
    return s or None


# ---------------------------------------------------------------------------
# Clean matrix loader + skeleton renderer


def _build_clean_matrix(
    raw: bytes,
    *,
    config: WideExcelConfig,
) -> np.ndarray:
    """读 PyXL → 应用 ExcelConfig 过滤 → 隐藏行/列剔除 → 返回含表头行的 ndarray.

    跟 ``PyExcel.matrix`` 一致, 但暴露给上层让我们能控制 header 行号判断 (``PyExcel.to_frame``
    会把 header 削掉, 我们要保留 header 行做 LLM 兜底定位).
    """
    xl = PyXL(file=io.BytesIO(raw), sheet=config.sheet)
    pe = PyExcel(xl, config.read_hidden, config.excel_config)
    return pe.matrix


def _build_skeleton(matrix: np.ndarray, *, max_rows: int = 16, max_cols: int = 64) -> str:
    """LLM 看的紧凑骨架. wide 场景过滤后矩阵通常很小 (<200 行 x <100 列), 一次塞下."""
    n_rows, n_cols = matrix.shape
    rmax = min(max_rows, n_rows)
    cmax = min(max_cols, n_cols)

    header = [
        f"matrix_shape: rows={n_rows}, cols={n_cols} (showing first {rmax}x{cmax})",
        f"col_indices (1-based): {list(range(1, cmax + 1))}",
        "format: R<row_1b> | <col_1b>=<value> | <col_1b>=<value> | ...",
        "",
    ]
    body: list[str] = []
    for r in range(rmax):
        cells: list[str] = []
        for c in range(cmax):
            v = matrix[r, c]
            if _is_nullish(v):
                cells.append(f"{c + 1}=")
                continue
            if hasattr(v, "isoformat"):
                try:
                    s = v.isoformat()
                except Exception:
                    s = str(v)
            else:
                s = str(v)
            if len(s) > 30:
                s = s[:30] + "..."
            cells.append(f"{c + 1}={s}")
        body.append(f"R{r + 1:02d} | " + " | ".join(cells))

    if n_cols > cmax:
        body.append(f"... ({n_cols - cmax} more cols not shown)")
    if n_rows > rmax:
        body.append(f"... ({n_rows - rmax} more rows not shown)")

    return "\n".join(header + body)


# ---------------------------------------------------------------------------
# Manual unpivot (Python 路径跟 LLM repair 路径共用)


def _extract_rows_wide(
    matrix: np.ndarray,
    *,
    header_row_1b: int,
    id_col_to_1b: dict[str, int],
    primary_id_excel_name: str,
    dynamic_cols: list[tuple[int, str]],
    output_key_for: dict[str, str],
    var_name: str,
    value_name: str,
) -> list[dict]:
    """LLM repair 路径: 按 col_index_1b 从 matrix 里取数并 unpivot.

    数据行 = header 之后所有行; 主 id 为空的行视为噪音丢弃.
    """
    data = matrix[header_row_1b:]  # 0-based slice: header_row_1b (1-based) 之后
    if data.shape[0] == 0:
        raise _PythonParseFailure(f"header_row={header_row_1b} 之后没有数据行")

    df = pd.DataFrame(data)  # columns = 0, 1, 2, ...
    primary_0b = id_col_to_1b[primary_id_excel_name] - 1
    df = df[df[primary_0b].apply(lambda v: not _is_nullish(v))].reset_index(drop=True)

    id_col_mapping = {v - 1: output_key_for[k] for k, v in id_col_to_1b.items()}

    rows: list[dict] = []
    for _, row in df.iterrows():
        id_vals = {json_name: _to_id_value(row[col_0b]) for col_0b, json_name in id_col_mapping.items()}
        data_list = []
        for col_1b, var_value in dynamic_cols:
            qty = _to_number(row.get(col_1b - 1))
            if qty is None:
                continue
            data_list.append({var_name: var_value, value_name: qty})
        if not data_list:
            continue
        rows.append({**id_vals, "data": data_list})

    if not rows:
        raise _PythonParseFailure(
            "unpivot 后 0 行 (LLM plan 给的动态列全是 null/非数字)",
        )

    return rows


# ---------------------------------------------------------------------------
# Python path


def _try_python_path(
    matrix: np.ndarray,
    config: WideExcelConfig,
) -> tuple[list[dict], int]:
    """纯 Python: 把 matrix 转成 DataFrame, 用 ``df.rename`` 重命名 id 列, 缺列直接
    抛 ``_PythonParseFailure`` 让 LLM 兜底.

    Returns:
        ``(rows, n_dynamic_cols)``. 上层用 n 拼成功 notice.
    """
    n_rows, n_cols = matrix.shape
    if n_rows == 0 or n_cols == 0:
        raise _PythonParseFailure("过滤后矩阵是空的 (0 行或 0 列)")

    hc = config.excel_config.header_config

    # 当有 block 但 row 没有显式设置（仍为默认 1）时，用 block 中最大的行号作为列标题行
    header_row = hc.row
    if hc.block and hc.row == 1 and any(v > 1 for v in hc.block.values()):
        header_row = max(hc.block.values())

    if header_row > n_rows:
        raise _PythonParseFailure(
            f"配置的 header_row={hc.row} 超出 clean matrix 行数 {n_rows}",
        )

    if hc.block:
        # 与 docs 逻辑对齐: block 走 MultiIndex, 但动态值默认取最后一层(通常是日期)
        names, block_rows = zip(*hc.block.items())
        level_idx = {name: i for i, name in enumerate(names)}
        df = pd.DataFrame(matrix[hc.rows:])
        df.columns = pd.MultiIndex.from_arrays(
            matrix[[i - 1 for i in block_rows]],
            names=names,
        )
        df.columns = df.columns.map(lambda x: tuple(map(str, x)))

        id_col_set = set(config.id_map)
        # 行内 id 列: 通过最后一层列名匹配, 记录列位置避免重复标签歧义
        id_col_pos_for: dict[str, int] = {}
        for pos, col in enumerate(df.columns):
            if col[-1] in id_col_set and col[-1] not in id_col_pos_for:
                id_col_pos_for[col[-1]] = pos

        # block 维度名本身也允许作为 id 字段来源 (如 key='车型' 来自第一层 header)
        missing = [
            k for k in config.id_map
            if k not in id_col_pos_for and k not in level_idx
        ]
        if missing:
            raise _PythonParseFailure(
                f"row {header_row} 表头里找不到这些约定 id 列: {missing}",
                ctx={
                    "actual_columns": sorted({c[-1] for c in df.columns}),
                    "block_levels": list(level_idx),
                    "missing": missing,
                },
            )

        # 主 id 只支持行内列 (跟 docs 的 set_index(frame) 语义一致)
        if config.primary_id in level_idx:
            raise _PythonParseFailure(
                f"primary_id={config.primary_id!r} 不能来自 block 维度, 请改成行内 id 列",
            )
        primary_pos = id_col_pos_for[config.primary_id]
        mask = df.iloc[:, primary_pos].apply(lambda v: not _is_nullish(v))
        df = df[mask.to_numpy()].reset_index(drop=True)

        dynamic_cols: list[tuple[int, tuple[str, ...], str]] = []
        var_re = config._var_re
        for pos, col in enumerate(df.columns):
            # id 列跳过；动态列在最后一层匹配 var_pattern
            if col[-1] in id_col_set:
                continue
            if not var_re.search(col[-1]):
                continue
            # docs 风格: data 里默认只放 date/qty, 取最后一层作为 var_value
            dynamic_cols.append((pos, col, col[-1]))

        if not dynamic_cols:
            raise _PythonParseFailure(
                f"row {header_row} 表头里没有任何 cell 匹配 var_pattern={config.var_pattern!r}",
                ctx={"candidate_columns": sorted({c[-1] for c in df.columns})},
            )

        rows_by_id: dict[tuple, dict] = {}
        for _, row in df.iterrows():
            for col_pos, col, var_value in dynamic_cols:
                qty = _to_number(row.iloc[col_pos])
                if qty is None:
                    continue
                id_vals: dict[str, object] = {}
                for excel_key, json_key in config.id_map.items():
                    if excel_key in level_idx:
                        id_vals[json_key] = _to_id_value(col[level_idx[excel_key]])
                    else:
                        id_vals[json_key] = _to_id_value(row.iloc[id_col_pos_for[excel_key]])

                row_key = tuple(id_vals[k] for k in config.id_map.values())
                if row_key not in rows_by_id:
                    rows_by_id[row_key] = {**id_vals, "data": []}
                rows_by_id[row_key]["data"].append(
                    {config.var_name: var_value, config.value_name: qty}
                )

        rows = list(rows_by_id.values())
    else:
        raw_header = [_header_text(v) for v in matrix[header_row - 1]]
        df = pd.DataFrame(matrix[hc.rows:], columns=raw_header)

        missing = [k for k in config.id_map if k not in df.columns]
        if missing:
            raise _PythonParseFailure(
                f"row {header_row} 表头里找不到这些约定 id 列: {missing}",
                ctx={"actual_columns": [h for h in raw_header if h], "missing": missing},
            )

        df = df[df[config.primary_id].apply(lambda v: not _is_nullish(v))].reset_index(drop=True)

        var_header_rows = config.var_header_rows or [header_row]
        for vhr in var_header_rows:
            if vhr > n_rows:
                raise _PythonParseFailure(
                    f"配置的 var_header_rows 含 {vhr}, 超出 clean matrix 行数 {n_rows}",
                )

        id_col_set = set(config.id_map)
        var_re = config._var_re
        dynamic_cols: list[tuple[str, str]] = []
        for i, col_name in enumerate(raw_header):
            if col_name in id_col_set or col_name is None:
                continue
            if not var_re.search(col_name):
                continue
            parts = [_header_text(matrix[r - 1, i]) for r in var_header_rows]
            parts = [p for p in parts if p]
            var_value = "/".join(parts) if parts else col_name
            dynamic_cols.append((col_name, var_value))

        if not dynamic_cols:
            raise _PythonParseFailure(
                f"row {header_row} 表头里没有任何 cell 匹配 var_pattern={config.var_pattern!r}",
                ctx={"candidate_columns": [h for h in raw_header if h]},
            )

        df = df.rename(columns=config.id_map)
        id_json_names = list(config.id_map.values())

        rows: list[dict] = []
        for _, row in df.iterrows():
            id_vals = {k: _to_id_value(row[k]) for k in id_json_names}
            data = []
            for raw_col, var_value in dynamic_cols:
                qty = _to_number(row.get(raw_col))
                if qty is None:
                    continue
                data.append({config.var_name: var_value, config.value_name: qty})
            if not data:
                continue
            rows.append({**id_vals, "data": data})

    if not rows:
        raise _PythonParseFailure(
            "unpivot 后 0 行 (id 列对了, 动态列也找了, 但所有行的 qty 都是 null/非数字)",
        )

    return rows, len(dynamic_cols)


# ---------------------------------------------------------------------------
# LLM repair path


_DEFAULT_REPAIR_PROMPT = """\
你是 Excel 解析助手. 业务方上传的"宽表" Excel 用纯 Python 解析失败了 (可能是某个
约定 id 列被改名, 或动态列 prefix 飘了, 或表头不在约定行). 请你看一份 sheet 骨架
(已过滤隐藏行/列 + 应用 row/col filter), 帮 Python 重新定位真正的表头 + 业务方关心
的每一列 + 全部动态列, 让流程继续走通.

# 业务上下文
- 业务方关心的 id 列 (Excel 列名 -> JSON 字段名):
{id_map}
- 动态列 (要 unpivot 成时序数据的列) 的列名匹配 regex: `{var_pattern}`
  动态列的 var 值取该列**表头 cell** (一般是日期, 例如 '2025-12-01').
- 输出每行 = id 字段平铺 + `data: [{{var_name}: var_value, {value_name}: qty}, ...]`.

# 你的任务 (严格按 WideExcelRepairPlan schema 输出)
1. header_row_1b: clean matrix 中真正的表头所在 1-based 行号.
2. id_columns: list, 长度跟 id_map 完全一致. 每项含:
   - expected_name: id_map 的 key (一字不差, 服务端会做集合相等校验, 不能多也不能少).
   - actual_name: 该列在 sheet 实际表头里的中文名 (跟 expected_name 一样也行).
   - col_index_1b: 该列在 clean matrix 中的 1-based 列号 (按表头中文 + 该列样本值综合判断).
3. dynamic_columns: list, 全部要 unpivot 的动态列, 至少 1 项. 每项含:
   - col_index_1b: 该列在 clean matrix 中的 1-based 列号.
   - var_value: 该列对应的 var 值 (一般是该列表头的 cell 值; 单元格是 datetime 时
     取 ISO 字符串).
4. summary: 给业务方看的中文 prose. **注意人称区分**: 直接展示给业务方, 用第一人称
   "我" 称呼自己 (assistant), 用敬称 "您" 称呼业务方. 不超过 4 句话, 风格示例:
   "我看了下您上传的文件, 之前约定的 id 列 '物料号' 现在叫 '物料编码', 我已经按
   您原本的约定字段重排输出, 您直接用即可."
   可适当用 Markdown 突出关键信息: `**加粗**` 用于数字, `` `code` `` 用于字段名/列名.

# 注意
- summary 严禁用 "您是 xxx 助手" / "你是 xxx" 把人称搞反.
- summary 不要谈技术细节 (栈帧 / strptime / openpyxl 等).
- id_columns 的 expected_name **必须**跟 id_map 的 keys 集合完全相等.
- dynamic_columns 至少 1 项, 不能空.
- 严格按 schema 输出 JSON, 不要解释, 不要 markdown 代码块.
"""


def _format_id_map(id_map: dict[str, str]) -> str:
    return "\n".join(f"  - {k!r} -> {v!r}" for k, v in id_map.items())


def _render_repair_prompt(config: WideExcelConfig) -> str:
    base = config.repair_prompt or _DEFAULT_REPAIR_PROMPT
    return (
        base
        .replace("{id_map}", _format_id_map(config.id_map))
        .replace("{var_pattern}", config.var_pattern)
        .replace("{var_name}", config.var_name)
        .replace("{value_name}", config.value_name)
    )


async def _repair_via_llm(
    matrix: np.ndarray,
    *,
    config: WideExcelConfig,
    failure: _PythonParseFailure,
    model: str,
    retry: int,
) -> WideExcelRepairPlan:
    """让 LLM 看 clean matrix 骨架 + Python 失败原因, 输出修复 plan, server 端 post-validate.

    Plan 必须满足: ``id_columns`` 的 ``expected_name`` 集合跟 ``config.id_map`` 的
    keys 完全相等, 且 ``dynamic_columns`` 非空. 不满足直接 5xx, 不二次调 LLM.
    """
    skeleton = _build_skeleton(matrix)
    user_msg = (
        "# Python 解析失败信息\n"
        f"约定的 id_map (Excel 列名 -> JSON 字段名):\n"
        f"{_format_id_map(config.id_map)}\n"
        f"约定的动态列 regex: {config.var_pattern!r}\n"
        f"约定的表头 row (clean matrix 内): {config.excel_config.header_config.row}\n"
        f"失败原因: {failure.reason}\n"
        f"额外上下文: {failure.ctx}\n"
        "\n"
        "# Clean Matrix 骨架 (隐藏行/列已剔除, row/col filter 已应用)\n"
        f"{skeleton}\n"
        "\n"
        "请严格按 WideExcelRepairPlan schema 输出 JSON: header_row_1b, id_columns, "
        "dynamic_columns, summary."
    )
    messages = [
        {"role": "system", "content": _render_repair_prompt(config)},
        {"role": "user", "content": user_msg},
    ]
    try:
        plan = await aclient.chat.completions.create(
            model=model,
            messages=messages,
            response_model=WideExcelRepairPlan,
            max_retries=max(retry, 0),
        )
    except APIError as e:
        raise HTTPException(
            status_code=getattr(e, "status_code", 500),
            detail=getattr(e, "message", str(e)),
        )

    expected = set(config.id_map)
    actual = {c.expected_name for c in plan.id_columns}
    if expected != actual:
        raise HTTPException(
            status_code=500,
            detail=(
                "LLM 输出的 id_columns 跟 config.id_map 对不上. "
                f"缺: {sorted(expected - actual)}, 多: {sorted(actual - expected)}. "
                f"plan.summary: {plan.summary}"
            ),
        )
    if not plan.dynamic_columns:
        raise HTTPException(
            status_code=500,
            detail=(
                "LLM 输出的 dynamic_columns 为空, 无法 unpivot. "
                f"plan.summary: {plan.summary}"
            ),
        )

    return plan


# ---------------------------------------------------------------------------
# Success path notice


def _render_success_notice(
    config: WideExcelConfig,
    *,
    n_rows: int,
    n_dynamic_cols: int,
) -> str:
    """成功走 Python 路径后的 notice. 多级 header 时额外提一下 var_value 拼合规则."""
    n_id = len(config.id_map)
    if config.var_header_rows and len(config.var_header_rows) > 1:
        rows_str = "+".join(f"R{r}" for r in config.var_header_rows)
        var_desc = (
            f"**{n_dynamic_cols} 个**动态列 (多级 header `{rows_str}` 拼合, "
            f"var 字段格式是 `<level1>/<level2>/...` 复合 key)"
        )
    else:
        var_desc = (
            f"**{n_dynamic_cols} 个**动态列 (按正则 `{config.var_pattern}` 匹配表头 cell)"
        )
    return (
        f"我看了下您上传的文件, 您约定的 **{n_id} 个** id 列我都在表头行找到了, "
        f"一共匹配到 {var_desc}, "
        f"unpivot 后共 **{n_rows} 行**已按 JSON 字段名重排, 您直接用即可."
    )


# ---------------------------------------------------------------------------
# ACK preview builder (POST 阶段从 config 推导出来给前端 render 占位 UI)
#
# 模板**完全固定**意味着 sheet 还没 parse 我们就知道会做啥, 整個預覽全是從
# ``WideExcelConfig`` 推導出來的, 0 LLM 調用, 0 等待. M2M 調用方 (Java 後端) 想要純
# 结构化的字段也都在 ack 上, prose 字段不需要可以無視.


def _build_processing_steps(config: WideExcelConfig) -> list[str]:
    """从 config 推出一串处理步骤描述 (前端 ``<ol>`` render). 跟 engine 真实跑的步骤一一对应."""
    steps: list[str] = []
    if not config.read_hidden:
        steps.append("跳过隐藏行/列 (供应商表常带几百到上千的隐藏行, 不跳算量爆炸)")

    rf = config.excel_config.row_filter
    cf = config.excel_config.col_filter
    rf_actions: list[str] = []
    if rf.before:
        rf_actions.append(f"砍掉前 **{rf.before}** 行")
    if rf.after is not None:
        rf_actions.append(f"砍掉第 **{rf.after + 1}** 行起的剩余行")
    if rf.positions:
            rf_actions.append(f"针对位置 `{rf.positions}`")
    if rf.blocks:
        tags = " / ".join(f"`{b.refer_name}`" for b in rf.blocks)
        rf_actions.append(f"按 {tags} 标签筛行")
    if rf_actions:
        steps.append(f"按 `row_filter ({rf.mode})` {' / '.join(rf_actions)} 干掉噪音行")
    if cf.blocks:
        block_names = " / ".join(f"`{b.refer_name}`" for b in cf.blocks)
        block_rows = sorted({b.refer_index for b in cf.blocks})
        rows_str = "+".join(f"`R{r}`" for r in block_rows)
        steps.append(
            f"用 {rows_str} 上的 group 标签 {block_names} 限制动态列范围, 砍掉无关 group"
        )

    hc = config.excel_config.header_config
    n_id = len(config.id_map)
    id_cn_names = list(config.id_map)
    sample_names = id_cn_names if n_id <= 5 else id_cn_names[:5] + [f"... +{n_id - 5} 列"]
    sample_str = ", ".join(f"`{n}`" for n in sample_names)
    steps.append(
        f"在 `row {hc.row}` 找您约定的 **{n_id} 个** id 列 ({sample_str})"
    )

    if config.var_header_rows and len(config.var_header_rows) > 1:
        rows_str = "+".join(f"`R{r}`" for r in config.var_header_rows)
        steps.append(
            f"用正则 `{config.var_pattern}` 在 `row {hc.row}` 匹配动态列, 然后拼合 "
            f"{rows_str} 各行的 cell 当 var_value (复合 key, 例: `CX11 A3 L/2025-12-30`)"
        )
    else:
        steps.append(
            f"用正则 `{config.var_pattern}` 在 `row {hc.row}` 匹配动态列, `var_value` 取该列表头 cell"
        )

    steps.append(
        f"按行 unpivot, 主 id 列 `{config.primary_id}` 为空的行视为噪音直接丢弃; "
        f"输出每行 = id 字段平铺 + `data: list[{{{config.var_name}, {config.value_name}}}]`"
    )
    steps.append(
        "若上述任一步失败 (列名漂移 / 0 动态列 / 0 数据行), 我会自动调一次 LLM 看 sheet "
        "骨架重新定位, 失败原因 + 修复方案会在 notice 里讲清楚"
    )
    return steps


def _build_dynamic_strategy(config: WideExcelConfig) -> str:
    if config.var_header_rows and len(config.var_header_rows) > 1:
        rows_str = "+".join(f"R{r}" for r in config.var_header_rows)
        return (
            f"动态列是多级 header (`{rows_str}` 拼合), var 字段格式是 `<level1>/<level2>/...` "
            f"复合 key (例: `CX11 A3 L/2025-12-30`), 业务方拿到后按 `/` split 即可分别取各维"
        )
    return (
        "动态列单层 header, var 字段直接取该列表头 cell (一般是 ISO 日期 `YYYY-MM-DD` "
        "格式; datetime cell 时间全 0 自动退化成纯日期)"
    )


def _build_headline(config: WideExcelConfig, file_name: str) -> str:
    desc = config.description or f"按 `{config.label}` 的固定模板"
    n_id = len(config.id_map)
    return (
        f"我已经收到您上传的文件 `{file_name}`, 我会{desc}解析它: "
        f"抽 **{n_id} 个** id 字段 + 把动态列 unpivot 成 `data` 时序列表."
    )


def _build_ack(config: WideExcelConfig, *, task_id: str, file_name: str) -> WideExcelTaskAck:
    """从 config 推导出完整的 ACK (含情绪价值预览). POST 阶段同步调一次, 0 LLM."""
    return WideExcelTaskAck(
        task_id=task_id,
        file_name=file_name,
        label=config.label,
        headline=_build_headline(config, file_name),
        processing_steps=_build_processing_steps(config),
        id_field_preview=[
            WideExcelMappingPreview(excel_column=k, json_field=v)
            for k, v in config.id_map.items()
        ],
        dynamic_strategy=_build_dynamic_strategy(config),
        id_map=config.id_map,
        var_pattern=config.var_pattern,
        var_name=config.var_name,
        value_name=config.value_name,
        var_header_rows=config.var_header_rows,
    )


# ---------------------------------------------------------------------------
# Main entry


async def parse_wide_excel(
    raw: bytes,
    *,
    config: WideExcelConfig,
    model: str = DEFAULT_MODEL,
    retry: int = 2,
) -> dict:
    """Python-first + LLM-fallback 主入口.

    Returns:
        ``{notice: str, rows: list[dict], via: 'python'|'llm_repair'}``. 上层用
        ``WideExcelResp[client_row_schema]`` 把它收窄成 typed response.
    """
    matrix = _build_clean_matrix(raw, config=config)

    try:
        rows, n_dyn = _try_python_path(matrix, config)
        return {
            "notice": _render_success_notice(
                config, n_rows=len(rows), n_dynamic_cols=n_dyn,
            ),
            "rows": rows,
            "via": "python",
        }
    except _PythonParseFailure as e:
        py_failure = e

    plan = await _repair_via_llm(
        matrix,
        config=config,
        failure=py_failure,
        model=model,
        retry=retry,
    )

    id_col_to_1b = {c.expected_name: c.col_index_1b for c in plan.id_columns}
    dynamic_cols = [(d.col_index_1b, d.var_value) for d in plan.dynamic_columns]
    try:
        rows = _extract_rows_wide(
            matrix,
            header_row_1b=plan.header_row_1b,
            id_col_to_1b=id_col_to_1b,
            primary_id_excel_name=config.primary_id,
            dynamic_cols=dynamic_cols,
            output_key_for=config.id_map,
            var_name=config.var_name,
            value_name=config.value_name,
        )
    except _PythonParseFailure as e:
        raise HTTPException(
            status_code=500,
            detail=(
                "Python 解析失败, 调 LLM 修复后仍失败. "
                f"原始失败: {py_failure.reason}. "
                f"LLM plan: header_row={plan.header_row_1b}, id_cols={id_col_to_1b}, "
                f"dyn_cols_count={len(dynamic_cols)}. "
                f"重跑失败: {e.reason} (ctx={e.ctx})"
            ),
        )

    return {
        "notice": plan.summary,
        "rows": rows,
        "via": "llm_repair",
    }


# ---------------------------------------------------------------------------
# Task wrapper


PHASE_PARSE = "parse"


async def wide_excel_task(
    task_id: str,
    raw: bytes,
    *,
    config: WideExcelConfig,
    model: str = DEFAULT_MODEL,
    retry: int = 2,
    phase: str = PHASE_PARSE,
) -> None:
    """通用 wide-excel 异步任务. 业务失败 / LLM 兜不住都落 error phase.

    成功时结果是 typed ``WideExcelResp[config.row_schema]`` 实例 (通过 row_schema 校验
    + Generic 收窄), 直接装进 ``TaskResult.result``, 给前端/Java 端拿到的 OpenAPI
    schema 就是具体的 typed row 字段, 而不是 ``dict[str, Any]``.
    """
    set_phase(task_id, phase, status=TaskStatus.processing)
    try:
        result = await parse_wide_excel(raw, config=config, model=model, retry=retry)
        typed_resp_cls = WideExcelResp[config.row_schema]
        typed_resp = typed_resp_cls(
            notice=result["notice"],
            rows=[config.row_schema.model_validate(r) for r in result["rows"]],
            via=result["via"],
        )
        set_phase(task_id, phase, status=TaskStatus.success, result=typed_resp)
    except Exception as e:
        set_phase(task_id, phase, status=TaskStatus.error, message=exception_detail(e))


# ---------------------------------------------------------------------------
# Route factory


def register_wide_excel(
    config: WideExcelConfig,
    router: APIRouter,
    *,
    path_prefix: str | None = None,
    summary_label: str | None = None,
    phase: str = PHASE_PARSE,
) -> None:
    """业务接入入口: 注册 wide-excel 的 POST + GET 两个端点.

    新客户最小动作: 写一份 ``WideExcelConfig`` 实例, 然后
    ``register_wide_excel(config, router)`` 一行搞定. 不必自己写 task wrapper /
    路由 boilerplate / response_model 收窄.

    Args:
        config: 客户的 ``WideExcelConfig``.
        router: APIRouter (一般是 ``apdfi.excel.routes.router``).
        path_prefix: URL 前缀 (含 '/'), 默认 ``"/{config.label}"``.
        summary_label: OpenAPI summary 用的标签, 默认 ``config.label``.
        phase: 内部 task phase 名, 默认 ``"parse"``.

    实际端点:
        * ``POST {path_prefix}`` -> 返 ``WideExcelTaskAck`` (task_id + preview).
        * ``GET  {path_prefix}/data/{{task_id}}`` -> 返 ``TaskResult[WideExcelResp[<row_schema>]]``.
    """
    if path_prefix is None:
        path_prefix = f"/{config.label}"
    label = summary_label or config.label

    typed_resp_cls = WideExcelResp[config.row_schema]
    typed_task_result_cls = TaskResult[typed_resp_cls]

    @router.post(
        path_prefix,
        summary=f"[M2M] 提交 {label} wide-excel 解析任务, 返回 task_id + 预设配置 preview",
        name=f"{label}_wide_excel_create",
    )
    async def _create(
        tasks: BackgroundTasks,
        file_params: dict = Depends(excel_upload),
        model: str = Form(
            DEFAULT_MODEL,
            description=(
                f"LLM 模型别名 (**仅 Python 解析失败兜底时才会用到**), 默认 `{DEFAULT_MODEL}`"
            ),
        ),
        retry: int = Form(
            2,
            description="LLM 输出 schema 校验最大重试次数 (兜底路径), ≥ 0, 默认 `2`",
        ),
    ) -> WideExcelTaskAck:
        """POST 即建任务 + 返预设配置 preview + 给前端的情绪价值字段.

        策略 (跟 simple 同):
        - **Python 优先**: 按约定 id_map + var_pattern 直接 unpivot. 模板没飘的时候 0 LLM.
        - **LLM 兜底**: 列名飘了 / pattern 0 列时调一次 LLM 重新定位 + 写 summary.

        响应里:
        - **情绪价值字段** (``headline`` / ``processing_steps`` / ``id_field_preview`` /
          ``dynamic_strategy``) 让前端立刻渲染 'AI Tip' / "我会按这些规则解析您的文件"
          占位 UI, 不必等任务跑完, 也不必前端拼业务相关的中文 prose. 模板**完全固定**
          意味着 sheet 还没 parse 我们就知道会做啥, 整个预览 0 LLM 0 等待.
        - **配置快照** (``id_map`` / ``var_pattern`` / ``var_name`` / ``value_name`` /
          ``var_header_rows``) 是本次任务实际生效的配置, Java 后端转发 / 程序化访问 /
          监控用.

        结果端点见 ``GET {path_prefix}/data/{task_id}``, 里面 ``via`` 字段标明走的是
        ``"python"`` 还是 ``"llm_repair"``, 可拿来做 LLM 调用率监控.
        """
        per_req_cfg = replace(config, sheet=file_params["sheet"])
        task_ack = await create_task(
            tasks,
            wide_excel_task,
            file_params["raw"],
            config=per_req_cfg,
            model=model,
            retry=retry,
            phase=phase,
        )
        return _build_ack(
            per_req_cfg,
            task_id=task_ack.task_id,
            file_name=file_params["file_name"],
        )

    @router.get(
        f"{path_prefix}/data/{{task_id}}",
        summary=f"[M2M] 取 {label} wide-excel 解析结果",
        name=f"{label}_wide_excel_data",
        response_model=typed_task_result_cls,
    )
    async def _data(task_id: str):
        """轮询拿解析结果. ``status`` ∈ processing / success / error / None.

        预设配置 preview 不在这里返 (前端在 POST 阶段已经拿到, 应本地缓存).
        """
        return await get_task(task_id, phase)


