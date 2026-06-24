"""预测准确度评估 — MAE / MAPE / RMSE / sMAPE 指标计算。"""

from __future__ import annotations

from datetime import date

from forecast.models.forecast import ForecastAccuracyMetrics, TimeSeriesPoint


def compute_accuracy(
    forecast: list[TimeSeriesPoint],
    actual: list[TimeSeriesPoint],
) -> ForecastAccuracyMetrics:
    """对比预测值与实际值，计算准确度指标。

    匹配规则：按日期对齐，仅计算 forecast 和 actual 都有的日期。
    如果没有任何重叠日期，所有指标返回 ``float('nan')``。

    指标定义：
    - MAE:  Mean Absolute Error
    - MAPE: Mean Absolute Percentage Error (实际值为 0 时跳过该点)
    - RMSE: Root Mean Squared Error
    - sMAPE: Symmetric MAPE (分母为 |forecast| + |actual|)
    """
    # 按日期对齐
    actual_map: dict[date, float] = {p.date: p.qty for p in actual}
    forecast_map: dict[date, float] = {p.date: p.qty for p in forecast}

    common_dates = sorted(actual_map.keys() & forecast_map.keys())
    n = len(common_dates)

    if n == 0:
        return ForecastAccuracyMetrics(
            mae=None,
            mape=None,
            rmse=None,
            smape=None,
            data_points=0,
        )

    abs_errors: list[float] = []
    abs_pct_errors: list[float] = []
    sq_errors: list[float] = []
    sym_pct_errors: list[float] = []

    for d in common_dates:
        f = forecast_map[d]
        a = actual_map[d]
        err = f - a

        abs_errors.append(abs(err))
        sq_errors.append(err * err)

        # MAPE: 跳过实际值为 0 的点（避免除零）
        if a != 0:
            abs_pct_errors.append(abs(err) / abs(a))

        # sMAPE: 分母为 |f| + |a|，两者都为 0 时误差为 0
        denom = abs(f) + abs(a)
        if denom > 0:
            sym_pct_errors.append(abs(err) / denom)
        else:
            sym_pct_errors.append(0.0)

    mae = sum(abs_errors) / n
    rmse = (sum(sq_errors) / n) ** 0.5
    mape = (sum(abs_pct_errors) / len(abs_pct_errors)) * 100 if abs_pct_errors else None
    smape = (sum(sym_pct_errors) / n) * 200  # sMAPE 范围 0~200%

    return ForecastAccuracyMetrics(
        mae=round(mae, 4),
        mape=round(mape, 4) if mape is not None else None,
        rmse=round(rmse, 4),
        smape=round(smape, 4),
        data_points=n,
    )
