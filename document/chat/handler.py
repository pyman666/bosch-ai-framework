"""ChatHandler 协议 + ``BusinessFailure`` + ``ChatPlan`` 公共基类.

一个业务客户接入 chat pipeline 只要做三件事:
    1. 定义 ``MyPlan(ChatPlan)`` (extends ``ChatPlan`` 拿到 ``summary`` 字段).
    2. 实现 ``ChatHandler``: 提供 prompt + skeleton 构造 + ``execute(plan, raw)``.
    3. 在自己模块 import 时调 ``register(handler)``, 然后挂业务路由.

通用层不知道任何业务细节, 只通过 ``handler.name`` 路由 + ``handler.PlanSchema`` 类型化 LLM 输出.
"""
from typing import Protocol, runtime_checkable
from pydantic import BaseModel, Field


class BusinessFailure(Exception):
    """业务侧主动抛出的"软失败"信号: 代码没崩, 但结果不对 (rows=0, 关键字段大量缺失等).

    通用层 ``safe_run`` 捕获后会调 LLM 出诊断 prose, 状态机进入 ``AWAITING_FEEDBACK``.
    M2M 模式 (非 chat) 也会捕获, 但走 HTTP 错误路径直接告诉调用方失败原因.
    """

    def __init__(self, reason: str, ctx: dict | None = None):
        self.reason = reason
        self.ctx = ctx or {}
        super().__init__(reason)


class ChatPlan(BaseModel):
    """所有走 chat pipeline 的 plan schema 都要继承它. 业务自己加 plan-specific 字段."""

    summary: str = Field(
        ...,
        description="给业务方看的人话总结. 列出本次解析将采用的规则: 哪些行/段保留, 哪些丢弃, 数据量预估.",
    )


@runtime_checkable
class ChatHandler(Protocol):
    """业务接入 chat pipeline 的统一协议.

    属性:
        name: 唯一标识, e.g. ``"xpeng-zq"``. 用于注册表查找.
        PlanSchema: ``ChatPlan`` 子类, instructor 用它做结构化输出.
        plan_prompt: 出 plan 的 system prompt.
        diagnose_prompt: 失败时让 LLM 总结的 system prompt.

    方法:
        build_skeleton: 把 raw 文件转成给 LLM 看的紧凑文本.
        execute: 拿 plan + raw 抽数据. 业务失败时 ``raise BusinessFailure``.
    """

    name: str
    PlanSchema: type[ChatPlan]
    plan_prompt: str
    diagnose_prompt: str

    def intro_message(self, file_name: str) -> str:
        """会话创建瞬间立刻给前端展示的中文"前瞻"文案.

        - **前瞻**: "我接下来会做什么", 不是"我已经做了什么".
        - **业务方言**: 业务侧自己写, 描述本次解析将采用的规则 (e.g. "我会识别到货计划/缺件
          推移段, 排除合计/累计行, 隐藏行也会跳过, 大约 5-15 秒后给您 plan 总结").
        - **第一人称**: 用"我"称呼自己, 用"您"称呼业务方.
        - **不走 LLM**: 模板字符串拼接即可, 不要为这段调 LLM, 否则就失去"立刻可见"的意义了.
        """
        ...

    def build_intent(self, file_name: str) -> BaseModel | None:
        """会话创建瞬间给前端的**结构化**预设方案 (跟 ``intro_message`` 互补).

        - **结构化**: 推断的文件元数据 (e.g. plant / period / type, 从文件名 regex 出) +
          业务约定的字段映射预设 + 主键策略 + 一组可参考的澄清问题. 前端把它直接铺到
          "AI Tip" 步骤的 detect grid / mapping / clarification 区域, 不必猜数据形状.
        - **不走 LLM**: 用文件名 regex + 业务侧 hardcode 的预设规则拼接即可.
        - **可选**: 默认返回 ``None`` -- handler 不实现也行, session 里 ``intent`` 字段就是
          ``None``, 前端跳过 'AI Tip' 步骤直接走 ``POST /chat/{chat_id}/start`` 即可.

        返回 ``BaseModel`` 子类即可, 通用层用 ``model_dump()`` 落到 ``Session.intent``;
        在业务的 typed Session 里收窄类型, OpenAPI 才看得到完整字段.
        """
        return None

    def build_skeleton(self, raw: bytes, *, sheet: int | str = 1) -> str: ...

    async def execute(self, plan: ChatPlan, raw: bytes, *, sheet: int | str = 1) -> list[BaseModel]: ...
