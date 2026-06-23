"""DSL module — recursive-descent expression parser and evaluator.

Install: ``pip install bosch-ai-framework[dsl]`` (no extra dependencies)

Provides:
    - ``DSLRegistry``: register functions, evaluate expressions
    - ``tokenize()``: tokenize a DSL expression string
    - ``register_function()``: register a callable for use in DSL expressions
    - ``get_registered_functions()``: list all registered function metadata

Usage::

    from bosch_ai_framework.dsl import DSLRegistry, register_function

    # Register business functions
    register_function("moving_average", my_ma_func, args="series, window=7", desc="Simple moving average")

    # Evaluate
    registry = DSLRegistry()
    result = registry.evaluate("moving_average(demand, 7)", context={"demand": [1,2,3,4,5]})

Extracted from: bosch-forecast
"""

from bosch_ai_framework.dsl.parser import (
    DSLEvaluator,
    get_registered_functions,
    register_function,
    tokenize,
)


class DSLRegistry:
    """Convenience wrapper: evaluate DSL expressions with the global function registry.

    Usage::

        dsl = DSLRegistry()
        dsl.evaluate("my_func(x, 7)", context={"x": [1,2,3]})
    """

    @staticmethod
    def evaluate(expression: str, context: dict) -> object:
        """Evaluate a DSL expression against a context dict."""
        evaluator = DSLEvaluator(context)
        return evaluator.evaluate(expression)


__all__ = [
    "DSLRegistry",
    "DSLEvaluator",
    "tokenize",
    "register_function",
    "get_registered_functions",
]
