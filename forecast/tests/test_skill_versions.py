"""Skill versioning tests."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forecast.core.skill_manager import (
    activate_skill,
    create_skill,
    get_skill_version,
    list_skill_versions,
    rollback_skill,
    update_skill,
)
from forecast.database import Base
from forecast.models.skill import SkillCreate, SkillStatus, SkillType, SkillUpdate


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def test_skill_create_update_and_activate_record_versions(db_session):
    skill = create_skill(
        db_session,
        SkillCreate(
            name="shipment forecast",
            skill_type=SkillType.DSL,
            dsl_expression="mean(demand)",
        ),
    )

    versions = list_skill_versions(db_session, skill.id)
    assert len(versions) == 1
    assert versions[0].version == 1
    assert versions[0].action == "create"

    updated = update_skill(
        db_session,
        skill.id,
        SkillUpdate(name="shipment forecast v2", dsl_expression="moving_average(demand, 3)"),
    )
    assert updated.version == 2

    active = activate_skill(db_session, skill.id)
    assert active.version == 3
    assert active.status == SkillStatus.ACTIVE

    versions = list_skill_versions(db_session, skill.id)
    assert [v.version for v in versions] == [3, 2, 1]
    assert [v.action for v in versions] == ["activate", "update", "create"]

    v2 = get_skill_version(db_session, skill.id, 2)
    assert v2 is not None
    assert v2.name == "shipment forecast v2"
    assert v2.dsl_expression == "moving_average(demand, 3)"


def test_rollback_skill_restores_snapshot_as_new_draft_version(db_session):
    skill = create_skill(
        db_session,
        SkillCreate(
            name="rollback demo",
            skill_type=SkillType.DSL,
            dsl_expression="mean(demand)",
        ),
    )
    update_skill(
        db_session,
        skill.id,
        SkillUpdate(name="rollback demo v2", dsl_expression="moving_average(demand, 3)"),
    )

    rolled_back = rollback_skill(db_session, skill.id, version=1)

    assert rolled_back.version == 3
    assert rolled_back.status == SkillStatus.DRAFT
    assert rolled_back.name == "rollback demo"
    assert rolled_back.dsl_expression == "mean(demand)"

    versions = list_skill_versions(db_session, skill.id)
    assert versions[0].version == 3
    assert versions[0].action == "rollback_v1"
    assert versions[0].dsl_expression == "mean(demand)"


def test_rollback_missing_version_raises(db_session):
    skill = create_skill(
        db_session,
        SkillCreate(
            name="rollback missing",
            skill_type=SkillType.DSL,
            dsl_expression="mean(demand)",
        ),
    )

    with pytest.raises(ValueError, match="version not found"):
        rollback_skill(db_session, skill.id, version=99)

