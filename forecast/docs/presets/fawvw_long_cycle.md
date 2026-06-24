# fawvw_long_cycle - FAW-VW 长周期需求预测（模式 C）

## 适用客户
- **一汽大众（FAW-VW）各工厂**：长春、佛山、天津、成都、青岛

> **注意**：FAW-VW（一汽大众）与 FWDY（富维东阳）是两个独立的 client。
> FAW-VW 各工厂使用本预设（模式 C），FWDY 使用 `fwdy_jitcall_priority` 预设（模式 A）。

---

## 业务场景

FAW-VW 工厂的需求来源比模式 A（`jitcall_priority`）更多样，覆盖从即时取货到一年以上的长周期需求：

| 数据源 | 时间范围 | 优先级 | 说明 |
|--------|---------|--------|------|
| 日计划 | ~90 天 | 基础 | 工厂每日生产计划 |
| JITCall | 7-10 天 | 最高 | 客户即时取货指令 |
| 周需求 | 1+ 年 | 中 | 多数据源由调用方合并为 unified `weekly_demand` |
| PGI | 2 周 | 扣减 | 已发货在途量 |

**核心问题**：
- 周需求来自多个独立数据源（调用方已合并）
- 日计划覆盖 ~90 天，远超单周范围，需要按周分组处理
- JITCall 仍然是最高优先级，有 JITCall 的日期必须优先满足

---

## 计算逻辑

### 第一步：合并周需求

```
总周需求 = weekly_demand（调用方已合并）
```

- 两个来源独立发布，但针对同一工厂同一周，需要加总
- 如果只提供了其中一个，另一个默认为 0

### 第二步：按周分组

将日计划（`demand`）按 ISO 周分组。每周独立计算，互不干扰。

### 第三步：每周内应用 JITCall 优先级（同模式 A）

对每一周：

1. **优先级 1 — JITCall**（如果有）
   - 当天有 JITCall 取货订单 → 直接取 JITCall 值

2. **优先级 2 — 日计划**（如果没有 JITCall）
   - 当天有日计划 → 取日计划值

3. **优先级 3 — 周需求余量**
   - 余量 = 总周需求 - 本周 PGI - Σ(已分配的每日发货量)
   - 余量 > 0：均摊到本周内无 JITCall 的日期
   - 余量 ≤ 0：不分配

### 第四步：合并输出

将所有周的日发货量结果按日期顺序合并输出。

---

## 输入参数

| 参数名 | 类型 | 必填 | 说明 | 示例 |
|--------|------|------|------|------|
| `weekly_demand` | 数字 | 是 | 总周需求（调用方已合并多数据源） | 800 |
| `demand` | 日期序列 | 是 | 日计划（~90 天） | `[{"date": "2026-01-05", "qty": 100}, ...]` |
| `jitcall` | 日期序列 | 否 | JITCall 取货订单 | `[{"date": "2026-01-06", "qty": 80}, ...]` |
| `pgi` | 日期序列 | 否 | PGI 在途货物 | `[{"date": "2026-01-05", "qty": 100}, ...]` |
| `transportationLT` | 整数 | 否 | 运输提前量（天），默认 3 天 | 3 |

> *如果未提供 `weekly_demand`，总周需求为 0。

---

## 示例

### 场景：FAW-VW 长春工厂（2 周数据）

**输入数据**：
```json
{
  "weekly_demand": 800,
  "demand": [
    {"date": "2026-01-05", "qty": 100},
    {"date": "2026-01-06", "qty": 100},
    {"date": "2026-01-07", "qty": 100},
    {"date": "2026-01-08", "qty": 100},
    {"date": "2026-01-09", "qty": 100},
    {"date": "2026-01-10", "qty": 100},
    {"date": "2026-01-11", "qty": 100},
    {"date": "2026-01-12", "qty": 120},
    {"date": "2026-01-13", "qty": 120},
    {"date": "2026-01-14", "qty": 120},
    {"date": "2026-01-15", "qty": 120},
    {"date": "2026-01-16", "qty": 120},
    {"date": "2026-01-17", "qty": 120},
    {"date": "2026-01-18", "qty": 120}
  ],
  "jitcall": [
    {"date": "2026-01-06", "qty": 80},
    {"date": "2026-01-07", "qty": 80},
    {"date": "2026-01-13", "qty": 90}
  ],
  "pgi": [
    {"date": "2026-01-05", "qty": 100},
    {"date": "2026-01-12", "qty": 80}
  ],
  "transportationLT": 3
}
```

**计算过程**：

**总周需求** = 800

**第 1 周（1/5 - 1/11）**：
- 周需求 = 800，PGI = 100
- JITCall：1/6=80, 1/7=80
- 日计划：每天 100
- 已分配（D1）：100 + 80 + 80 + 100 + 100 + 100 + 100 = 660
- 余量 = 800 - 100 - 660 = 40
- 非 JITCall 天（5 天）：每天平摊 40/5 = 8
- 日发货量：[108, 80, 80, 108, 108, 108, 108]

**第 2 周（1/12 - 1/18）**：
- 周需求 = 800，PGI = 80
- JITCall：1/13=90
- 日计划：每天 120
- 已分配（D1）：120 + 90 + 120×5 = 810
- 余量 = 800 - 80 - 810 = -90 ≤ 0，不分配
- 日发货量：[120, 90, 120, 120, 120, 120, 120]

---

## 与模式 A（jitcall_priority）的区别

| 对比项 | 模式 A（jitcall_priority） | 模式 C（fawvw_long_cycle） |
|--------|--------------------------|------------------------|
| 周需求 | 单一 `weekly_demand` | 单一 `weekly_demand` |
| 日需求 | `demand`（通常 7 天） | `demand`（日计划，~90 天） |
| 跨周处理 | 按 ISO 周分组，每周独立计算 | 按 ISO 周分组，每周独立计算 |
| week 均分 | 按周数均分 weekly_demand | 不按周数均分，直接使用 |
| PGI | 全周汇总扣减 | 按日期映射到对应周扣减 |
| 适用客户 | FWDY、名辰 | FAW-VW 各工厂 |

---

## 特殊情况处理

### Case 1：周需求为 0

如果未提供 `weekly_demand`（或值为 0）：
- 总周需求 = 0
- 日发货量仅包含 JITCall 和日需求（无余量分配）

### Case 2：日计划跨越多周

系统自动按 ISO 周分组，每周使用总周需求计算余量平摊。
例如：90 天日计划 → 约 13 周 → 每周独立计算。

### Case 4：JITCall 跨周

JITCall 按日期精确匹配到对应周的日计划天，不会跨周干扰。

### Case 5：余量为负

当周需求已被 PGI 和已分配量超额满足（余量 < 0），不分配余量，日发货量仅包含优先级 1 和 2 的结果。

---

## DSL 表达式示例

通过 preset 方式调用：

```
preset_name: fawvw_long_cycle
```

输入数据中需包含 `weekly_demand`、`demand`、`jitcall`、`pgi` 字段。

---

## Python 脚本示例

```python
def forecast(record):
    total_weekly = float(record.get("weekly_demand", 0) or 0)
    
    daily = record.get("demand", [])
    jitcall_raw = record.get("jitcall", [])
    pgi_raw = record.get("pgi", [])
    
    if not daily:
        return []
    
    # 构建 JITCall/PGI 映射
    jitcall_map = {}
    for j in jitcall_raw:
        jitcall_map[j["date"]] = jitcall_map.get(j["date"], 0) + j["qty"]
    
    pgi_map = {}
    for p in pgi_raw:
        pgi_map[p["date"]] = pgi_map.get(p["date"], 0) + p["qty"]
    
    # 按周分组
    from datetime import date
    weeks = {}
    for d in daily:
        dd = date.fromisoformat(d["date"])
        iso_year, iso_week, _ = dd.isocalendar()
        key = (iso_year, iso_week)
        weeks.setdefault(key, []).append({"date": dd, "qty": d["qty"]})
    
    result = []
    for week_key in sorted(weeks.keys()):
        week_days = weeks[week_key]
        week_pgi = sum(pgi_map.get(d["date"].isoformat(), 0) for d in week_days)
        
        # 优先级 1: JITCall > 日计划
        d1 = {}
        for d in week_days:
            dd = d["date"]
            jc = jitcall_map.get(dd.isoformat(), 0)
            d1[dd] = jc if jc > 0 else d["qty"]
        
        # 余量
        remaining = total_weekly - week_pgi - sum(d1.values())
        non_jc = [d["date"] for d in week_days 
                  if jitcall_map.get(d["date"].isoformat(), 0) == 0]
        
        # 平摊
        if remaining > 0 and non_jc:
            spread = remaining / len(non_jc)
            for d in week_days:
                dd = d["date"]
                val = d1[dd]
                if jitcall_map.get(dd.isoformat(), 0) == 0:
                    val += spread
                result.append({"date": dd.isoformat(), "qty": max(0, round(val))})
        else:
            for d in week_days:
                result.append({"date": d["date"].isoformat(), 
                              "qty": max(0, round(d1[d["date"]]))})
    
    return result
```

---

## 常见问题

**Q1：周需求一和周需求二有什么区别？**  
A：多个数据源的周需求在调用 preset 前已由上游合并为 unified `weekly_demand`。

**Q2：日计划覆盖 90 天，周需求怎么对应？**  
A：系统按 ISO 周自动分组，每周使用相同的总周需求值计算。如果每周需求不同，建议分多次调用。

**Q3：和 jitcall_priority 预设有什么本质区别？**  
A：核心算法一致（JITCall 优先级 + 余量平摊），但 fawvw_long_cycle 支持：① 自动按周分组；② PGI 按日期映射到对应周；③ weekly_demand 不按周数均分。

**Q4：如果 JITCall 跨周怎么处理？**  
A：JITCall 按日期精确匹配到日计划中的对应天，归属到该天所在的 ISO 周，不会跨周干扰。

**Q5：运输提前量（transportationLT）在这个预设中如何使用？**  
A：当前版本中 transportationLT 作为元数据传入，核心计算不依赖它。后续可用于过滤"已释放"需求范围。

---

## 数据来源

本预设的逻辑提取自以下 Excel 文件：
- `docs/FWDY.xlsx` — FAW-VW 各工厂工作表（长春、佛山、天津、成都、青岛）

**关键说明**（来自 Excel）：
- "日计划：周三中午 12:00 发布，覆盖约 90 天"
- "JITCall：每日自动下载，7-10 天窗口，最高优先级"
- "周需求：多个数据源汇总为 unified weekly_demand，1+ 年"
