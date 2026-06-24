"""xpeng-zq 客户: 小鹏汽车 ZQ 工厂的到货计划 / 缺件推移 Excel 解析.

跟 ``clients/chery/`` 不同, 这里业务结构复杂 (同类数据多段散布 / 行号每月浮动 / 顶部
"上线"段 + 合计累计要剔除), 必须 LLM 全程规划. 走通用 ``apdfi.excel.complex`` 引擎 +
``apdfi.chat`` skill orchestrator, 本子包只负责业务专属的四件事:

    - ``schemas.py``: 业务 schema (Plan / Row / Intent / Session).
    - ``prompts.py``: planner + diagnose system prompt 字面量.
    - ``executor.py``: 业务 skill 实现 (``XpengZqExecutor`` + intro / intent 拼装),
      由 chat 状态机回调. **不是** linear pipeline, 跟 ``apdfi/pdf/pipeline/`` 是
      两种范式 (前者是 skill, 后者是 workflow). 详见 README "设计哲学" 一节.
    - ``__init__.py`` (本文件): 拼一份 ``ComplexExcelConfig`` 调 ``register_complex_excel``.

import 这个包会触发 ``register_complex_excel`` 注册副作用, 把 6 个端点挂到全局
``excel/`` router 上 (M2M POST/GET + chat 5 个), 并把 ``XpengZqHandler`` 加进 chat 注册表.
"""
from ...complex import ComplexExcelConfig
from .executor import XpengZqExecutor, xpeng_zq_intent, xpeng_zq_intro
from .prompts import DIAGNOSE_PROMPT, XPENG_ZQ_PROMPT
from .schemas import XpengZqRow, XpengZqSession, XpengZqPlan
from .. import support


CONFIG = ComplexExcelConfig(
    label="xpeng-zq",
    plan_schema=XpengZqPlan,
    row_schema=XpengZqRow,
    session_schema=XpengZqSession,
    plan_prompt=XPENG_ZQ_PROMPT,
    diagnose_prompt=DIAGNOSE_PROMPT,
    intro_message_fn=xpeng_zq_intro,
    build_intent_fn=xpeng_zq_intent,
    executor=XpengZqExecutor(),
)


support(CONFIG)


