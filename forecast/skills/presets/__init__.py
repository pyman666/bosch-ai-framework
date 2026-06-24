"""预设预测方法 — 可本地运行的轻量级算法。

子模块按类别拆分：
- _helpers: 工具函数（需求序列提取、日期解析）
- _statistical: 统计类预设（移动平均、指数平滑、线性趋势、安全库存、库存优化）
- _base_models: 基础模型预设（zero_shot、timesfm、chronos）
- _business: 业务逻辑预设（fwdy_jitcall_priority、geely_monthly_daily_blend、fawvw_long_cycle、gac_ne_monthly_split、saic_daily_to_monthly_split、toyota_row_merge_freeze、ming_daily_order_blend）
- _statsmodels: 基于 statsmodels 的预设（Holt-Winters、ARIMA）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

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
# 预设注册表
# ---------------------------------------------------------------------------

@dataclass
class _Preset:
    handler: Callable[..., Any]
    aliases: tuple[str, ...]
    description: str
    params: dict[str, Any] = field(default_factory=dict)
    category: str = ""  # "algorithm" | "business"
    trigger_fields: tuple[str, ...] = ()   # 识别特征字段
    algorithm: str = ""                     # 计算流程简述
    output: str = ""                        # 输出含义
    card: dict[str, Any] = field(default_factory=dict)  # 前端详情卡片


_PRESETS: list[_Preset] = [
    _Preset(moving_average, ("moving_average",),
            "简单移动平均预测。取最近 N 天需求均值作为未来预测",
            {"window": 7}, "algorithm",
            ("demand",), "取最近 N 天 qty 均值 → 输出为 horizon 天常量预测", "每个输出点为等额常量（平滑值）"),
    _Preset(exponential_smoothing, ("exponential_smoothing",),
            "指数平滑预测。越近的数据权重越大",
            {"alpha": 0.3}, "algorithm",
            ("demand",), "EMA: forecast[t+1] = α·actual[t] + (1−α)·forecast[t]", "每个输出点为指数加权值"),
    _Preset(linear_trend, ("linear_trend",),
            "线性趋势外推。用最近数据拟合趋势线延长",
            {"window": 30}, "algorithm",
            ("demand",), "线性回归拟合最近 N 点 → 外推 horizon 天", "沿趋势线的外推值"),
    _Preset(safety_stock_planning, ("safety_stock_planning",),
            "安全库存计划。需求预测 + 安全库存 - 期初库存 - PGI",
            {"z_score": 1.65, "window": 30}, "algorithm",
            ("demand", "beginningInventory", "pgi"), "需求预测 + z_score·σ − beginningInventory − PGI", "每日建议补货量"),
    _Preset(inventory_optimization, ("inventory_optimization",),
            "库存优化。考虑需求波动、在途库存、安全库存的综合计划",
            {"service_level": 0.95}, "algorithm",
            ("demand", "beginningInventory", "pgi"), "服务水准 → 安全系数 → 目标库存 − 当前库存 − 在途", "最优补货量"),
    _Preset(zero_shot, ("zero_shot",),
            "Zero-shot 预测。使用 Holt-Winters 作为核心引擎（数据不足时自动降级为线性趋势或移动平均）",
            {"horizon": 7, "engine": "holt_winters"}, "algorithm",
            ("demand",), "Holt-Winters → 数据不足时降级为 linear_trend → moving_average", "horizon 天预测序列"),
    _Preset(timesfm, ("timesfm",),
            "TimesFM 预测占位实现。当前用本地趋势外推 fallback，接口保持为 preset，便于后续接入真实 TimesFM 服务",
            {"horizon": 7, "fallback": "linear_trend"}, "algorithm",
            ("demand",), "当前 fallback 到 linear_trend；预留 TimesFM 接入点", "horizon 天预测序列"),
    _Preset(chronos, ("chronos",),
            "Chronos 预测。使用 ARIMA 自动选参作为核心引擎（数据不足时自动降级为线性趋势或指数平滑）",
            {"horizon": 7, "engine": "arima"}, "algorithm",
            ("demand",), "ARIMA 自动选参 → 数据不足时降级为 linear_trend → exponential_smoothing", "horizon 天预测序列"),
    _Preset(fwdy_jitcall_priority, ("fwdy_jitcall_priority", "jitcall_priority"),
            "JITCall 优先级预测。按 JITCall > 日订单 > 周需求余量平摊的优先级计算每日发货量，周需求按周数均分（适用于富维东阳 FWDY 等客户）",
            {"transportation_lt": 3}, "business",
            ("weekly_demand", "jitcall", "transportationLT"),
            "JITCall 天取 JITCall 值 → gap = 周需求−PGI−JITCall 合计 → gap 平摊到非 JITCall 天 → 每日发货量",
            "每日发货量",
            {"how_it_works": "1. JITCall 天直接取 JITCall 值覆盖原日需求\n2. 计算余量 = 周需求 − PGI − JITCall 合计\n3. 余量 > 0 时均摊到非 JITCall 天; 余量 ≤ 0 则只取 JITCall\n4. 跨多周时 weekly_demand 按周数均分",
             "input_fields": {"weekly_demand": "float — 单周需求总量", "demand": "[{date,qty}] — 每日需求计划", "jitcall": "[{date,qty}] — JITCall 取货订单(可选)", "pgi": "[{date,qty}] — 已发货在途(可选)", "transportationLT": "int=3 — 运输提前期(天)"},
             "notes": "每周一至周日为一个周期; 日需求的 0 值算需求(空值不算); JITCall 优先级最高",
             "doc": "docs/presets/fwdy_jitcall_priority.md"}),
    _Preset(geely_monthly_daily_blend, ("geely_monthly_daily_blend", "monthly_daily_blend"),
            "月预测+日需求整合预测。有日需求取日需求，无日需求取月预测平摊，再扣除期初库存和 PGI/INS 得到净需求（适用于吉利 Geely / 小鹏 Xpeng 等客户）",
            {"transportation_lt": 3}, "business",
            ("monthly_forecast", "beginningInventory", "ins"),
            "逐月 fill_rate=(月预测−当月demand)/剩余天数 → blended_demand → Balance=期初+INS−流出 → 净需求=max(0,−Balance)\nmonthly_forecast 支持 float(单月) 或 dict{\"2026-01\":8014}(多月)",
            "每日净需求 + 库存余额",
            {"how_it_works": "1. 每月独立计算 fill_rate = (月预测 − 当月 demand 合计) / 当月无 demand 的天数\n2. 日期轴从最早 demand(或首月1号)到末月最后一天\n3. 有 demand 的日期取 demand 值, 无 demand 取当月的 fill_rate\n4. 库存链路: Balance[i] = Balance[i-1] + INS/PGI[i] − 流出[i]\n5. 净需求 = max(0, −Balance); 日终 Balance clamp 到 ≥0",
             "input_fields": {"monthly_forecast": "float 或 dict{\"2026-01\":8014,\"2026-02\":5471} — 多月模式下每月的 forecast 独立计算", "demand": "[{date,qty}] — 日需求(0 值算需求)", "beginningInventory": "float — 期初库存", "ins": "[{date,qty}] — 预计到货(优先), 无则 fallback 到 pgi", "pgi": "[{date,qty}] — 已发货在途(fallback)"},
             "notes": "多月 dict 模式日期轴跨所有 forecast 月份; 预预测期(首月之前)有 demand 取 demand 无则 0; 单月 float 模式应用于 first_day 所在日历月",
             "doc": "docs/presets/geely_monthly_daily_blend.md"}),
    _Preset(fawvw_long_cycle, ("fawvw_long_cycle",),
            "FAW-VW 长周期需求预测。组合周需求一(7XM) + 周需求二(新项目)，按周分组，JITCall > 日计划 > 周需求余量平摊（适用于一汽大众 FAW-VW 各工厂）",
            {"transportation_lt": 3}, "business",
            ("weekly_demand", "demand", "jitcall", "pgi"),
            "demand 按 ISO 周分组 → 每周内 JITCall 优先 → 余量平摊（weekly_demand 不按周数均分）",
            "每周每日发货量",
            {"how_it_works": "1. demand 按 ISO 周分组(周一~周日)\n2. weekly_demand 已由调用方合并 7XM+新项目, 不按周数均分\n3. 每周内 JITCall 优先级最高 → 余量平摊到非 JITCall 天",
             "input_fields": {"weekly_demand": "float — 总周需求(调用方已合并)", "demand": "[{date,qty}] — 日计划", "jitcall": "[{date,qty}] — JITCall 取货订单", "pgi": "[{date,qty}] — 已发货在途", "transportationLT": "int=3"},
             "notes": "与模式 A(FWDY)的区别: FAW-VW 的 weekly_demand 不按周数均分, 直接作为每周需求量; 适用 FAW-VW 佛山/长春/天津/成都/青岛工厂",
             "doc": "docs/presets/fawvw_long_cycle.md"}),
    _Preset(gac_ne_monthly_split, ("gac_ne_monthly_split",),
            "GAC-NE 月预测拆分。将 6 个月的预测数量拆分为月度 ForecastEntity，当月扣减已交货量（适用于广汽新能源 GAC-NE Legacy 系统）",
            {}, "business",
            ("forecast_first_num", "current_month", "delivery_count"),
            "6 个月预测 → 当月 forecast − delivery_count → 输出当月净预测",
            "月度 ForecastEntity 列表",
            {"how_it_works": "1. 输入 6 个月预测值(forecast_first_num~forecast_sixth_num)\n2. 当月 forecast 减去已交货量 delivery_count\n3. 超交时 clamp 到 0",
             "input_fields": {"current_month": "int — 当前月份(如 202511)", "forecast_first_num~forecast_sixth_num": "float — 最近6个月预测", "delivery_count": "float — 当月已交货量"},
             "notes": "Legacy 系统(LINC/NCIC)的月度预测拆分格式; 只需要当月结果"}),
    _Preset(saic_daily_to_monthly_split, ("saic_daily_to_monthly_split", "daily_to_monthly_split"),
            "日需求转月度拆分。按日期合并日需求，生成日度明细 + 月度汇总（适用于 SAIC-KD、SAIC-NON-KD、GAC-PC）",
            {"merge_by_date": True}, "business",
            ("demand",),
            "按日期提取月份 → 同月 qty 求和 → 输出日度明细(type=daily) + 月度汇总(type=monthly)\n某月有日度数据则移除该月月度项",
            "日度明细 + 月度汇总列表",
            {"how_it_works": "1. 遍历 demand 数组, 按日期提取月份\n2. merge_by_date=True 时同日期 qty 求和(默认)\n3. 生成两份输出: type=daily 日度明细 + type=monthly 月度汇总\n4. 某月已有日度数据则移除该月月度项(去重)",
             "input_fields": {"demand": "[{date,qty}] — 日需求数据", "merge_by_date": "bool=True — 是否按日期合并求和"},
             "notes": "适用于 SAIC-KD、SAIC-NON-KD、GAC-PC 等需要日/月双视图的客户"}),
    _Preset(toyota_row_merge_freeze, ("toyota_row_merge_freeze", "gtmc_row_merge_freeze"),
            "Toyota GTMC 行合并 + 冻结汇总。按 Plant+Supplier+PartNo+Date 合并行，同日期数量求和，去重月度数据（适用于广汽丰田 GTMC）",
            {}, "business",
            ("rows",),
            "按 plant+supplier+partNo+date 分组 → 日度 qty 求和 → 同月有日度则移除月度\nrows 中 date 支持 str \"20251115\" 或 datetime.date",
            "日度明细 + 月度汇总（去重后）",
            {"how_it_works": "1. RowMerge: 按 plant+supplier+partNo+date 四字段分组合并\n2. Freeze: 同日 date 的 qty 求和; 同月有日度数据则移除该月月度汇总\n3. date 兼容 str(\"20251115\") 和 datetime.date 两种格式",
             "input_fields": {"rows": "[{plant, supplier_code, part_no, date, qty, monthly_qty}] — 原始行数据"},
             "notes": "适用于广汽丰田 GTMC 的零件级行合并去重场景; monthly_qty 和 qty 可同时存在于同一行"}),
    _Preset(ming_daily_order_blend, ("ming_daily_order_blend",),
            "名辰 Ming 日订单+预测缺口补足。日订单优先，缺口由周/月预测平摊：周度模式缺口放周日，月度模式从最后订单日平摊到月末",
            {"transportation_lt": 2}, "business",
            ("forecast_type", "daily_orders", "forecast"),
            "日订单取订单值 → 周度: gap=forecast−PGI−订单合计, 全放周日 → 月度: forecast 从最后订单日平摊到日历月末\nforecast_type: \"weekly\"|\"monthly\"",
            "每日发货量（订单或平摊值）",
            {"how_it_works": "1. 日订单优先: 有 daily_orders 的日期直接取订单值\n2. 周度模式: gap = forecast − PGI合计 − 订单合计, 全部放到周日\n3. 月度模式: forecast 从最后订单日平摊到日历月最后一天\n4. 如果日订单已覆盖周期末(周日/月末) → 不平摊, 直接用订单",
             "input_fields": {"forecast_type": "\"weekly\" 或 \"monthly\" — 预测周期类型", "forecast": "float — 周度=总需求; 月度=剩余平摊量(已扣PGI/订单)", "daily_orders": "[{date,qty}] — 日订单(非 demand)", "pgi": "[{date,qty}] — 已发货在途", "transportation_lt": "int=2 — 运输提前期"},
             "notes": "与 Geely(模式B)区别: 无库存 Balance 链路、无多月 dict、无 JITCall; 逻辑是纯'订单→缺口→平摊'; 无日订单时返回空列表",
             "doc": "docs/presets/ming_daily_order_blend.md"}),
    _Preset(holt_winters, ("holt_winters",),
            "Holt-Winters 三次指数平滑预测。自动检测趋势和季节性，适用于有明显周期规律的需求预测",
            {"horizon": 7, "seasonal_periods": 7}, "algorithm",
            ("demand",), "Level + Trend + Seasonal 三因子分解 → 外推 horizon 天。数据不足时自动降级", "含趋势和季节分量的 horizon 天预测"),
    _Preset(arima, ("arima",),
            "ARIMA 自动选参预测。基于 AIC 准则自动选择最优参数 (p,d,q)，适用于各种时间序列模式",
            {"horizon": 7, "max_p": 3, "max_d": 2, "max_q": 3}, "algorithm",
            ("demand",), "网格搜索 (p,d,q) 最小化 AIC → 拟合 ARIMA → 外推 horizon 天。数据不足时降级", "horizon 天预测序列"),
]

# name → handler 查找表（含别名），由 _PRESETS 自动生成
_DISPATCH: dict[str, Callable[..., Any]] = {
    alias: preset.handler
    for preset in _PRESETS
    for alias in preset.aliases
}


def run_preset(name: str, record: dict[str, Any]) -> list[dict[str, Any]]:
    """按名称调度预设方法。

    返回字典列表，格式如 [{"date": ..., "qty": ...}, ...]。
    """
    normalized = name.strip().lower().replace("-", "_")
    handler = _DISPATCH.get(normalized)
    if handler is None:
        available = ", ".join(preset.aliases[0] for preset in _PRESETS)
        raise ValueError(f"Unknown preset: {name}. Available: {available}")
    return handler(record)


def get_preset_info() -> list[dict[str, Any]]:
    """返回所有可用预设的元数据。"""
    return [
        {
            "name": preset.aliases[0],
            "aliases": list(preset.aliases),
            "description": preset.description,
            "parameters": preset.params,
            "category": preset.category,
            "trigger_fields": list(preset.trigger_fields),
            "algorithm": preset.algorithm,
            "output": preset.output,
            "card": preset.card,
        }
        for preset in _PRESETS
    ]
