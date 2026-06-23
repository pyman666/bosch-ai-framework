"""Python sandbox — AST-validated code execution with timeout.

Provides a secure execution environment for user-submitted Python code:
- AST-level validation (blocked calls, blocked imports, dunder access)
- Restricted ``__builtins__`` and ``__import__``
- Thread-pool execution with configurable timeout

Usage::

    from bosch_ai_framework.sandbox import Sandbox

    sandbox = Sandbox(allowed_modules=["numpy"], timeout=30)
    sandbox.validate("result = sum(data)")
    output = sandbox.execute("result = sum(data)", context={"data": [1,2,3]})

Extracted from: bosch-forecast
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
# Default security rules
# ---------------------------------------------------------------------------

DEFAULT_ALLOWED_BUILTINS = {
    "abs", "all", "any", "bool", "dict", "enumerate", "float", "int",
    "len", "list", "map", "max", "min", "pow", "range", "round", "set",
    "sorted", "str", "sum", "tuple", "zip", "True", "False", "None",
    "print", "isinstance", "type",
}

DEFAULT_ALLOWED_MODULES = {
    "math", "statistics", "numpy", "pandas", "scipy",
    "sklearn", "statsmodels",
}

DEFAULT_BLOCKED_MODULES = {
    "os", "subprocess", "sys", "socket", "shutil", "importlib",
    "__builtins__", "builtins", "ctypes", "multiprocessing", "threading",
    "signal", "posix", "nt", "winreg", "msvcrt",
}

DEFAULT_BLOCKED_CALLS = {
    "eval", "exec", "open", "compile", "input", "globals", "locals",
    "vars", "dir", "getattr", "setattr", "delattr", "__import__",
    "breakpoint", "help",
}

BLOCKED_NAME_PREFIXES = ("__",)


class SandboxError(Exception):
    """Raised when a sandbox rule is violated."""
    pass


def restricted_import_factory(allowed_modules: set[str], blocked_modules: set[str]):
    """Build a restricted ``__import__`` function for the sandbox."""

    def _restricted_import(name: str, globals_dict: dict, locals_dict: dict, fromlist: tuple, level: int):
        top_level = name.split(".")[0]
        if top_level in blocked_modules:
            raise SandboxError(f"Import of '{name}' is not allowed")
        if top_level not in allowed_modules:
            raise SandboxError(
                f"Import of '{name}' is not in allowed modules: {sorted(allowed_modules)}"
            )
        return __import__(name, globals_dict, locals_dict, fromlist, level)

    return _restricted_import


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------

class Sandbox:
    """AST-validated Python code execution sandbox.

    Usage::

        sandbox = Sandbox(allowed_modules=["numpy", "pandas"], timeout=30)

        # Validate code (no execution)
        sandbox.validate(code_string)

        # Execute
        result = sandbox.execute(code_string, context={"df": dataframe})
    """

    def __init__(
        self,
        *,
        allowed_modules: set[str] | None = None,
        blocked_modules: set[str] | None = None,
        allowed_builtins: set[str] | None = None,
        blocked_calls: set[str] | None = None,
        timeout: int = 30,
        max_workers: int = 8,
    ) -> None:
        self.allowed_modules = allowed_modules or DEFAULT_ALLOWED_MODULES
        self.blocked_modules = blocked_modules or DEFAULT_BLOCKED_MODULES
        self.allowed_builtins = allowed_builtins or DEFAULT_ALLOWED_BUILTINS
        self.blocked_calls = blocked_calls or DEFAULT_BLOCKED_CALLS
        self.timeout = timeout

        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="sandbox")
        self._restricted_import = restricted_import_factory(
            self.allowed_modules, self.blocked_modules,
        )

    # -- validation ----------------------------------------------------------

    def validate(self, code: str) -> None:
        """Static AST validation (no execution). Raises SandboxError on violation.

        Checks: syntax, blocked imports, blocked calls, dunder access.
        """
        try:
            tree = ast.parse(code, filename="<sandbox>", mode="exec")
        except SyntaxError as exc:
            raise SandboxError(f"Syntax error: {exc.msg} at line {exc.lineno}") from exc

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self._check_module(alias.name)

            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    raise SandboxError("Relative imports are not allowed")
                if not node.module:
                    raise SandboxError("Import without module is not allowed")
                self._check_module(node.module)
                if any(alias.name == "*" for alias in node.names):
                    raise SandboxError("Wildcard imports are not allowed")

            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in self.blocked_calls:
                    raise SandboxError(f"Call to '{func.id}' is not allowed")
                if isinstance(func, ast.Attribute) and func.attr.startswith(BLOCKED_NAME_PREFIXES):
                    raise SandboxError(f"Access to dunder attribute '{func.attr}' is not allowed")

            elif isinstance(node, ast.Attribute):
                if node.attr.startswith(BLOCKED_NAME_PREFIXES):
                    raise SandboxError(f"Access to dunder attribute '{node.attr}' is not allowed")

            elif isinstance(node, ast.Name):
                if node.id.startswith(BLOCKED_NAME_PREFIXES):
                    raise SandboxError(f"Use of dunder name '{node.id}' is not allowed")

    def _check_module(self, name: str) -> None:
        top_level = name.split(".")[0]
        if top_level in self.blocked_modules:
            raise SandboxError(f"Import of '{name}' is not allowed")
        if top_level not in self.allowed_modules:
            raise SandboxError(
                f"Import of '{name}' is not in allowed modules: {sorted(self.allowed_modules)}"
            )

    # -- execution -----------------------------------------------------------

    @lru_cache(maxsize=256)
    def compile(self, code: str) -> Any:
        """Validate and compile code (cached)."""
        self.validate(code)
        return compile(code, "<sandbox>", "exec")

    def execute(self, code: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute code in the sandbox and return the resulting namespace.

        Args:
            code: Python source code.
            context: Extra globals to inject into the sandbox namespace.

        Returns:
            The sandbox's globals dict after execution.

        Raises:
            SandboxError: on validation failure, timeout, or execution error.
        """
        compiled = self.compile(code)

        sandbox_globals: dict[str, Any] = {
            "__builtins__": {
                k: v for k, v in builtins.__dict__.items()
                if k in self.allowed_builtins
            },
            "__name__": "__sandbox__",
            **(context or {}),
        }
        sandbox_globals["__builtins__"]["__import__"] = self._restricted_import

        def _run():
            exec(compiled, sandbox_globals, sandbox_globals)
            return sandbox_globals

        future = self._executor.submit(_run)
        try:
            result = future.result(timeout=self.timeout)
            return result
        except FuturesTimeoutError:
            future.cancel()
            raise SandboxError(f"Execution timeout: {self.timeout}s exceeded")
        except SandboxError:
            raise
        except Exception as e:
            raise SandboxError(f"Execution failed: {e}")
