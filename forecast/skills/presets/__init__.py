"""预设预测方法 — 基于 infra.skill 框架.

子模块按类别拆分：
- _helpers: 工具函数（需求序列提取、日期解析）
- _statistical: 统计类预设（移动平均、指数平滑、线性趋势、安全库存、库存优化）
- _base_models: 基础模型预设（zero_shot、timesfm、chronos）
- _business: 业务逻辑预设
- _statsmodels: 基于 statsmodels 的预设（Holt-Winters、ARIMA）
"""

from __future__ import annotations

from typing import Any

from infra.skill import Skill, SkillRegistry

# ---------------------------------------------------------------------------
# 导入所有预设实现函数
# ---------------------------------------------------------------------------

from forecast.skills.presets._statistical import (
    moving_average,
    exponential_smoothing,
    linear_trend,
    safety_stock_planning,
    inventory_optimization,
)
from forecast.skills.presets._base_models import (
    zero_shot,
    timesfm,
    chronos,
)
from forecast.skills.presets._business import (
    fwdy_jitcall_priority,
    geely_monthly_daily_blend,
    fawvw_long_cycle,
    gac_ne_monthly_split,
    saic_daily_to_monthly_split,
    toyota_row_merge_freeze,
    ming_daily_order_blend,
)
from forecast.skills.presets._statsmodels import (
    holt_winters,
    arima,
)

# ---------------------------------------------------------------------------
# 注册表 (infra.skill.SkillRegistry)
# ---------------------------------------------------------------------------

_PRESETS = SkillRegistry()

_PRESETS.register_all([
    # -- Algorithm --
    Skill(name="moving_average", handler=moving_average, aliases=["moving_average"],
          description="简单移动平均预测。取最近 N 天需求均值作为未来预测",
          params={"window": 7}, category="algorithm",
          tags=["demand"],
          metadata={"algorithm": "取最近 N 天 qty 均值 → 输出为 horizon 天常量预测",
                     "output": "每个输出点为等额常量（平滑值）"}),
    Skill(name="exponential_smoothing", handler=exponential_smoothing, aliases=["exponential_smoothing"],
          description="指数平滑预测。越近的数据权重越大",
          params={"alpha": 0.3}, category="algorithm",
          tags=["demand"],
          metadata={"algorithm": "EMA: forecast[t+1] = α·actual[t] + (1−α)·forecast[t]",
                     "output": "每个输出点为指数加权值"}),
    Skill(name="linear_trend", handler=linear_trend, aliases=["linear_trend"],
          description="线性趋势外推。用最近数据拟合趋势线延长",
          params={"window": 30}, category="algorithm",
          tags=["demand"],
          metadata={"algorithm": "线性回归拟合最近 N 点 → 外推 horizon 天",
                     "output": "沿趋势线的外推值"}),
    Skill(name="safety_stock_planning", handler=safety_stock_planning, aliases=["safety_stock_planning"],
          description="安全库存计划。需求预测 + 安全库存 - 期初库存 - PGI",
          params={"z_score": 1.65, "window": 30}, category="algorithm",
          tags=["demand", "beginningInventory", "pgi"],
          metadata={"algorithm": "需求预测 + z_score·σ − beginningInventory − PGI",
                     "output": "每日建议补货量"}),
    Skill(name="inventory_optimization", handler=inventory_optimization, aliases=["inventory_optimization"],
          description="库存优化。考虑需求波动、在途库存、安全库存的综合计划",
          params={"service_level": 0.95}, category="algorithm",
          tags=["demand", "beginningInventory", "pgi"],
          metadata={"algorithm": "服务水准 → 安全系数 → 目标库存 − 当前库存 − 在途",
                     "output": "最优补货量"}),
    Skill(name="zero_shot", handler=zero_shot, aliases=["zero_shot"],
          description="Zero-shot 预测。使用 Holt-Winters 作为核心引擎（数据不足时自动降级）",
          params={"horizon": 7, "engine": "holt_winters"}, category="algorithm",
          tags=["demand"],
          metadata={"algorithm": "Holt-Winters → 数据不足时降级为 linear_trend → moving_average",
                     "output": "horizon 天预测序列"}),
    Skill(name="timesfm", handler=timesfm, aliases=["timesfm"],
          description="TimesFM 预测占位实现。当前用本地趋势外推 fallback",
          params={"horizon": 7, "fallback": "linear_trend"}, category="algorithm",
          tags=["demand"],
          metadata={"algorithm": "当前 fallback 到 linear_trend；预留 TimesFM 接入点",
                     "output": "horizon 天预测序列"}),
    Skill(name="chronos", handler=chronos, aliases=["chronos"],
          description="Chronos 预测。使用 ARIMA 自动选参作为核心引擎（数据不足时自动降级）",
          params={"horizon": 7, "engine": "arima"}, category="algorithm",
          tags=["demand"],
          metadata={"algorithm": "ARIMA 自动选参 → 数据不足时降级为 linear_trend → exponential_smoothing",
                     "output": "horizon 天预测序列"}),
    Skill(name="holt_winters", handler=holt_winters, aliases=["holt_winters"],
          description="Holt-Winters 三次指数平滑预测。自动检测趋势和季节性",
          params={"horizon": 7, "seasonal_periods": 7}, category="algorithm",
          tags=["demand"],
          metadata={"algorithm": "Level + Trend + Seasonal 三因子分解 → 外推 horizon 天",
                     "output": "含趋势和季节分量的 horizon 天预测"}),
    Skill(name="arima", handler=arima, aliases=["arima"],
          description="ARIMA 自动选参预测。基于 AIC 准则自动选择最优参数",
          params={"horizon": 7, "max_p": 3, "max_d": 2, "max_q": 3}, category="algorithm",
          tags=["demand"],
          metadata={"algorithm": "网格搜索 (p,d,q) 最小化 AIC → 拟合 ARIMA → 外推 horizon 天",
                     "output": "horizon 天预测序列"}),

    # -- Business --
    Skill(name="fwdy_jitcall_priority", handler=fwdy_jitcall_priority, aliases=["fwdy_jitcall_priority", "jitcall_priority"],
          description="JITCall 优先级预测。按 JITCall > 日订单 > 周需求余量平摊的优先级计算每日发货量",
          params={"transportation_lt": 3}, category="business",
          tags=["weekly_demand", "jitcall", "transportationLT"],
          metadata={
              "algorithm": "JITCall 天取 JITCall 值 → gap = 周需求−PGI−JITCall 合计 → gap 平摊到非 JITCall 天",
              "output": "每日发货量",
              "card": {"how_it_works": "1. JITCall 天直接取 JITCall 值覆盖原日需求\n2. 计算余量 = 周需求 − PGI − JITCall 合计\n3. 余量 > 0 时均摊到非 JITCall 天",
                       "doc": "docs/presets/fwdy_jitcall_priority.md"},
          }),
    Skill(name="geely_monthly_daily_blend", handler=geely_monthly_daily_blend, aliases=["geely_monthly_daily_blend", "monthly_daily_blend"],
          description="月预测+日需求整合预测。有日需求取日需求，无日需求取月预测平摊",
          params={"transportation_lt": 3}, category="business",
          tags=["monthly_forecast", "beginningInventory", "ins"],
          metadata={
              "algorithm": "逐月 fill_rate → blended_demand → Balance → 净需求",
              "output": "每日净需求 + 库存余额",
              "card": {"how_it_works": "1. 每月独立计算 fill_rate\n2. 有 demand 取 demand, 无 demand 取 fill_rate\n3. 库存链路: Balance[i] = Balance[i-1] + INS[i] − 流出[i]\n4. 净需求 = max(0, −Balance)",
                       "doc": "docs/presets/geely_monthly_daily_blend.md"},
          }),
    Skill(name="fawvw_long_cycle", handler=fawvw_long_cycle, aliases=["fawvw_long_cycle"],
          description="FAW-VW 长周期需求预测。组合周需求一(7XM) + 周需求二(新项目)，按周分组",
          params={"transportation_lt": 3}, category="business",
          tags=["weekly_demand", "demand", "jitcall", "pgi"],
          metadata={
              "algorithm": "demand 按 ISO 周分组 → 每周内 JITCall 优先 → 余量平摊",
              "output": "每周每日发货量",
              "card": {"doc": "docs/presets/fawvw_long_cycle.md"},
          }),
    Skill(name="gac_ne_monthly_split", handler=gac_ne_monthly_split, aliases=["gac_ne_monthly_split"],
          description="GAC-NE 月预测拆分。将 6 个月的预测数量拆分为月度 ForecastEntity",
          params={}, category="business",
          tags=["forecast_first_num", "current_month", "delivery_count"],
          metadata={
              "algorithm": "6 个月预测 → 当月 forecast − delivery_count → 输出当月净预测",
              "output": "月度 ForecastEntity 列表",
          }),
    Skill(name="saic_daily_to_monthly_split", handler=saic_daily_to_monthly_split, aliases=["saic_daily_to_monthly_split", "daily_to_monthly_split"],
          description="日需求转月度拆分。按日期合并日需求，生成日度明细 + 月度汇总",
          params={"merge_by_date": True}, category="business",
          tags=["demand"],
          metadata={
              "algorithm": "按日期提取月份 → 同月 qty 求和 → 日度明细 + 月度汇总",
              "output": "日度明细 + 月度汇总列表",
          }),
    Skill(name="toyota_row_merge_freeze", handler=toyota_row_merge_freeze, aliases=["toyota_row_merge_freeze", "gtmc_row_merge_freeze"],
          description="Toyota GTMC 行合并 + 冻结汇总。按 Plant+Supplier+PartNo+Date 合并行",
          params={}, category="business",
          tags=["rows"],
          metadata={
              "algorithm": "按 plant+supplier+partNo+date 分组 → 日度 qty 求和 → 去重月度",
              "output": "日度明细 + 月度汇总（去重后）",
          }),
    Skill(name="ming_daily_order_blend", handler=ming_daily_order_blend, aliases=["ming_daily_order_blend"],
          description="名辰 Ming 日订单+预测缺口补足。日订单优先，缺口由周/月预测平摊",
          params={"transportation_lt": 2}, category="business",
          tags=["forecast_type", "daily_orders", "forecast"],
          metadata={
              "algorithm": "日订单优先 → 周度: gap 放周日 → 月度: 平摊到月末",
              "output": "每日发货量（订单或平摊值）",
              "card": {"doc": "docs/presets/ming_daily_order_blend.md"},
          }),
])

# ---------------------------------------------------------------------------
# Public API (backward compatible)
# ---------------------------------------------------------------------------


def run_preset(name: str, record: dict[str, Any]) -> list[dict[str, Any]]:
    """按名称调度预设方法 → 委托 infra.skill.SkillRegistry.execute."""
    return _PRESETS.execute(name, record)


def get_preset_info() -> list[dict[str, Any]]:
    """返回所有可用预设的元数据 → 委托 infra.skill.SkillRegistry.list_skills."""
    return _PRESETS.list_skills()
