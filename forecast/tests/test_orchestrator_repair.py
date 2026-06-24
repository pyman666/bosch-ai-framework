"""Skill generation repair-loop tests."""

import asyncio
import json

import pytest

from forecast.core import orchestrator
from forecast.models.forecast import ForecastInput


SAMPLE_INPUT = ForecastInput.model_validate({
    "carModel": "A3",
    "color": "black",
    "demand": [
        {"date": "2026-01-01", "qty": 10},
        {"date": "2026-01-02", "qty": 12},
        {"date": "2026-01-03", "qty": 14},
    ],
    "pgi": [],
    "beginningInventory": 0,
})


def test_repair_skill_until_valid_retries_after_failed_dry_run(monkeypatch):
    calls = []

    async def fake_llm_chat(messages, model=None):
        calls.append(messages[-1]["content"])
        repaired = {
            "skill_name": "repaired",
            "skill_type": "dsl",
            "description": "fixed dsl",
            "dsl_expression": "mean(demand)",
        }
        return {"content": json.dumps(repaired, ensure_ascii=False)}

    monkeypatch.setattr(orchestrator, "llm_chat", fake_llm_chat)

    initial = {
        "skill_name": "broken",
        "skill_type": "dsl",
        "description": "bad dsl",
        "dsl_expression": "unknown_func(demand)",
    }

    repaired_data, skill_create, attempts = asyncio.run(orchestrator._repair_skill_until_valid(
        skill_data=initial,
        session_id="session1",
        conversation="用户: 修一下公式",
        calculation_logic_md="# logic",
        sample_input=SAMPLE_INPUT,
    ))

    assert attempts == 1
    assert len(calls) == 1
    assert "unknown_func" in calls[0]
    assert repaired_data["dsl_expression"] == "mean(demand)"
    assert skill_create.dsl_expression == "mean(demand)"


def test_repair_skill_until_valid_fails_after_retry_limit(monkeypatch):
    async def fake_llm_chat(messages, model=None):
        return {"content": json.dumps({
            "skill_name": "still broken",
            "skill_type": "dsl",
            "dsl_expression": "missing_func(demand)",
        })}

    monkeypatch.setattr(orchestrator, "llm_chat", fake_llm_chat)
    initial = {
        "skill_name": "broken",
        "skill_type": "dsl",
        "dsl_expression": "unknown_func(demand)",
    }

    with pytest.raises(ValueError, match="after repair attempts"):
        asyncio.run(orchestrator._repair_skill_until_valid(
            skill_data=initial,
            session_id="session1",
            conversation="用户: 修一下公式",
            calculation_logic_md="# logic",
            sample_input=SAMPLE_INPUT,
        ))


def test_build_skill_create_supports_preset_data():
    skill_create = orchestrator._build_skill_create({
        "skill_name": "TimesFM",
        "skill_type": "preset",
        "description": "preset forecast",
        "preset_name": "timesfm",
    }, session_id="session1")

    assert skill_create.skill_type.value == "preset"
    assert skill_create.preset_name == "timesfm"
    assert skill_create.input_params

