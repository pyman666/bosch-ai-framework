"""轻量级聊天记忆存储，用于 Agent 上下文复用。

数据库仍然是会话的唯一真实来源。本模块将对话镜像到 JSONL 文件，
以便 Agent 工具可以搜索之前的讨论和 Skill 设计决策，而无需数据库会话。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


MEMORY_DIR = Path(__file__).parent.parent / "memory" / "sessions"
MEMORY_DIR.mkdir(parents=True, exist_ok=True)

SUMMARY_TRIGGER_MESSAGE_COUNT = 8
RECENT_CONTEXT_MESSAGE_COUNT = 8
SUMMARY_MAX_CHARS = 1800


def _session_path(session_id: str) -> Path:
    safe_id = "".join(ch for ch in session_id if ch.isalnum() or ch in ("-", "_"))
    if not safe_id:
        raise ValueError("Invalid session_id for memory storage")
    return MEMORY_DIR / f"{safe_id}.jsonl"


def _truncate(text: str, max_chars: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _input_data_brief(input_data: Any | None) -> str:
    if not input_data:
        return ""
    records = input_data if isinstance(input_data, list) else [input_data]
    if not records or not isinstance(records[0], dict):
        return ""
    # 提取所有非计算字段作为标识
    _CS = {
        "demand", "pgi", "beginningInventory", "jitcall", "ins", "other_factors",
        "other_factors_to_be_added", "weekly_demand", "monthly_forecast",
        "transportationLT", "forecast_period",
        "dsl_expression", "python_code", "preset_name", "skill_type",
        "description", "name", "id", "title", "status", "created_at", "updated_at",
        "metadata", "forecast", "error", "message", "role", "content"
    }
    identity_keys = [k for k in records[0].keys() if k not in _CS]
    parts = []
    for key in identity_keys:
        vals = sorted({str(r.get(key, "")) for r in records if isinstance(r, dict) and r.get(key)})
        if vals:
            parts.append(f"{key}={', '.join(vals[:5])}")
    if not parts:
        return f"输入数据：{len(records)} 条记录。"
    return f"输入数据：{len(records)} 条记录；{'；'.join(parts)}。"


def build_session_summary(
    messages: list[dict[str, Any]],
    input_data: Any | None = None,
    target_skill_id: str | None = None,
    max_chars: int = SUMMARY_MAX_CHARS,
) -> str:
    """为较长的预测设计对话构建确定性的紧凑摘要。

    此函数完全本地运行且无外部依赖，后台记忆写入不需要 LLM 可用。
    摘要重点关注业务目标、公式/Skill 决策以及最近的用户约束。
    """
    non_empty = [m for m in messages if (m.get("content") or "").strip()]
    if not non_empty:
        return ""

    user_msgs = [m for m in non_empty if m.get("role") == "user"]
    assistant_msgs = [m for m in non_empty if m.get("role") == "assistant"]
    keywords = (
        "公式", "逻辑", "skill", "dsl", "python", "preset", "预测", "发货",
        "库存", "pgi", "demand", "moving_average", "safety_stock", "timesfm", "chronos",
    )

    latest_goal = _truncate(user_msgs[-1].get("content", "") if user_msgs else "", 260)
    first_goal = _truncate(user_msgs[0].get("content", "") if user_msgs else "", 220)
    decisions = []
    for msg in assistant_msgs:
        content = msg.get("content", "")
        if any(k.lower() in content.lower() for k in keywords):
            decisions.append(_truncate(content, 320))
    recent_constraints = [_truncate(m.get("content", ""), 180) for m in user_msgs[-3:]]

    lines = [
        "# 会话摘要",
        f"消息数：{len(non_empty)}；用户消息：{len(user_msgs)}；助手消息：{len(assistant_msgs)}。",
    ]
    input_brief = _input_data_brief(input_data)
    if input_brief:
        lines.append(input_brief)
    if target_skill_id:
        lines.append(f"关联 Skill：{target_skill_id}。")
    if first_goal:
        lines.append(f"初始目标：{first_goal}")
    if latest_goal and latest_goal != first_goal:
        lines.append(f"最新需求：{latest_goal}")
    if decisions:
        lines.append("关键计算/Skill 决策：")
        for item in decisions[-5:]:
            lines.append(f"- {item}")
    if recent_constraints:
        lines.append("最近用户约束/反馈：")
        for item in recent_constraints:
            lines.append(f"- {item}")

    return _truncate("\n".join(lines), max_chars)


def compact_messages_for_agent(
    session_id: str,  # 保留参数以便调用方传递，当前未使用
    messages: list[dict[str, Any]],
    input_data: Any | None = None,
    target_skill_id: str | None = None,
    threshold: int = SUMMARY_TRIGGER_MESSAGE_COUNT,
    recent_count: int = RECENT_CONTEXT_MESSAGE_COUNT,
) -> list[dict[str, Any]]:
    """对短对话返回完整消息，对长对话返回摘要 + 最近消息。"""
    if len(messages) <= threshold:
        return messages

    summary = build_session_summary(messages, input_data=input_data, target_skill_id=target_skill_id)
    summary_msg = {
        "role": "system",
        "content": (
            "以下是本会话较早内容的自动摘要，用于继续编辑/新建 Forecast Skill。"
            "请结合摘要和最近消息保持上下文一致。\n\n"
            f"{summary}"
        ),
    }
    return [summary_msg] + messages[-recent_count:]


def save_session_snapshot(
    session_id: str,
    messages: list[dict[str, Any]],
    input_data: Any | None = None,
    target_skill_id: str | None = None,
) -> Path:
    """将当前会话消息持久化为 JSONL 文件。

    每次保存时原子覆盖文件，避免会话反复更新时产生重复消息。
    """
    path = _session_path(session_id)
    now = datetime.now(timezone.utc).isoformat()
    lines: list[str] = []

    header = {
        "type": "session",
        "session_id": session_id,
        "target_skill_id": target_skill_id,
        "input_data_preview": input_data[:3] if isinstance(input_data, list) else input_data,
        "summary": build_session_summary(messages, input_data, target_skill_id)
        if len(messages) >= SUMMARY_TRIGGER_MESSAGE_COUNT else "",
        "updated_at": now,
    }
    lines.append(json.dumps(header, ensure_ascii=False, default=str))

    for index, msg in enumerate(messages):
        lines.append(json.dumps({
            "type": "message",
            "session_id": session_id,
            "index": index,
            "role": msg.get("role", ""),
            "content": msg.get("content", ""),
            "timestamp": msg.get("timestamp") or now,
        }, ensure_ascii=False, default=str))

    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def save_messages(session_id: str, messages: list[dict[str, Any]]) -> None:
    """为旧调用方提供的向后兼容包装器。"""
    save_session_snapshot(session_id=session_id, messages=messages)


def load_messages(session_id: str) -> list[dict[str, Any]]:
    """从会话 JSONL 记忆文件中加载消息记录。"""
    path = _session_path(session_id)
    if not path.exists():
        return []

    messages: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            log.warning("Skipping corrupted JSONL line in %s", path)
            continue
        if item.get("type") == "message":
            messages.append({
                "role": item.get("role", ""),
                "content": item.get("content", ""),
            })
        elif "role" in item:
            messages.append(item)
    return messages


def load_session_summary(session_id: str) -> str:
    """从会话记忆文件中加载已存储的摘要。"""
    path = _session_path(session_id)
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            log.warning("Skipping corrupted JSONL line in %s", path)
            continue
        if item.get("type") == "session":
            return item.get("summary", "") or ""
    return ""


def append_message(session_id: str, message: dict[str, Any]) -> None:
    """追加一条消息，同时保留已有消息。"""
    messages = load_messages(session_id)
    messages.append(message)
    save_session_snapshot(session_id=session_id, messages=messages)


def delete_session_memory(session_id: str) -> None:
    """删除会话的镜像 JSONL 记忆文件。"""
    path = _session_path(session_id)
    if path.exists():
        path.unlink()


def search_memory(query: str, limit: int = 5, exclude_session_id: str | None = None) -> list[dict[str, Any]]:
    """对镜像的会话记忆 JSONL 文件进行关键词搜索。"""
    query = (query or "").strip().lower()
    if not query:
        return []

    results: list[dict[str, Any]] = []
    for file_path in sorted(MEMORY_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        if file_path.name.startswith("_"):
            continue
        session_id = file_path.stem
        if exclude_session_id and session_id == exclude_session_id:
            continue
        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError:
            continue

        summary = ""
        try:
            first = next((line for line in text.splitlines() if line.strip()), "")
            header = json.loads(first) if first else {}
            if header.get("type") == "session":
                summary = header.get("summary", "") or ""
        except (json.JSONDecodeError, StopIteration):
            summary = ""

        searchable = f"{summary}\n{text}" if summary else text
        lower = searchable.lower()
        match_at = lower.find(query)
        if match_at < 0:
            continue

        start = max(0, match_at - 120)
        end = min(len(searchable), match_at + 240)
        snippet = searchable[start:end].replace("\n", " ")
        results.append({
            "session_id": session_id,
            "summary": summary,
            "snippet": f"...{snippet}...",
            "memory_file": str(file_path),
        })
        if len(results) >= limit:
            break

    return results
