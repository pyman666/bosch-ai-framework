# ming_daily_order_blend - 日订单+预测缺口补足预测

名辰 Ming 的每日发货量预测。日订单优先，预测缺口由周/月预测补足。

## 调用方式

```python
run_preset("ming_daily_order_blend", record)
```

### record 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `forecast_type` | `"weekly"` / `"monthly"` | 预测周期类型 |
| `forecast` | `float` | 预测总量（周度=总周需求，月度=剩余平摊量） |
| `daily_orders` | `list[dict]` | 日订单，`[{"date": "2025-12-11", "qty": 100}]` |
| `pgi` | `list[dict]` | 已发货在途量 |
| `transportation_lt` | `int` | 运输提前期（天），仅记录用 |

### 返回值

```python
[{"date": "2025-12-08", "qty": 0.0, "type": "empty"}, ...]
```

## 算法逻辑

1. **日订单优先**：有日订单的日期取日订单值
2. **缺口计算**：
   - 周度：`gap = forecast − PGI合计 − 订单合计`
   - 月度：`spread = forecast`（月度 forecast 已是净剩余）
3. **缺口平摊**：
   - 周度：缺口全部放到周日（Case 2）
   - 月度：从最后订单日平摊到日历月最后一天（Case 6）
4. 如果日订单已覆盖到周期末（周日/月末），不平摊直接取订单

## 场景说明

### 周度模式 (forecast_type="weekly")

| 场景 | 条件 | 行为 |
|------|------|------|
| Case 1 | 周日有日订单 | 整个周期直接用日订单 |
| Case 2 | 周日无日订单 | 缺口全放到周日 |

**Case 1 示例**：周预测 800，日订单 Thu-Sun 各 100，PGI Mon-Wed 各 100
→ 日发货量: Mon-Wed=0, Thu-Sun=100

**Case 2 示例**：周预测 800，日订单 Thu-Sat 各 100，PGI Mon-Wed 各 100
→ 日发货量: Mon-Wed=0, Thu-Sat=100, Sun=**200**（缺口）

### 月度模式 (forecast_type="monthly")

| 场景 | 条件 | 行为 |
|------|------|------|
| Case 5 | 日订单覆盖到周期末 | 直接用日订单 |
| Case 6 | 日订单未覆盖 | forecast 从最后订单日平摊到月末 |

**Case 6 示例**：月度 forecast=800，日订单 Thu-Sat 各 100，PGI Mon-Wed 各 100
→ Dec 11-12: order(100), Dec 13-31: spread(800/19≈42.11)

## FAQ

**Q：周度和月度模式的核心区别？**
A：周度模式的 forecast 是总需求，会扣 PGI 和订单得到 gap；月度模式的 forecast 是已净化的剩余量，直接平摊。

**Q：与 Geely monthly_daily_blend 有什么区别？**
A：Ming 没有库存 Balance 计算、没有 JITCall、没有多月 dict。Ming 的逻辑更简单：订单 → 缺口 → 平摊。

**Q：与 FWDY jitcall_priority 有什么区别？**
A：FWDY 有 JITCall 替换机制 + 整周 spread，但无库存 Balance 链路。Ming 是纯订单优先 + 简单缺口补齐。

## 数据来源

本预设的逻辑提取自以下 Excel 文件：
- `docs/raw/Ming.xlsx` — "Logic sample" 工作表
