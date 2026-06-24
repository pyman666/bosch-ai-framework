"""通用"简单 Excel"解析引擎: **Python 优先, LLM 兜底**.

策略 (核心思想):
    1. 拿到文件先用纯 Python (pandas) 按"业务约定的列名 + 起始行 + 自动日期格式探测"
       解析一次. 这条路径 0 LLM 调用, 0 网络延迟, 完全确定性, 90% 的客户上传都能
       直接通过.
    2. **仅当** Python 失败 (column_map 任何一列在表头里找不到 / 日期一行都 parse
       不出) 才掉头调一次 LLM "修复", 让 LLM 看一份 sheet 骨架, 重新定位真正的
       表头行 + 整套 column_map, 然后 Python 再按 LLM 的结果跑一遍.

这样的好处:
    - 省 token / 省时间: 大部分请求不调 LLM.
    - LLM 输出更可靠: 它只在失败场景出场, 任务边界清晰 (只让它做语义定位 + 写一段
      情绪价值 prose), 不做无用功.
    - 公共可复用: 不同客户接入只需配 ``SimpleExcelConfig`` (列名 map / 日期列 /
      目标格式 / 起始行 / repair prompt), 不必每家重写一份 pipeline.

实现取舍: simple = 简单. 这条路径**用纯 pandas**, 不走 ``PyXL`` 那套 openpyxl + numpy
matrix. 客户的"简单长表"不应该有隐藏行/列, 也不应该有合并单元格, 真有这些复杂场景说明
不属于 simple 范畴, 应该走 ``wide`` 或自定义 client (如 xpeng_zq).

API 形态: **POST 异步建任务 + GET 轮询拿结果**.
    - POST 返 ``SimpleExcelTaskAck`` (task_id + **结构化的预设配置 preview**: 文件名,
      column_map, date_column, target_date_format, header_row). 前端拿到响应立刻能渲染
      "我会按以下规则解析您的文件..."占位文案, 不依赖 server 出 prose 模板.
    - GET ``/{path_prefix}/data/{task_id}`` 返 ``TaskResult[SimpleExcelResp]``.

为什么不直接同步? 90% Python 路径亚秒级同步返回也没问题, 但 10% 走 LLM 兜底 ~5-10s
同步 hang 体验差, 而且 task 模式让前端有机会立刻渲染"规则确认"型 UI, 真正出结果时
顺势替换. 一次成本换了一致体验.

接入新客户的最小动作: 在 ``apdfi/excel/clients/<name>/__init__.py`` 里:
    1. 写一份 ``SimpleExcelConfig``:
        - ``column_map``: 业务关心的 Excel 列名 -> JSON 字段名;
        - ``date_column``: ``column_map`` 里其中一个 key, 指向日期列;
        - ``target_date_format``: Java 风格目标日期格式;
        - ``header_row``: 表头所在 1-based 行号 (默认 1);
        - ``repair_prompt``: LLM 兜底用的 system prompt (含业务上下文).
    2. 调一次 ``register_simple_excel_routes(router, path_prefix="/<name>", config=...)``
       挂上 POST + GET 两个端点 (task wrapper / phase 上报 / 异常 -> error 全在引擎里).

范本见 ``apdfi/excel/clients/chery/__init__.py``.
"""
from __future__ import annotations

import io
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any, Literal

import pandas as pd
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException
from litellm.exceptions import APIError
from pydantic import BaseModel, Field

from ._common import excel_upload, _is_nullish, _clean_cell
from .date_normalize import NormalizeReport, normalize_column
from infra.llm import aclient
from ..chat import ChatPlan
from infra.settings import DEFAULT_MODEL
from infra.task import TaskResult, TaskStatus, create_task, get_task, set_phase
from infra.utils import exception_detail


# ---------------------------------------------------------------------------
# config (客户接入只需要填这个)


@dataclass
class SimpleExcelConfig:
    """单个客户接入 simple excel parser 所需的全部配置.

    设计上**只有四件事可配**: 业务关心的列 (Excel 列名 → JSON 字段名 map), 哪一列是
    日期列, 目标日期格式, 表头起始行. 别的 (notice 文案 / 兜底逻辑 / 路由 boilerplate)
    全在通用层 hardcode, 客户不需要 (也不应该) 改.

    Args:
        column_map: 业务方关心的列, key 是 **Excel 表头中文名**, value 是 **输出
            JSON 里的字段名**. e.g. ``{"日期": "date", "物料单号": "orderNo", ...}``.
            通用层只输出这里指定的列, 其它 Excel 列丢掉; 输出 rows 的 key 用 value
            (JSON 字段名). Python 路径要求**所有** key 在 ``header_row`` 那一行精确
            出现, 缺任何一个就走 LLM 兜底. 不在这里搞 alias 列表 — 列名漂移由 LLM
            处理, Python 不猜.
        date_column: 业务方约定的日期列名 (Excel 列名). **必须是 ``column_map`` 的
            key 之一**, 否则 ``__post_init__`` 直接报错. 该列在规范化时会被转成
            ``target_date_format`` 字符串.
        target_date_format: Java 风格目标日期格式 (e.g. ``"yyyy/MM/dd"``). 日期列最终
            会被规范化成这个格式的字符串. 源格式 (``yyyy-MM-dd`` / ``yyyyMMdd`` /
            Excel 序列号 / Excel 内置 datetime 等) 由通用 ``date_normalize`` 探测,
            不必每个客户重写.
        header_row: 表头所在 1-based 行号, 默认 ``1``. 大多数 simple 客户表头就在
            row 1, 偶尔顶部有 banner / 标题行的客户可以配 row 2/3. 真实场景如果客户
            把 banner 行数也搞漂了, Python 路径会失败, 走 LLM 兜底重新定位.
        repair_prompt: LLM 兜底用的 system prompt. 由客户业务侧写, 把业务上下文塞
            进去. 模板里可以用 ``{date_column}`` / ``{target_date_format}`` /
            ``{column_map}`` 占位符, 引擎调 LLM 前会自动替换 (用 str.replace,
            对原文 ``{...}`` 不敏感). ``{column_map}`` 会被替换成一段多行 bullet
            list 描述每个 key->value 对.

            **prompt 风格约定**: prompt 内部用第二人称 "你是 xxx 助手 / 你的任务..."
            指挥 LLM; 但要在 prompt 里明确告诉它**对外输出 prose (summary 字段) 用
            "我" 称呼自己, 用 "您" 称呼业务方**. 详见 ``apdfi/excel/clients/chery/``
            的 ``_CHERY_REPAIR_PROMPT`` 示例.
    """

    column_map: dict[str, str]
    date_column: str
    target_date_format: str
    repair_prompt: str
    header_row: int = 1

    def __post_init__(self):
        if not self.column_map:
            raise ValueError(
                "SimpleExcelConfig.column_map 不能为空, 业务方至少要约定 1 列"
            )
        if self.date_column not in self.column_map:
            raise ValueError(
                f"SimpleExcelConfig.date_column={self.date_column!r} 必须是 "
                f"column_map 的 key 之一. 实际 column_map keys: "
                f"{list(self.column_map)}"
            )
        # JSON 字段名 (column_map values) 不允许重复, 否则输出 dict 会 key collision
        dup_values = [v for v, c in Counter(self.column_map.values()).items() if c > 1]
        if dup_values:
            raise ValueError(
                f"SimpleExcelConfig.column_map values (JSON 字段名) 不允许重复, "
                f"重复值: {dup_values}"
            )
        if self.header_row < 1:
            raise ValueError(
                f"SimpleExcelConfig.header_row 必须 >= 1, 当前 {self.header_row}"
            )

    @property
    def date_field(self) -> str:
        """日期列对应的 JSON 字段名 (即 ``column_map[date_column]``)."""
        return self.column_map[self.date_column]


# ---------------------------------------------------------------------------
# response


class SimpleExcelResp(BaseModel):
    """``parse_simple_excel`` 的返回. M2M 端口在 ``TaskResult[SimpleExcelResp]`` 里包一层."""

    notice: str = Field(..., description="给业务方看的中文文案 (情绪价值). 成功走模板, 兜底走 LLM 写好的 summary.")
    rows: list[dict[str, Any]] = Field(..., description="解析后的数据, 每行 = {JSON 字段名: 单元格值} (key 已按 ``config.column_map`` 重命名, 只含 map 里指定的列). 日期列已替换成目标格式字符串.")
    date_report: NormalizeReport = Field(..., description="日期规范化的结构化诊断 (源/目标格式 / 是否匹配 / 解析失败数 / 失败 sample).")
    via: Literal["python", "llm_repair"] = Field(..., description="走的哪条路径: 纯 Python / LLM 兜底修复. Java 端做监控时能看到 LLM 调用率.")


class SimpleExcelTaskAck(BaseModel):
    """POST 建任务时立刻返回的 ACK, 携带 task_id + 当前请求实际用的预设配置 preview.

    前端拿到响应即可立刻渲染"我已经收到您上传的文件 xxx, 接下来会按这些规则解析:
    表头在 row N, 找这 K 列 (...), 日期列归一化成 yyy/MM/dd"占位文案, 不需要 server
    端出 prose. 这套字段是**当前请求**的有效值 (e.g. ``target_date_format`` 已合并 form
    覆盖), 不是 config 的 server 端原始默认值.

    GET ``/{path_prefix}/data/{task_id}`` 是真正的结果端口, 返 ``TaskResult[
    SimpleExcelResp]``; 那里只装任务运行状态 + 最终 rows, 不再带 preview (preview
    应由前端在 POST 阶段本地缓存).
    """

    task_id: str = Field(..., description="任务 id, 用来 GET 轮询拿结果.")
    file_name: str = Field(..., description="客户上传的原始文件名 (可能是 ``'(未命名)'``).")
    column_map: dict[str, str] = Field(..., description="本次任务用的 Excel 列名 -> JSON 字段名 map.")
    date_column: str = Field(..., description="本次任务用的日期列名 (Excel 列名, 是 ``column_map`` 的 key 之一).")
    target_date_format: str = Field(..., description="本次任务用的 Java 风格目标日期格式 (form 覆盖后的最终值).")
    header_row: int = Field(..., ge=1, description="本次任务用的表头起始 1-based 行号.")


# ---------------------------------------------------------------------------
# LLM repair plan schema (引擎统一定义, 客户不需要自己写)


class ColumnLocation(BaseModel):
    """LLM 兜底时, 一条"业务约定列 → Excel 实际位置"的定位记录."""

    expected_name: str = Field(..., description="业务方约定的 Excel 列名 (即 ``config.column_map`` 的 key 之一, 一字不差).")
    actual_name: str = Field(..., description="该列在 Excel 实际表头里的中文名 (跟 expected_name 可能一致, 也可能漂移).")
    col_index_1b: int = Field(..., ge=1, description="该列在 sheet 中的 1-based 列号. 按表头文本 + 样本值综合判断, 日期列尤其按内容判断.")


class SimpleExcelRepairPlan(ChatPlan):
    """Python 失败时 LLM 给出的修复计划. 继承 ``ChatPlan`` 拿到 ``summary`` (情绪价值文案)."""

    header_row: int = Field(..., ge=1, description="真正的表头所在 1-based 行号.")
    columns: list[ColumnLocation] = Field(
        ...,
        description=(
            "业务关心的每一列在 sheet 里的实际定位. **必须**跟 ``config.column_map`` "
            "一一对应: ``expected_name`` 集合相等, 不能缺也不能多. 服务端会 post-validate, "
            "对不上直接 5xx 不重试."
        ),
    )


# ---------------------------------------------------------------------------
# 内部异常: Python 路径失败时抛, 触发 LLM repair, 不出 route.


class _PythonParseFailure(Exception):
    def __init__(self, reason: str, ctx: dict | None = None):
        self.reason = reason
        self.ctx = ctx or {}
        super().__init__(reason)


# ---------------------------------------------------------------------------
# helpers



def _cell_text(v: object, max_len: int = 30) -> str:
    """LLM 看的 cell 渲染. NaN/NaT 当空, 日期 ISO 化, 长字符串截断."""
    if _is_nullish(v):
        return ""
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except Exception:
            pass
    s = str(v).strip()
    if len(s) > max_len:
        s = s[:max_len] + "..."
    return s


def _header_text(v: object) -> str | None:
    """表头 cell 标准化: NaN/None/空白 -> None, 非空字符串 strip 后返回."""
    if _is_nullish(v):
        return None
    s = str(v).strip()
    return s or None


def _sheet_arg(sheet: int | str) -> int | str:
    """1-based int 或 sheet 名 -> pandas sheet_name (0-based int 或 str)."""
    return max(sheet - 1, 0) if isinstance(sheet, int) else sheet


def _read_raw_sheet(raw: bytes, sheet: int | str) -> pd.DataFrame:
    """读 raw sheet (header=None), 仅供 LLM repair 路径构建骨架用."""
    try:
        return pd.read_excel(io.BytesIO(raw), sheet_name=_sheet_arg(sheet), header=None)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"读取 Excel 失败 (sheet={sheet!r}): {type(e).__name__}: {e}",
        )


def _build_skeleton(df: pd.DataFrame, *, max_rows: int = 8) -> str:
    """给 LLM 看的紧凑骨架: 前 ``max_rows`` 行 + 全部列. simple 场景假设没有隐藏行/列."""
    n_rows, n_cols = df.shape
    head = df.head(max_rows)

    header = [
        f"sheet_shape: rows={n_rows}, cols={n_cols}",
        f"col_indices (1-based): {list(range(1, n_cols + 1))}",
        "format: R<row_1b> | <col_1b>=<value> | <col_1b>=<value> | ...",
        "",
    ]
    body = []
    for r1b, (_, row) in enumerate(head.iterrows(), start=1):
        cells = [f"{j + 1}={_cell_text(row.iloc[j])}" for j in range(n_cols)]
        body.append(f"R{r1b:02d} | " + " | ".join(cells))

    return "\n".join(header + body)


def _normalize_and_build_rows(
    df: pd.DataFrame,
    *,
    config: SimpleExcelConfig,
) -> tuple[list[dict], NormalizeReport]:
    """df 已按 JSON 字段名命名好列, 做日期规范化 + 转 rows list."""
    raw_dates = [_clean_cell(v) for v in df[config.date_field].tolist()]
    normalized, report = normalize_column(
        raw_dates,
        date_column=config.date_field,
        target_date_format=config.target_date_format,
    )
    df = df.copy()
    df[config.date_field] = normalized
    rows: list[dict] = [
        {k: _clean_cell(v) for k, v in row.items()}
        for row in df.to_dict(orient="records")
    ]
    return rows, report


# ---------------------------------------------------------------------------
# Python 路径 (一上来先试)


def _try_python_path(
    raw: bytes,
    sheet: int | str,
    config: SimpleExcelConfig,
) -> tuple[list[dict], NormalizeReport]:
    """纯 pandas: read_excel 直接按 header_row + 列名定位.

    列名任何一个找不到 -> pandas 自己抛 ValueError -> 我们转成 ``_PythonParseFailure``
    触发 LLM 兜底. 不用手动遍历表头, 不用维护列下标.
    """
    try:
        df = pd.read_excel(
            io.BytesIO(raw),
            sheet_name=_sheet_arg(sheet),
            header=config.header_row - 1,
            usecols=list(config.column_map.keys()),
        )
    except ValueError as e:
        raise _PythonParseFailure(str(e))
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"读取 Excel 失败 (sheet={sheet!r}): {type(e).__name__}: {e}",
        )

    df = df.dropna(how="all").reset_index(drop=True)
    df = df.rename(columns=config.column_map)

    rows, report = _normalize_and_build_rows(df, config=config)

    if report.parsed_rows == 0:
        raise _PythonParseFailure(
            f"日期列 {config.date_column!r} 找到了, 但一行都 parse 不出来 "
            f"(可能格式 server 还没见过, 或这列其实不是日期)",
            ctx={
                "unparseable_samples": report.unparseable_samples,
                "total_rows": report.total_rows,
            },
        )

    return rows, report


# ---------------------------------------------------------------------------
# LLM repair 路径 (Python 失败兜底)


def _format_column_map(config: SimpleExcelConfig) -> str:
    """把 ``config.column_map`` 渲染成多行 bullet, 给 prompt 跟 user_msg 共用.

    日期列标注 (日期列), LLM 更容易抓重点.
    """
    return "\n".join(
        f"  - {k!r} -> {v!r}" + (" (日期列)" if k == config.date_column else "")
        for k, v in config.column_map.items()
    )


def _render_repair_prompt(config: SimpleExcelConfig) -> str:
    """把 config 里 ``{date_column}`` / ``{target_date_format}`` / ``{column_map}`` 占位符
    填进 repair prompt.

    用 ``str.replace`` 而不是 ``.format``: 业务方 prompt 里可能有原生 ``{...}`` (e.g.
    JSON example), 用 ``.format`` 会炸. 这里**精确替换三个占位符**, 没碰到其它 ``{...}``.
    """
    return (
        config.repair_prompt
        .replace("{date_column}", config.date_column)
        .replace("{target_date_format}", config.target_date_format)
        .replace("{column_map}", _format_column_map(config))
    )


async def _repair_via_llm(
    df: pd.DataFrame,
    *,
    config: SimpleExcelConfig,
    failure: _PythonParseFailure,
    model: str,
    retry: int,
) -> SimpleExcelRepairPlan:
    """让 LLM 看 sheet 骨架 + Python 失败原因, 输出修复 plan, server 端 post-validate.

    Plan 必须满足: ``columns`` 的 ``expected_name`` 集合跟 ``config.column_map`` 的
    keys 完全相等 (不多不少, 一字不差). 不满足直接 5xx, 不二次调 LLM —— 列名"找不全"
    这种业务级偏差更适合上游 (前端引导用户改文件 / 业务侧改 column_map) 处理.
    """
    skeleton = _build_skeleton(df)
    user_msg = (
        "# Python 解析失败信息\n"
        f"约定的日期列 (Excel 列名): {config.date_column!r}\n"
        f"约定的目标日期格式: {config.target_date_format!r}\n"
        f"约定的表头起始行: row {config.header_row}\n"
        f"业务方关心的列 (Excel 列名 -> JSON 字段名):\n"
        f"{_format_column_map(config)}\n"
        f"失败原因: {failure.reason}\n"
        f"额外上下文: {failure.ctx}\n"
        "\n"
        "# Sheet 骨架 (前几行原始数据)\n"
        f"{skeleton}\n"
        "\n"
        "请严格按 SimpleExcelRepairPlan schema 输出 JSON: header_row, columns "
        "(每个含 expected_name / actual_name / col_index_1b, 一一对应上面 column_map "
        "的每个 key, 不能多也不能少), summary. summary 是直接给业务方看的情绪价值 "
        "prose, 用 '我' 称呼自己, 用 '您' 称呼业务方, 不超过 4 句话. "
        "可适当用 Markdown 突出关键信息: `**加粗**` 用于数字/字段名, `` `code` `` 用于格式串/列名."
    )
    messages = [
        {"role": "system", "content": _render_repair_prompt(config)},
        {"role": "user", "content": user_msg},
    ]
    try:
        plan = await aclient.chat.completions.create(
            model=model,
            messages=messages,
            response_model=SimpleExcelRepairPlan,
            max_retries=max(retry, 0),
        )
    except APIError as e:
        raise HTTPException(
            status_code=getattr(e, "status_code", 500),
            detail=getattr(e, "message", str(e)),
        )

    expected = set(config.column_map)
    actual = {c.expected_name for c in plan.columns}
    if expected != actual:
        raise HTTPException(
            status_code=500,
            detail=(
                "LLM 输出的 columns 跟 config.column_map 对不上. "
                f"缺: {sorted(expected - actual)}, 多: {sorted(actual - expected)}. "
                f"plan.summary: {plan.summary}"
            ),
        )

    return plan


def _extract_rows_by_plan(
    raw: bytes,
    sheet: int | str,
    *,
    plan: SimpleExcelRepairPlan,
    config: SimpleExcelConfig,
) -> tuple[list[dict], NormalizeReport]:
    """LLM repair 路径: 按 plan 给出的列位置 (1-based) + 表头行重新读取 Excel.

    用列位置 (0-based int) 而不是列名, 因为 LLM 已经帮我们处理了列名漂移,
    actual_name 是 sheet 里实际的表头文字, 不一定跟 column_map key 相同.
    """
    col_positions = [c.col_index_1b - 1 for c in plan.columns]  # 0-based
    try:
        df = pd.read_excel(
            io.BytesIO(raw),
            sheet_name=_sheet_arg(sheet),
            header=plan.header_row - 1,
            usecols=col_positions,
        )
    except Exception as e:
        raise _PythonParseFailure(
            f"LLM plan 给出的列位置 {[c.col_index_1b for c in plan.columns]} 无效: {e}",
        )

    # actual_name (Excel 里的表头) -> JSON 字段名
    rename_map = {c.actual_name: config.column_map[c.expected_name] for c in plan.columns}
    df = df.rename(columns=rename_map).dropna(how="all").reset_index(drop=True)

    return _normalize_and_build_rows(df, config=config)


# ---------------------------------------------------------------------------
# 成功路径 notice 渲染


def _render_success_notice(
    config: SimpleExcelConfig,
    report: NormalizeReport,
) -> str:
    """Python 路径成功后的 notice.

    Python 路径意味着 ``config.header_row`` 那一行里 ``column_map`` 全部 keys 都
    精确 match (任何一列缺都已经走 LLM 路径了), 所以这里只剩**日期格式**两种状态:
    跟目标一致 / 已自动规范化.

    LLM 路径下 notice 是 LLM 自己写的 (``plan.summary``), 不走这里.
    """
    n_cols = len(config.column_map)
    if report.source_matches_target_format:
        notice = (
            f"我看了下您上传的文件, 您约定的 **{n_cols} 列**我都在 `row {config.header_row}` "
            f"找到了, 日期列 `{config.date_column}` 的格式也是约定的 "
            f"`{config.target_date_format}`, 一切正常, **{report.parsed_rows}/{report.total_rows} 行**"
            f"解析成功, 已按 JSON 字段名重排后返还."
        )
    else:
        notice = (
            f"我看了下您上传的文件, 您约定的 **{n_cols} 列**我都在 `row {config.header_row}` "
            f"找到了, 日期列 `{config.date_column}` 探测到的源格式是 "
            f"`{report.source_date_format}`, 跟约定的 `{config.target_date_format}` 不一致. "
            f"不影响, 我已经帮您自动转成 `{config.target_date_format}` 了, 已按 JSON "
            f"字段名重排后返还, 您直接用即可."
        )
    if report.unparseable_rows > 0:
        samples = " / ".join(f"`{s}`" for s in report.unparseable_samples)
        notice += (
            f"\n\n> ⚠️ 有 **{report.unparseable_rows} 行**日期无法识别"
            f"(样例: {samples}), 这些行日期列已置为 `null`."
        )
    return notice


# ---------------------------------------------------------------------------
# 主入口


async def parse_simple_excel(
    raw: bytes,
    *,
    sheet: int | str = 1,
    config: SimpleExcelConfig,
    model: str = DEFAULT_MODEL,
    retry: int = 2,
) -> SimpleExcelResp:
    """Python-first + LLM-fallback 主入口.

    Args:
        raw: 上传的 Excel bytes.
        sheet: sheet 序号 (1-based) 或 sheet 名.
        config: 客户的 ``SimpleExcelConfig``.
        model: LLM 模型别名 (仅在 Python 失败兜底时才用到).
        retry: instructor schema 校验重试次数.

    Returns:
        ``SimpleExcelResp(notice, rows, date_report, via)``. ``via`` 字段标明走了
        哪条路径, 方便上层做监控 / 报表 (e.g. LLM 调用率).
    """
    # Python 优先: pandas 直接用列名 + header_row 定位, 找不到列自动触发 LLM 兜底.
    try:
        rows, report = _try_python_path(raw, sheet, config)
        return SimpleExcelResp(
            notice=_render_success_notice(config, report),
            rows=rows,
            date_report=report,
            via="python",
        )
    except _PythonParseFailure as e:
        py_failure = e

    # LLM repair: 只在 Python 失败时才读 raw sheet 供骨架构建.
    df_raw = _read_raw_sheet(raw, sheet)
    plan = await _repair_via_llm(
        df_raw,
        config=config,
        failure=py_failure,
        model=model,
        retry=retry,
    )

    try:
        rows, report = _extract_rows_by_plan(raw, sheet, plan=plan, config=config)
    except _PythonParseFailure as e:
        raise HTTPException(
            status_code=500,
            detail=(
                "Python 解析失败, 调 LLM 修复后仍失败. "
                f"原始失败: {py_failure.reason}. LLM plan: header_row={plan.header_row}, "
                f"columns={[c.col_index_1b for c in plan.columns]}. "
                f"重跑失败: {e.reason} (ctx={e.ctx})"
            ),
        )

    return SimpleExcelResp(
        notice=plan.summary,
        rows=rows,
        date_report=report,
        via="llm_repair",
    )


# ---------------------------------------------------------------------------
# 通用 task wrapper (给客户路由层 BackgroundTasks 用; 不必每个客户重写)


PHASE_PARSE = "parse"


async def simple_excel_task(
    task_id: str,
    raw: bytes,
    *,
    sheet: int | str,
    config: SimpleExcelConfig,
    model: str = DEFAULT_MODEL,
    retry: int = 2,
    phase: str = PHASE_PARSE,
) -> None:
    """通用 simple-excel 异步任务. ``parse_simple_excel`` 跑完后上报 phase 状态.

    业务失败 (LLM 也兜不住 / 文件空 / 等) 落到 error phase, 跟代码异常一视同仁 ——
    M2M 调用方只关心 success / error 二元状态, 不需要交互式诊断.
    """
    set_phase(task_id, phase, status=TaskStatus.processing)
    try:
        result = await parse_simple_excel(
            raw,
            sheet=sheet,
            config=config,
            model=model,
            retry=retry,
        )
        set_phase(task_id, phase, status=TaskStatus.success, result=result)
    except Exception as e:
        set_phase(task_id, phase, status=TaskStatus.error, message=exception_detail(e))


# ---------------------------------------------------------------------------
# 通用路由 factory: POST 建任务 (返结构化 ACK) + GET 拿结果
#
# 为什么 simple 也走 task 模式? 90% Python 路径亚秒级同步也行, 但 10% LLM 兜底
# ~5-10s, 同步会卡住前端; 而且 task 模式让 POST 阶段就拿到**结构化的预设配置**
# (column_map / date_column / target_date_format / header_row), 给上层 (Java 后端转发
# 或前端直接铺) 一份明确的"本次任务实际生效配置"快照, 一次成本换体验一致.
#
# 注意: M2M 端口 (Java 后端调) 不带任何 prose 字段, 预设配置全部以结构化字段挂在
# ``SimpleExcelTaskAck`` 上, 文案表达全部留给上层 (i18n / 个性化 / UI 样式都自由).
# 想要 prose 前瞻文案的客户走 chat 端口的 ``Session.initial_notice`` (本引擎不提供).


def register_simple_excel_routes(
    router: APIRouter,
    *,
    path_prefix: str,
    config: SimpleExcelConfig,
    summary_label: str | None = None,
    phase: str = PHASE_PARSE,
) -> None:
    """给一个 ``SimpleExcelConfig`` + URL prefix, 在 ``router`` 上注册 simple-excel
    的 POST + GET 两个端点.

    新客户接入: 写一份 ``SimpleExcelConfig`` 实例, 然后 ``register_simple_excel_routes(
    router, path_prefix='/<name>', config=...)`` 一行搞定, 不必自己写 pipeline wrapper
    跟两个 handler 函数 (那些都是 boilerplate).

    Args:
        router: 已经创建好的 APIRouter (通常是 ``apdfi.excel.routes.router``).
        path_prefix: URL 前缀, 含开头的 ``/`` (e.g. ``"/chery"``). 实际端点:
            * ``POST {path_prefix}`` -> 返 ``SimpleExcelTaskAck`` (task_id + preview)
            * ``GET  {path_prefix}/data/{{task_id}}`` -> 返 ``TaskResult[SimpleExcelResp]``
        config: 客户的 config. ``config.target_date_format`` 是默认值, 请求层可以传
            新的 ``target_date_format`` form field 覆盖.
        summary_label: OpenAPI summary 里用的标识 (e.g. ``"chery"``). 缺省从 path
            推导.
        phase: 内部 task phase 名, 默认 ``"parse"``. 多客户共用同一 router 时一般
            不必改, 因为每个 task_id 是独立的, phase 名 collision 不会跨 task.
    """
    label = summary_label or path_prefix.lstrip("/")

    @router.post(
        path_prefix,
        summary=f"[M2M] 提交 {label} Excel 解析任务, 返回 task_id + 预设配置 preview",
        name=f"{label}_simple_excel_create",
    )
    async def _create(
        tasks: BackgroundTasks,
        file_params: dict = Depends(excel_upload),
        target_date_format: str = Form(
            config.target_date_format,
            description=(
                f"Java 风格目标日期格式, 默认 {config.target_date_format!r}. "
                "支持 yyyy/MM/dd, yyyy-MM-dd, yyyy.MM.dd, yyyyMMdd, "
                "yyyy-MM-dd HH:mm:ss 等. 注意 Java 大小写敏感: MM=月, mm=分."
            ),
        ),
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
    ) -> SimpleExcelTaskAck:
        """**M2M 端口**, POST 即建任务 + 返预设配置 preview.

        策略 (省 LLM, 省时间):
        - **Python 优先**: 按约定列名 + 自动日期格式探测试一次. 90% 请求在这一步搞定, 0 LLM 调用.
        - **LLM 兜底**: 列名漂移找不到约定列时调一次 LLM 重新定位 + 写 summary.

        响应里 ``column_map`` / ``date_column`` / ``target_date_format`` / ``header_row``
        是**本次任务**的有效值 (合并 form 覆盖后), 前端可直接拿来渲染"我会按这些
        规则解析"占位 UI, 不必等任务跑完.

        结果端点见 ``GET {path_prefix}/data/{task_id}``, 里面 ``via`` 字段标明走的
        是 ``"python"`` 还是 ``"llm_repair"``, 可拿来做 LLM 调用率监控.
        """
        cfg = replace(config, target_date_format=target_date_format)
        ack = await create_task(
            tasks,
            simple_excel_task,
            file_params["raw"],
            sheet=file_params["sheet"],
            config=cfg,
            model=model,
            retry=retry,
            phase=phase,
        )
        return SimpleExcelTaskAck(
            task_id=ack.task_id,
            file_name=file_params["file_name"],
            column_map=cfg.column_map,
            date_column=cfg.date_column,
            target_date_format=cfg.target_date_format,
            header_row=cfg.header_row,
        )

    @router.get(
        f"{path_prefix}/data/{{task_id}}",
        summary=f"[M2M] 取 {label} 解析结果",
        name=f"{label}_simple_excel_data",
    )
    async def _data(task_id: str) -> TaskResult[SimpleExcelResp]:
        """轮询拿解析结果. ``status`` ∈ processing / success / error / None.

        预设配置 preview 不在这里返 (前端在 POST 阶段已经拿到, 应本地缓存).
        """
        return await get_task(task_id, phase)


