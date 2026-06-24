"""简易 DSL 解析与求值器，用于预测表达式。

语法（非形式化）：
    expr      = function_call | arithmetic
    function  = name "(" [expr ("," expr)*] ")"
    name      = moving_average | exponential_smoothing | linear_trend
                | seasonal_index | inventory_planning | safety_stock
                | sum | mean | std | min | max | shift | cumsum
                | if_then_else | ...

算术运算支持: +, -, *, /, **, 括号。
变量引用输入数据记录中的字段（如 demand、pgi 等）。
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

TOKEN_RE = re.compile(
    r"""
    \s*(?:
        (?P<number>\d+(?:\.\d*)?|\.\d+)
        |(?P<name>[a-zA-Z_][a-zA-Z_0-9]*)
        |(?P<comma>,)
        |(?P<op>\*\*|==|!=|<=|>=|[+\-*/%()<>=!])
        |(?P<dquote>"[^"]*")
        |(?P<squote>'[^']*')
    )
    """,
    re.VERBOSE,
)


def tokenize(expr: str) -> list[dict[str, str]]:
    tokens = []
    pos = 0
    for m in TOKEN_RE.finditer(expr):
        if m.start() != pos and expr[pos:m.start()].strip():
            raise ValueError(f"Invalid DSL token near: {expr[pos:m.start()]!r}")
        pos = m.end()
        kind = m.lastgroup
        value = m.group().strip()
        if kind == "dquote":
            value = value[1:-1]
            kind = "string"
        elif kind == "squote":
            value = value[1:-1]
            kind = "string"
        if not value:
            continue
        tokens.append({"kind": kind, "value": value})
    if pos != len(expr) and expr[pos:].strip():
        raise ValueError(f"Invalid DSL token near: {expr[pos:]!r}")
    return tokens


@lru_cache(maxsize=256)
def _cached_tokenize(expr: str) -> list[dict[str, str]]:
    """缓存 tokenize 结果，同一表达式在 batch 内只解析一次。"""
    return tokenize(expr)


# ---------------------------------------------------------------------------
# Parser / evaluator (recursive descent)
# ---------------------------------------------------------------------------

MAX_DEPTH = 50


class DSLEval:
    """对数据记录进行 DSL 表达式求值。"""

    def __init__(self, record: dict[str, Any]):
        self.record = record
        self.tokens: list[dict[str, str]] = []
        self.pos = 0
        self._depth = 0

    def _enter(self):
        self._depth += 1
        if self._depth > MAX_DEPTH:
            raise ValueError(f"Expression too deeply nested (max {MAX_DEPTH} levels)")

    def _exit(self):
        self._depth -= 1

    # ---- 公共入口 ----
    def evaluate(self, expr: str) -> Any:
        self.tokens = _cached_tokenize(expr)
        self.pos = 0
        result = self._expr()
        if self.pos < len(self.tokens):
            self._error(f"Unexpected token: {self.tokens[self.pos]['value']}")
        return result

    # ---- 辅助方法 ----
    def _peek(self, offset: int = 0):
        idx = self.pos + offset
        return self.tokens[idx] if idx < len(self.tokens) else None

    def _consume(self, kind: str | None = None):
        tok = self._peek()
        if tok is None:
            self._error("Unexpected end of expression")
        if kind and tok["kind"] != kind:
            self._error(f"Expected {kind}, got {tok['kind']}")
        self.pos += 1
        return tok

    def _consume_op(self, value: str):
        tok = self._consume("op")
        if tok["value"] != value:
            self._error(f"Expected operator {value!r}, got {tok['value']!r}")
        return tok

    def _error(self, msg: str):
        ctx = " ".join(t["value"] for t in self.tokens[max(0, self.pos - 3):self.pos + 3])
        raise ValueError(f"DSL error at pos {self.pos}: {msg} (near: ...{ctx}...)")

    # ---- 语法规则 ----
    def _expr(self):
        """expr = comparison"""
        self._enter()
        try:
            return self._comparison()
        finally:
            self._exit()

    def _comparison(self):
        """comparison = addsub [(<|>|<=|>=|==|!=) addsub]"""
        left = self._addsub()
        tok = self._peek()
        if tok and tok["kind"] == "op" and tok["value"] in (">", "<", ">=", "<=", "==", "!="):
            op = self._consume("op")["value"]
            right = self._addsub()
            ops = {">": lambda a, b: a > b, "<": lambda a, b: a < b,
                   ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
                   "==": lambda a, b: a == b, "!=": lambda a, b: a != b}
            return ops[op](left, right)
        return left

    def _addsub(self):
        """addsub = muldiv (('+'|'-') muldiv)*"""
        left = self._muldiv()
        while self._peek() and self._peek()["kind"] == "op" and self._peek()["value"] in ("+", "-"):
            op = self._consume("op")["value"]
            right = self._muldiv()
            if op == "+":
                left = _binary_op(left, right, lambda a, b: a + b)
            else:
                left = _binary_op(left, right, lambda a, b: a - b)
        return left

    def _muldiv(self):
        """muldiv = power (('*'|'/'|'%') power)*"""
        left = self._power()
        while self._peek() and self._peek()["kind"] == "op" and self._peek()["value"] in ("*/%"):
            op = self._consume("op")["value"]
            right = self._power()
            if op == "*":
                left = _binary_op(left, right, lambda a, b: a * b)
            elif op == "/":
                left = _binary_op(left, right, lambda a, b: 0.0 if b == 0 else a / b)
            elif op == "%":
                left = _binary_op(left, right, lambda a, b: 0.0 if b == 0 else a % b)
        return left

    def _power(self):
        """power = unary ('**' unary)*"""
        left = self._unary()
        while self._peek() and self._peek()["kind"] == "op" and self._peek()["value"] == "**":
            self._consume("op")
            right = self._unary()
            left = _binary_op(left, right, lambda a, b: a ** b)
        return left

    def _unary(self):
        """unary = ('+'|'-') unary | atom"""
        tok = self._peek()
        if tok and tok["kind"] == "op" and tok["value"] in ("+", "-"):
            op = self._consume("op")["value"]
            val = self._unary()
            return -val if op == "-" else val
        return self._atom()

    def _atom(self):
        """atom = number | function_call | name | '(' expr ')'"""
        tok = self._peek()
        if tok is None:
            self._error("Unexpected end of expression")

        if tok["kind"] == "number":
            self._consume()
            val = tok["value"].strip()
            return float(val) if "." in val else int(val)

        if tok["kind"] == "string":
            self._consume()
            return tok["value"]

        if tok["kind"] == "op" and tok["value"] == "(":
            self._consume_op("(")
            val = self._expr()
            self._consume_op(")")
            return val

        if tok["kind"] == "name":
            name = self._consume("name")["value"]
            # Check for function call
            if self._peek() and self._peek()["kind"] == "op" and self._peek()["value"] == "(":
                return self._call(name)
            # Otherwise resolve as variable
            return self._resolve_var(name)

        self._error(f"Unexpected token: {tok['value']}")

    def _call(self, name: str):
        """function_call = name '(' [expr (',' expr)*] ')'"""
        self._consume_op("(")
        args = []
        kwargs: dict[str, Any] = {}
        if not (self._peek() and self._peek()["kind"] == "op" and self._peek()["value"] == ")"):
            self._parse_call_arg(args, kwargs)
            while self._peek() and self._peek()["kind"] == "comma":
                self._consume("comma")
                self._parse_call_arg(args, kwargs)
        self._consume_op(")")
        return _call_function(name, args, self.record, kwargs)

    def _parse_call_arg(self, args: list, kwargs: dict[str, Any]) -> None:
        """解析函数调用中的位置参数或 keyword=value 参数。"""
        tok = self._peek()
        next_tok = self._peek(1)
        if tok and tok["kind"] == "name" and next_tok and next_tok["kind"] == "op" and next_tok["value"] == "=":
            key = self._consume("name")["value"]
            self._consume_op("=")
            if key in kwargs:
                self._error(f"Duplicate keyword argument: {key}")
            kwargs[key] = self._expr()
            return
        if kwargs:
            self._error("Positional arguments cannot follow keyword arguments")
        args.append(self._expr())

    def _resolve_var(self, name: str):
        """根据数据记录解析变量名。"""
        rec = self.record

        # 直接字段访问
        if name in rec:
            val = rec[name]
            if isinstance(val, dict):
                # dict 类型字段（如 monthly_forecast: {"2026-01": 8000}）取合计值
                return sum(float(v) for v in val.values())
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return _extract_qty_series(val)
            return val

        # demand / pgi 作为时间序列
        if name == "demand":
            return _extract_qty_series(rec.get("demand", []))
        if name == "pgi":
            return _extract_qty_series(rec.get("pgi", []))
        if name == "beginningInventory" or name == "beginning_inventory":
            return float(rec.get("beginningInventory", 0))
        if name == "horizon":
            return 30  # default forecast horizon

        self._error(f"Unknown variable: {name}")


# ---------------------------------------------------------------------------
# Built-in functions
# ---------------------------------------------------------------------------

_FUNCTIONS: dict[str, Any] = {}
_FUNCTION_META: dict[str, dict[str, str]] = {}  # name → {args, desc}

_FUNCTION_KWARGS: dict[str, dict[str, int]] = {
    "moving_average": {"series": 0, "window": 1},
    "exponential_smoothing": {"series": 0, "alpha": 1},
    "linear_trend": {"series": 0, "window": 1},
    "seasonal_index": {"series": 0, "period": 1},
    "shift": {"series": 0, "n": 1},
    "round": {"val": 0, "value": 0, "ndigits": 1},
    "safety_stock": {"demand_series": 0, "series": 0, "z": 1, "z_score": 1},
    "inventory_planning": {
        "forecast": 0,
        "demand_forecast": 0,
        "begin_inv": 1,
        "beginning_inventory": 1,
        "beginningInventory": 1,
        "pgi": 2,
        "pgi_series": 2,
    },
    "jitcall_priority": {
        "weekly_demand": 0,
        "daily_demand": 1,
        "jitcall": 2,
        "pgi": 3,
        "lt": 4,
        "transportation_lt": 4,
    },
    "monthly_daily_blend": {
        "daily_demand": 0,
        "monthly_forecast": 1,
        "beginning_inventory": 2,
        "beginningInventory": 2,
        "ins": 3,
        "lt": 4,
        "transportation_lt": 4,
    },
    "balance": {
        "beginning_inventory": 0,
        "beginningInventory": 0,
        "demand": 1,
        "demand_series": 1,
        "supply": 2,
        "supply_series": 2,
    },
    "net_demand": {
        "balance": 0,
        "balance_series": 0,
    },
}


def _register(name: str, args: str = "", desc: str = ""):
    def deco(fn):
        _FUNCTIONS[name] = fn
        if args or desc:
            _FUNCTION_META[name] = {"args": args, "desc": desc}
        return fn
    return deco


def _call_function(name: str, args: list, record: dict, kwargs: dict[str, Any] | None = None) -> Any:
    if name not in _FUNCTIONS:
        raise ValueError(f"Unknown function: {name}. Available: {list(_FUNCTIONS.keys())}")
    if kwargs:
        mapping = _FUNCTION_KWARGS.get(name)
        if mapping is None:
            raise ValueError(f"Function {name} does not accept keyword arguments")
        args = list(args)
        for key, value in kwargs.items():
            if key not in mapping:
                raise ValueError(f"Unknown keyword argument for {name}: {key}")
            idx = mapping[key]
            if idx < len(args):
                raise ValueError(f"Multiple values for argument {key} in {name}")
            while len(args) <= idx:
                args.append(None)
            args[idx] = value
    return _FUNCTIONS[name](args, record)


def _is_number_list(value: Any) -> bool:
    return isinstance(value, (list, tuple))


def _broadcast_pair(left: Any, right: Any) -> tuple[list[float], list[float]]:
    left_is_list = _is_number_list(left)
    right_is_list = _is_number_list(right)

    if not left_is_list and not right_is_list:
        return [float(left)], [float(right)]

    left_vals = _ensure_list(left) if left_is_list else [float(left)]
    right_vals = _ensure_list(right) if right_is_list else [float(right)]
    max_len = max(len(left_vals), len(right_vals))
    if max_len == 0:
        return [], []
    if len(left_vals) == 1 and max_len > 1:
        left_vals = left_vals * max_len
    elif len(left_vals) < max_len:
        left_vals = left_vals + [0.0] * (max_len - len(left_vals))
    if len(right_vals) == 1 and max_len > 1:
        right_vals = right_vals * max_len
    elif len(right_vals) < max_len:
        right_vals = right_vals + [0.0] * (max_len - len(right_vals))
    return left_vals, right_vals


def _binary_op(left: Any, right: Any, fn) -> Any:
    if _is_number_list(left) or _is_number_list(right):
        left_vals, right_vals = _broadcast_pair(left, right)
        return [fn(a, b) for a, b in zip(left_vals, right_vals)]
    return fn(left, right)


def _extract_qty_series(data: list) -> list[float]:
    """从 [{'date': ..., 'qty': ...}, ...] 中提取浮点数列表。"""
    if not data:
        return []
    if isinstance(data[0], dict):
        return [float(d.get("qty", 0.0) or 0.0) if isinstance(d, dict) else float(d) for d in data]
    return [float(x) for x in data]


def _ensure_list(val: Any) -> list[float]:
    """确保值为浮点数列表。"""
    if isinstance(val, (int, float)):
        return [float(val)]
    if isinstance(val, list):
        return [float(x) for x in val]
    raise ValueError(f"Cannot convert {type(val)} to list")


# ---------------------------------------------------------------------------
# Statistical functions
# ---------------------------------------------------------------------------

@_register("moving_average", "series, window=7", "简单移动平均")
def fn_moving_average(args: list, rec: dict) -> list[float]:
    series = _ensure_list(args[0])
    window = int(args[1]) if len(args) > 1 else 7
    result = []
    for i in range(len(series)):
        if i < window - 1:
            # 早期数据点使用部分窗口
            partial = series[:i + 1]
            result.append(sum(partial) / len(partial))
        else:
            result.append(sum(series[i - window + 1:i + 1]) / window)
    return result


@_register("exponential_smoothing", "series, alpha=0.3", "指数平滑")
def fn_exponential_smoothing(args: list, rec: dict) -> list[float]:
    series = _ensure_list(args[0])
    alpha = float(args[1]) if len(args) > 1 else 0.3
    result = [series[0]]
    for i in range(1, len(series)):
        result.append(alpha * series[i] + (1 - alpha) * result[-1])
    return result


@_register("linear_trend", "series, window=30", "线性趋势外推")
def fn_linear_trend(args: list, rec: dict) -> list[float]:
    series = _ensure_list(args[0])
    window = int(args[1]) if len(args) > 1 else min(30, len(series))
    # 在最后 `window` 个数据点上拟合线性趋势并外推
    n = min(window, len(series))
    recent = series[-n:]
    x_mean = (n - 1) / 2.0
    y_mean = sum(recent) / n
    num = sum((i - x_mean) * (recent[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    slope = num / den if den != 0 else 0
    intercept = y_mean - slope * x_mean

    # 向前延伸 n 个数据点
    result = list(series)
    for i in range(n):
        result.append(intercept + slope * (n + i))
    return result


@_register("seasonal_index", "series, period=7", "季节性指数")
def fn_seasonal_index(args: list, rec: dict) -> list[float]:
    series = _ensure_list(args[0])
    period = int(args[1]) if len(args) > 1 else 7
    if len(series) < period:
        return [1.0] * period
    n_periods = len(series) // period
    if n_periods == 0:
        return [1.0] * period
    # 每个位置取平均
    indices = []
    mean = sum(series) / len(series) if series else 1
    for i in range(period):
        pos_vals = [series[i + p * period] for p in range(n_periods)]
        pos_mean = sum(pos_vals) / len(pos_vals)
        indices.append(pos_mean / mean if mean != 0 else 1.0)
    return indices


# ---------------------------------------------------------------------------
# Arithmetic aggregate functions
# ---------------------------------------------------------------------------

@_register("sum", "series", "求和")
def fn_sum(args: list, rec: dict) -> float:
    series = _ensure_list(args[0])
    return sum(series)


@_register("mean", "series", "平均值")
def fn_mean(args: list, rec: dict) -> float:
    series = _ensure_list(args[0])
    return sum(series) / len(series) if series else 0


@_register("std", "series", "标准差")
def fn_std(args: list, rec: dict) -> float:
    series = _ensure_list(args[0])
    if not series:
        return 0
    mu = sum(series) / len(series)
    return (sum((x - mu) ** 2 for x in series) / len(series)) ** 0.5


@_register("min", "series", "最小值")
def fn_min(args: list, rec: dict) -> float:
    series = _ensure_list(args[0])
    return min(series) if series else 0


@_register("max", "series", "最大值")
def fn_max(args: list, rec: dict) -> float:
    series = _ensure_list(args[0])
    return max(series) if series else 0


@_register("shift", "series, n", "平移序列")
def fn_shift(args: list, rec: dict) -> list[float]:
    series = _ensure_list(args[0])
    n = int(args[1]) if len(args) > 1 else 1
    if n >= 0:
        return [0.0] * n + list(series)
    else:
        return list(series[-n:]) + [0.0] * (-n)


@_register("cumsum", "series", "累积和")
def fn_cumsum(args: list, rec: dict) -> list[float]:
    series = _ensure_list(args[0])
    result = []
    total = 0.0
    for x in series:
        total += x
        result.append(total)
    return result


@_register("if_then_else", "cond, true_val, false_val", "条件判断")
def fn_if_then_else(args: list, rec: dict) -> Any:
    condition = args[0] if len(args) > 0 else False
    true_val = args[1] if len(args) > 1 else 0
    false_val = args[2] if len(args) > 2 else 0
    return true_val if condition else false_val


@_register("round", "val, ndigits=0", "四舍五入")
def fn_round(args: list, rec: dict) -> float:
    val = float(args[0]) if len(args) > 0 else 0
    ndigits = int(args[1]) if len(args) > 1 else 0
    return round(val, ndigits)


@_register("abs", "val", "绝对值")
def fn_abs(args: list, rec: dict) -> float:
    return abs(float(args[0])) if args else 0


# ---------------------------------------------------------------------------
# Supply-chain specific functions
# ---------------------------------------------------------------------------

@_register("safety_stock", "demand_series, z=1.65", "安全库存计算")
def fn_safety_stock(args: list, rec: dict) -> float:
    """safety_stock(demand_series, z_score=1.65) -> 安全库存量"""
    series = _ensure_list(args[0])
    z = float(args[1]) if len(args) > 1 else 1.65  # 95% 服务水平
    mu = sum(series) / len(series) if series else 0
    sigma = (sum((x - mu) ** 2 for x in series) / len(series)) ** 0.5 if series else 0
    return z * sigma


@_register("inventory_planning", "forecast, begin_inv, pgi", "库存计划")
def fn_inventory_planning(args: list, rec: dict) -> list[float]:
    """inventory_planning(demand_forecast, beginning_inventory, pgi_series) -> 净发货量"""
    fcast = _ensure_list(args[0])
    begin_inv = float(args[1]) if len(args) > 1 else float(rec.get("beginningInventory", 0))
    pgi = _ensure_list(args[2]) if len(args) > 2 else _extract_qty_series(rec.get("pgi", []))

    result = []
    inv = begin_inv
    max_len = max(len(fcast), len(pgi))
    for i in range(max_len):
        d = fcast[i] if i < len(fcast) else fcast[-1]
        p = pgi[i] if i < len(pgi) else 0
        net = d - inv - p
        result.append(max(0, net))
        inv = max(0, inv + p - d)
    return result


# ---------------------------------------------------------------------------
# Business logic functions — 业务逻辑函数
# ---------------------------------------------------------------------------

@_register("jitcall_priority", "weekly_demand, daily_demand, jitcall, pgi, lt=3", "JITCall 优先级（模式 A：富维东阳 FWDY）")
@_register("fwdy_jitcall_priority")
def fn_jitcall_priority(args: list, rec: dict) -> list[float]:
    """fwdy_jitcall_priority(weekly_demand, daily_demand, jitcall, pgi, lt=3) -> 日发货量序列。

    模式 A：周需求 + JITCall 优先级 + 日订单 → 日发货量（富维东阳 FWDY）。
    优先级：JITCall > 日订单 > 周需求余量平摊。
    """
    weekly = float(args[0]) if len(args) > 0 else float(rec.get("weekly_demand", 0))
    daily_raw = args[1] if len(args) > 1 else _extract_qty_series(rec.get("demand", []))
    jitcall_raw = args[2] if len(args) > 2 else _extract_qty_series(rec.get("jitcall", []))
    # pgi 可能是列表（时间序列）或数字（总量）
    if len(args) > 3:
        pgi_arg = args[3]
        if isinstance(pgi_arg, list):
            pgi_total = sum(float(v) for v in pgi_arg)
        else:
            pgi_total = float(pgi_arg)
    else:
        pgi_total = sum(_extract_qty_series(rec.get("pgi", [])))
    lt = int(args[4]) if len(args) > 4 else int(rec.get("transportation_lt", rec.get("transportationLT", 3)))

    # 简化版：按 daily_raw 长度构建输出
    n = len(daily_raw) if isinstance(daily_raw, list) else 7
    if not isinstance(daily_raw, list) or len(daily_raw) == 0:
        return []

    jitcall_vals = _ensure_list(jitcall_raw) if isinstance(jitcall_raw, (int, float, list)) else [0.0] * n
    if len(jitcall_vals) < n:
        jitcall_vals = jitcall_vals + [0.0] * (n - len(jitcall_vals))
    daily_vals = list(daily_raw) if isinstance(daily_raw, list) else [0.0] * n

    # Step 1: 日需求1（JITCall 优先）
    d1 = [0.0] * n
    for i in range(n):
        jv = jitcall_vals[i] if i < len(jitcall_vals) else 0.0
        dv = daily_vals[i] if i < len(daily_vals) else 0.0
        d1[i] = jv if jv > 0 else dv

    # Step 2: 余量计算
    d1_total = sum(d1)
    remaining = weekly - pgi_total - d1_total

    # Step 3: 确定空缺天数（无 JITCall 的天）
    unfilled = [i for i in range(n) if (jitcall_vals[i] if i < len(jitcall_vals) else 0) == 0]

    # Step 4: 平摊
    result = [0.0] * n
    if remaining > 0 and unfilled:
        spread = remaining / len(unfilled)
        for i in range(n):
            result[i] = d1[i] + (spread if i in unfilled else 0)
    else:
        for i in range(n):
            result[i] = max(0.0, d1[i])

    return [max(0.0, round(v, 2)) for v in result]


@_register("monthly_daily_blend", "daily_demand, monthly_forecast, begin_inv, ins, lt=3", "月预测+日需求整合（模式 B：吉利/小鹏）")
@_register("geely_monthly_daily_blend")
def fn_monthly_daily_blend(args: list, rec: dict) -> list[float]:
    """geely_monthly_daily_blend(daily_demand, monthly_forecast, beginning_inventory, ins, lt=3) -> 净需求序列。

    模式 B：月预测 + 日需求整合 + 库存 Balance → 净需求（日发货量，吉利/小鹏）。
    有日需求取日需求，无日需求取月预测平摊，再扣除期初库存和 INS。
    """
    daily_raw = args[0] if len(args) > 0 else _extract_qty_series(rec.get("demand", []))
    monthly = float(args[1]) if len(args) > 1 else float(rec.get("monthly_forecast", 0))
    begin_inv = float(args[2]) if len(args) > 2 else float(rec.get("beginningInventory", 0))
    ins_raw = args[3] if len(args) > 3 else _extract_qty_series(rec.get("ins", rec.get("pgi", [])))
    lt = int(args[4]) if len(args) > 4 else int(rec.get("transportation_lt", rec.get("transportationLT", 3)))

    if not isinstance(daily_raw, list) or len(daily_raw) == 0:
        return []

    n = len(daily_raw)
    ins_vals = _ensure_list(ins_raw) if isinstance(ins_raw, (int, float, list)) else [0.0] * n
    if len(ins_vals) < n:
        ins_vals = ins_vals + [0.0] * (n - len(ins_vals))

    # 日需求覆盖标记
    demand_has = [True] * n  # 简化：假设所有天都有 date
    demand_vals = list(daily_raw)

    # 月预测平摊
    if monthly > 0:
        daily_avg = monthly / max(n, 1)
    else:
        available = [v for v in demand_vals if v > 0]
        daily_avg = sum(available) / len(available) if available else 0.0

    # 整合需求
    blended = [0.0] * n
    for i in range(n):
        if demand_has[i]:
            blended[i] = demand_vals[i] if i < len(demand_vals) else 0.0
        else:
            blended[i] = daily_avg

    # 库存 Balance
    bal = begin_inv
    result = []
    for i in range(n):
        inflow = ins_vals[i] if i < len(ins_vals) else 0.0
        outflow = blended[i]
        bal = bal + inflow - outflow
        net = max(0.0, -bal) if bal < 0 else 0.0
        result.append(round(net, 2))
        bal = max(0.0, bal)

    return result


@_register("balance", "begin_inv, demand, supply", "库存余额计算")
def fn_balance(args: list, rec: dict) -> list[float]:
    """balance(beginning_inventory, demand_series, supply_series) -> 库存余额序列。

    计算每日库存余额：Balance[i] = Balance[i-1] + supply[i] - demand[i]。
    """
    begin_inv = float(args[0]) if len(args) > 0 else float(rec.get("beginningInventory", 0))
    demand = _ensure_list(args[1]) if len(args) > 1 else _extract_qty_series(rec.get("demand", []))
    supply = _ensure_list(args[2]) if len(args) > 2 else _extract_qty_series(rec.get("pgi", []))

    n = max(len(demand), len(supply))
    bal = begin_inv
    result = []
    for i in range(n):
        d = demand[i] if i < len(demand) else demand[-1] if demand else 0
        s = supply[i] if i < len(supply) else 0
        bal = bal + s - d
        result.append(bal)
    return result


@_register("net_demand", "balance_series", "从库存余额计算净需求")
def fn_net_demand(args: list, rec: dict) -> list[float]:
    """net_demand(balance_series) -> 净需求序列。

    从库存余额序列计算净需求：当 Balance 为负时取绝对值，否则为 0。
    """
    bal = _ensure_list(args[0])
    result = []
    prev = 0.0
    for b in bal:
        if b < 0:
            net = -b
        else:
            net = 0.0
        result.append(net)
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def eval_dsl(expression: str, record: dict[str, Any]) -> Any:
    """对单条数据记录进行 DSL 表达式求值。

    Args:
        expression: DSL 表达式字符串，例如 "moving_average(demand, 7)"
        record: 包含 demand/pgi/beginningInventory 的字典，如 forecast.json 中的一行

    Returns:
        计算结果（float、list[float] 等）。
    """
    evaluator = DSLEval(record)
    return evaluator.evaluate(expression)


def get_available_functions() -> list[dict[str, str]]:
    """返回所有已注册的 DSL 函数及其描述（由 _FUNCTION_META 自动生成）。"""
    return [
        {"name": name, **meta}
        for name, meta in _FUNCTION_META.items()
    ]
