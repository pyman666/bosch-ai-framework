# crd_vehicle_topdown — 按车型自上而下 PN 需求规划（CRD 模式B）

## 适用场景

- **按车型维度（Vehicle）进行年度 PN 需求规划**
- 已知各车型未来的平均月销量和车辆 BOM（物料清单）
- 需要从车型销量出发，通过 BOM 展开到具体 PN 的月度需求
- 适用于有完整 BOM 数据、以车型为规划核心的业务场景

> **注意**：本预设与 `crd_product_topdown`（模式A）是并列的两种自上而下规划方法。
> 模式A 从产品市占率出发，模式B 从车型销量 × BOM 出发。
> 输出同样是 PN 级别月度需求预测，可以作为执行层预设的输入。

---

## 业务场景

汽车零部件 Tier-1 供应商在为不同车型供货时，需要从车型的销量预测出发，通过 BOM 逐层展开到每个零件号（PN）的需求：

1. 客户/SLA 提供各车型未来的平均月销量预测
2. 通过 Car BOM（含 Take Rate 装车率和 Usage 单车用量）计算每个 PN 的月度需求
3. 汇总同一 Running Project 下所有车型-PN 组合的需求
4. 根据同一 Vehicle 下各 PN 的历史交付占比分摊
5. 叠加季节性趋势调整
6. 对于新增/停产项目，严格按照 SLA 月度需求及 SOP/EOP 时间规划

**核心问题**：如何从"某车型每月卖多少"科学地推导出"某个 PN 每月需要交付多少"？

---

## 计算逻辑

### 总体数据流

```
Vehicle Future Average Monthly Volume × Car BOM
        ↓
  BOM 展开：车型月销量 × Take Rate × Usage
        ↓
  汇总同 Running Project 下所有车型-PN 需求
        ↓
  Running Project Demand (Output1)
        ↓
  按 Vehicle 下各 PN 历史占比分摊
        ↓
  Running PN Demand Top-down 1 (基础版)
        ↓
  叠加 Future Month Seasonal Trend
        ↓
  Seasonal Adjusted Running PN Demand

  ———— 并行线 ————

  SLA Monthly Demand + SOP/EOP Date
        ↓
  New/EOP PN Monthly Demand
        ↓
  叠加 Seasonal Trend
        ↓
  Seasonal Adjusted PN Monthly Demand
```

### 第一步：计算 Running Project Demand（BOM 展开）

通过 Car BOM 将车型销量展开到 PN 级需求：

```
PN月度需求 = Vehicle Monthly Volume × Take Rate × Usage
```

- **Vehicle Monthly Volume**：SLA 提供的车型未来平均月销量
- **Take Rate（装车率）**：该零件在对应车型上的装配比例。例如某车型 80% 配置天窗，则天窗 PN 的 Take Rate = 0.80
- **Usage（单车用量）**：每辆车使用该零件的数量。例如每车 4 个门把手，则 Usage = 4
- 将同一 Running Project 下所有车型-PN 组合的月度需求汇总，得到 Running Project Demand（Output1）
- CRD/CDD 在此步骤中用于确定 PN 需求的时间分布（按客户要求的交货日期分配到月）

**BOM 展开示例**：

| Vehicle | Monthly Vol | PN | Take Rate | Usage | PN Monthly Demand |
|---------|------------|-----|-----------|-------|-------------------|
| Camry | 15,000 | PN_Brake_001 | 1.00 | 4 | 60,000 |
| Camry | 15,000 | PN_Sunroof_001 | 0.80 | 1 | 12,000 |
| RAV4 | 10,000 | PN_Brake_001 | 1.00 | 4 | 40,000 |
| RAV4 | 10,000 | PN_Sunroof_001 | 0.60 | 1 | 6,000 |

汇总后 PN_Brake_001 月度需求 = 60,000 + 40,000 = 100,000

### 第二步：计算 Running PN Demand（基础版本）

按同一 Vehicle 下各 PN 的历史交付占比，对 Output1 进行分摊：

```
Running PN Demand[i] = Output1 × (该 PN 历史交付量 / 该 Vehicle 下所有 Running PN 总交付量)
```

- 对于有多车型共享的 PN，按各车型的历史占比加权
- 如果某 PN 没有历史数据（新零件），则由 SLA/PJM 提供初始占比估算
- 输出：Running PN Demand Top-down 1

### 第三步：季节性调整

```
Seasonal Adjusted PN Demand[month] = Running PN Demand × Seasonal Coefficient[month]
```

- 对基础预测结果应用季节性趋势修正
- 如果某月无季节性系数，默认为 1.0
- 输出：Seasonal Adjusted Running PN Demand

### 第四步：New/EOP PN 月度需求

```
New/EOP PN Monthly Demand = SLA Monthly Demand（严格按 SOP/EOP 时间窗口）
```

- 不通过 BOM 展开（新车没有完整的 BOM 或历史占比数据）
- 严格按照 SLA 提供的月度需求及 SOP/EOP 时间规划

### 第五步：New/EOP PN 季节性调整

```
Seasonal Adjusted = SLA Monthly Demand × Seasonal Coefficient[month]
```

- 在 SLA 月度需求基础上叠加季节性趋势

---

## 输入参数

| 参数名 | 类型 | 必填 | 说明 | 示例 |
|--------|------|------|------|------|
| `vehicle_monthly_volume` | 字典 | 是 | 各车型未来平均月销量 `{"Camry": 15000, "RAV4": 10000, ...}` | 见下方示例 |
| `car_bom` | 数组 | 是 | 车辆 BOM 数据，包含车型、项目、PN、Take Rate、Usage | `[{"vehicle": "Camry", "project": "Brake", "pn": "PN001", "take_rate": 1.0, "usage": 4}]` |
| `actual_delivery` | 数组 | 是 | SAP 历史实际交付数据 `[{"pn": "PN001", "date": "2025-06", "qty": 1200}, ...]` | 见下方示例 |
| `crd_cdd` | 数组 | 否 | 客户要求/确认交货日期 `[{"pn": "PN001", "crd_date": "2026-03", "qty": 800}, ...]` | 用于确定时间分布 |
| `seasonal_trend` | 字典 | 否 | 各月季节性系数 `{"2026-01": 0.85, ...}` | 默认为 1.0 |
| `new_eop_projects` | 数组 | 否 | 新增/停产项目清单 | `[{"project": "P1", "type": "new", "sop_date": "2026-04", "monthly_demand": 5000}]` |
| `sla_monthly_demand` | 数组 | 条件 | New/EOP 项目的 SLA 月度需求 | `[{"project": "P1", "date": "2026-04", "qty": 5000}]` |
| `pn_matrix` | 数组 | 否 | PN 分类矩阵（Running/New/EOP） | `[{"pn": "PN001", "vehicle": "Camry", "project": "Brake", "status": "running"}]` |

---

## 输出格式

```json
[
  {"pn": "PN_Brake_001", "date": "2026-01", "qty": 100000, "type": "running", "version": "seasonal_adjusted", "vehicles": ["Camry", "RAV4"]},
  {"pn": "PN_Sunroof_001", "date": "2026-01", "qty": 18000, "type": "running", "version": "seasonal_adjusted", "vehicles": ["Camry", "RAV4"]},
  {"pn": "PN_New_Gen4", "date": "2026-04", "qty": 5000, "type": "new_eop", "version": "base"},
  ...
]
```

- `type`: `"running"` | `"new_eop"`
- `version`: `"base"` | `"seasonal_adjusted"`
- `vehicles`: 该 PN 供应的车型列表（模式B 特有，追溯来源车型）

---

## 示例

### 场景：两个车型的年度 PN 需求规划

**输入数据**：
```json
{
  "vehicle_monthly_volume": {
    "Camry": 15000,
    "RAV4": 10000
  },
  "car_bom": [
    {"vehicle": "Camry", "project": "Brake_System", "pn": "PN_Brake_001", "take_rate": 1.0, "usage": 4},
    {"vehicle": "Camry", "project": "Body", "pn": "PN_Sunroof_001", "take_rate": 0.8, "usage": 1},
    {"vehicle": "RAV4", "project": "Brake_System", "pn": "PN_Brake_001", "take_rate": 1.0, "usage": 4},
    {"vehicle": "RAV4", "project": "Body", "pn": "PN_Sunroof_001", "take_rate": 0.6, "usage": 1}
  ],
  "actual_delivery": [
    {"pn": "PN_Brake_001", "date": "2025-01", "qty": 95000},
    {"pn": "PN_Brake_001", "date": "2025-02", "qty": 88000},
    {"pn": "PN_Sunroof_001", "date": "2025-01", "qty": 17000},
    {"pn": "PN_Sunroof_001", "date": "2025-02", "qty": 15500}
  ],
  "seasonal_trend": {
    "2026-01": 0.85, "2026-02": 0.80, "2026-03": 1.00,
    "2026-04": 1.05, "2026-05": 1.10, "2026-06": 1.10
  }
}
```

**计算过程**：

1. **BOM 展开**：
   - PN_Brake_001: Camry(15000×1.0×4) + RAV4(10000×1.0×4) = 60,000 + 40,000 = **100,000/月**
   - PN_Sunroof_001: Camry(15000×0.8×1) + RAV4(10000×0.6×1) = 12,000 + 6,000 = **18,000/月**

2. **历史占比**（同 Vehicle 下 PN 分摊）：
   - 如果 Camry 下只有这两个 PN：PN_Brake_001 占 95000/(95000+17000) ≈ 84.8%
   - PN_Sunroof_001 占 17000/(95000+17000) ≈ 15.2%

3. **Running PN 基础需求**（如果 Vehicle 下有更多 PN，按占比分摊 Output1）

4. **季节性调整**：
   - 1月 PN_Brake_001: 100,000 × 0.85 = **85,000**
   - 2月 PN_Brake_001: 100,000 × 0.80 = **80,000**

**输出**（部分）：
```json
[
  {"pn": "PN_Brake_001", "date": "2026-01", "qty": 85000, "type": "running", "version": "seasonal_adjusted", "vehicles": ["Camry", "RAV4"]},
  {"pn": "PN_Brake_001", "date": "2026-02", "qty": 80000, "type": "running", "version": "seasonal_adjusted", "vehicles": ["Camry", "RAV4"]},
  {"pn": "PN_Sunroof_001", "date": "2026-01", "qty": 15300, "type": "running", "version": "seasonal_adjusted", "vehicles": ["Camry", "RAV4"]}
]
```

---

## 特殊情况处理

### BOM 数据不完整

如果某车型的 BOM 数据不完整（缺少 Take Rate 或 Usage）：
- Take Rate 缺失：默认为 1.0（假设全系标配）
- Usage 缺失：默认为 1（每车 1 个）
- 建议在数据接入层做 BOM 完整性校验，缺失数据提前告警

### 一个 PN 供应多个车型

这是模式B的常见场景（如 PN_Brake_001 同时供应 Camry 和 RAV4）。
系统自动按车型汇总：将同一 PN 在多个车型下的 BOM 展开结果求和。
输出中 `vehicles` 字段会列出所有来源车型，方便追溯。

### 新车没有历史交付数据

新车型的 PN 可能没有历史交付数据，无法计算历史占比：
- 方案1：使用同平台/同级别车型的历史占比作为代理
- 方案2：由 PJM/Sales 提供初始占比估算
- 具体方案待业务确认

### Take Rate < 1 的含义

Take Rate = 0.8 表示该车型中 80% 的车会装配此零件。
例如：天窗不是标配，只有 80% 的 Camry 配置天窗。
这直接影响了该 PN 的月度需求量。

---

## 常见问题

**Q1：模式B 和模式A 的核心区别是什么？**
A：模式A 从"产品市占率"出发（OEM 年销量 × RB Share），模式B 从"车型销量"出发（Vehicle Volume × BOM）。
模式B 需要完整的 BOM 数据，模式A 需要 RB Share 数据。选择哪种取决于客户提供的数据类型。

**Q2：Take Rate 和 Usage 的区别？**
A：Take Rate 是"装不装"（装配比例），Usage 是"装几个"（单车用量）。
例如：天窗 Take Rate=0.8（80%的车装天窗），门把手 Usage=4（每车需要4个）。
两者相乘才得到一个 PN 在单车型上的单车需求系数。

**Q3：BOM 数据从哪里来？**
A：BOM 通常由工程部门/PJM 维护，包含车型-项目-PN 的对应关系、Take Rate 和 Usage。
数据可能来自 SAP、PLM 系统或 Excel 文件。

**Q4：模式B 的输出怎么和现有预设配合？**
A：和模式A 一样，CRD 输出 PN 月度需求 → 可作为执行层预设的月度/周度输入。
模式B 额外具备追溯车型来源的能力（`vehicles` 字段），有助于后续的车型级分析。

**Q5：如果同一个 PN 在不同车型中 Take Rate 不同怎么办？**
A：这正是 BOM 展开要处理的标准场景。每个车型独立计算（车型月销量 × 该车型下的 Take Rate × Usage），
然后汇总。Take Rate 是车型维度的属性，不同车型可以有不同的值。

---

## 数据来源

本预设的逻辑提取自：
- `docs/raw/CRD.xlsx` — "PN demand Top-down-Plan by Vehicle"
- `docs/raw/CRD.md` — 模式B 详细流程说明

**待确认的数据源**：
- Car BOM 的确切格式和字段映射（Take Rate 和 Usage 的数据来源）
- Vehicle → Project → PN 的映射规则是否完全由 BOM 决定
- PN Matrix 在计算中的过滤逻辑
