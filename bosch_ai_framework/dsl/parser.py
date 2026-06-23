"""DSL parser and evaluator — recursive-descent expression engine.

Grammar (informal):
    expr      = comparison
    comparison = addsub [(<|>|<=|>=|==|!=) addsub]
    addsub    = muldiv (('+'|'-') muldiv)*
    muldiv    = power (('*'|'/'|'%') power)*
    power     = unary ('**' unary)*
    unary     = ('+'|'-') unary | atom
    atom      = number | string | function_call | variable | '(' expr ')'
    function  = name '(' [expr (',' expr)*] ')'

Supports: arithmetic, comparisons, function calls with positional and keyword args.
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
    """Tokenize a DSL expression string."""
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
    return tokenize(expr)


# ---------------------------------------------------------------------------
# Evaluator (recursive descent)
# ---------------------------------------------------------------------------

MAX_DEPTH = 50


class DSLEvaluator:
    """Recursive-descent DSL expression evaluator.

    Usage::

        evaluator = DSLEvaluator(context={"demand": [1,2,3], "horizon": 30})
        result = evaluator.evaluate("moving_average(demand, 7)")

    The evaluator resolves variables from ``context`` and dispatches function
    calls through a shared registry (see ``DSLRegistry``).
    """

    def __init__(self, context: dict[str, Any]):
        self.context = context
        self.tokens: list[dict[str, str]] = []
        self.pos = 0
        self._depth = 0

    def _enter(self):
        self._depth += 1
        if self._depth > MAX_DEPTH:
            raise ValueError(f"Expression too deeply nested (max {MAX_DEPTH})")

    def _exit(self):
        self._depth -= 1

    # -- public entry point --

    def evaluate(self, expr: str) -> Any:
        self.tokens = _cached_tokenize(expr)
        self.pos = 0
        result = self._expr()
        if self.pos < len(self.tokens):
            self._error(f"Unexpected token: {self.tokens[self.pos]['value']}")
        return result

    # -- helpers --

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

    # -- grammar rules --

    def _expr(self):
        self._enter()
        try:
            return self._comparison()
        finally:
            self._exit()

    def _comparison(self):
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
        left = self._power()
        while self._peek() and self._peek()["kind"] == "op" and self._peek()["value"] in "*/%":
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
        left = self._unary()
        while self._peek() and self._peek()["kind"] == "op" and self._peek()["value"] == "**":
            self._consume("op")
            right = self._unary()
            left = _binary_op(left, right, lambda a, b: a ** b)
        return left

    def _unary(self):
        tok = self._peek()
        if tok and tok["kind"] == "op" and tok["value"] in ("+", "-"):
            op = self._consume("op")["value"]
            val = self._unary()
            return -val if op == "-" else val
        return self._atom()

    def _atom(self):
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
            if self._peek() and self._peek()["kind"] == "op" and self._peek()["value"] == "(":
                return self._call(name)
            return self._resolve_var(name)

        self._error(f"Unexpected token: {tok['value']}")

    def _call(self, name: str):
        self._consume_op("(")
        args = []
        kwargs: dict[str, Any] = {}
        if not (self._peek() and self._peek()["kind"] == "op" and self._peek()["value"] == ")"):
            self._parse_call_arg(args, kwargs)
            while self._peek() and self._peek()["kind"] == "comma":
                self._consume("comma")
                self._parse_call_arg(args, kwargs)
        self._consume_op(")")
        return _dispatch_call(name, args, self.context, kwargs)

    def _parse_call_arg(self, args: list, kwargs: dict[str, Any]) -> None:
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
        if name in self.context:
            return self.context[name]
        raise self._error(f"Unknown variable: {name}")


# ---------------------------------------------------------------------------
# Function dispatch
# ---------------------------------------------------------------------------

_FUNCTIONS: dict[str, Any] = {}
_FUNCTION_META: dict[str, dict[str, str]] = {}
_FUNCTION_KWARGS: dict[str, dict[str, int]] = {}


def register_function(name: str, fn, *, args: str = "", desc: str = "", kwargs_map: dict[str, int] | None = None):
    """Register a function callable from DSL expressions.

    Args:
        name: Function name in DSL (e.g. "moving_average").
        fn: Python callable ``fn(args: list, context: dict) -> Any``.
        args: Human-readable argument signature (e.g. "series, window=7").
        desc: Human-readable description.
        kwargs_map: Maps keyword argument names to positional indices.
    """
    _FUNCTIONS[name] = fn
    if args or desc:
        _FUNCTION_META[name] = {"args": args, "desc": desc}
    if kwargs_map:
        _FUNCTION_KWARGS[name] = kwargs_map


def _dispatch_call(name: str, args: list, context: dict, kwargs: dict[str, Any] | None = None) -> Any:
    if name not in _FUNCTIONS:
        raise ValueError(f"Unknown function: {name}. Available: {list(_FUNCTIONS.keys())}")
    if kwargs:
        mapping = _FUNCTION_KWARGS.get(name)
        if mapping is None:
            raise ValueError(f"Function '{name}' does not accept keyword arguments")
        args = list(args)
        for key, value in kwargs.items():
            if key not in mapping:
                raise ValueError(f"Unknown keyword argument for '{name}': {key}")
            idx = mapping[key]
            if idx < len(args):
                raise ValueError(f"Multiple values for argument '{key}' in '{name}'")
            while len(args) <= idx:
                args.append(None)
            args[idx] = value
    return _FUNCTIONS[name](args, context)


def get_registered_functions() -> list[dict[str, str]]:
    """Return metadata for all registered DSL functions."""
    return [{"name": name, **meta} for name, meta in _FUNCTION_META.items()]


# ---------------------------------------------------------------------------
# Arithmetic helpers
# ---------------------------------------------------------------------------

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


def _ensure_list(val: Any) -> list[float]:
    if isinstance(val, (int, float)):
        return [float(val)]
    if isinstance(val, list):
        return [float(x) for x in val]
    raise ValueError(f"Cannot convert {type(val)} to list")
