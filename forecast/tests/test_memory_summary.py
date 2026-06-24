"""Memory summary tests."""

from forecast.core import memory


def _long_messages():
    msgs = []
    for i in range(5):
        msgs.append({"role": "user", "content": f"第{i}轮：我要基于 demand 和 PGI 预测 A3 发货，并考虑库存。"})
        msgs.append({"role": "assistant", "content": f"第{i}轮建议：使用 moving_average(demand, 7) + safety_stock(demand, 1.65) 作为 DSL skill。"})
    return msgs


def test_save_session_snapshot_writes_summary_for_long_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "MEMORY_DIR", tmp_path)

    memory.save_session_snapshot(
        session_id="summary-session",
        messages=_long_messages(),
        input_data=[{"carModel": "A3", "color": "黑色", "beginningInventory": 10}],
        target_skill_id="skill_123",
    )

    summary = memory.load_session_summary("summary-session")

    assert "会话摘要" in summary
    assert "A3" in summary
    assert "skill_123" in summary
    assert "moving_average" in summary


def test_compact_messages_for_agent_uses_summary_and_recent_messages():
    messages = _long_messages()

    compacted = memory.compact_messages_for_agent(
        session_id="summary-session",
        messages=messages,
        input_data=[{"carModel": "A3", "color": "黑色"}],
        target_skill_id="skill_123",
        threshold=4,
        recent_count=3,
    )

    assert len(compacted) == 4
    assert compacted[0]["role"] == "system"
    assert "自动摘要" in compacted[0]["content"]
    assert "moving_average" in compacted[0]["content"]
    assert compacted[1:] == messages[-3:]


def test_search_memory_returns_summary_and_can_match_summary_only(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "MEMORY_DIR", tmp_path)

    memory.save_session_snapshot(
        session_id="summary-search",
        messages=_long_messages(),
        input_data=[{"carModel": "A3", "color": "黑色"}],
        target_skill_id="skill_123",
    )

    results = memory.search_memory("关联 Skill")

    assert len(results) == 1
    assert results[0]["session_id"] == "summary-search"
    assert "summary" in results[0]
    assert "skill_123" in results[0]["summary"]


def test_short_chat_does_not_store_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "MEMORY_DIR", tmp_path)

    memory.save_session_snapshot(
        session_id="short-chat",
        messages=[{"role": "user", "content": "短对话"}],
    )

    assert memory.load_session_summary("short-chat") == ""

