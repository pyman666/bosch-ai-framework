"""编排器 — 将聊天对话转化为预测 Skill 和文档。"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from forecast.db_models import ChatSessionORM
from infra.llm import chat as llm_chat
from forecast.models.forecast import ForecastInput
from forecast.models.skill import SkillCreate, SkillType, ParamDef
from forecast.core import skill_manager
from forecast.core.executor import execute_skill

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

SUMMARIZE_PROMPT = """你是一个预测公式文档撰写专家。根据以下用户与 AI 助手的对话记录，生成一份**计算逻辑文档**（Markdown 格式）。

文档应包含：
1. **业务目标**: 用户想要预测什么
2. **输入数据**: 使用了哪些字段（demand需求、PGI在途、期初库存等）
3. **计算逻辑**: 分步骤描述公式的计算过程
4. **公式/伪代码**: 用数学公式或伪代码描述核心逻辑
5. **参数说明**: 各参数的含义和推荐值
6. **注意事项**: 数据要求、边界情况、局限性

对话记录：
{conversation}

请只输出 Markdown 文档，不要加额外解释。"""


SKILL_GENERATE_PROMPT = """你是一个预测系统开发者。根据以下对话记录和计算逻辑，生成一个可执行的预测 Skill。

对话记录：
{conversation}

计算逻辑文档：
{logic_doc}

请以 JSON 格式返回 skill 定义：
```json
{{
    "skill_name": "技能名称",
    "skill_type": "dsl 或 python",
    "description": "简短描述",
    "dsl_expression": "DSL 表达式（skill_type=dsl 时填写）",
    "python_code": "Python 代码（skill_type=python 时填写，必须包含 forecast(record) 函数）",
    "preset_name": "预设方法名（skill_type=preset 时填写，可选 zero_shot/timesfm/chronos/moving_average 等）",
    "input_params": [
        {{"name": "demand", "type": "date_series", "description": "...", "required": true}},
        ...
    ],
    "output_params": [
        {{"name": "forecast", "type": "date_series", "description": "预测结果"}}
    ]
}}
```

DSL 可用的函数包括：moving_average, exponential_smoothing, linear_trend, seasonal_index,
safety_stock, inventory_planning, sum, mean, std, min, max, shift, cumsum, if_then_else, round, abs.

如果逻辑较简单，优先使用 DSL 表达式。只在 DSL 无法表达时才用 Python。若用户明确选择内置算法，可返回 preset。

请只输出 JSON，不要加额外解释。"""


SKILL_REPAIR_PROMPT = """你生成的预测 Skill 未通过后端 dry-run 校验，请根据错误修复 Skill JSON。

对话记录：
{conversation}

计算逻辑文档：
{logic_doc}

上一次 Skill JSON：
```json
{skill_json}
```

后端 dry-run 错误：
{error}

修复要求：
1. 只返回完整 JSON，不要解释。
2. 简单逻辑优先用 DSL。
3. DSL 只能使用这些函数：moving_average, exponential_smoothing, linear_trend, seasonal_index, safety_stock, inventory_planning, sum, mean, std, min, max, shift, cumsum, if_then_else, round, abs。
4. Python 必须定义 forecast(record) 函数，并避免文件、网络、进程、动态执行等高风险操作。
5. 如果用户选择内置模型，可返回 preset，preset_name 可选：moving_average, exponential_smoothing, linear_trend, safety_stock_planning, inventory_optimization, zero_shot, timesfm, chronos。
"""

MAX_SKILL_REPAIR_ATTEMPTS = 2


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def generate_skill_from_chat(
    session_id: str,
    messages: list[dict[str, Any]],
    db: Session,
    model: str | None = None,
) -> dict[str, Any]:
    """根据聊天消息生成计算逻辑文档和 Skill。

    返回字典，包含以下键：
        - calculation_logic_md
        - skill_md
        - skill (Skill Pydantic 模型)
        - dsl_expression / python_code
    """

    # 1. 构建对话文本
    conversation = _format_conversation(messages)

    # 2. 生成计算逻辑.md
    logic_prompt = SUMMARIZE_PROMPT.format(conversation=conversation)
    logic_response = await llm_chat(
        messages=[{"role": "user", "content": logic_prompt}],
        model=model,
    )
    calculation_logic_md = logic_response.get("content", "")

    # 3. 生成 Skill 定义（DSL 或 Python）
    skill_prompt = SKILL_GENERATE_PROMPT.format(
        conversation=conversation,
        logic_doc=calculation_logic_md,
    )
    skill_response = await llm_chat(
        messages=[{"role": "user", "content": skill_prompt}],
        model=model,
    )

    skill_json_str = skill_response.get("content", "{}")
    skill_data = _parse_skill_json(skill_json_str)
    _normalize_skill_data(skill_data)

    # 4. 保存前 dry-run；失败时让 LLM 根据错误自动修复后重试
    sample_input = _sample_input_from_session(session_id, db)
    skill_data, skill_create, repair_attempts = await _repair_skill_until_valid(
        skill_data=skill_data,
        session_id=session_id,
        conversation=conversation,
        calculation_logic_md=calculation_logic_md,
        sample_input=sample_input,
        model=model,
    )

    # 5. 生成 skill.md（用户友好的 Skill 文档）并保存 Skill 到数据库
    skill_md = build_skill_md(skill_data, calculation_logic_md)

    skill = skill_manager.create_skill(db, skill_create)

    return {
        "calculation_logic_md": calculation_logic_md,
        "skill_md": skill_md,
        "skill": skill,
        "dsl_expression": skill_data.get("dsl_expression"),
        "python_code": skill_data.get("python_code"),
        "repair_attempts": repair_attempts,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_conversation(messages: list[dict[str, Any]]) -> str:
    """将消息列表格式化为可读的对话文本。"""
    lines = []
    for msg in messages:
        if msg["role"] == "system":
            continue
        role = "用户" if msg["role"] == "user" else "AI助手"
        content = msg.get("content", "")
        if content:
            lines.append(f"**{role}**: {content}")
    return "\n\n".join(lines)


def _parse_skill_json(raw: str) -> dict[str, Any]:
    """从 LLM 响应中提取 JSON（可能包裹在 ```json 代码块中）。"""
    # 尝试从 ```json ... ``` 代码块中提取
    if "```json" in raw:
        start = raw.find("```json") + 7
        end = raw.find("```", start)
        raw = raw[start:end].strip()
    elif "```" in raw:
        start = raw.find("```") + 3
        end = raw.find("```", start)
        raw = raw[start:end].strip()

    raw = raw.strip()

    # 有些模型会在 JSON 前后加说明文字，尝试截取最外层对象。
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if 0 <= start < end:
            raw = raw[start:end + 1]

    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Skill JSON must be an object")
        return data
    except (json.JSONDecodeError, ValueError):
        log.warning(f"Failed to parse skill JSON: {raw[:200]}")
        return {}


def _normalize_skill_data(skill_data: dict[str, Any]) -> None:
    """将 LLM 生成的 skill 数据原地规范化，以便后续校验。"""
    skill_type = str(skill_data.get("skill_type") or "dsl").strip().lower()
    aliases = {
        "python_script": "python",
        "py": "python",
        "formula": "dsl",
        "expression": "dsl",
        "builtin": "preset",
    }
    skill_data["skill_type"] = aliases.get(skill_type, skill_type)

    for key in ("skill_name", "description", "dsl_expression", "python_code", "preset_name"):
        if isinstance(skill_data.get(key), str):
            skill_data[key] = skill_data[key].strip()


def _build_skill_create(skill_data: dict[str, Any], session_id: str) -> SkillCreate:
    """从规范化后的 LLM skill 数据构建 SkillCreate 模型。"""
    input_params = [
        ParamDef(**p) for p in skill_data.get("input_params", [])
    ] or [
        ParamDef(name="demand", type="date_series", description="需求时间序列", required=True),
        ParamDef(name="pgi", type="date_series", description="PGI在途时间序列", required=False),
        ParamDef(name="beginningInventory", type="number", description="期初库存", required=True),
    ]

    output_params = [
        ParamDef(**p) for p in skill_data.get("output_params", [])
    ] or [
        ParamDef(name="forecast", type="date_series", description="预测发货量"),
    ]

    return SkillCreate(
        name=skill_data.get("skill_name") or f"Skill from {session_id}",
        description=skill_data.get("description", ""),
        skill_type=SkillType(skill_data.get("skill_type", "dsl")),
        dsl_expression=skill_data.get("dsl_expression"),
        python_code=skill_data.get("python_code"),
        preset_name=skill_data.get("preset_name"),
        input_params=input_params,
        output_params=output_params,
        chat_session_id=session_id,
    )


async def _repair_skill_until_valid(
    skill_data: dict[str, Any],
    session_id: str,
    conversation: str,
    calculation_logic_md: str,
    sample_input: ForecastInput,
    model: str | None = None,
) -> tuple[dict[str, Any], SkillCreate, int]:
    """对生成的 Skill 执行干跑测试，失败时让 LLM 自动修复，最多重试数次。"""
    last_error: Exception | None = None

    for attempt in range(MAX_SKILL_REPAIR_ATTEMPTS + 1):
        try:
            skill_create = _build_skill_create(skill_data, session_id)
            _dry_run_skill(skill_create, sample_input)
            return skill_data, skill_create, attempt
        except Exception as exc:
            last_error = exc
            if attempt >= MAX_SKILL_REPAIR_ATTEMPTS:
                break

            repair_prompt = SKILL_REPAIR_PROMPT.format(
                conversation=conversation,
                logic_doc=calculation_logic_md,
                skill_json=json.dumps(skill_data, ensure_ascii=False, indent=2),
                error=str(exc),
            )
            repair_response = await llm_chat(
                messages=[{"role": "user", "content": repair_prompt}],
                model=model,
            )
            skill_data = _parse_skill_json(repair_response.get("content", "{}"))
            _normalize_skill_data(skill_data)

    raise ValueError(f"Generated skill dry-run failed after repair attempts: {last_error}")


def _sample_input_from_session(session_id: str, db: Session) -> ForecastInput:
    """从会话数据中构建 ForecastInput 样本，用于 Skill 干跑测试。"""
    orm = db.query(ChatSessionORM).filter(ChatSessionORM.id == session_id).first()
    input_data = orm.get_input_data() if orm else None
    if isinstance(input_data, list) and input_data:
        return ForecastInput.model_validate(input_data[0])
    if isinstance(input_data, dict):
        return ForecastInput.model_validate(input_data)
    return ForecastInput.model_validate({
        "demand": [
            {"date": "2026-01-01", "qty": 10},
            {"date": "2026-01-02", "qty": 12},
            {"date": "2026-01-03", "qty": 14},
        ],
        "pgi": [
            {"date": "2026-01-01", "qty": 0},
            {"date": "2026-01-02", "qty": 1},
            {"date": "2026-01-03", "qty": 0},
        ],
        "beginningInventory": 5,
    })


def _dry_run_skill(skill_create: SkillCreate, sample_input: ForecastInput) -> None:
    """在持久化前执行一次生成的 Skill，验证其有效性。"""
    try:
        output = execute_skill(
            skill_type=skill_create.skill_type.value,
            dsl_expression=skill_create.dsl_expression,
            python_code=skill_create.python_code,
            preset_name=skill_create.preset_name,
            input_data=sample_input,
        )
    except Exception as exc:
        raise ValueError(f"Generated skill dry-run failed: {exc}") from exc

    if not output.forecast:
        raise ValueError("Generated skill dry-run produced an empty forecast")


def build_skill_md(skill_data: dict, logic_doc: str) -> str:
    """生成用户友好的 Skill 说明文档。"""
    name = skill_data.get("skill_name", "Unnamed Skill")
    desc = skill_data.get("description", "")
    skill_type = skill_data.get("skill_type", "dsl")
    dsl = skill_data.get("dsl_expression", "")
    py_code = skill_data.get("python_code", "")
    preset_name = skill_data.get("preset_name", "")

    # 检测是否为重计算 skill
    from forecast.core.rate_limit import is_heavy_skill
    is_heavy = is_heavy_skill(skill_type, preset_name=preset_name, python_code=py_code)

    md = f"""# {name}

## 描述
{desc}

## 类型
{skill_type}

"""
    if dsl:
        md += f"""## DSL 表达式
```
{dsl}
```

"""
    if py_code:
        md += f"""## Python 代码
```python
{py_code}
```

"""
    if preset_name:
        md += f"""## 预设方法
`{preset_name}`

"""
    if is_heavy:
        md += """> ⚠️ **性能提醒**：此 Skill 为计算密集型（ARIMA/Holt-Winters 等统计模型），
> 单次执行耗时较长。大批量使用时建议走异步端点 `/forecast/async-batch`，
> 系统会自动限制并发数以保护服务稳定性。

"""

    md += f"""## 计算逻辑
{logic_doc}
"""
    return md
