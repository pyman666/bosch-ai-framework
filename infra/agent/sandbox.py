"""Python 沙箱执行器 — AST 验证 + 受限 import + 线程池超时执行.

通用沙箱，供所有 agent 安全执行用户提交的 Python 代码。
每个 agent 通过 ``build_sandbox_helpers()`` 注入领域 helper 函数。

用法::

    from infra.agent.sandbox import SandboxError, prepare_sandbox, execute_sandbox

    helpers = {"my_domain_fn": my_fn, ...}
    fn, _globals = prepare_sandbox(code, helpers=helpers, required_fn="forecast")
    result = execute_sandbox(fn, {"key": "value"}, timeout=30)
"""

from __future__ import annotations

import ast
import builtins
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from functools import lru_cache
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 沙箱策略配置
# ---------------------------------------------------------------------------

ALLOWED_BUILTINS: set[str] = {
    "abs", "all", "any", "bool", "dict", "enumerate", "float", "int",
    "len", "list", "map", "max", "min", "pow", "range", "round", "set",
    "sorted", "str", "sum", "tuple", "zip", "True", "False", "None",
    "print", "isinstance", "type",
}

ALLOWED_MODULES: set[str] = {
    "math", "statistics", "numpy", "pandas", "scipy",
    "sklearn", "statsmodels",
}

BLOCKED_MODULES: set[str] = {
    "os", "subprocess", "sys", "socket", "shutil", "importlib",
    "__builtins__", "builtins", "ctypes", "multiprocessing", "threading",
    "signal", "posix", "nt", "winreg", "msvcrt",
}

BLOCKED_CALL_NAMES: set[str] = {
    "eval", "exec", "open", "compile", "input", "globals", "locals",
    "vars", "dir", "getattr", "setattr", "delattr", "__import__",
    "breakpoint", "help",
}

BLOCKED_NAME_PREFIXES: tuple[str, ...] = ("__",)

# 模块级共享线程池
_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="sandbox")


class SandboxError(Exception):
    """沙箱规则违反时抛出的异常."""
    pass


# ---------------------------------------------------------------------------
# Import 控制
# ---------------------------------------------------------------------------

def _assert_allowed_module(name: str) -> None:
    top_level = name.split(".")[0]
    if top_level in BLOCKED_MODULES:
        raise SandboxError(f"Import of '{name}' is not allowed in sandbox")
    if top_level not in ALLOWED_MODULES:
        raise SandboxError(
            f"Import of '{name}' is not in the allowed modules list: {sorted(ALLOWED_MODULES)}"
        )


def restricted_import(
    name: str,
    globals_dict: dict | None = None,
    locals_dict: dict | None = None,
    fromlist: tuple = (),
    level: int = 0,
) -> Any:
    """沙箱专用的 __import__ 钩子."""
    _assert_allowed_module(name)
    return __import__(name, globals_dict or {}, locals_dict or {}, fromlist, level)


# ---------------------------------------------------------------------------
# AST 静态验证
# ---------------------------------------------------------------------------

def validate_code(code: str) -> None:
    """静态检查 Python 代码，作为执行前的安全门.

    Raises:
        SandboxError: 语法错误或违反沙箱规则.
    """
    try:
        tree = ast.parse(code, filename="<sandbox>", mode="exec")
    except SyntaxError as exc:
        raise SandboxError(
            f"Python syntax error: {exc.msg} at line {exc.lineno}"
        ) from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _assert_allowed_module(alias.name)

        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise SandboxError("Relative imports are not allowed in sandbox")
            if not node.module:
                raise SandboxError("Import without module is not allowed in sandbox")
            _assert_allowed_module(node.module)
            if any(alias.name == "*" for alias in node.names):
                raise SandboxError("Wildcard imports are not allowed in sandbox")

        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BLOCKED_CALL_NAMES:
                raise SandboxError(
                    f"Call to '{func.id}' is not allowed in sandbox"
                )
            if isinstance(func, ast.Attribute) and func.attr.startswith(
                BLOCKED_NAME_PREFIXES
            ):
                raise SandboxError(
                    f"Access to dunder attribute '{func.attr}' is not allowed"
                )

        elif isinstance(node, ast.Attribute):
            if node.attr.startswith(BLOCKED_NAME_PREFIXES):
                raise SandboxError(
                    f"Access to dunder attribute '{node.attr}' is not allowed"
                )

        elif isinstance(node, ast.Name):
            if node.id.startswith(BLOCKED_NAME_PREFIXES):
                raise SandboxError(
                    f"Use of dunder name '{node.id}' is not allowed"
                )


# ---------------------------------------------------------------------------
# 编译 + 执行
# ---------------------------------------------------------------------------

@lru_cache(maxsize=256)
def compile_code(code: str) -> Any:
    """编译 Python 代码对象（LRU 缓存).

    仅负责验证 + compile()，不执行 exec，确保编译结果无状态可复用.
    """
    validate_code(code)
    return compile(code, "<sandbox>", "exec")


def prepare_sandbox(
    code: str,
    *,
    helpers: dict[str, Any] | None = None,
    required_fn: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    """编译并加载代码，返回 (入口函数, 沙箱全局命名空间).

    Args:
        code: Python 源代码.
        helpers: 注入沙箱的领域 helper 函数字典.
        required_fn: 要求代码必须定义的函数名 (None = 不检查).

    Returns:
        (entry_fn, sandbox_globals) — 入口函数引用 + 完整命名空间.

    Raises:
        SandboxError: 编译失败或缺少必需函数.
    """
    compiled = compile_code(code)

    sandbox_globals: dict[str, Any] = {
        "__builtins__": {
            k: v
            for k, v in builtins.__dict__.items()
            if k in ALLOWED_BUILTINS
        },
        "__name__": "__sandbox__",
    }
    sandbox_globals["__builtins__"]["__import__"] = restricted_import

    # 注入领域 helper
    if helpers:
        sandbox_globals.update(helpers)

    exec(compiled, sandbox_globals, sandbox_globals)

    if required_fn and required_fn not in sandbox_globals:
        raise SandboxError(
            f"Code must define a '{required_fn}(record) -> ...' function"
        )

    entry = sandbox_globals.get(required_fn) if required_fn else None
    return entry, sandbox_globals


def execute_sandbox(
    code: str,
    record: dict[str, Any],
    *,
    helpers: dict[str, Any] | None = None,
    required_fn: str = "forecast",
    timeout: int = 30,
) -> Any:
    """在受限沙箱中执行 Python 代码.

    Args:
        code: Python 源代码.
        record: 传入入口函数的输入数据.
        helpers: 领域 helper 函数字典.
        required_fn: 入口函数名.
        timeout: 执行超时秒数.

    Returns:
        入口函数的返回值.

    Raises:
        SandboxError: 编译/验证/超时/运行时错误.
    """
    entry_fn, _ = prepare_sandbox(
        code, helpers=helpers, required_fn=required_fn
    )

    future = _executor.submit(entry_fn, record)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        future.cancel()
        raise SandboxError(f"Sandbox execution timeout: {timeout}s exceeded")
    except SandboxError:
        raise
    except Exception as e:
        raise SandboxError(f"Sandbox execution failed: {e}")
