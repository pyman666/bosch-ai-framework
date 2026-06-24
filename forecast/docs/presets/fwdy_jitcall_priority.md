# fwdy_jitcall_priority - JITCall 优先级预测（模式 A）

## 适用客户
- **富维东阳（FWDY）**：一汽大众 FAW-VW 的 Tier-1 供应商
- **名辰（Ming）**：MC-AT、JNMP、MC-FZ、MH-BMW、MY-SGM 等客户群

> **注意**：FWDY（富维东阳）与 FAW-VW（一汽大众）是两个独立的 client。
> FWDY 使用本预设（模式 A，`divide_by_weeks=True`），
> FAW-VW 各工厂使用 `fawvw_long_cycle` 预设（模式 C，`divide_by_weeks=False`）。

---

## 业务场景

客户每周发布**周需求总量**，同时提供**每日订单明细**和**JITCall 取货订单**。系统需要按优先级计算每天的发货量。

**核心问题**：
- JITCall 是最高优先级的取货指令，必须优先满足
- 日订单是常规每日需求
- 周需求总量是本周的总目标，需要合理分配到每天

---

## 计算逻辑

### 第一步：确定优先级

每天的发货量按以下优先级确定：

1. **优先级 1 — JITCall**（如果有）
   - 当天有 JITCall 取货订单 → 直接取 JITCall 值
   - JITCall 是客户的即时取货指令，必须优先执行

2. **优先级 2 — 日订单**（如果没有 JITCall）
   - 当天有日订单（即使是 0 也算需求）→ 取日订单值
   - 日订单是客户的常规每日需求

3. **优先级 3 — 周需求余量**（如果既没有 JITCall 也没有日订单）
   - 将周需求的剩余部分平摊到空缺日期

### 第二步：计算余量

```
余量 = 周需求总量 - PGI 在途总量 - Σ(已分配的每日发货量)
```

- **PGI**：已在途的货物，不需要再发货
- **已分配的每日发货量**：按优先级 1 和 2 已经确定的所有天的发货量之和

### 第三步：平摊余量

- 如果**余量 > 0**：将余量平均分配到所有"没有 JITCall 且没有日订单"的日期
- 如果**余量 ≤ 0**：不分配余量（周需求已被满足或超额完成）

### 第四步：输出日发货量

最终日发货量 = 日需求1（优先级 1+2）+ 平摊余量

**最小值为 0**：即使计算结果为负，发货量也不会低于 0。

---

## 输入参数

| 参数名 | 类型 | 必填 | 说明 | 示例 |
|--------|------|------|------|------|
| `weekly_demand` | 数字 | 是 | 本周需求总量 | 800 |
| `demand` | 日期序列 | 是 | 每日订单明细 `[{"date": "2026-01-01", "qty": 100}, ...]` | 见下方示例 |
| `jitcall` | 日期序列 | 否 | JITCall 取货订单明细 | `[{"date": "2026-01-02", "qty": 80}, ...]` |
| `pgi` | 日期序列 | 否 | PGI 在途货物明细 | `[{"date": "2026-01-01", "qty": 100}, ...]` |
| `transportationLT` | 整数 | 否 | 运输提前量（天），用于判断已释放需求范围。默认 3 天 | 3 |

---

## 示例

### 场景：一汽大众长春工厂（本周从周一开始）

**输入数据**：
```json
{
  "weekly_demand": 800,
  "demand": [
    {"date": "2026-01-05", "qty": 100},  // 周一
    {"date": "2026-01-06", "qty": 100},  // 周二
    {"date": "2026-01-07", "qty": 100},  // 周三
    {"date": "2026-01-08", "qty": 100},  // 周四
    {"date": "2026-01-09", "qty": 100},  // 周五
    {"date": "2026-01-10", "qty": 100},  // 周六
    {"date": "2026-01-11", "qty": 100}   // 周日
  ],
  "jitcall": [
    {"date": "2026-01-06", "qty": 80},   // 周二有 JITCall
    {"date": "2026-01-07", "qty": 80},   // 周三有 JITCall
    {"date": "2026-01-08", "qty": 80}    // 周四有 JITCall
  ],
  "pgi": [
    {"date": "2026-01-05", "qty": 100}   // 周一已有 PGI 在途
  ]
}
```

**计算过程**：

1. **优先级 1（JITCall）**：
   - 周二：80（取 JITCall）
   - 周三：80（取 JITCall）
   - 周四：80（取 JITCall）

2. **优先级 2（日订单）**：
   - 周一：100（取日订单）
   - 周五：100（取日订单）
   - 周六：100（取日订单）
   - 周日：100（取日订单）

3. **计算余量**：
   - 已分配总量 = 80 + 80 + 80 + 100 + 100 + 100 + 100 = 640
   - 余量 = 800（周需求）- 100（PGI）- 640（已分配）= 60

4. **平摊余量**：
   - 所有天都有 JITCall 或日订单，没有空缺日期
   - 余量 60 不分配

5. **最终日发货量**：
   ```
   周一：100
   周二：80
   周三：80
   周四：80
   周五：100
   周六：100
   周日：100
   ```

---

## 特殊情况处理

### Case 1：周日无需求

如果周日没有日需求（没有 date 记录），系统会将余量平摊到所有"无需求"的日期。

### Case 2：余量为负

如果周需求已被 PGI 和已分配量超额满足（余量 < 0），则不再分配余量，日发货量仅包含优先级 1 和 2 的结果。

### Case 3：运输提前量

`transportationLT` 参数用于判断"已释放"的需求范围。例如：
- 处理当天 = 2026-01-05（周一）
- transportationLT = 3 天
- 则 2026-01-05 到 2026-01-07（周一到周三）的需求视为"已释放"，优先处理

---

## DSL 表达式示例

```
jitcall_priority(800, demand, jitcall, pgi, lt=3)
```

**参数说明**：
- `800`：周需求总量
- `demand`：日需求序列（自动从输入数据提取）
- `jitcall`：JITCall 序列（自动从输入数据提取）
- `pgi`：PGI 序列（自动从输入数据提取）
- `lt=3`：运输提前量（可选，默认 3 天）

---

## Python 脚本示例

```python
def forecast(record):
    weekly = float(record.get("weekly_demand", 0))
    daily = [float(x.get("qty", 0)) for x in record.get("demand", [])]
    jitcall = [float(x.get("qty", 0)) for x in record.get("jitcall", [])]
    pgi_total = sum(float(x.get("qty", 0)) for x in record.get("pgi", []))
    
    # 优先级 1：JITCall
    d1 = [max(jitcall[i], daily[i]) if jitcall[i] > 0 else daily[i] for i in range(len(daily))]
    
    # 优先级 2：余量
    remaining = weekly - pgi_total - sum(d1)
    
    # 平摊
    unfilled = [i for i in range(len(daily)) if jitcall[i] == 0]
    if remaining > 0 and unfilled:
        spread = remaining / len(unfilled)
        result = [d1[i] + (spread if i in unfilled else 0) for i in range(len(daily))]
    else:
        result = d1
    
    return [max(0, round(v)) for v in result]
```

---

## 常见问题

**Q1：JITCall 和日订单同时存在时，取哪个？**  
A：取 JITCall（优先级更高）。

**Q2：日需求为 0 算不算"有需求"？**  
A：算。只要该日期在 `demand` 数组中存在（有 date），即使 qty=0 也算"有需求"。只有完全缺失该日期（没有 date）才算"无需求"。

**Q3：余量为负怎么办？**  
A：不分配余量，日发货量仅包含按优先级 1 和 2 确定的量。

**Q4：PGI 已经在途，为什么还要减去？**  
A：PGI 是已经发出的货物，不需要再次发货，所以从周需求中扣除。

---

## 数据来源

本预设的逻辑提取自以下 Excel 文件：
- `docs/FWDY.xlsx` — "logic example" 工作表
- `docs/Ming.xlsx` — "Logic sample" 工作表

**关键判断条件**（来自 Excel）：
- "判断日需求当周周日是否有需求，日需求有日期的，0 值也算需求"
- "有 Open JITCall 的，取 Open JITCall"
- "计算 日需求当周总量 - PGI - Open JITCall 的结果"
