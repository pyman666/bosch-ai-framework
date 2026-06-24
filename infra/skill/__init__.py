"""Skill 框架 — 声明式技能注册与执行.

用法::

    from infra.skill import Skill, SkillRegistry

    registry = SkillRegistry()

    def my_skill(record: dict) -> list[dict]:
        return [{"date": "2026-01-01", "qty": 100}]

    registry.register(Skill(
        name="my_skill",
        handler=my_skill,
        description="我的技能",
        params={"param1": 42},
    ))

    result = registry.execute("my_skill", {"demand": [...]})
    meta = registry.list_skills()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


@dataclass
class Skill:
    """单个技能的完整定义.

    Args:
        name: 唯一标识符 (snake_case)
        handler: 执行函数 ``(input: dict) -> Any``
        description: 一句话描述
        params: 默认参数
        category: 分类标签，如 "algorithm" / "business"
        tags: 触发特征字段列表
        metadata: 额外元数据 (card, doc, algorithm, output 等)
    """

    name: str
    handler: Callable[..., Any]
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    category: str = ""
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# SkillRegistry
# ---------------------------------------------------------------------------


class SkillRegistry:
    """声明式技能注册表.

    用法::

        registry = SkillRegistry()

        def my_skill(record: dict) -> list[dict]:
            ...

        registry.register(Skill(
            name="my_skill",
            handler=my_skill,
            description="...",
            params={"horizon": 7},
        ))

        # 按名称执行
        result = registry.execute("my_skill", {"demand": [...]})

        # 列出所有技能元数据 (给前端/LLM)
        meta = registry.list_skills()
    """

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}
        self._aliases: dict[str, str] = {}  # alias → name

    # -------------------------------------------------------------------
    # Register
    # -------------------------------------------------------------------

    def register(self, skill: Skill) -> Skill:
        """注册一个技能 (可重复调用覆盖同名)."""
        self._skills[skill.name] = skill
        for alias in skill.aliases:
            self._aliases[self._normalize(alias)] = skill.name
        return skill

    def register_all(self, skills: list[Skill]) -> None:
        """批量注册."""
        for skill in skills:
            self.register(skill)

    # -------------------------------------------------------------------
    # Execute
    # -------------------------------------------------------------------

    def execute(self, name: str, input: dict[str, Any], **overrides) -> Any:
        """按名称执行技能.

        Args:
            name: 技能名 (支持 snake_case / kebab-case / 空格混合)
            input: 输入数据
            **overrides: 覆盖默认参数 (e.g. ``horizon=14``)

        Returns:
            技能执行结果

        Raises:
            ValueError: 技能不存在
        """
        skill = self._resolve(name)
        merged = {**skill.params, **input}
        try:
            # 优先传 kwargs（支持 handler(record, window=7) 写法）
            return skill.handler(merged, **overrides)
        except TypeError:
            # handler 不接受 kwargs（如 handler(record)），回退到纯 dict
            if overrides:
                merged = {**merged, **overrides}
            return skill.handler(merged)
        except Exception:
            log.exception(f"Skill {skill.name} failed")
            raise

    # -------------------------------------------------------------------
    # Query
    # -------------------------------------------------------------------

    def get(self, name: str) -> Skill | None:
        """按名称获取技能定义."""
        normalized = self._normalize(name)
        return self._skills.get(normalized)

    def list_skills(self) -> list[dict[str, Any]]:
        """返回所有已注册技能的元数据列表."""
        return [
            {
                "name": s.name,
                "description": s.description,
                "params": s.params,
                "category": s.category,
                "tags": s.tags,
                **s.metadata,
            }
            for s in self._skills.values()
        ]

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, name: str) -> bool:
        return self._normalize(name) in self._skills

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    def _resolve(self, name: str) -> Skill:
        normalized = self._normalize(name)
        # 先查主名，再查别名
        skill = self._skills.get(normalized)
        if skill is None:
            real_name = self._aliases.get(normalized)
            if real_name:
                skill = self._skills.get(real_name)
        if skill is None:
            available = ", ".join(sorted(self._skills.keys()))
            raise ValueError(f"Unknown skill: {name}. Available: {available}")
        return skill

    @staticmethod
    def _normalize(name: str) -> str:
        return name.strip().lower().replace("-", "_").replace(" ", "_")
