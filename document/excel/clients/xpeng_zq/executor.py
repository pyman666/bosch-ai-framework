"""xpeng-zq 业务 skill 实现, 由通用 ``apdfi.excel.complex`` 引擎 + chat 状态机回调.

这个文件**不是** linear pipeline (跟 ``apdfi/pdf/pipeline/`` 区分), 而是给上层
orchestrator 注册的一组业务 skill -- chat 状态机在 planning / execute / diagnose 各
个 step 会回调这里的实现:

    - ``XpengZqExecutor`` (实现 ``ComplexExcelExecutor`` protocol):
        - ``build_skeleton(raw)``: skeleton 渲染 skill, 给 planner LLM 看;
        - ``execute(plan, raw)``: plan -> rows skill, 用 ``_execute_plan`` 抽业务数据
          (列定义 + 聚合 key + Row 怎么 build, 真正的业务核心).
    - ``xpeng_zq_intro(file_name)``: intro prose 渲染 skill, chat session 上传瞬间立即给
      前端 "我会按这些规则解析" 文案 (无 LLM, 模板拼接).
    - ``xpeng_zq_intent(file_name)``: 结构化预设 skill, 拼 plant / type / period 推断 +
      字段映射 + 澄清问题 (无 LLM, 文件名 regex + 业务约定预设).

通用 boilerplate (LLM one-shot / 状态机 / 路由注册 / M2M task wrapper) 全在
``apdfi.excel.complex``, 这里只放业务真正特有的 skill 实现.
"""
import io
import re

from ....chat import BusinessFailure
from ...complex import (
    boundary_pattern,
    build_complex_skeleton,
    date_columns,
    infer_period,
    to_int,
)
from ...core import PyXL
from .schemas import DataPoint, XpengZqIntent, XpengZqMappingPreview, XpengZqPlan, XpengZqRow


# ---------------------------------------------------------------------------
# _execute_plan: xpeng-zq 业务核心 (plan -> rows)


def _execute_plan(xl: PyXL, plan: XpengZqPlan) -> list[XpengZqRow]:
    """plan -> rows. 业务级失败 (sheet 太窄 / 日期列识别错 / plan 全是空段) 抛 ``BusinessFailure``.

    工作流:
    1. 检查 sheet 至少有 (车型, 零件号, 零件名) 3 列;
    2. 用通用 ``date_columns`` 拿日期表头行里所有日期 cell (跳过隐藏列);
    3. 遍历 plan 给的每个 (category, rows) 段, 对每行抽前 3 列做 key, 对每个日期列抽数量;
    4. 同 key 跨段聚合, 输出 ``list[XpengZqRow{ category, carModel, partNo, partName, data=[DataPoint] }]``.
    """
    mat = xl.matrix_filled
    n_rows, n_cols = mat.shape
    if n_cols < 3:
        raise BusinessFailure(
            "sheet 列数不足 3, 没法读 (车型/零件号/零件名)",
            ctx={"shape": [int(n_rows), int(n_cols)]},
        )

    hidden = xl.hidden_rows_1b
    date_cols = date_columns(xl, plan.date_header_row)
    if not date_cols:
        raise BusinessFailure(
            f"plan 指定的 date_header_row={plan.date_header_row} 没有任何能解析为日期的 cell",
            ctx={"date_header_row": plan.date_header_row, "n_rows": int(n_rows)},
        )

    # 同一 category 下按 (carModel, partNo, partName) 聚合, 因为 LLM 可能把同一份数据
    # 按段拆到多个 block, 但实际上业务方期望按 key 合并到一行.
    grouped: dict[tuple[str, tuple[str | None, str, str]], dict[str, int]] = {}

    for block in plan.blocks:
        for r1b in block.rows:
            if r1b in hidden:
                continue
            if not (1 <= r1b <= n_rows):
                continue
            row = mat[r1b - 1]

            car = row[0]
            pn = row[1]
            name = row[2]
            pn_s = "" if pn is None else str(pn).strip()
            name_s = "" if name is None else str(name).strip()
            if not pn_s or not name_s:
                continue
            car_s = None if car is None else (str(car).strip() or None)

            key = (block.category, (car_s, pn_s, name_s))
            bucket = grouped.setdefault(key, {})
            for col_idx, iso in date_cols:
                if col_idx >= n_cols:
                    continue
                qty = to_int(row[col_idx])
                if qty is None:
                    continue
                bucket[iso] = qty

    out: list[XpengZqRow] = []
    for (category, (car_s, pn_s, name_s)), bucket in grouped.items():
        if not bucket:
            continue
        data = [DataPoint(date=d, qty=q) for d, q in sorted(bucket.items())]
        out.append(XpengZqRow(
            category=category,
            carModel=car_s,
            partNo=pn_s,
            partName=name_s,
            data=data,
        ))

    if not out:
        raise BusinessFailure(
            "plan 走完后产生 0 行有效数据",
            ctx={
                "n_blocks": len(plan.blocks),
                "n_planned_rows": sum(len(b.rows) for b in plan.blocks),
                "n_date_cols": len(date_cols),
            },
        )
    return out


# ---------------------------------------------------------------------------
# Executor (实现 ComplexExcelExecutor protocol; 由 config 注入)


class XpengZqExecutor:
    """xpeng-zq executor. ``build_skeleton`` / ``execute`` 都走默认 sheet=1.

    chat 模式跟 M2M 模式都用同一个 executor 实例, 因为 xpeng-zq 业务约定 sheet 永远是第
    一张. 真有 multi-sheet 需求未来再扩.
    """

    def build_skeleton(self, raw: bytes, *, sheet: int | str = 1) -> str:
        xl = PyXL(file=io.BytesIO(raw), sheet=sheet)
        return build_complex_skeleton(xl)

    async def execute(self, plan: XpengZqPlan, raw: bytes, *, sheet: int | str = 1) -> list[XpengZqRow]:
        xl = PyXL(file=io.BytesIO(raw), sheet=sheet)
        return _execute_plan(xl, plan)


# ---------------------------------------------------------------------------
# Intro prose (给 config.intro_message_fn)


_INTRO_TEMPLATE = """\
我已经收到您上传的文件 `{file_name}`. 接下来我会按小鹏 ZQ 工厂的规则解析这张表:
- 识别 **「到货计划」** 和 **「缺件推移」** 两个段, 按 `(车型, 零件号, 零件名)` 拼成长表;
- 自动剔除 **「上线」** 段以及合计 / 累计 / 小计行, 隐藏行 / 隐藏列也会跳过;
- 大约 **5-15 秒**后我会把识别到的规则总结好让您审核; 如果哪里不对您直接告诉我, 我会重新识别.\
"""


def xpeng_zq_intro(file_name: str) -> str:
    """会话/任务创建瞬间给前端的中文前瞻文案."""
    return _INTRO_TEMPLATE.format(file_name=file_name or "(未命名)")


# ---------------------------------------------------------------------------
# Intent (给 config.build_intent_fn): 不调 LLM, 文件名 regex + 业务预设拼出来


_FILE_TYPE_PATTERNS: list[tuple[str, str]] = [
    (rf"{boundary_pattern('FCST')}|预测|forecast", "FCST"),
    (rf"{boundary_pattern('DAILY')}|日报", "DAILY"),
    (rf"{boundary_pattern('INS')}|检验", "INS"),
    (rf"{boundary_pattern('STOCK')}|库存", "STOCK"),
]
_PLANT_RX = re.compile(boundary_pattern(r"XP-?(?:ZQ|GZS?|WH)"), re.IGNORECASE)


def _infer_plant(file_name: str) -> str | None:
    m = _PLANT_RX.search(file_name)
    if not m:
        return None
    raw = m.group(0).upper()
    return raw if raw.startswith("XP-") else "XP-" + raw[2:]


def _infer_file_type(file_name: str) -> str | None:
    for pat, label in _FILE_TYPE_PATTERNS:
        if re.search(pat, file_name, re.IGNORECASE):
            return label
    return None


_COLUMN_MAP_PREVIEW: list[XpengZqMappingPreview] = [
    XpengZqMappingPreview(excel_column="车型 (列 1)", json_field="carModel"),
    XpengZqMappingPreview(excel_column="零件号 (列 2)", json_field="partNo"),
    XpengZqMappingPreview(excel_column="零件名 (列 3)", json_field="partName"),
    XpengZqMappingPreview(excel_column="段标识 (到货计划 / 缺件推移)", json_field="category"),
    XpengZqMappingPreview(excel_column="日期表头行 -> 每日数量", json_field="data[].date / data[].qty"),
]

_PROCESSING_STEPS: list[str] = [
    "识别日期表头行 (该行某一列起是连续日期)",
    "标记 **「到货计划」** 和 **「缺件推移」** 两类段, 同类多段合并到同一组",
    "排除顶部 **「上线数据」** 段, 以及合计 / 累计 / 小计 / 段标题行",
    "排除 Excel 隐藏行 / 隐藏列",
    "按 `(车型, 零件号, 零件名)` 复合主键, 把宽表 unpivot 成长表",
]

_CLARIFICATION_QUESTIONS: list[str] = [
    "这份文件的日期表头实际在第几行? (一般是第 3-5 行附近)",
    "是否有需要保留的 '上线' 段, 还是按默认全部丢弃?",
    "是否有需要忽略的额外车型 / 零件号前缀?",
]


def xpeng_zq_intent(file_name: str) -> XpengZqIntent:
    """会话创建瞬间给前端的结构化预设. 纯规则, 不调 LLM."""
    plant = _infer_plant(file_name)
    file_type = _infer_file_type(file_name)
    period = infer_period(file_name)

    headline_parts = ["看起来是小鹏"]
    headline_parts.append(f"{plant} 工厂" if plant else "ZQ 工厂")
    if period:
        headline_parts.append(f"{period} 期间")
    headline_parts.append(f"的 {file_type}" if file_type else "的到货计划")
    headline_parts.append("文件, 我一般这样处理:")
    headline = " ".join(headline_parts)

    return XpengZqIntent(
        plant_inferred=plant,
        file_type_inferred=file_type,
        period_inferred=period,
        headline=headline,
        processing_steps=list(_PROCESSING_STEPS),
        key_strategy="(车型 carModel, 零件号 partNo, 零件名 partName) 复合主键",
        column_map_preview=list(_COLUMN_MAP_PREVIEW),
        clarification_questions=list(_CLARIFICATION_QUESTIONS),
    )


