"""xpeng-zq 客户的所有 schema.

- **planner output**: ``XpengZqPlan`` / ``XpengZqBlock`` / ``XpengZqCategory``,
  LLM 看 sheet 骨架后输出, instructor 校验 schema.
- **行级输出**: ``XpengZqRow`` + ``DataPoint``, M2M 和 chat 共用.
- **intent preview**: ``XpengZqIntent`` + ``XpengZqMappingPreview``, 上传瞬间不调 LLM
  返给前端 "我一般这样处理" 区域的结构化预设.
- **typed chat session**: ``XpengZqSession`` 继承通用 ``Session`` 把泛型字段
  窄化, OpenAPI 才能给前端展示完整 plan / rows / intent 结构.
"""
from typing import Literal

from pydantic import BaseModel, Field

from ....chat import ChatPlan, Session as _ChatSession


# ---------------------------------------------------------------------------
# planner output (LLM 通过 instructor 输出这个)


XpengZqCategory = Literal["到货计划", "缺件推移"]


class XpengZqBlock(BaseModel):
    category: XpengZqCategory = Field(..., description="数据类别")
    rows: list[int] = Field(
        ...,
        description="属于该类别的**数据行**的 1-based 行号列表 (不含表头/段标题/合计/累计/隐藏行)",
    )


class XpengZqPlan(ChatPlan):
    """xpeng-zq plan. 继承 ``ChatPlan`` 拿到 ``summary`` (人话总结, 给前端展示)."""

    date_header_row: int = Field(
        ...,
        description="日期表头所在的 1-based 行号 (该行从某一列起是连续的日期值)",
        ge=1,
    )
    blocks: list[XpengZqBlock] = Field(
        ...,
        description="数据分类计划. 同一个 category 在 sheet 中出现多段时, 把这些段的行合并到同一个 block.rows 里.",
    )


# ---------------------------------------------------------------------------
# 行级输出 (实际数据 row)


class DataPoint(BaseModel):
    date: str = Field(..., description="日期 (YYYY-MM-DD)")
    qty: int = Field(..., description="数量")


class XpengZqRow(BaseModel):
    category: XpengZqCategory
    carModel: str | None = Field(None, description="车型 (列 1), 数据行此列允许为空")
    partNo: str = Field(..., description="零件号 (列 2)")
    partName: str = Field(..., description="零件名 (列 3)")
    data: list[DataPoint]


# ---------------------------------------------------------------------------
# intent preview (上传瞬间给前端 'AI Tip' 区域用的结构化预设, 不调 LLM)


class XpengZqMappingPreview(BaseModel):
    """字段映射预设的一项: ``excel_column`` -> ``json_field``."""
    excel_column: str = Field(..., description="Excel 中的列含义, 例: '物料编码'")
    json_field: str = Field(..., description="输出 JSON 字段, 例: 'partNo'")


class XpengZqIntent(BaseModel):
    """会话创建瞬间返给前端的**结构化**预设方案. 不调 LLM, 全部 server 端基于文件名 + 业务规则拼出来.

    前端可以直接铺到 demo "AI Tip" 步骤的 detect grid / mapping / clarification 区域,
    UI 不必猜数据形状.
    """
    plant_inferred: str | None = Field(
        None,
        description="从文件名推断的工厂代码 (例: 'XP-ZQ'), 推断不出来为 null. 仅供 UI 展示, 不影响后续解析",
    )
    file_type_inferred: str | None = Field(
        None,
        description="从文件名推断的文件类型 (例: 'FCST' / 'DAILY'), 推断不出来为 null",
    )
    period_inferred: str | None = Field(
        None,
        description="从文件名推断的期间 (例: '2026-05'), 推断不出来为 null",
    )
    headline: str = Field(
        ...,
        description="一句中文 prose, 给前端 'AI Tip' 标题区域用. 例: '看起来是 Xpeng ZQ 工厂的到货计划文件'",
    )
    processing_steps: list[str] = Field(
        ...,
        description="处理步骤列表, 给前端 ol 渲染. 例: ['识别到货计划/缺件推移段', '排除合计/累计行', ...]",
    )
    key_strategy: str = Field(
        ...,
        description="主键策略, 一句话描述. 例: '(车型, 零件号, 零件名) 复合主键'",
    )
    column_map_preview: list[XpengZqMappingPreview] = Field(
        ...,
        description="字段映射预设, 给前端 mapping 区域用",
    )
    clarification_questions: list[str] = Field(
        ...,
        description="如果业务方选择 'complex chat' 路径, 这是会按顺序问的几个澄清问题. "
                    "前端按需展示, 用户答完拼成 prose 通过 ``POST /chat/{chat_id}/start`` 的 ``clarifications`` 字段提交",
    )


# ---------------------------------------------------------------------------
# typed chat session


class XpengZqSession(_ChatSession):
    """chat 模式下 xpeng-zq 的 typed session. 重写父类几个泛型字段为具体类型,
    OpenAPI 才能给前端展示完整的 plan / rows / intent schema (而不是 ``dict[str, Any]``).
    """
    intent: XpengZqIntent | None = None
    latest_plan: XpengZqPlan | None = None
    latest_rows: list[XpengZqRow] | None = None


