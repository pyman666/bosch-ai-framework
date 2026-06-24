"""SQLAlchemy ORM 模型 — Skill 与聊天会话持久化。"""

from __future__ import annotations

import json
import logging

from sqlalchemy import Column, String, Integer, Text, DateTime, ForeignKey

from forecast.database import Base
from forecast.utils import utcnow as _utcnow

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ChatSession ORM
# ---------------------------------------------------------------------------

class ChatSessionORM(Base):
    __tablename__ = "chat_sessions"

    id = Column(String(32), primary_key=True)
    title = Column(String(256), default="New Forecast Chat")
    messages_json = Column(Text, default="[]")          # JSON 编码的 ChatMessage 字典列表
    input_data_json = Column(Text, nullable=True)        # JSON 编码的预测输入数据
    target_skill_id = Column(String(32), nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    def get_messages(self) -> list[dict]:
        try:
            return json.loads(self.messages_json) if self.messages_json else []
        except json.JSONDecodeError:
            log.warning("Corrupted JSON in messages_json for id=%s", self.id)
            return []

    def set_messages(self, messages: list[dict]):
        self.messages_json = json.dumps(messages, ensure_ascii=False, default=str)

    def get_input_data(self) -> dict | None:
        try:
            return json.loads(self.input_data_json) if self.input_data_json else None
        except json.JSONDecodeError:
            log.warning("Corrupted JSON in input_data_json for id=%s", self.id)
            return None

    def set_input_data(self, data: dict | None):
        self.input_data_json = json.dumps(data, ensure_ascii=False, default=str) if data else None


# ---------------------------------------------------------------------------
# JsonFieldsMixin — shared JSON column helpers
# ---------------------------------------------------------------------------

class JsonFieldsMixin:
    """ORM 模型的共享 JSON 列辅助方法，用于处理 tags 和 I/O 参数。"""

    def get_tags(self) -> list[str]:
        """获取标签列表。"""
        try:
            return json.loads(self.tags_json) if self.tags_json else []
        except json.JSONDecodeError:
            log.warning("Corrupted JSON in tags_json for id=%s", self.id)
            return []

    def set_tags(self, tags: list[str]) -> None:
        """设置标签列表。"""
        self.tags_json = json.dumps(tags, ensure_ascii=False)

    def get_input_params(self) -> list[dict]:
        """获取输入参数定义。"""
        try:
            return json.loads(self.input_params_json) if self.input_params_json else []
        except json.JSONDecodeError:
            log.warning("Corrupted JSON in input_params_json for id=%s", self.id)
            return []

    def set_input_params(self, params: list[dict]) -> None:
        """设置输入参数定义。"""
        self.input_params_json = json.dumps(params, ensure_ascii=False)

    def get_output_params(self) -> list[dict]:
        """获取输出参数定义。"""
        try:
            return json.loads(self.output_params_json) if self.output_params_json else []
        except json.JSONDecodeError:
            log.warning("Corrupted JSON in output_params_json for id=%s", self.id)
            return []

    def set_output_params(self, params: list[dict]) -> None:
        """设置输出参数定义。"""
        self.output_params_json = json.dumps(params, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Skill ORM
# ---------------------------------------------------------------------------

class SkillORM(Base, JsonFieldsMixin):
    __tablename__ = "skills"

    id = Column(String(32), primary_key=True)
    name = Column(String(256), nullable=False)
    description = Column(Text, default="")
    skill_type = Column(String(16), default="dsl")       # dsl / python / preset
    status = Column(String(16), default="draft")          # draft / reviewed / active / archived
    dsl_expression = Column(Text, nullable=True)
    python_code = Column(Text, nullable=True)
    preset_name = Column(String(128), nullable=True)
    input_params_json = Column(Text, default="[]")
    output_params_json = Column(Text, default="[]")
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    version = Column(Integer, default=1)
    chat_session_id = Column(String(32), nullable=True)
    tags_json = Column(Text, default="[]")


class SkillVersionORM(Base, JsonFieldsMixin):
    """Skill 历史版本快照。"""
    __tablename__ = "skill_versions"

    id = Column(String(64), primary_key=True)
    skill_id = Column(String(32), ForeignKey("skills.id", ondelete="CASCADE"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    action = Column(String(32), default="snapshot")
    name = Column(String(256), nullable=False)
    description = Column(Text, default="")
    skill_type = Column(String(16), default="dsl")
    status = Column(String(16), default="draft")
    dsl_expression = Column(Text, nullable=True)
    python_code = Column(Text, nullable=True)
    preset_name = Column(String(128), nullable=True)
    input_params_json = Column(Text, default="[]")
    output_params_json = Column(Text, default="[]")
    chat_session_id = Column(String(32), nullable=True)
    tags_json = Column(Text, default="[]")
    created_at = Column(DateTime, default=_utcnow)
