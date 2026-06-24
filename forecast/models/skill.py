"""Skill 定义的 Pydantic 数据模型。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from infra.utils import utcnow as _utcnow


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SkillType(str, Enum):
    DSL = "dsl"
    PYTHON = "python"
    PRESET = "preset"


class SkillStatus(str, Enum):
    DRAFT = "draft"
    REVIEWED = "reviewed"
    ACTIVE = "active"
    ARCHIVED = "archived"


# ---------------------------------------------------------------------------
# ParamDef
# ---------------------------------------------------------------------------

class ParamDef(BaseModel):
    """Skill 的输入/输出参数定义。"""
    name: str
    type: str = "number"  # number, array, date_series, string, ...
    description: str = ""
    required: bool = True
    default: Any | None = None


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------

class SkillCreate(BaseModel):
    """创建新 Skill 的请求体。"""
    name: str
    description: str = ""
    skill_type: SkillType = SkillType.DSL
    dsl_expression: str | None = None
    python_code: str | None = None
    preset_name: str | None = None
    input_params: list[ParamDef] = Field(default_factory=list)
    output_params: list[ParamDef] = Field(default_factory=list)
    chat_session_id: str | None = None
    tags: list[str] = Field(default_factory=list)


class SkillUpdate(BaseModel):
    """更新已有 Skill 的请求体。"""
    name: str | None = None
    description: str | None = None
    dsl_expression: str | None = None
    python_code: str | None = None
    input_params: list[ParamDef] | None = None
    output_params: list[ParamDef] | None = None
    tags: list[str] | None = None


class SkillRollbackRequest(BaseModel):
    """回滚 Skill 到指定历史版本的请求体。"""
    version: int


class Skill(BaseModel):
    """完整的 Skill 记录。"""
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    name: str
    description: str = ""
    skill_type: SkillType = SkillType.DSL
    status: SkillStatus = SkillStatus.DRAFT
    dsl_expression: str | None = None
    python_code: str | None = None
    preset_name: str | None = None
    input_params: list[ParamDef] = Field(default_factory=list)
    output_params: list[ParamDef] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    version: int = 1
    chat_session_id: str | None = None
    tags: list[str] = Field(default_factory=list)


class SkillVersion(BaseModel):
    """Skill 历史版本快照。"""
    id: str
    skill_id: str
    version: int
    action: str = "snapshot"
    name: str
    description: str = ""
    skill_type: SkillType = SkillType.DSL
    status: SkillStatus = SkillStatus.DRAFT
    dsl_expression: str | None = None
    python_code: str | None = None
    preset_name: str | None = None
    input_params: list[ParamDef] = Field(default_factory=list)
    output_params: list[ParamDef] = Field(default_factory=list)
    chat_session_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: datetime


# ---------------------------------------------------------------------------
# Skill preview
# ---------------------------------------------------------------------------

class SkillPreview(BaseModel):
    """Skill 计算逻辑的 Markdown 预览。"""
    skill_id: str
    skill_name: str
    calculation_logic_md: str  # 计算逻辑.md 内容
    skill_md: str  # skill.md 内容
    dsl_expression: str | None = None
    python_code: str | None = None
