# saic_daily_to_monthly_split - 日需求转月度拆分

## 适用客户
- **SAIC-KD（上汽 KD）**：整车散件 KD 业务
- **SAIC-NON-KD（上汽非 KD）**：非 KD 业务
- **GAC-PC（广汽传祺）**：广汽传祺业务

---

## 业务场景

客户通过 JSON API 上送每日需求明细（demandDate + amount），系统需要：
1. 将日需求按日期汇总
2. 同时生成日度明细和月度汇总
3. 最终推送到 SAP

**核心问题**：
- SAIC-KD：同一天可能有多条需求记录，需要按日期合并求和
- SAIC-NON-KD：需求逐条独立，不需要合并
- 两种客户都需要日度和月度两个维度的汇总

---

## 计算逻辑

### SAIC-KD 模式（`merge_by_date = true`，默认）

#### 第一步：按日期合并（mergeCountByDate）

对同一 `demandDate` 的多条需求，按日期分组求和：

```
输入：[{amount:10, date:"2026-01-15"}, {amount:5, date:"2026-01-15"}]
输出：[{amount:15, date:"2026-01-15"}]
```

#### 第二步：生成日度明细

每条合并后的数据生成一条日度明细，日期格式 `yyyyMMdd`：
```
{date: "20260115", count: 15}
```

#### 第三步：按月汇总

按 `yyyyMM` 分组求和，生成月度明细：
```
{date: "202601", count: 30}   // 1月所有日期求和
```

### SAIC-NON-KD 模式（`merge_by_date = false`）

- 不合并同日期数据，保留每条原始日度明细
- 同时仍生成月度汇总

---

## 输入参数

| 参数名 | 类型 | 必填 | 说明 | 示例 |
|--------|------|------|------|------|
| `demand` | 日期序列 | 是 | 每日需求明细 `[{"date": "2026-01-15", "qty": 10}, ...]` | 见下方示例 |
| `merge_by_date` | 布尔 | 否 | 是否按日期合并（默认 true）。SAIC-KD=true, SAIC-NON-KD=false | true |

---

## 示例

### SAIC-KD 场景（合并模式）

**输入数据**：
```json
{
  "demand": [
    {"date": "2025-11-15", "qty": 10},
    {"date": "2025-11-15", "qty": 20},
    {"date": "2025-11-20", "qty": 30},
    {"date": "2025-12-10", "qty": 50}
  ],
  "merge_by_date": true
}
```

**计算过程**：

1. **按日期合并**：11/15 两条合并为 30
2. **日度明细**：
```
20251115: 30
20251120: 30
20251210: 50
```
3. **月度明细**：
```
202511: 60（30+30）
202512: 50
```

### SAIC-NON-KD 场景（不合并模式）

**输入数据**：
```json
{
  "demand": [
    {"date": "2025-11-15", "qty": 10},
    {"date": "2025-11-15", "qty": 20}
  ],
  "merge_by_date": false
}
```

**输出**：日度明细保留 2 条（不合并），月度汇总为 30。

---

## SAIC-KD vs SAIC-NON-KD 差异

| 维度 | SAIC-KD | SAIC-NON-KD |
|------|---------|-------------|
| 合并模式 | 按日期合并求和 | 不合并，保留原始 |
| Excel 上传列 | 发运工厂、客户号、零件号、供应商编号、自制件 | 工厂、预测版本号、客户零件号、成品编码、成品名称 |
| SubFileId 前缀 | 2001（`SAIC{yyMMdd}{serial}`） | 1001 |
| 表名 | FORECAST_SAIC_KD_ORIGINAL_DATA | FORECAST_SAIC_NON_KD_ORIGINAL_DATA |

---

## 特殊情况处理

### Case 1：空需求

如果 `demand` 数组为空，返回空列表。

### Case 2：同日期多条

- SAIC-KD：自动合并
- SAIC-NON-KD：保留原样

---

## 常见问题

**Q1：为什么 SAIC-KD 要按日期合并而 SAIC-NON-KD 不需要？**
A：SAIC-KD 的业务特点是同一天可能有多个供应商对同一个零件号的需求，需要汇总后统一推送。SAIC-NON-KD 则每条独立处理。

**Q2：日度和月度可以同时输出吗？**
A：可以。输出结果中每条记录带 `type` 字段（`"daily"` 或 `"monthly"`），方便区分。

**Q3：GAC-PC 用哪种模式？**
A：GAC-PC 按 Key（factoryName + componentNo + partDesc + receiveDate）合并，与 SAIC-KD 的按日期合并类似，使用默认的 `merge_by_date = true` 模式。

---

## 数据来源

本预设的逻辑提取自：
- `docs/bpae/forecast-saickd-calculation.md` — SAIC-KD 计算逻辑文档
- `docs/bpae/forecast-saicnonkd-calculation.md` — SAIC-NON-KD 计算逻辑文档