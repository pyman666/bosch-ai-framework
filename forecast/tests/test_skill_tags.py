"""Phase 4.5 — Skill tag auto-labeling, tag filtering, and version tag propagation tests."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forecast.core.skill_manager import (
    create_skill,
    list_skills,
    rollback_skill,
    seed_preset_skills,
    update_skill,
)
from forecast.database import Base
from forecast.models.skill import SkillCreate, SkillType, SkillUpdate


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


# ---------------------------------------------------------------------------
# 4.5.3 — Auto-tagging on creation
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Duplicate name check
# ---------------------------------------------------------------------------

class TestDuplicateName:
    def test_create_duplicate_name_raises(self, db_session):
        create_skill(
            db_session,
            SkillCreate(
                name="my forecast",
                skill_type=SkillType.DSL,
                dsl_expression="mean(demand)",
            ),
        )
        with pytest.raises(ValueError, match="Skill name already exists"):
            create_skill(
                db_session,
                SkillCreate(
                    name="my forecast",
                    skill_type=SkillType.DSL,
                    dsl_expression="mean(demand)",
                ),
            )

    def test_different_names_allowed(self, db_session):
        s1 = create_skill(
            db_session,
            SkillCreate(
                name="skill A",
                skill_type=SkillType.DSL,
                dsl_expression="mean(demand)",
            ),
        )
        s2 = create_skill(
            db_session,
            SkillCreate(
                name="skill B",
                skill_type=SkillType.DSL,
                dsl_expression="sum(demand)",
            ),
        )
        assert s1.name != s2.name


# ---------------------------------------------------------------------------
# 4.5.3 — Auto-tagging on creation
# ---------------------------------------------------------------------------

class TestAutoTagging:
    def test_dsl_skill_gets_business_user_tags(self, db_session):
        skill = create_skill(
            db_session,
            SkillCreate(
                name="custom dsl",
                skill_type=SkillType.DSL,
                dsl_expression="mean(demand)",
            ),
        )
        assert skill.tags == ["business", "user"]

    def test_python_skill_gets_business_user_tags(self, db_session):
        skill = create_skill(
            db_session,
            SkillCreate(
                name="custom python",
                skill_type=SkillType.PYTHON,
                python_code="def forecast(record):\n    return [1]",
            ),
        )
        assert skill.tags == ["business", "user"]

    def test_explicit_tags_preserved(self, db_session):
        skill = create_skill(
            db_session,
            SkillCreate(
                name="explicit tags",
                skill_type=SkillType.DSL,
                dsl_expression="mean(demand)",
                tags=["algorithm", "zhangsan"],
            ),
        )
        assert skill.tags == ["algorithm", "zhangsan"]

    def test_preset_seed_gets_algorithm_tag(self, db_session):
        """Statistical presets should get ["preset", "algorithm"]."""
        seed_preset_skills(db_session)
        skills = list_skills(db_session, skill_type="preset")
        # Find a known statistical preset (e.g. moving_average)
        ma = [s for s in skills if s.name == "moving_average"]
        assert len(ma) == 1
        assert "preset" in ma[0].tags
        assert "algorithm" in ma[0].tags

    def test_preset_seed_business_logic_gets_business_tag(self, db_session):
        """Business logic presets should get ["preset", "business"]."""
        seed_preset_skills(db_session)
        skills = list_skills(db_session, skill_type="preset")
        jit = [s for s in skills if s.name == "fwdy_jitcall_priority"]
        assert len(jit) == 1
        assert "preset" in jit[0].tags
        assert "business" in jit[0].tags


# ---------------------------------------------------------------------------
# 4.5.4 — Tag filtering in list_skills
# ---------------------------------------------------------------------------

class TestTagFiltering:
    def _seed_variety(self, db):
        """Create skills with distinct tags for filtering tests."""
        create_skill(db, SkillCreate(
            name="dsl-business",
            skill_type=SkillType.DSL,
            dsl_expression="mean(demand)",
            tags=["business", "user"],
        ))
        create_skill(db, SkillCreate(
            name="dsl-algo",
            skill_type=SkillType.DSL,
            dsl_expression="holt_winters(demand)",
            tags=["algorithm", "lisi"],
        ))
        create_skill(db, SkillCreate(
            name="preset-ma",
            skill_type=SkillType.PRESET,
            preset_name="moving_average",
            tags=["preset", "algorithm"],
        ))

    def test_filter_by_business_tag(self, db_session):
        self._seed_variety(db_session)
        result = list_skills(db_session, tag="business")
        names = [s.name for s in result]
        assert "dsl-business" in names
        assert "dsl-algo" not in names
        assert "preset-ma" not in names

    def test_filter_by_algorithm_tag(self, db_session):
        self._seed_variety(db_session)
        result = list_skills(db_session, tag="algorithm")
        names = [s.name for s in result]
        assert "dsl-algo" in names
        assert "preset-ma" in names
        assert "dsl-business" not in names

    def test_filter_by_preset_tag(self, db_session):
        self._seed_variety(db_session)
        result = list_skills(db_session, tag="preset")
        names = [s.name for s in result]
        assert names == ["preset-ma"]

    def test_filter_by_user_tag(self, db_session):
        self._seed_variety(db_session)
        result = list_skills(db_session, tag="user")
        names = [s.name for s in result]
        assert names == ["dsl-business"]

    def test_filter_by_nonexistent_tag(self, db_session):
        self._seed_variety(db_session)
        result = list_skills(db_session, tag="nonexistent")
        assert result == []

    def test_filter_combined_with_skill_type(self, db_session):
        self._seed_variety(db_session)
        result = list_skills(db_session, skill_type="dsl", tag="algorithm")
        names = [s.name for s in result]
        assert names == ["dsl-algo"]

    def test_no_tag_filter_returns_all(self, db_session):
        self._seed_variety(db_session)
        result = list_skills(db_session)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# Tag update & version propagation
# ---------------------------------------------------------------------------

class TestTagUpdateAndVersioning:
    def test_update_tags(self, db_session):
        skill = create_skill(
            db_session,
            SkillCreate(
                name="tag-update",
                skill_type=SkillType.DSL,
                dsl_expression="mean(demand)",
            ),
        )
        assert skill.tags == ["business", "user"]

        updated = update_skill(
            db_session,
            skill.id,
            SkillUpdate(tags=["algorithm", "zhangsan"]),
        )
        assert updated.tags == ["algorithm", "zhangsan"]

    def test_tags_preserved_in_version_snapshot(self, db_session):
        skill = create_skill(
            db_session,
            SkillCreate(
                name="version-tags",
                skill_type=SkillType.DSL,
                dsl_expression="mean(demand)",
                tags=["business", "user"],
            ),
        )
        update_skill(
            db_session,
            skill.id,
            SkillUpdate(tags=["algorithm"]),
        )
        # Rollback to v1 which had ["business", "user"]
        rolled = rollback_skill(db_session, skill.id, version=1)
        assert rolled.tags == ["business", "user"]

    def test_empty_tags_list(self, db_session):
        """Empty tags should still trigger auto-tagging."""
        skill = create_skill(
            db_session,
            SkillCreate(
                name="empty-tags",
                skill_type=SkillType.DSL,
                dsl_expression="mean(demand)",
                tags=[],
            ),
        )
        # Empty list triggers auto-tag
        assert skill.tags == ["business", "user"]
