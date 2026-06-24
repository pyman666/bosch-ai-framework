"""Skill 路由 — /api/v1/skills/*"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from forecast.database import get_db
from forecast.models.skill import (
    Skill, SkillCreate, SkillUpdate,
    SkillPreview, SkillRollbackRequest, SkillVersion,
)
from forecast.core import skill_manager

router = APIRouter(prefix="/api/v1/skills", tags=["skills"])


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.post("", response_model=Skill, status_code=201)
def create_skill(payload: SkillCreate, db: Session = Depends(get_db)):
    """创建新 Skill（草稿状态）。"""
    try:
        return skill_manager.create_skill(db, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("", response_model=list[Skill])
def list_skills(
    skill_type: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    db: Session = Depends(get_db),
):
    """列出所有 Skill，支持可选过滤条件（skill_type / status / tag）。"""
    return skill_manager.list_skills(db, skill_type=skill_type, status=status, tag=tag)


@router.get("/presets", response_model=list[Skill])
def list_presets(db: Session = Depends(get_db)):
    """列出所有预设 Skill。"""
    return skill_manager.list_skills(db, skill_type="preset")


@router.get("/{skill_id}", response_model=Skill)
def get_skill(skill_id: str, db: Session = Depends(get_db)):
    """按 ID 获取单个 Skill。"""
    skill = skill_manager.get_skill(db, skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@router.put("/{skill_id}", response_model=Skill)
def update_skill(skill_id: str, payload: SkillUpdate, db: Session = Depends(get_db)):
    """更新已有的 Skill。"""
    try:
        return skill_manager.update_skill(db, skill_id, payload)
    except ValueError as e:
        status_code = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(e))


@router.delete("/{skill_id}")
def delete_skill(skill_id: str, db: Session = Depends(get_db)):
    """删除一个 Skill。"""
    try:
        skill_manager.delete_skill(db, skill_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@router.get("/{skill_id}/versions", response_model=list[SkillVersion])
def list_skill_versions(skill_id: str, db: Session = Depends(get_db)):
    """列出某个 Skill 的历史版本快照。"""
    try:
        return skill_manager.list_skill_versions(db, skill_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{skill_id}/versions/{version}", response_model=SkillVersion)
def get_skill_version(skill_id: str, version: int, db: Session = Depends(get_db)):
    """获取某个 Skill 的指定历史版本快照。"""
    item = skill_manager.get_skill_version(db, skill_id, version)
    if not item:
        raise HTTPException(status_code=404, detail="Skill version not found")
    return item


@router.post("/{skill_id}/rollback", response_model=Skill)
def rollback_skill(skill_id: str, payload: SkillRollbackRequest, db: Session = Depends(get_db)):
    """将 Skill 回滚到指定历史版本；回滚后状态置为 draft，需重新审核/激活。"""
    try:
        return skill_manager.rollback_skill(db, skill_id, payload.version)
    except ValueError as e:
        status_code = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(e))


# ---------------------------------------------------------------------------
# Skill lifecycle
# ---------------------------------------------------------------------------

@router.post("/{skill_id}/activate", response_model=Skill)
def activate_skill(skill_id: str, db: Session = Depends(get_db)):
    """激活 Skill。Python Skill 需要先审核才能激活。"""
    try:
        return skill_manager.activate_skill(db, skill_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{skill_id}/review", response_model=Skill)
def review_skill(skill_id: str, db: Session = Depends(get_db)):
    """人工审核通过 Python Skill，审核后可调用 activate 激活。"""
    try:
        return skill_manager.review_skill(db, skill_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{skill_id}/deactivate", response_model=Skill)
def deactivate_skill(skill_id: str, db: Session = Depends(get_db)):
    """停用一个 Skill。"""
    try:
        return skill_manager.deactivate_skill(db, skill_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

@router.get("/{skill_id}/preview", response_model=SkillPreview)
def preview_skill(skill_id: str, db: Session = Depends(get_db)):
    """预览 Skill 的计算逻辑 Markdown 文档。"""
    from forecast.core.orchestrator import build_skill_md

    skill = skill_manager.get_skill(db, skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    # 从 Skill 元数据生成基础计算逻辑文档
    logic_md = f"""# {skill.name}

## 描述
{skill.description}

## 类型
{skill.skill_type.value}

## 输入参数
"""
    for p in skill.input_params:
        logic_md += f"- **{p.name}** ({p.type}): {p.description}\n"

    logic_md += "\n## 输出参数\n"
    for p in skill.output_params:
        logic_md += f"- **{p.name}** ({p.type}): {p.description}\n"

    if skill.dsl_expression:
        logic_md += f"\n## DSL 表达式\n```\n{skill.dsl_expression}\n```\n"
    if skill.python_code:
        logic_md += f"\n## Python 代码\n```python\n{skill.python_code}\n```\n"

    skill_data = {
        "skill_name": skill.name,
        "description": skill.description,
        "skill_type": skill.skill_type.value,
        "dsl_expression": skill.dsl_expression,
        "python_code": skill.python_code,
    }
    skill_md = build_skill_md(skill_data, logic_md)

    return SkillPreview(
        skill_id=skill.id,
        skill_name=skill.name,
        calculation_logic_md=logic_md,
        skill_md=skill_md,
        dsl_expression=skill.dsl_expression,
        python_code=skill.python_code,
    )
