# gac_ne_monthly_split - GAC-NE 月预测拆分

## 适用客户
- **广汽新能源（GAC-NE）**

---

## 业务场景

广汽新能源每月通过 Excel 上传 6 个月的预测数量。系统接收后将预测拆分为月度明细，当月预测会扣减已交货量，最终推送到 SAP。

**核心问题**：
- 客户一次提供 6 个月的预测，需要按月份拆分
- 当月预测需要扣除已经交付的数量（避免重复计算）
- 只推送当月预测到 SAP

---

## 计算逻辑

### 第一步：月份递增

输入的当前月份自动 +1 个月：

```
currentMonth = 202601 → 变为 202602（即下月）
```

### 第二步：按月拆分预测

将 6 个月的预测数量拆分为 6 条月度 Forecast：

| 月份 | 预测数量 |
|------|---------|
| currentMonth + 0（下月） | forecastFirstNum |
| currentMonth + 1 | forecastSecondNum |
| currentMonth + 2 | forecastThirdNum |
| currentMonth + 3 | forecastFourthNum |
| currentMonth + 4 | forecastFifthNum |
| currentMonth + 5 | forecastSixthNum |

### 第三步：当月扣减已交货量

当月（currentMonth）的预测数量扣减已交货量：

```
当月预测 = max(0, forecastFirstNum - deliveryCount)
```

- 如果已交货量超过预测数量，扣减后最低为 0

### 第四步：只保留当月

最终只保留一个月（currentMonth）的预测数据输出，其余月份的预测被移除。

---

## 输入参数

| 参数名 | 类型 | 必填 | 说明 | 示例 |
|--------|------|------|------|------|
| `current_month` | 整数 | 是 | 当前月份，格式 yyyyMM | 202601 |
| `forecast_first_num` ~ `forecast_sixth_num` | 数字 | 是 | 6 个月的预测数量 | 100, 200, 150, 180, 220, 300 |
| `delivery_count` | 数字 | 否 | 当月已交货数量（默认 0） | 300 |

---

## 示例

### 场景：广汽新能源 2025年12月上送

**输入数据**：
```json
{
  "current_month": 202511,
  "forecast_first_num": 1000,
  "forecast_second_num": 1200,
  "forecast_third_num": 1100,
  "forecast_fourth_num": 1300,
  "forecast_fifth_num": 1000,
  "forecast_sixth_num": 1500,
  "delivery_count": 300
}
```

**计算过程**：

1. **月份递增**：currentMonth 202511 → 202512（12月）

2. **拆分 6 个月**：
```
202512: forecastFirstNum  = 1000  ← 当月
202601: forecastSecondNum = 1200
202602: forecastThirdNum  = 1100
202603: forecastFourthNum = 1300
202604: forecastFifthNum  = 1000
202605: forecastSixthNum  = 1500
```

3. **当月扣减**：
   - 202512 月：1000 - 300（已交货）= **700**

4. **输出**：
```json
[{"date": "202512", "qty": 700, "type": "monthly"}]
```

---

## 特殊情况处理

### Case 1：已交货量超过预测

如果当月已交货量 > 预测数量：
- 当月预测 = 0（最多扣到 0，不会为负）

**示例**：`forecast_first_num = 1000`, `delivery_count = 1500` → 输出 `qty = 0`

### Case 2：跨年

如果当月是 12 月（如 202512），下月自动变为次年 1 月（202601）。

---

## 常见问题

**Q1：为什么要 currentMonth +1？**
A：客户上送的是"当前月"，但预测实际针对的是"下个月"的需求，所以系统自动 +1。

**Q2：已交货数据从哪里来？**
A：从 Delivery 表中按 soldTo + shipTo + deliveryPlant + boschPartNo 汇总当月实际交货量。

**Q3：为什么只保留当月？**
A：Legacy 系统只向 SAP 推送当月预测，后续月份用于前端展示参考。

---

## 数据来源

本预设的逻辑提取自：
- `docs/bpae/forecast-gacne-calculation.md` — GAC-NE 计算逻辑文档