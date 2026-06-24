# 预设预测方法文档

本目录包含所有预设预测方法的详细说明文档，面向业务用户，无需编程知识。

---

## 预设列表

### 统计类

| 预设名 | 说明 | 适用场景 | 文档 |
|--------|------|----------|------|
| `moving_average` | 简单移动平均 | 需求平稳，无明显趋势 | - |
| `exponential_smoothing` | 指数平滑 | 需求有趋势，无季节性 | - |
| `linear_trend` | 线性趋势外推 | 需求有明显上升/下降趋势 | - |
| `holt_winters` | Holt-Winters 三次指数平滑 | 有明显周期规律（周/月） | [holt_winters.md](holt_winters.md) |
| `arima` | ARIMA 自动选参 | 各种时间序列，通用预测 | [arima.md](arima.md) |

### 供应链类

| 预设名 | 说明 | 适用场景 |
|--------|------|----------|
| `safety_stock_planning` | 安全库存计划 | 需要保持安全库存缓冲 |
| `inventory_optimization` | 库存优化 | 综合考虑需求波动和服务水平 |

### 业务逻辑类（执行排产层）

| 预设名 | 说明 | 适用客户 | 文档 |
|--------|------|----------|------|
| `fwdy_jitcall_priority` | JITCall 优先级预测（模式 A） | 富维东阳 FWDY | [fwdy_jitcall_priority.md](fwdy_jitcall_priority.md) |
| `geely_monthly_daily_blend` | 月预测+日需求整合（模式 B） | 吉利 Geely / 小鹏 Xpeng | [geely_monthly_daily_blend.md](geely_monthly_daily_blend.md) |
| `fawvw_long_cycle` | 长周期需求（模式 C） | 一汽大众 FAW-VW 各工厂 | [fawvw_long_cycle.md](fawvw_long_cycle.md) |
| `gac_ne_monthly_split` | 月预测拆分 + 扣减已交货 | 广汽新能源 GAC-NE | [gac_ne_monthly_split.md](gac_ne_monthly_split.md) |
| `saic_daily_to_monthly_split` | 日需求转月度拆分 | SAIC-KD / SAIC-NON-KD / GAC-PC | [saic_daily_to_monthly_split.md](saic_daily_to_monthly_split.md) |
| `toyota_row_merge_freeze` | 行合并 + 冻结汇总 | 广汽丰田 GTMC | [toyota_row_merge_freeze.md](toyota_row_merge_freeze.md) |
| `ming_daily_order_blend` | 日订单+预测缺口补足 | 名辰 Ming | [ming_daily_order_blend.md](ming_daily_order_blend.md) |

### 业务逻辑类（战略规划层 — 自上而下）

> **注意**：以下两个 CRD 预设属于**战略规划层**，输出为 PN 级别月度需求预测（月度粒度，1年+时间跨度）。
> 其输出可作为上述执行排产层预设的输入（`monthly_forecast` / `weekly_demand` / `forecast`）。
> 目前处于**文档设计阶段**，代码实现待业务 TBD 项确认后启动。

| 预设名 | 说明 | 适用场景 | 文档 |
|--------|------|----------|------|
| `crd_product_topdown` | 按产品自上而下 PN 需求规划（CRD 模式A） | OEM 年销量 × RB 市占率 → PN 月度需求 | [crd_product_topdown.md](crd_product_topdown.md) |
| `crd_vehicle_topdown` | 按车型自上而下 PN 需求规划（CRD 模式B） | 车型月销量 × BOM（Take Rate × Usage）→ PN 月度需求 | [crd_vehicle_topdown.md](crd_vehicle_topdown.md) |

### 基础模型

| 预设名 | 说明 | 核心引擎 |
|--------|------|----------|
| `zero_shot` | 零样本预测（自动选择最佳模型） | Holt-Winters + 自动降级 |
| `chronos` | Chronos 预测（经典时间序列） | ARIMA + 自动降级 |
| `timesfm` | TimesFM 占位（预留外部模型接口） | 线性趋势 fallback |

---

## 如何使用

### 方式 1：通过 Agent 对话

在与 AI Agent 对话时，直接描述你的业务场景，Agent 会自动推荐合适的预设：

```
用户：我是一汽大众的供应商，客户每周发周需求，还有 JITCall 取货订单，怎么算每天的发货量？
Agent：推荐使用 fwdy_jitcall_priority 预设...
```

### 方式 2：直接调用 API

```bash
# 示例：调用 FWDY JITCall 优先级预设
curl -X POST http://localhost:8080/api/v1/forecast/run/preset_fwdy_jitcall_priority \
  -H "Content-Type: application/json" \
  -d @forecast.json

# 示例：调用广汽新能源月预测拆分预设
curl -X POST http://localhost:8080/api/v1/forecast/run/preset_gac_ne_monthly_split \
  -H "Content-Type: application/json" \
  -d @forecast.json
```

### 方式 3：使用 DSL 表达式

```
fwdy_jitcall_priority(800, demand, jitcall, pgi, lt=3)
geely_monthly_daily_blend(demand, 8014, beginningInventory, ins, lt=3)
```

---

## 选择建议

| 你的情况 | 推荐预设 |
|----------|----------|
| 我是富维东阳 FWDY 的供应商 | `fwdy_jitcall_priority` |
| 我是名辰 Ming 的供应商 | `ming_daily_order_blend`（主）/ `fwdy_jitcall_priority`（部分客户群） |
| 我是吉利 Geely / 小鹏 Xpeng 的供应商 | `geely_monthly_daily_blend` |
| 我是一汽大众 FAW-VW 工厂的供应商（多数据源长周期） | `fawvw_long_cycle` |
| 我是广汽新能源 GAC-NE 的供应商 | `gac_ne_monthly_split` |
| 我是上汽 SAIC-KD / SAIC-NON-KD 的供应商 | `saic_daily_to_monthly_split` |
| 我是广汽丰田 GTMC 的供应商 | `toyota_row_merge_freeze` |
| 需求比较平稳，想快速试算 | `moving_average` |
| 需求有趋势，想预测未来 | `linear_trend` |
| 需求有明显周期性（周/月波动） | `holt_winters` |
| 不确定用什么模型，想要自动选参 | `arima` 或 `zero_shot` |
| 需要考虑安全库存 | `safety_stock_planning` |
| 我需要做年度 PN 需求规划（自上而下，从 OEM 销量出发） | `crd_product_topdown` 或 `crd_vehicle_topdown`（文档阶段） |
| 完全不确定 | 让 Agent 帮你分析（会自动运行 `analyze_data_pattern` 推荐） |

---

## 业务逻辑预设速查

### 模式 A：周需求 + JITCall 优先级

**预设**：`fwdy_jitcall_priority`
**客户**：富维东阳 FWDY、名辰 Ming
**输入**：周需求 + 日需求 + JITCall + PGI
**输出**：每日发货量
**逻辑**：JITCall（最高优先级）→ 日订单 → 周需求余量平摊

### 模式 B：月预测 + 日需求整合 + 库存 Balance

**预设**：`geely_monthly_daily_blend`
**客户**：吉利 Geely、小鹏 Xpeng
**输入**：月预测 + 日需求 + 期初库存 + INS/PGI
**输出**：每日净需求（日发货量）
**逻辑**：有日需求取日需求 → 无日需求取月预测平摊 → 库存 Balance → 净需求

### 模式 C：FAW-VW 长周期需求

**预设**：`fawvw_long_cycle`
**客户**：一汽大众 FAW-VW 各工厂
**输入**：周需求 + 日计划 + JITCall + PGI
**输出**：每日发货量（按 ISO 周分组）
**逻辑**：按 ISO 周分组 → 每周独立 JITCall 优先级 + 余量平摊

### 月预测拆分

**预设**：`gac_ne_monthly_split`
**客户**：广汽新能源 GAC-NE（Legacy）
**输入**：6 个月预测数量 + 当月已交货量
**输出**：当月净预测（扣减后）
**逻辑**：currentMonth+1 → 拆 6 月 → 当月扣减已交货 → 只保留当月

### 日需求转月度拆分

**预设**：`saic_daily_to_monthly_split`
**客户**：SAIC-KD、SAIC-NON-KD、GAC-PC
**输入**：日需求明细（demandDate + amount）
**输出**：日度明细 + 月度汇总
**逻辑**：按日期合并（KD）/ 原样保留（NON-KD）→ 按 yyyyMM 汇总

### 行合并 + 冻结汇总

**预设**：`toyota_row_merge_freeze`
**客户**：广汽丰田 GTMC
**输入**：Excel 行数据（plant + supplier + partNo + date + qty）
**输出**：合并冻结后的日度 + 月度数据（有日度则去月度）
**逻辑**：按 Plant+Supplier+PartNo+Date 合并 → 同日期求和 → 去重

### 日订单 + 预测缺口补足

**预设**：`ming_daily_order_blend`
**客户**：名辰 Ming
**输入**：预测总量 + 日订单 + PGI
**输出**：每日发货量
**逻辑**：日订单优先 → 缺口 = 预测 − PGI − 订单 → 周度缺口放周日 / 月度缺口从最后订单日平摊到月末

---

### 战略规划层（自上而下）🆕

> 以下预设处于**文档设计阶段**，代码实现待业务 TBD 项确认。

### CRD 模式A：按产品自上而下 PN 需求规划

**预设**：`crd_product_topdown`
**适用场景**：有 OEM 年销量 + RB 市占率数据，需要做产品级年度需求分解
**输入**：OEM 年销量 + RB 市占率 + New/EOP 项目 + 历史交付 + CRD/CDD + 季节性系数 + SLA 月度需求
**输出**：PN 级别月度需求（含 Running PN + New/EOP PN + 季节性调整版本）
**逻辑**：OEM 年销量 × RB Share → 产品总需求 → 扣减 New/EOP → 按 PN 历史占比分摊 → 季节性调整

### CRD 模式B：按车型自上而下 PN 需求规划

**预设**：`crd_vehicle_topdown`
**适用场景**：有车型月销量 + Car BOM 数据，需要从车型出发做 PN 需求分解
**输入**：车型月销量 + Car BOM（Take Rate + Usage）+ New/EOP 项目 + 历史交付 + CRD/CDD + 季节性系数 + SLA 月度需求
**输出**：PN 级别月度需求（含来源车型追溯 + 季节性调整版本）
**逻辑**：车型月销量 × Take Rate × Usage → BOM 展开到 PN → 按 Vehicle 下 PN 历史占比分摊 → 季节性调整

---

## 名词解释

以下是容易混淆的业务术语，供开发和 AI Agent 参考。

### 主数据 / 维度字段（不参与计算）

| 术语 | 含义 | 说明                                                                               |
|------|------|----------------------------------------------------------------------------------|
| **SDSA** | DALI 主数据中的分组维度 | 和 orgCode、color、plant 一样是标签，不参与数学计算。一个物料可能有多个 SDSA，此时结果可能需要均分。**不要**把它当成计算输出值的名称 |
| **GB** | legalEntity 下的 planning org | 一个 legalEntity 可以有多个 GB，不是 "Global Business"                                     |
| **LE / legalEntity** | 法律实体 | GB 的上一级，一个 LE 可包含多个 GB                                                           |
| **RBAC** | 一个 legalEntity | 其下包含 ME、VM 等 GB                                                                  |
| **RBCC** | 另一个 legalEntity | 与 RBAC 平级                                                                        |
| **ME** | RBAC 下的一个 GB | 不是独立 legalEntity                                                                 |
| **orgCode** | 组织代码 | 纯标签维度，不参与计算                                                                      |

### 平台 / 系统（非业务术语）

| 术语 | 含义 | 说明 |
|------|------|------|
| **SBS** | Side-by-Side，博世的 SAP BTP 平台 | 所有 BPAE 客户（FAW-VW、Geely 等）都部署在上面，跟业务逻辑完全无关。**不要**出现在变量名、preset 名、skill 名中 |
| **DALI** | 博世主数据系统 | 提供 SDSA、GB 等主数据查询 |
| **BTP** | SAP Business Technology Platform | Geely/Xpeng 数据上送走 BTP 批量接口 |

### 客户专属字段（非通用术语）

以下只是特定客户的 API 字段名，**不要**过度解释或当成通用概念：

| 字段 | 所属客户 |
|------|---------|
| `7XM` | FAW-VW 的一个 supplier code |
| `forecast_first_num` ~ `forecast_sixth_num` | GAC-NE 的 6 个月预测字段 |
| `merge_by_date` | SAIC-KD/NON-KD 的合并开关 |
| `rows` (plant/supplier_code/part_no/date) | Toyota GTMC 的行数据格式 |

### 计算输出（通用的结果名称）

预设的返回结果统一用以下名称，**不要**用 SDSA 来命名输出：

| 场景 | 输出名称 |
|------|---------|
| 模式 A：JITCall 优先级 | `qty` / 日发货量 |
| 模式 B：库存 Balance | `qty` / 净需求（日发货量） |
| 模式 C：FAW-VW 长周期 | `qty` / 日发货量 |
| 日订单+缺口补足 | `qty` + `type` 标记（daily_order/spread/empty） |
| 月预测拆分 / 日转月 / 行合并 | `qty` + 可选的 `type` 标记（daily/monthly） |
| CRD 模式A/B（自上而下规划） | `qty` + `pn` + `type`（running/new_eop）+ `version`（base/seasonal_adjusted） |

### CRD 专属术语 🆕

CRD 自上而下规划引入了一批现有预设中没有的概念，不要与执行层预设的术语混淆：

| 术语 | 含义 | 所属预设 |
|------|------|---------|
| **OEM Annual Volume** | 主机厂年度销量目标 | CRD 模式A |
| **RB Share** | 博世在某产品上的市占率 | CRD 模式A |
| **Take Rate** | 某零件在车型上的装配比例（如 80% 的车装天窗） | CRD 模式B |
| **Usage** | 单车用量（每车用几个该零件） | CRD 模式B |
| **Car BOM** | 车辆物料清单（车型→项目→PN 的映射 + Take Rate + Usage） | CRD 模式B |
| **New/EOP Project** | 新增项目（SOP 后量产）/ 停产项目（EOP 后归零） | CRD 模式A/B |
| **Running PN** | 持续量产状态下的 PN（非 New/EOP） | CRD 模式A/B |
| **Seasonal Trend** | 月度季节性需求波动系数 | CRD 模式A/B |
| **PN Share%** | 某 PN 在同 Product/Vehicle 下的历史交付占比 | CRD 模式A/B |
