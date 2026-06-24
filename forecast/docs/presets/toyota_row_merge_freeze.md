# toyota_row_merge_freeze - 行合并 + 冻结汇总

## 适用客户
- **广汽丰田（GTMC）**：通过 Excel 上传预测

---

## 业务场景

广汽丰田（GTMC）每月通过 Excel 上传预测数据，每个 Excel 文件可能包含多行相同零件号、工厂、供应商和日期的数据。系统需要：
1. 将相同维度的多行数据合并
2. 汇总同日期/月份的日度和月度需求数量
3. 去重：如果某月有日度数据，则不保留该月的月度汇总数据
4. 最终推送到 SAP

**核心问题**：
- Excel 中可能有多行重复的"工厂+供应商+零件号+日期"数据
- 需要按维度合并，避免向 SAP 发送重复数据
- 日度数据和月度数据可能同时存在，需要去重

---

## 计算逻辑

### 第一步：行合并（RowMerge）

按以下 4 个维度将多行数据合并为一行：

| 维度 | 说明 |
|------|------|
| Plant | 工厂 |
| SupplierCode | 供应商代码（innerSupplierCode + supplierRegion） |
| PartNo | 客户零件号（品番） |
| Date | 日期 |

合并后，每组取第一条作为代理，其余行的日/月数据指向代理。

### 第二步：冻结汇总（Freeze）

将同一行下相同日期/月份的数量求和：

- **日度数据**：按 `yyyyMMdd` 分组求和
- **月度数据**：按 `yyyyMM` 分组求和

### 第三步：去重

如果某月有日度数据，则移除该月的月度数据。

**规则**：
```
如果 exists(日度数据 where month = M)：
    移除月度数据 where month = M
```

---

## 输入参数

| 参数名 | 类型 | 必填 | 说明 | 示例 |
|--------|------|------|------|------|
| `rows` | 数组 | 是 | 原始行数据 `[{plant, supplier_code, part_no, date, qty, monthly_qty}, ...]` | 见下方示例 |

**行数据字段说明**：

| 字段 | 说明 | 示例 |
|------|------|------|
| `plant` | 工厂 | "A" |
| `supplier_code` | 供应商代码 | "S1" |
| `part_no` | 客户零件号 | "P1" |
| `date` | 日期（yyyyMMdd 或 yyyy-MM-dd） | "20251115" |
| `qty` | 日度数量 | 10 |
| `monthly_qty` | 月度数量（可选） | 100 |

---

## 示例

### 场景：GTMC Excel 上传

**输入数据**：
```json
{
  "rows": [
    {"plant": "A", "supplier_code": "S1", "part_no": "P1", "date": "20251115", "qty": 10, "monthly_qty": 100},
    {"plant": "A", "supplier_code": "S1", "part_no": "P1", "date": "20251115", "qty": 20, "monthly_qty": 100},
    {"plant": "A", "supplier_code": "S1", "part_no": "P1", "date": "20251120", "qty": 30},
    {"plant": "B", "supplier_code": "S2", "part_no": "P2", "date": "20251220", "monthly_qty": 200}
  ]
}
```

**计算过程**：

1. **行合并**：前两条数据（相同 plant+supplier+partNo+date）合并
   - 11/15: qty = 10 + 20 = 30, monthly_qty = 100 + 100 = 200

2. **冻结汇总**：
   - 日度：
     - 20251115: 30
     - 20251120: 30
   - 月度：
     - 202511: 200
     - 202512: 200

3. **去重**：
   - 11 月有日度数据（11/15, 11/20）→ 移除 11 月月度数据
   - 12 月只有月度数据 → 保留

**输出**：
```json
[
  {"date": "20251115", "qty": 30, "type": "daily"},
  {"date": "20251120", "qty": 30, "type": "daily"},
  {"date": "202512", "qty": 200, "type": "monthly"}
]
```

---

## GTMC vs FAW（一汽丰田）差异

| 维度 | GTMC | FAW |
|------|------|-----|
| 文件格式 | Excel (.xlsx/.xls) | TXT (GB2312) / PDF (OCR) |
| 行合并 | 按 Plant+Supplier+PartNo+Date 合并 | **不合并**，每行独立 |
| 冻结 | 同日期数量求和 | **不冻结**，直接处理 |
| 供应商代码 | innerSupplierCode + supplierRegion | supplierCode + "-" + supplierRegion |

> 本预设仅实现 GTMC 逻辑。FAW 不需要行合并和冻结。

---

## 特殊情况处理

### Case 1：空输入

如果 `rows` 数组为空，返回空列表。

### Case 2：只有月度数据

如果没有日度数据，所有月度数据都会被保留。

### Case 3：只有日度数据

如果没有月度数据，所有日度数据都会被保留，不触发去重。

### Case 4：同月部分日有日度数据

只要某月有任何一天的日度数据，该月的月度汇总就会被移除。

---

## Excel 列头映射（GTMC）

供参考：Excel 列名到系统字段的映射关系

| Excel 列名 | 系统字段 |
|-----------|---------|
| 对象年月 | date（yyyyMM） |
| 供应商代码 | innerSupplierCode |
| 供应商工区 | supplierRegion |
| 品番 | customerPN |
| 日程别必要数 | 日度 qty（多列，列头为日期） |
| 月计必要数 | monthly_qty |

---

## 常见问题

**Q1：为什么要去重月度数据？**
A：如果某月已有日度明细，SAP 可以通过日度汇总得到月度总量。同时发送日度和月度会导致 SAP 重复计算。

**Q2：FAW 为什么不需要行合并？**
A：FAW 的数据来源（TXT/PDF）已经是按行独立的格式，不存在重复维度。

**Q3：供应商代码为什么要拼接？**
A：GTMC 的供应商由"代码+工区"唯一标识，单独代码可能重复。

---

## 数据来源

本预设的逻辑提取自：
- `docs/bpae/forecast-toyota-calculation.md` — Toyota 计算逻辑文档