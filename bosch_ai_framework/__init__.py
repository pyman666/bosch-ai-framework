"""bosch-ai-framework — LLM application framework.

A collection of independent, composable modules for building LLM-powered
microservices. No hidden behavior, no deep abstractions — just tools.

Layer 1 (Core — always installed):
    - ``llm``: LiteLLM Router wrapper, chat/chat_stream, instructor structured output
    - ``agent``: ToolRegistry + AgentLoop for multi-turn tool-calling agents
    - ``auth``: HTTP Basic + SAP XSUAA dual-mode authentication
    - ``tasks``: Lightweight async task tracking
    - ``server``: FastAPI app factory, Gunicorn config, middleware
    - ``config``: YAML → LiteLLM model_list configuration
    - ``utils``: exception_detail, date utilities

Layer 2 (Extras — install on demand):
    - ``rag``: BM25 + FAISS + RRF hybrid retrieval ``[rag]``
    - ``document``: PDF VLM parser + 3-tier Excel engine ``[document]``
    - ``dsl``: Recursive-descent expression parser/evaluator ``[dsl]``
    - ``sandbox``: AST-validated Python sandbox ``[sandbox]``
    - ``chat``: 5-state human-in-the-loop FSM orchestrator ``[chat]``
"""

__version__ = "0.1.0"
