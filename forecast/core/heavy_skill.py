"""重计算 Skill 识别 — 判断一个 skill 是否吃资源（ARIMA、Holt-Winters 等）。"""

from __future__ import annotations

# 已知吃资源的预设名称
_HEAVY_PRESETS: set[str] = {
    "arima", "holt_winters", "chronos", "zero_shot",
}

# Python skill 中包含这些 import 则视为重计算
_HEAVY_IMPORTS: tuple[str, ...] = ("statsmodels",)


def is_heavy_skill(
    skill_type: str,
    preset_name: str | None = None,
    python_code: str | None = None,
) -> bool:
    """判断一个 skill 是否为重计算类型（ARIMA、Holt-Winters 等）。"""
    if skill_type == "preset" and preset_name and preset_name in _HEAVY_PRESETS:
        return True
    if skill_type == "python" and python_code:
        return any(imp in python_code for imp in _HEAVY_IMPORTS)
    return False
