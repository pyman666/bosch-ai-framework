"""Skill 生命周期管理 — 带审核流程的 CRUD 操作。"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from forecast.db_models import SkillORM, SkillVersionORM
from forecast.utils import utcnow as _utcnow
from forecast.models.skill import (
    Skill, SkillCreate, SkillUpdate, SkillType, SkillStatus, ParamDef, SkillVersion,
)
from forecast.core.executor import SandboxError, validate_python_skill_code

log = logging.getLogger(__name__)


def validate_skill_config(
    skill_type: str,
    dsl_expression: str | None = None,
    python_code: str | None = None,
    preset_name: str | None = None,
) -> None:
    """验证 Skill 的最小可执行配置是否完整。"""
    if skill_type == SkillType.DSL.value and not (dsl_expression or "").strip():
        raise ValueError("DSL skills require a non-empty dsl_expression")
    if skill_type == SkillType.PYTHON.value and not (python_code or "").strip():
        raise ValueError("Python skills require a non-empty python_code")
    if skill_type == SkillType.PRESET.value and not (preset_name or "").strip():
        raise ValueError("Preset skills require a non-empty preset_name")
    if skill_type not in {t.value for t in SkillType}:
        raise ValueError(f"Unsupported skill_type: {skill_type}")


# ---------------------------------------------------------------------------
# ORM <-> Pydantic
# ---------------------------------------------------------------------------

def orm_to_skill(orm: SkillORM) -> Skill:
    return Skill(
        id=orm.id,
        name=orm.name,
        description=orm.description,
        skill_type=SkillType(orm.skill_type),
        status=SkillStatus(orm.status),
        dsl_expression=orm.dsl_expression,
        python_code=orm.python_code,
        preset_name=orm.preset_name,
        input_params=[ParamDef(**p) for p in orm.get_input_params()],
        output_params=[ParamDef(**p) for p in orm.get_output_params()],
        created_at=orm.created_at,
        updated_at=orm.updated_at,
        version=orm.version,
        chat_session_id=orm.chat_session_id,
        tags=orm.get_tags(),
    )


def orm_to_skill_version(orm: SkillVersionORM) -> SkillVersion:
    return SkillVersion(
        id=orm.id,
        skill_id=orm.skill_id,
        version=orm.version,
        action=orm.action,
        name=orm.name,
        description=orm.description,
        skill_type=SkillType(orm.skill_type),
        status=SkillStatus(orm.status),
        dsl_expression=orm.dsl_expression,
        python_code=orm.python_code,
        preset_name=orm.preset_name,
        input_params=[ParamDef(**p) for p in orm.get_input_params()],
        output_params=[ParamDef(**p) for p in orm.get_output_params()],
        chat_session_id=orm.chat_session_id,
        tags=orm.get_tags(),
        created_at=orm.created_at,
    )


def _record_version(db: Session, orm: SkillORM, action: str) -> SkillVersionORM:
    """记录当前 SkillORM 状态的瞬时快照。"""
    version_orm = SkillVersionORM(
        id=f"{orm.id}_v{orm.version}_{action}_{int(_utcnow().timestamp() * 1000000)}",
        skill_id=orm.id,
        version=orm.version,
        action=action,
        name=orm.name,
        description=orm.description,
        skill_type=orm.skill_type,
        status=orm.status,
        dsl_expression=orm.dsl_expression,
        python_code=orm.python_code,
        preset_name=orm.preset_name,
        chat_session_id=orm.chat_session_id,
    )
    version_orm.set_input_params(orm.get_input_params())
    version_orm.set_output_params(orm.get_output_params())
    version_orm.set_tags(orm.get_tags())
    db.add(version_orm)
    return version_orm


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_skill(db: Session, payload: SkillCreate) -> Skill:
    """在草稿状态下创建新 Skill，自动打标。"""
    existing = db.query(SkillORM).filter(SkillORM.name == payload.name).first()
    if existing:
        raise ValueError(f"Skill name already exists: {payload.name}")
    skill = Skill(**payload.model_dump())
    validate_skill_config(
        skill.skill_type.value,
        dsl_expression=skill.dsl_expression,
        python_code=skill.python_code,
        preset_name=skill.preset_name,
    )
    # 自动打标：如果 tags 为空，根据 skill_type 推断
    tags = list(skill.tags)
    if not tags:
        if skill.skill_type == SkillType.PRESET:
            tags = ["preset"]
        else:
            # DSL/Python 默认标 business（用户通常创建的是业务逻辑）
            tags = ["business", "user"]

    # 重计算 Python skill 自动打 heavy 标签
    from forecast.core.rate_limit import is_heavy_skill
    if is_heavy_skill(skill.skill_type.value, preset_name=skill.preset_name, python_code=skill.python_code):
        if "heavy" not in tags:
            tags.append("heavy")
    orm = SkillORM(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        skill_type=skill.skill_type.value,
        status=SkillStatus.DRAFT.value,
        dsl_expression=skill.dsl_expression,
        python_code=skill.python_code,
        preset_name=skill.preset_name,
        chat_session_id=skill.chat_session_id,
        version=skill.version,
    )
    orm.set_input_params([p.model_dump() for p in skill.input_params])
    orm.set_output_params([p.model_dump() for p in skill.output_params])
    orm.set_tags(tags)
    db.add(orm)
    _record_version(db, orm, action="create")
    db.commit()
    db.refresh(orm)
    return orm_to_skill(orm)


def get_skill(db: Session, skill_id: str) -> Skill | None:
    orm = db.query(SkillORM).filter(SkillORM.id == skill_id).first()
    return orm_to_skill(orm) if orm else None


def list_skills(
    db: Session,
    skill_type: str | None = None,
    status: str | None = None,
    tag: str | None = None,
) -> list[Skill]:
    q = db.query(SkillORM)
    if skill_type:
        q = q.filter(SkillORM.skill_type == skill_type)
    if status:
        q = q.filter(SkillORM.status == status)
    if tag:
        # SQLite JSON 数组的 LIKE 匹配
        q = q.filter(SkillORM.tags_json.like(f'%"{tag}"%'))
    return [orm_to_skill(o) for o in q.order_by(SkillORM.updated_at.desc()).all()]


def update_skill(db: Session, skill_id: str, payload: SkillUpdate) -> Skill:
    orm = db.query(SkillORM).filter(SkillORM.id == skill_id).first()
    if not orm:
        raise ValueError(f"Skill not found: {skill_id}")
    if payload.name is not None:
        orm.name = payload.name
    if payload.description is not None:
        orm.description = payload.description
    if payload.dsl_expression is not None:
        orm.dsl_expression = payload.dsl_expression
    if payload.python_code is not None:
        orm.python_code = payload.python_code
    if payload.input_params is not None:
        orm.set_input_params([p.model_dump() for p in payload.input_params])
    if payload.output_params is not None:
        orm.set_output_params([p.model_dump() for p in payload.output_params])
    if payload.tags is not None:
        orm.set_tags(payload.tags)
    validate_skill_config(
        orm.skill_type,
        dsl_expression=orm.dsl_expression,
        python_code=orm.python_code,
        preset_name=orm.preset_name,
    )
    orm.updated_at = _utcnow()
    orm.version += 1
    _record_version(db, orm, action="update")
    db.commit()
    db.refresh(orm)
    return orm_to_skill(orm)


def delete_skill(db: Session, skill_id: str) -> None:
    orm = db.query(SkillORM).filter(SkillORM.id == skill_id).first()
    if not orm:
        raise ValueError(f"Skill not found: {skill_id}")
    db.delete(orm)
    db.commit()


def list_skill_versions(db: Session, skill_id: str) -> list[SkillVersion]:
    if not db.query(SkillORM).filter(SkillORM.id == skill_id).first():
        raise ValueError(f"Skill not found: {skill_id}")
    versions = (
        db.query(SkillVersionORM)
        .filter(SkillVersionORM.skill_id == skill_id)
        .order_by(SkillVersionORM.version.desc(), SkillVersionORM.created_at.desc())
        .all()
    )
    return [orm_to_skill_version(v) for v in versions]


def get_skill_version(db: Session, skill_id: str, version: int) -> SkillVersion | None:
    orm = (
        db.query(SkillVersionORM)
        .filter(SkillVersionORM.skill_id == skill_id, SkillVersionORM.version == version)
        .order_by(SkillVersionORM.created_at.desc())
        .first()
    )
    return orm_to_skill_version(orm) if orm else None


def rollback_skill(db: Session, skill_id: str, version: int) -> Skill:
    orm = db.query(SkillORM).filter(SkillORM.id == skill_id).first()
    if not orm:
        raise ValueError(f"Skill not found: {skill_id}")
    snapshot = (
        db.query(SkillVersionORM)
        .filter(SkillVersionORM.skill_id == skill_id, SkillVersionORM.version == version)
        .order_by(SkillVersionORM.created_at.desc())
        .first()
    )
    if not snapshot:
        raise ValueError(f"Skill version not found: {skill_id}@v{version}")

    orm.name = snapshot.name
    orm.description = snapshot.description
    orm.skill_type = snapshot.skill_type
    orm.status = SkillStatus.DRAFT.value
    orm.dsl_expression = snapshot.dsl_expression
    orm.python_code = snapshot.python_code
    orm.preset_name = snapshot.preset_name
    orm.set_input_params(snapshot.get_input_params())
    orm.set_output_params(snapshot.get_output_params())
    orm.set_tags(snapshot.get_tags())
    orm.chat_session_id = snapshot.chat_session_id

    validate_skill_config(
        orm.skill_type,
        dsl_expression=orm.dsl_expression,
        python_code=orm.python_code,
        preset_name=orm.preset_name,
    )

    orm.updated_at = _utcnow()
    orm.version += 1
    _record_version(db, orm, action=f"rollback_v{version}")
    db.commit()
    db.refresh(orm)
    return orm_to_skill(orm)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def activate_skill(db: Session, skill_id: str) -> Skill:
    """激活 Skill。Python Skill 需要通过审核。"""
    orm = db.query(SkillORM).filter(SkillORM.id == skill_id).first()
    if not orm:
        raise ValueError(f"Skill not found: {skill_id}")
    validate_skill_config(
        orm.skill_type,
        dsl_expression=orm.dsl_expression,
        python_code=orm.python_code,
        preset_name=orm.preset_name,
    )
    if orm.skill_type == "python" and orm.status != SkillStatus.REVIEWED.value:
        raise ValueError("Python skills must be reviewed before activation")
    orm.status = SkillStatus.ACTIVE.value
    orm.updated_at = _utcnow()
    orm.version += 1
    _record_version(db, orm, action="activate")
    db.commit()
    db.refresh(orm)
    return orm_to_skill(orm)


def review_skill(db: Session, skill_id: str) -> Skill:
    """将 Python Skill 标记为已审核，允许后续激活。"""
    orm = db.query(SkillORM).filter(SkillORM.id == skill_id).first()
    if not orm:
        raise ValueError(f"Skill not found: {skill_id}")
    if orm.skill_type != SkillType.PYTHON.value:
        raise ValueError("Only Python skills require review")
    if not orm.python_code:
        raise ValueError("Python skill has no code to review")
    try:
        validate_python_skill_code(orm.python_code)
    except SandboxError as exc:
        raise ValueError(f"Python skill security check failed: {exc}") from exc
    orm.status = SkillStatus.REVIEWED.value
    orm.updated_at = _utcnow()
    orm.version += 1
    _record_version(db, orm, action="review")
    db.commit()
    db.refresh(orm)
    return orm_to_skill(orm)


def deactivate_skill(db: Session, skill_id: str) -> Skill:
    orm = db.query(SkillORM).filter(SkillORM.id == skill_id).first()
    if not orm:
        raise ValueError(f"Skill not found: {skill_id}")
    orm.status = SkillStatus.ARCHIVED.value
    orm.updated_at = _utcnow()
    orm.version += 1
    _record_version(db, orm, action="deactivate")
    db.commit()
    db.refresh(orm)
    return orm_to_skill(orm)


# ---------------------------------------------------------------------------
# Preset seeding
# ---------------------------------------------------------------------------

def seed_preset_skills(db: Session) -> int:
    """如果内置预设 Skill 尚不存在则插入。返回种子数量。"""
    from forecast.skills.presets import get_preset_info

    existing = set(
        row[0] for row in db.query(SkillORM.preset_name)
        .filter(SkillORM.skill_type == "preset")
        .all()
    )

    count = 0
    for info in get_preset_info():
        if info["name"] in existing:
            continue
        orm = SkillORM(
            id=f"preset_{info['name']}",
            name=info["name"],
            description=info["description"],
            skill_type="preset",
            status="active",  # 预设技能默认激活
            preset_name=info["name"],
            version=1,
        )
        orm.set_input_params([
            {"name": "demand", "type": "date_series", "description": "需求时间序列", "required": True},
            {"name": "pgi", "type": "date_series", "description": "PGI在途时间序列", "required": False},
            {"name": "beginningInventory", "type": "number", "description": "期初库存", "required": True},
        ])
        orm.set_output_params([
            {"name": "forecast", "type": "date_series", "description": "预测发货量"},
        ])
        # 预设自动打标：category 直接作为 tag（algorithm / business）
        from forecast.core.rate_limit import is_heavy_skill

        tags = ["preset", info.get("category", "algorithm")]

        # 重计算预设额外打 heavy 标签
        if is_heavy_skill("preset", preset_name=info["name"]):
            tags.append("heavy")

        orm.set_tags(tags)
        db.add(orm)
        _record_version(db, orm, action="seed")
        count += 1

    if count > 0:
        db.commit()
        log.info(f"Seeded {count} preset skills")
    return count
