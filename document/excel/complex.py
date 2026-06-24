"""通用"复杂 Excel" 解析引擎: **LLM 规划 + Python 执行 + chat 人在环路**.

跟 ``simple.py`` 对称, 是 complex 这条赛道的所有通用基础设施.

# 跟 simple 的区别
| 维度 | simple | complex |
|---|---|---|
| 客户表的差异 | **格式**漂移 (列名 / 日期 format / 顶部 banner) | **结构**漂移 (同类数据多段散布, 行号每月浮动, 段标题位置不固定) |
| LLM 角色 | 失败兜底 (Python first) | 全程规划 (LLM first) |
| 输出形态 | 单一通用 ``SimpleExcelResp{rows, notice, ...}`` | 业务自定义 ``list[<Row>]`` (每家客户 Row schema 不同) |
| 失败语义 | LLM 兜底里包含 prose; 兜底再失败 5xx | 业务失败抛 ``BusinessFailure``, LLM 写诊断 prose 走 chat AWAITING_FEEDBACK |
| 调用形态 | M2M task only | M2M task + chat 5-state 状态机 |

# 设计契约
业务客户只写**四个**东西:
    1. ``schemas.py``: ``<Customer>Plan(ChatPlan)`` / ``<Customer>Row(BaseModel)`` /
       ``<Customer>Intent(BaseModel)`` / ``<Customer>Session(Session)``.
    2. ``prompts.py``: planner system prompt 字面量 (``diagnose_prompt`` 可选 override,
       不传走通用 ``DEFAULT_DIAGNOSE_PROMPT``).
    3. **executor** (一个类, 实现 ``ComplexExcelExecutor`` protocol): ``build_skeleton(raw)``
       渲染 LLM 看的骨架文本, ``execute(plan, raw)`` 按 plan 抽数据. 这块是业务核心
       (~30-80 行), 通用层不掺和 -- 不同客户的列定义 / 聚合 key / Row 怎么 build 不一样.
    4. ``__init__.py``: 拼一份 ``ComplexExcelConfig`` 然后一行 ``register_complex_excel``.

其它 (M2M task wrapper / phase 上报 / 状态机 / typed session OpenAPI 收窄 / intent dict 化 /
路由 boilerplate / 异常 -> error / LLM one-shot 调用 / 鉴权) 全部在通用层. 接入新客户的最小
动作就是上面四个文件, 路由会自动挂上 6 个端点 (M2M POST/GET + chat 5 个).

# 公共 helpers
``to_iso_date`` / ``to_int`` / ``date_columns`` / ``build_complex_skeleton`` /
``infer_period`` / ``boundary_pattern`` 都作为 public 函数暴露, 业务 executor 写
``_execute_plan`` / ``build_intent_fn`` 时直接 import 用即可, 不必每家重写一遍.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from textwrap import dedent
from typing import Any, Callable, Protocol

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from litellm.exceptions import APIError
from pydantic import BaseModel

from ._common import excel_upload
from .core import PyXL
from ..llm import aclient
from ..chat import ChatPlan, Session, ops as chat_ops, register
from ..settings import DEFAULT_MODEL
from ..tasks import TaskID, TaskResult, TaskStatus, create_task, get_task, set_phase
from ..utils import exception_detail


# ---------------------------------------------------------------------------
# 公共 helpers: cell 类型转换 (业务 executor 直接 import 用)


def to_iso_date(v: Any) -> str | None:
    """日期 cell -> ``YYYY-MM-DD`` 字符串. 不是日期返 None.

    支持: ``datetime`` / ``date`` / 常见 ISO + 斜杠 + 带时间的字符串. Excel 序列号已经
    在 ``PyXL`` 那一层被转成 ``datetime`` 了, 这里只处理面值.
    """
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, str):
        s = v.strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(s, fmt).date().isoformat()
            except ValueError:
                continue
    return None


def to_int(v: Any) -> int | None:
    """cell -> int, 容错: 浮点抹零, 千分位逗号, 空白 strip. 失败返 None."""
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        if v != v:  # NaN
            return None
        return int(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if not s:
            return None
        try:
            return int(float(s))
        except ValueError:
            return None
    return None


def date_columns(xl: PyXL, header_row_1b: int) -> list[tuple[int, str]]:
    """给一个 1-based 表头行号, 返回该行里所有"是日期"且**未隐藏**的 cell.

    返回 ``[(col_index_0b, iso_date), ...]``. 跟 ``PyExcel`` / ``WideExcel`` 行为对齐:
    隐藏列哪怕看着像日期也不读. 业务 executor 通常第一步就是调它定位日期列, 然后 unpivot
    数据.
    """
    mat = xl.matrix_filled
    if not (1 <= header_row_1b <= mat.shape[0]):
        return []
    row = mat[header_row_1b - 1]
    hidden_cols = xl.hidden_cols_0b
    out: list[tuple[int, str]] = []
    for j, cell in enumerate(row):
        if j in hidden_cols:
            continue
        iso = to_iso_date(cell)
        if iso is not None:
            out.append((j, iso))
    return out


# ---------------------------------------------------------------------------
# 骨架渲染 (LLM 看的紧凑文本)


def _skeleton_cell_text(v: Any) -> str:
    """单 cell -> 给 LLM 的字符串, 日期保留 ISO."""
    if v is None:
        return ""
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v).strip()


def build_complex_skeleton(
    xl: PyXL,
    *,
    id_cols: int = 3,
    max_date_preview: int = 3,
) -> str:
    """生成 LLM 输入用的紧凑骨架.

    只摊开前 ``id_cols`` 列 + 日期列前 ``max_date_preview`` 个 cell 的预览, 数字 cell 一律
    不进入骨架 -- LLM 不需要数字做语义判断, 反而会被干扰. 隐藏行 / 列号单独列出, 跟
    ``PyXL`` / ``PyExcel`` 同源, 颗粒度对齐.

    复杂表客户的 ``build_skeleton`` 99% 直接 ``return build_complex_skeleton(PyXL(raw))``
    就够了, 真有特殊布局再 override.
    """
    mat = xl.matrix_filled
    n_rows, n_cols = mat.shape
    id_cols = min(id_cols, n_cols)

    hidden_rows = sorted(xl.hidden_rows_1b)
    hidden_cols_1b = sorted(c + 1 for c in xl.hidden_cols_0b)

    header_lines = [
        f"sheet_shape: rows={n_rows}, cols={n_cols}",
        f"hidden_rows: {hidden_rows}",
        f"hidden_cols: {hidden_cols_1b}  (服务端会自动跳过这些列, plan 里不必再处理)",
        f"format: R<row> | <col1> | <col2> | <col3> | preview=<col4..col{3 + max_date_preview}>",
        "",
    ]
    body_lines: list[str] = []
    for r in range(n_rows):
        row = mat[r]
        ids = [_skeleton_cell_text(row[c]) if c < n_cols else "" for c in range(id_cols)]
        preview_cells = [
            _skeleton_cell_text(row[c])
            for c in range(id_cols, min(id_cols + max_date_preview, n_cols))
        ]
        if not any(ids):
            if not any(preview_cells):
                continue
            preview = " | ".join(preview_cells)
            body_lines.append(f"R{r + 1:02d} |  |  |  | preview={preview}")
            continue
        preview = " | ".join(preview_cells) if any(preview_cells) else ""
        line = f"R{r + 1:02d} | " + " | ".join(ids)
        if preview:
            line += f" | preview={preview}"
        body_lines.append(line)

    return "\n".join(header_lines + body_lines)


# ---------------------------------------------------------------------------
# 文件名 regex 推断 (intent 用; 业务 build_intent_fn 里 call)
#
# 文件名里 ``_`` 是 word char, 用 ``\b`` 会漏匹配 ``XP-ZQ_FCST_2026-05.xlsx`` 这类格式;
# 这里统一用显式的"非字母数字"边界.


_NB_L = r"(?<![A-Za-z0-9])"
_NB_R = r"(?![A-Za-z0-9])"


def boundary_pattern(token: str) -> str:
    """把一段字面量包成"不被字母数字夹"的 regex.

    e.g. ``boundary_pattern("FCST")`` -> ``"(?<![A-Za-z0-9])FCST(?![A-Za-z0-9])"``.
    业务方写 ``build_intent_fn`` 里的文件类型 / plant 识别表时常用. ``token`` 内部如果
    有正则元字符 (``.`` / ``-``) **不**自动转义, 业务方自己确保字面安全.
    """
    return f"{_NB_L}{token}{_NB_R}"


_PERIOD_RX = re.compile(
    r"(20\d{2})[-_/年]?(0?[1-9]|1[0-2])(?:[-_/月]|(?![A-Za-z0-9]))"
)


def infer_period(file_name: str) -> str | None:
    """从文件名里抽 ``YYYY-MM`` 期间. 失败返 None.

    支持: ``2026-05`` / ``2026_05`` / ``2026/05`` / ``2026年5月`` 等. 月份会 zero-pad 成两位.
    """
    m = _PERIOD_RX.search(file_name)
    if not m:
        return None
    return f"{m.group(1)}-{int(m.group(2)):02d}"


# ---------------------------------------------------------------------------
# Executor protocol (业务方实现这个; chat 跟 M2M 都通过它跟业务交互)


class ComplexExcelExecutor(Protocol):
    """业务方要实现的最小接口.

    两个方法都接 raw bytes (而不是 ``PyXL`` 实例), 因为 chat 跟 M2M 两条路径都要从 bytes
    起步, executor 自己控制 ``PyXL`` 怎么实例化 (sheet 哪个 / 怎么处理合并 cell 等).

    实现样板:

        class MyExecutor:
            def build_skeleton(self, raw: bytes) -> str:
                xl = PyXL(file=io.BytesIO(raw), sheet=1)
                return build_complex_skeleton(xl)

            async def execute(self, plan: MyPlan, raw: bytes) -> list[MyRow]:
                xl = PyXL(file=io.BytesIO(raw), sheet=1)
                return _execute_plan(xl, plan)   # 业务核心
    """

    def build_skeleton(self, raw: bytes, *, sheet: int | str = 1) -> str: ...

    async def execute(self, plan: ChatPlan, raw: bytes, *, sheet: int | str = 1) -> list[BaseModel]: ...


# ---------------------------------------------------------------------------
# 默认 diagnose prompt (业务方不传走这个; 想要更贴业务的诊断 prose 自己写一份覆盖)


DEFAULT_DIAGNOSE_PROMPT = dedent("""
你是一名 Excel 解析助手. 当前一次解析失败了, 业务方在等你给一段中文 prose 说明
为什么失败 + 希望业务方提供什么信息可以恢复.

**注意人称区分**: 这一段输出会原样展示给业务方对话框, 要用第一人称 "我" 称呼自己
(assistant), 用敬称 "您" 称呼业务方. 例: "我刚才把第 3 行当成了日期表头, 但跑出来
0 行, 您能告诉我表头实际在第几行吗?"

判定常见原因:
- BusinessFailure reason 提到 "0 数据行": 通常是把段标题/合计行当成了数据行, 或日期表头
  识别错位了, 请业务方确认日期表头是哪一行 / 哪些段是数据.
- BusinessFailure reason 提到 "date_header_row" / "日期表头": 请业务方告诉你日期表头实际
  在第几行.
- BusinessFailure reason 提到 "列数不足": 文件结构异常, 请业务方核对是不是上传错文件.
- 其他异常: 简洁告知异常类型, 请业务方上传更具体的提示.

不要解释技术细节 (栈帧/文件路径), 不超过 5 句话, 不要客套.
可适当用 Markdown 突出关键信息: `**加粗**` 用于数字/行号, `` `code` `` 用于字段名.
""").strip()


# ---------------------------------------------------------------------------
# Config: 业务客户接入唯一要填的 dataclass


@dataclass
class ComplexExcelConfig:
    """单个客户接入 complex excel pipeline 所需的全部配置.

    跟 ``SimpleExcelConfig`` 对称: 业务方只填这些字段, 通用层吃掉 task wrapper / phase
    上报 / intent dict 化 / 路由 boilerplate / 状态机 / 异常 -> 5xx 转换. 不允许业务方
    在通用层之外另写一份 ``XxxHandler`` 类 -- 那是回到老的"每家客户复制粘贴" 反模式.

    Args:
        label: 唯一标识 (e.g. ``"xpeng-zq"``), 同时用作 ``ChatHandler.name`` (chat 注册
            表 key) / URL 前缀默认值 / OpenAPI summary 标签. **必须 URL-safe** (不含
            空格 / 中文 / ``/``), 通用层不做 escape.
        plan_schema: planner 输出 plan 的 pydantic 类型, 必须是 ``ChatPlan`` 子类 (拿到
            ``summary`` 字段). instructor 用它做结构化输出 + schema 校验.
        row_schema: 单条解析结果的 pydantic 类型. M2M 端口 ``TaskResult[list[row_schema]]``;
            chat 端口 ``<Customer>Session.latest_rows: list[row_schema]``.
        session_schema: 业务的 typed Session 子类 (extends ``apdfi.chat.Session``), 在
            ``intent`` / ``latest_plan`` / ``latest_rows`` 三个字段上做了具体类型收窄.
            通用层用它当 chat 端点的 return type, OpenAPI 才能给前端展示完整 schema (而
            不是 ``dict[str, Any]``).
        plan_prompt: planner system prompt 字面量. 业务方写, 描述 sheet 业务结构 + 输出
            规则. 风格约定: prompt 内部用第二人称 "你是 xxx 助手, 你的任务..." 指挥 LLM,
            但要在 prompt 里明确告诉它**对外输出 prose (summary 字段) 用 "我" 称呼自己,
            用 "您" 称呼业务方**.
        intro_message_fn: ``(file_name) -> prose``. **chat 端口专用**: 会话创建瞬间给前端
            的中文前瞻文案 (落到 ``Session.initial_notice``). 业务方自己拼字符串模板, 不走
            LLM, 是同步函数. M2M 端口 (Java 后端) 不消费 prose, 不会走这个回调.
        executor: 业务核心 (``build_skeleton`` 渲染 LLM 骨架 + ``execute`` 按 plan 抽数据).
            ``execute`` 业务失败时 ``raise BusinessFailure(reason, ctx)``, M2M 走 error,
            chat 走 LLM 诊断 -> AWAITING_FEEDBACK.
        build_intent_fn: ``(file_name) -> Intent`` 可选回调. 不调 LLM 的结构化预设拼装函数,
            返回值应该是 ``session_schema.intent`` 字段的具体类型. None 表示业务方不想要
            "AI Tip" 步骤 (demo 那种结构化预设展示), 通用层回退到老的"上传即 PLANNING"语义.
        diagnose_prompt: 诊断 prompt (业务失败时让 LLM 写 prose). 留空走
            ``DEFAULT_DIAGNOSE_PROMPT``, 业务方需要更贴业务的诊断时 override.
    """

    label: str
    plan_schema: type[ChatPlan]
    row_schema: type[BaseModel]
    session_schema: type[Session]
    plan_prompt: str
    intro_message_fn: Callable[[str], str]
    executor: ComplexExcelExecutor
    build_intent_fn: Callable[[str], BaseModel] | None = None
    diagnose_prompt: str = ""

    def __post_init__(self):
        if not self.label:
            raise ValueError("ComplexExcelConfig.label 不能为空")
        if "/" in self.label or " " in self.label:
            raise ValueError(
                f"ComplexExcelConfig.label={self.label!r} 必须 URL-safe (不含空格 / '/')"
            )


# ---------------------------------------------------------------------------
# Internal: 通用 ChatHandler 实现 (业务方不要继承它, 也不要直接用; ``register_complex_excel``
# 内部实例化并注册)


class _ComplexExcelHandler:
    """从 ``ComplexExcelConfig`` 装配出来的 ``ChatHandler`` 协议实现.

    业务客户**不要**继承也**不要**直接实例化它. 唯一入口是 ``register_complex_excel``.
    """

    def __init__(self, config: ComplexExcelConfig):
        self.config = config
        self.name = config.label
        self.PlanSchema = config.plan_schema
        self.plan_prompt = config.plan_prompt
        self.diagnose_prompt = config.diagnose_prompt or DEFAULT_DIAGNOSE_PROMPT

    def intro_message(self, file_name: str) -> str:
        return self.config.intro_message_fn(file_name)

    def build_intent(self, file_name: str) -> BaseModel | None:
        if self.config.build_intent_fn is None:
            return None
        return self.config.build_intent_fn(file_name)

    def build_skeleton(self, raw: bytes, *, sheet: int | str = 1) -> str:
        return self.config.executor.build_skeleton(raw, sheet=sheet)

    async def execute(self, plan: ChatPlan, raw: bytes, *, sheet: int | str = 1) -> list[BaseModel]:
        return await self.config.executor.execute(plan, raw, sheet=sheet)


# ---------------------------------------------------------------------------
# M2M: planner one-shot + task wrapper


PHASE_PARSE = "parse"


async def _call_planner_one_shot(
    config: ComplexExcelConfig,
    raw: bytes,
    *,
    system_prompt: str,
    model: str,
    retry: int,
    sheet: int | str = 1,
) -> ChatPlan:
    """M2M one-shot 调 LLM: skeleton + instructor structured output.

    chat 模式不走这条 (它要保留多轮 messages, 由 ``apdfi.chat.ops`` 自己拼对话),
    所以这函数只服务 M2M.
    """
    skeleton = config.executor.build_skeleton(raw, sheet=sheet)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": skeleton},
    ]
    try:
        return await aclient.chat.completions.create(
            model=model,
            messages=messages,
            response_model=config.plan_schema,
            max_retries=max(retry, 0),
        )
    except APIError as e:
        raise HTTPException(
            status_code=getattr(e, "status_code", 500),
            detail=getattr(e, "message", str(e)),
        )


async def complex_excel_task(
    task_id: str,
    raw: bytes,
    *,
    config: ComplexExcelConfig,
    model: str,
    prompt: str | None,
    retry: int,
    sheet: int | str = 1,
    phase: str = PHASE_PARSE,
) -> None:
    """通用 M2M 后台任务: planner + executor + phase 上报.

    业务失败 (``BusinessFailure``) 也会落到 error phase, 跟代码异常一视同仁 -- M2M 调用方
    只关心成功/失败, 不需要交互式诊断. 想要诊断走 ``/chat`` 端口.
    """
    set_phase(task_id, phase, status=TaskStatus.processing)
    try:
        plan = await _call_planner_one_shot(
            config,
            raw,
            system_prompt=prompt or config.plan_prompt,
            model=model,
            retry=retry,
            sheet=sheet,
        )
        rows = await config.executor.execute(plan, raw, sheet=sheet)
        set_phase(task_id, phase, status=TaskStatus.success, result=rows)
    except Exception as e:
        set_phase(task_id, phase, status=TaskStatus.error, message=exception_detail(e))


# ---------------------------------------------------------------------------
# 入口: 一行注册业务客户 (M2M POST/GET + chat 5 个端点)
#
# 路由 return type 是 generic over 业务的 (``row_schema``, ``session_schema``), 但 Python
# 函数定义的 ``-> X`` 是字面量解析, 没法捕获 factory 参数. 所以用装饰器的 ``response_model=``
# 显式传 schema (FastAPI 优先用 ``response_model`` 参数, 忽略函数 annotation), 这样每个
# 客户的 OpenAPI 都能看到完整的 plan / rows / intent 字段 (而不是 base ``Session`` 的
# ``dict[str, Any]``).


def register_chat_handler(config: ComplexExcelConfig) -> None:
    """把 ComplexExcelConfig 装配成 ChatHandler 并注册到 chat 系统.

    由 ``support()`` 在 complex 客户声明时自动调用, 确保 chat 路由能通过
    ``handler_name`` 找到对应的业务 handler.
    """
    handler = _ComplexExcelHandler(config)
    register(handler)


def register_complex_excel(
    config: ComplexExcelConfig,
    router: APIRouter,
    *,
    path_prefix: str | None = None,
) -> None:
    """业务客户接入入口: 注册 ChatHandler + 6 个端点 (M2M POST/GET + chat 5 个).

    新客户最小工作: 写完 ``schemas`` / ``prompts`` / ``executor`` 后, 在
    ``apdfi/excel/clients/<name>/__init__.py``::

        config = ComplexExcelConfig(...)
        register_complex_excel(config, router)

    所有 boilerplate (task wrapper / phase 上报 / 状态机 / typed session 反序列化 /
    intent dict 化 / 异常处理 / 鉴权) 都在通用层. import 该客户子包就触发注册副作用.

    Args:
        config: 客户的 ``ComplexExcelConfig`` 实例.
        router: 已经创建好的 ``APIRouter`` (通常是 ``apdfi.excel.routes.router``).
        path_prefix: URL 前缀, 含开头的 ``/`` (e.g. ``"/xpeng-zq"``). 缺省 ``"/{label}"``.
    """
    handler = _ComplexExcelHandler(config)
    register(handler)

    if path_prefix is None:
        path_prefix = f"/{config.label}"

    label = config.label
    session_schema = config.session_schema
    row_schema = config.row_schema

    # ----- M2M: POST + GET task ----------------------------------------------

    @router.post(
        path_prefix,
        summary=f"[M2M] 提交 {label} Excel 解析任务, 返回 task_id",
        name=f"{label}_complex_excel_create",
    )
    async def _m2m_create(
        tasks: BackgroundTasks,
        file_params: dict = Depends(excel_upload),
        model: str = Form(DEFAULT_MODEL, description=f"LLM 模型别名, 默认 `{DEFAULT_MODEL}`"),
        prompt: str = Form(None, description="覆盖默认 plan system prompt, 一般不需要传"),
        retry: int = Form(2, description="LLM 输出 schema 校验最大重试次数, ≥ 0, 默认 `2`"),
    ) -> TaskID:
        """**M2M 端口** (Java 后端直接调). 一发即走 + 立即返 ``task_id``.

        返回 task_id, 用 ``GET {path_prefix}/data/{task_id}`` 取结果. 业务失败 (rows=0
        等) 在这条路径上等同于 error, 不会触发 LLM 诊断 -- 想要交互式诊断走 ``/chat`` 端口.
        """
        return await create_task(
            tasks,
            complex_excel_task,
            file_params["raw"],
            config=config,
            model=model,
            prompt=prompt,
            retry=retry,
            sheet=file_params["sheet"],
        )

    @router.get(
        f"{path_prefix}/data/{{task_id}}",
        summary=f"[M2M] 取 {label} 解析结果",
        name=f"{label}_complex_excel_data",
        response_model=TaskResult[list[row_schema]],
    )
    async def _m2m_data(task_id: str):
        """获取 M2M 解析结果. ``status`` 可能是 processing / success / error / None."""
        return await get_task(task_id, PHASE_PARSE)

    # ----- Chat: 5 个端点 ----------------------------------------------------

    @router.post(
        f"{path_prefix}/chat",
        summary=f"[Chat] 创建 {label} 解析会话, 返回结构化预设 intent (不调 LLM)",
        name=f"{label}_complex_excel_chat_create",
        response_model=session_schema,
    )
    async def _chat_create(
        file: UploadFile = File(..., description=f"{label} Excel 文件"),
    ):
        """**Chat 端口** - 第 1 步 / demo Step 2 'AI Tip'.

        上传文件, **不调 LLM**, 立即返 typed session (``state=intent_preview``):

        - ``initial_notice``: 中文 prose 前瞻文案.
        - ``intent``: 结构化预设 (业务方在 ``build_intent_fn`` 里拼好的), 前端直接铺到
          'AI Tip' 区域. 业务方没实现 ``build_intent_fn`` 时 ``intent=null``, 前端跳过
          'AI Tip' 步骤, 直接走下一步即可.

        业务方看完决定继续, 调 ``POST {path_prefix}/chat/{{chat_id}}/start`` 触发实际 LLM.
        """
        raw = await file.read()
        return await chat_ops.start(
            handler_name=label,
            file_name=file.filename or "unnamed.xlsx",
            raw=raw,
        )

    @router.post(
        f"{path_prefix}/chat/{{chat_id}}/start",
        summary=f"[Chat] 业务方确认预设/答完澄清, 触发 LLM planning",
        name=f"{label}_complex_excel_chat_start",
        response_model=session_schema,
    )
    async def _chat_start_planning(
        chat_id: str,
        bg: BackgroundTasks,
        clarifications: str = Form(
            None,
            description=(
                "(可选) 若业务方走了 demo Step 2.5 'complex chat' 路径, 这里传答完的"
                "澄清答复 prose. 例: '日期表头在第 5 行; 月度桶; 忽略隐藏列'. 直接选"
                "'按预设直接解析' 路径就不传."
            ),
        ),
        retry: int = Form(2, description="LLM 输出 schema 校验最大重试次数, ≥ 0, 默认 `2`"),
    ):
        """从 ``intent_preview`` 转 ``planning``, 后台跑 LLM. 对应 demo Step 3 '开始解析'."""
        return await chat_ops.start_planning(
            chat_id,
            clarifications=clarifications,
            bg=bg,
            retry=retry,
        )

    @router.get(
        f"{path_prefix}/chat/{{chat_id}}",
        summary=f"[Chat] 拉取 {label} 会话状态 (state=done 时 latest_rows 即最终结果)",
        name=f"{label}_complex_excel_chat_get",
        response_model=session_schema,
    )
    async def _chat_get(chat_id: str):
        """轮询拉取 session. 解析最终结果就在这里:

        - ``state=intent_preview``: ``intent`` 是 server 预设方案, 等业务方点 start.
        - ``state=planning``: 后台还在跑 LLM, 稍后再轮.
        - ``state=awaiting_confirm``: ``latest_plan.summary`` 是规则总结, 等业务方确认.
        - ``state=done``: ``latest_rows`` 是 ``list[row_schema]`` 解析结果, ``latest_plan``
          是用过的 plan.
        - ``state=awaiting_feedback``: ``diagnosis`` 是 LLM 给业务方的失败说明.
        """
        return await chat_ops.get(chat_id)

    @router.post(
        f"{path_prefix}/chat/{{chat_id}}/confirm",
        summary=f"[Chat] 业务方确认 {label} plan, 同步触发抽取",
        name=f"{label}_complex_excel_chat_confirm",
        response_model=session_schema,
    )
    async def _chat_confirm(chat_id: str):
        """业务方确认 plan, 立即跑 executor. ~50ms 出结果. 对应 demo Step 5 'Apply'.

        成功 -> ``state=done`` + ``latest_rows``;
        业务失败 -> LLM 出诊断 prose, ``state=awaiting_feedback`` + ``diagnosis``.
        """
        return await chat_ops.confirm(chat_id)

    @router.post(
        f"{path_prefix}/chat/{{chat_id}}/feedback",
        summary=f"[Chat] 业务方提反馈, 异步触发 {label} 重 plan",
        name=f"{label}_complex_excel_chat_feedback",
        response_model=session_schema,
    )
    async def _chat_feedback(
        chat_id: str,
        bg: BackgroundTasks,
        text: str = Form(
            ...,
            description="自然语言反馈, 例: '日期表头是第 5 行不是第 3 行' / '把第 X 段当成另一类'",
        ),
        retry: int = Form(2, description="LLM 输出 schema 校验最大重试次数, ≥ 0, 默认 `2`"),
    ):
        """业务方提反馈. ``state -> planning``, 后台重跑 LLM, 客户端轮 GET 看转回
        ``awaiting_confirm``. 对应 demo Step 4 右侧 chat.
        """
        return await chat_ops.feedback(chat_id, text=text, bg=bg, retry=retry)


__all__ = (
    "to_iso_date",
    "to_int",
    "date_columns",
    "build_complex_skeleton",
    "boundary_pattern",
    "infer_period",
    "ComplexExcelExecutor",
    "ComplexExcelConfig",
    "DEFAULT_DIAGNOSE_PROMPT",
    "register_chat_handler",
    "complex_excel_task",
    "register_complex_excel",
)
