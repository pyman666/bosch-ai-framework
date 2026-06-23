"""Config module — YAML configuration loading.

Provides:
    - ``load_config()``: load/reload model config from YAML
    - ``DEFAULT_MODEL``: current default model name
    - ``MODEL_LIST``: expanded LiteLLM model_list
    - ``ROUTER_KWARGS``: LiteLLM Router kwargs
"""

from bosch_ai_framework.config.settings import (
    DEFAULT_MODEL,
    MODEL_LIST,
    ROUTER_KWARGS,
    load_config,
)

__all__ = [
    "load_config",
    "DEFAULT_MODEL",
    "MODEL_LIST",
    "ROUTER_KWARGS",
]
