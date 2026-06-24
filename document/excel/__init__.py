"""Excel 解析包. 通用平铺在根, 客户独立目录.

根目录 (通用算法/工具):
    - ``core``: 通用低层 IO (PyXL / PyExcel / ExcelConfig).
    - ``date_normalize``: 通用日期格式探测 + 规范化.
    - ``llm``: 共享 LLM client (复用 ``apdfi.llm``).
    - ``wide``: Python-first + LLM-fallback 宽表引擎 + ``WideExcelConfig`` /
      ``WideExcelResp`` / ``register_wide_excel``.
    - ``simple``: Python-first + LLM-fallback 引擎 + ``SimpleExcelConfig`` / 响应 schema.
    - ``_common``: 路由共用的 form dependency (``excel_upload``).
    - ``routes``: ``APIRouter`` 实例 + 触发客户子包注册.

``clients/`` (客户定制, 加新客户从这里开始):
    - ``chery/``: 走 ``simple`` 引擎的简单客户, 一个 ``__init__.py`` 集齐 prompt /
      config / pipeline / route.
    - ``xpeng_zq/``: 复杂客户 (LLM plan-based + chat handler), 拆 4 文件
      (prompts / schemas / pipeline / routes).

外部 (``apdfi.server``) 通过 ``from document.excel import router`` 拿到挂好所有
endpoint 的 APIRouter, 不需要触碰内部子模块.
"""
from .routes import router


