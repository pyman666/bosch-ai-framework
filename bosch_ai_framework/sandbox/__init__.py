"""Sandbox module — AST-validated Python code execution.

Install: ``pip install bosch-ai-framework[sandbox]`` (no extra dependencies)

Provides:
    - ``Sandbox``: secure code executor with AST validation, whitelist, timeout, thread pool
    - ``SandboxError``: raised on sandbox rule violations

Usage::

    from bosch_ai_framework.sandbox import Sandbox

    sandbox = Sandbox(allowed_modules=["numpy"], timeout=30)
    sandbox.validate("result = sum(data) + 1")
    result = sandbox.execute("result = sum(data) + 1", context={"data": [1,2,3]})
    # result is the sandbox globals dict; look for your output variable there

Extracted from: bosch-forecast
"""

from bosch_ai_framework.sandbox.executor import Sandbox, SandboxError

__all__ = ["Sandbox", "SandboxError"]
