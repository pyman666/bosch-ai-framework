# Holt-Winters 三次指数平滑预测

## 适用场景

适用于**有明显周期规律**的需求预测，例如：
- 每周/每月的周期性波动（季节性）
- 同时存在上升趋势或下降趋势
- 数据点足够多（至少 14 个以上，最好覆盖 2 个完整周期）

**典型客户**：需求呈现明显的周度或月度模式，且伴随趋势变化。

---

## 核心算法

Holt-Winters 是三次指数平滑方法，可以同时捕捉：
1. **水平（Level）**：需求的基准值
2. **趋势（Trend）**：需求的上升或下降方向
3. **季节性（Seasonality）**：周期性波动模式

### 自动降级策略

算法会根据数据量自动选择最合适的模型：

| 数据点数 | 使用模型 | 说明 |
|---------|---------|------|
| ≥ 2×seasonal_periods | Holt-Winters 完整版 | 带季节性 + 趋势 |
| 4 ~ 13 | Holt 双指数平滑 | 仅趋势，无季节性 |
| < 4 | 简单移动平均 | 数据不足，使用 fallback |

---

## 参数说明

### 必填参数
- `record`: 包含 `demand` 字段的字典，格式为 `[{"date": "...", "qty": ...}, ...]`

### 可选参数
- `horizon`: 预测未来多少天，默认 `7`
- `seasonal_periods`: 季节周期长度，默认 `7`（一周）
  - 如果是月度周期，可设为 `30` 或 `31`
  - 如果是年度周期，可设为 `365`

### 返回值
列表格式：`[{"date": "...", "qty": ...}, ...]`，每个元素的 `qty` 为预测需求量（已四舍五入并保证非负）。

---

## 使用示例

### DSL 表达式
```
holt_winters(demand, horizon=7, seasonal_periods=7)
```

### Python 脚本
```python
def forecast(record):
    from fcst.skills.presets import _holt_winters
    return _holt_winters(record, horizon=7, seasonal_periods=7)
```

### API 调用
```json
{
  "preset_name": "holt_winters",
  "parameters": {
    "horizon": 7,
    "seasonal_periods": 7
  }
}
```

---

## 实际案例

### 案例 1：周度周期 + 上升趋势
```python
record = {
    "demand": [
        {"date": "2026-01-01", "qty": 100},  # 周一
        {"date": "2026-01-02", "qty": 120},  # 周二
        {"date": "2026-01-03", "qty": 150},  # 周三
        {"date": "2026-01-04", "qty": 180},  # 周四（峰值）
        {"date": "2026-01-05", "qty": 160},  # 周五
        {"date": "2026-01-06", "qty": 130},  # 周六
        {"date": "2026-01-07", "qty": 110},  # 周日
        # ... 更多周期数据
    ]
}
```
算法会自动学习周四为峰值的模式，并预测未来 7 天的需求。

### 案例 2：月度周期 + 下降趋势
```python
record = {
    "demand": [
        {"date": "2026-01-01", "qty": 300},  # 月初高
        {"date": "2026-01-15", "qty": 200},  # 月中
        {"date": "2026-01-31", "qty": 100},  # 月末低
        # ... 更多月度数据
    ]
}
# 设置 seasonal_periods=30
result = _holt_winters(record, horizon=30, seasonal_periods=30)
```

---

## 数据要求

### 最低要求
- 至少 **4 个数据点**（会使用 Holt 双指数平滑）
- 数据按时间顺序排列
- `qty` 必须为非负数值

### 推荐要求
- 至少 **2×seasonal_periods 个数据点**（例如周度周期需要 14+ 个点）
- 覆盖至少 2 个完整周期
- 数据无大量缺失值

### 数据质量影响
- **缺失值**：算法会自动填充为 0，可能影响预测准确性
- **异常值**：单个异常点会被平滑，但多个异常点会影响模型拟合
- **零值**：允许存在零值，算法会将负值自动修正为 0.01

---

## 性能说明

- **拟合速度**：通常 < 1 秒（取决于数据量）
- **内存占用**：低（仅存储历史数据和拟合参数）
- **适用数据量**：4 ~ 1000 个数据点

### 计算复杂度
- Holt-Winters：O(n × iterations)
- Holt（仅趋势）：O(n × iterations)
- 移动平均 fallback：O(n)

---

## 常见问题

### Q1: 如何选择合适的 seasonal_periods？
- 观察数据的周期性：
  - 每天数据 → 周度周期设为 `7`
  - 每天数据 → 月度周期设为 `30`
  - 每周数据 → 季度周期设为 `13`
- 如果不确定，可以用 `analyze_data_pattern` 工具检测

### Q2: 预测结果出现负数怎么办？
算法会自动将负值修正为 0，但如果历史数据中有大量零值，预测可能会偏低。建议检查数据质量。

### Q3: 为什么数据量不足时会降级？
Holt-Winters 需要足够的数据来估计季节性参数。数据不足时：
- < 2×seasonal_periods：无法可靠估计季节性 → 降级为 Holt
- < 4：无法可靠估计趋势 → 降级为移动平均

### Q4: 如何处理非等间隔数据？
算法假设数据按天连续排列。如果数据有缺失日期，建议先用 `moving_average` 填充或使用 `arima`。

### Q5: 与 ARIMA 相比如何选择？
- **Holt-Winters**：适合有明显季节性 + 趋势的数据
- **ARIMA**：适合纯趋势或复杂自相关模式
- 可以用 `analyze_data_pattern` 检测季节性强度，自动推荐

---

## 技术实现

- 底层使用 `statsmodels.tsa.holtwinters.ExponentialSmoothing`
- 趋势模式：加法模型（`trend="add"`）
- 季节性模式：加法模型（`seasonal="add"`）
- 初始化方法：`estimated`（自动估计初始参数）
- 优化：`optimized=True, use_brute=True`（全局搜索最优参数）

---

## 相关预设

- `arima`：ARIMA 自动选参，适合无季节性但有趋势的数据
- `zero_shot`：综合模型，会自动选择 Holt-Winters 或 ARIMA
- `moving_average`：简单移动平均，适合平稳数据
- `linear_trend`：线性趋势外推，适合单调趋势

---

## 版本历史

- **v1.0** (2026-06-02): 初始版本，支持 Holt-Winters 和自动降级
