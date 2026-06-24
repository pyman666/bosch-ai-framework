# crd_product_topdown — 按产品自上而下 PN 需求规划（CRD 模式A）

## 适用场景

- **按产品维度（Product）进行年度 PN 需求规划**
- 已知 OEM 年度销量目标和博世在各产品上的市占率
- 需要将宏观销量目标逐层分解到具体 PN 的月度需求
- 适用于有明确产品线划分、有历史交付数据的业务场景

> **注意**：本预设解决"全年需要生产多少"的战略规划问题，输出为 PN 级别月度需求预测。
> 与 `fwdy_jitcall_priority` / `geely_monthly_daily_blend` 等执行层预设是上下游关系——
> CRD 的月度预测输出可以作为这些预设的 `monthly_forecast` 或 `weekly_demand` 输入。

---

## 业务场景

汽车零部件 Tier-1 供应商需要从 OEM 主机厂的年度销量目标出发，自顶向下规划每个零件号（PN）的年度需求：

1. OEM 发布年度销量目标（如 Toyota 2026 年在中国销售 200 万辆）
2. 博世根据各产品线的市占率（RB Share）算出博世产品级总需求
3. 扣除新增项目（New Project）和老项目停产（EOP）的影响
4. 根据历史交付数据计算各 Running PN 的占比，将 Running 总需求分摊到 PN
5. 叠加季节性趋势调整
6. 对于新增/停产项目，严格按照 SLA 提供的月度需求及 SOP/EOP 时间规划

**核心问题**：如何从宏观的"OEM 年销量 × 市占率"科学地分解到每个 PN 每个月需要交付多少？

---

## 计算逻辑

### 总体数据流

```
OEM Annual Volume × RB Share
        ↓
  产品总需求 (Output1)
        ↓
  减去 New/EOP Project Demand (Output2)
        ↓
  Running Project Total Demand
        ↓
  按 Running PN 历史占比分摊 + CRD/CDD 时间分布
        ↓
  Running PN Demand Top-down 1 (基础版)
        ↓
  叠加 Future Month Seasonal Trend
        ↓
  Running PN Demand Top-down 2 (季节性调整版)

  ———— 并行线 ————

  SLA Monthly Demand + SOP/EOP Date
        ↓
  New/EOP PN Monthly Demand
        ↓
  叠加 Seasonal Trend
        ↓
  Seasonal Adjusted PN Monthly Demand
```

### 第一步：计算产品总需求

```
产品总需求 = OEM Annual Volume × RB Share per Product
```

- 输入：OEM 年度销量（按 OEM 品牌/车型）、各产品 RB 市占率
- 输出：各 Bosch Product 的年度总需求（Output1）
- **待确认**：Output1 是按年度还是需要先拆分到月度？

### 第二步：计算新增/停产项目需求

```
New/EOP Project Demand = Σ(各 New Project 年度需求) + Σ(各 EOP Project 剩余需求)
```

- 输入：New/EOP Project 清单（来自 Sales / PJM）
- New Project：按 SOP 日期和 SLA 提供的月度需求计算
- EOP Project：按 EOP 日期计算当年仍需要交付的量
- 输出：New/EOP Project Demand（Output2）

### 第三步：计算 Running Project 总需求

```
Running Project Total Demand = 产品总需求(Output1) − New/EOP Project Demand(Output2)
```

- 从产品总需求中扣除新增/停产项目部分，得到持续运行项目的总需求

### 第四步：计算 Running PN Demand（基础版本）

按各 Running PN 的历史交付占比分摊总需求：

```
Running PN Demand[i] = Running Project Total Demand × Share%[i]
```

- **Share%[i]** = 该 PN 历史交付量 / 该 Product 下所有 Running PN 总交付量
- CRD/CDD 的作用（待确认具体规则）：
  - 过滤范围：可能只统计有 CRD（客户订单）的 PN，无订单的不参与分摊
  - 时间分布：按 CRD 要求的交货日期，将年度需求分配到各月
- 输出：Running PN Demand Top-down 1

### 第五步：季节性调整

```
Seasonal Adjusted PN Demand[i][month] = Running PN Demand[i] × Seasonal Coefficient[month]
```

- 在基础需求上叠加未来月份的季节性系数
- 季节性系数由业务部门提供
- 输出：Running PN Demand Top-down 2

### 第六步：New/EOP PN 月度需求

```
New/EOP PN Monthly Demand = SLA Monthly Demand（严格按 SOP/EOP 时间窗口）
```

- 严格按照 SLA 提供的月度需求和 SOP/EOP 时间进行规划
- 不对 SLA 数据做分摊或调整
- 输出：New/EOP PN Monthly Demand

### 第七步：New/EOP PN 季节性调整

```
Seasonal Adjusted = SLA Monthly Demand × Seasonal Coefficient[month]
```

- 在 SLA 月度需求基础上叠加季节性趋势
- 输出：Seasonal Adjusted PN Monthly Demand

---

## 输入参数

| 参数名 | 类型 | 必填 | 说明 | 示例 |
|--------|------|------|------|------|
| `oem_annual_volume` | 数字 | 是 | OEM 主机厂年度销量目标 | 2000000 |
| `rb_share` | 字典 | 是 | 各产品 RB 市占率 `{"Product_A": 0.15, ...}` | 见下方示例 |
| `new_eop_projects` | 数组 | 否 | 新增/停产项目清单 | `[{"project": "P1", "type": "new", "sop_date": "2026-03", ...}]` |
| `actual_delivery` | 数组 | 是 | SAP 历史实际交付数据 `[{"pn": "PN001", "date": "2025-06", "qty": 1200}, ...]` | 见下方示例 |
| `crd_cdd` | 数组 | 否 | 客户要求/确认交货日期 `[{"pn": "PN001", "crd_date": "2026-03", "qty": 800}, ...]` | 用于确定时间分布 |
| `seasonal_trend` | 字典 | 否 | 各月季节性系数 `{"2026-01": 0.85, "2026-02": 0.90, ...}` | 默认为 1.0 |
| `sla_monthly_demand` | 数组 | 条件 | New/EOP 项目的 SLA 月度需求（如有 New/EOP 项目则必填） | `[{"project": "P1", "date": "2026-03", "qty": 5000}]` |
| `pn_matrix` | 数组 | 否 | New/EOP/Running Project PN 分类矩阵 | `[{"pn": "PN001", "project": "Running_Project_A", "status": "running"}]` |

---

## 输出格式

```json
[
  {"pn": "PN001", "date": "2026-01", "qty": 1500, "type": "running", "version": "seasonal_adjusted"},
  {"pn": "PN001", "date": "2026-02", "qty": 1600, "type": "running", "version": "seasonal_adjusted"},
  {"pn": "PN002", "date": "2026-03", "qty": 5000, "type": "new_eop", "version": "base"},
  ...
]
```

- `type`: `"running"` | `"new_eop"`
- `version`: `"base"` | `"seasonal_adjusted"`
- 输出同时包含 Running PN 和 New/EOP PN 两个维度的结果

---

## 示例

### 场景：某产品线的年度需求规划

**输入数据**：
```json
{
  "oem_annual_volume": 2000000,
  "rb_share": {
    "Brake_System": 0.15,
    "Steering_System": 0.08
  },
  "actual_delivery": [
    {"pn": "PN001", "date": "2025-01", "qty": 1200},
    {"pn": "PN001", "date": "2025-02", "qty": 1300},
    {"pn": "PN002", "date": "2025-01", "qty": 800},
    {"pn": "PN002", "date": "2025-02", "qty": 750}
  ],
  "new_eop_projects": [
    {"project": "Brake_Gen3", "type": "new", "sop_date": "2026-04", "monthly_demand": 5000}
  ],
  "seasonal_trend": {
    "2026-01": 0.85, "2026-02": 0.80, "2026-03": 1.00,
    "2026-04": 1.05, "2026-05": 1.10, "2026-06": 1.10,
    "2026-07": 1.05, "2026-08": 1.00, "2026-09": 1.10,
    "2026-10": 1.05, "2026-11": 1.00, "2026-12": 0.95
  }
}
```

**计算过程**：

1. **产品总需求**：
   - Brake_System: 2,000,000 × 0.15 = 300,000
   - Steering_System: 2,000,000 × 0.08 = 160,000

2. **New/EOP 项目需求**：
   - Brake_Gen3（4月 SOP）：5,000 × 9 个月 = 45,000

3. **Running Project 总需求**：
   - Brake_System: 300,000 − 45,000 = 255,000
   - Steering_System: 160,000（无 New/EOP）

4. **PN 历史占比**（以 Brake_System 为例）：
   - PN001 Share = (1200+1300) / (1200+1300+800+750) = 2500/4050 ≈ 61.7%
   - PN002 Share = 1550/4050 ≈ 38.3%

5. **Running PN 基础需求**：
   - PN001: 255,000 × 61.7% ≈ 157,335
   - PN002: 255,000 × 38.3% ≈ 97,665

6. **季节性调整后**（PN001 为例，按月份配比）：
   - 1月: 157,335/12 × 0.85 ≈ 11,144
   - ...

---

## 特殊情况处理

### 新产品无历史数据

New Project 的 PN 没有历史交付数据，不参与 Share% 计算。
SOP 后按 SLA 提供的月度需求执行，待运行一段时间积累足够交付数据后，
再由 PJM/Sales 决定是否转入 Running PN 流程。

### EOP 停产项目

EOP 项目的 PN 在 EOP Date 之后需求归零。
EOP Date 之前的需求根据 SLA 或剩余合同量计算。

### 季节性系数缺失

如果未提供某月的季节性系数，默认为 1.0（不做调整）。

### RB Share 汇总不为 1

RB Share 是按产品线独立给出的，各产品 Share 之和不一定为 1
（因为 OEM 年销量是所有供应商的总量，博世只拿其中的一部分）。
不需要归一化。

---

## 常见问题

**Q1：CRD 和 CDD 在计算中到底怎么用？**
A：这是当前的待确认项。可能的用法包括：① 过滤有实际客户需求的 PN（无 CRD 的不参与分摊）；
② 按 CRD 的交货日期将年度需求分配到各月（作为时间分布的依据）。

**Q2：Running PN 和 New/EOP PN 是并行计算的吗？**
A：从流程看两步是并行的——Running PN 走步骤1-5，New/EOP PN 走步骤6-7。
两者的输出最终合并为完整的 PN 月度需求。New/EOP PN 在 SOP 后是否转入
Running PN 流程也是待确认项。

**Q3：Product Relationship 是做什么的？**
A：可能用于处理跨产品共享 PN 的需求合并场景。例如同一个 PN 供应给多个 Product，
需要按关联关系合并或拆分需求。具体规则待确认。

**Q4：这个预设的输出怎么和现有预设配合使用？**
A：CRD 输出 PN 月度需求预测 → 可作为 `geely_monthly_daily_blend` 的 `monthly_forecast`、
`fwdy_jitcall_priority` 的 `weekly_demand`（需先月转周）、
`ming_daily_order_blend` 的 `forecast` 输入。

---

## 数据来源

本预设的逻辑提取自：
- `docs/raw/CRD.xlsx` — "PN demand Top-down-Plan by Products"
- `docs/raw/CRD.md` — 模式A 详细流程说明

**待确认的数据源**：
- SLA 数据的确切格式和接入方式
- SAP Actual Delivery 的字段映射
- 季节性趋势数据的提供周期和来源部门
