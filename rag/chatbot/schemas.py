from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..settings import CHAT_MAX_CONTENT_CHARS, CHAT_MAX_TURNS


class AskQuery(BaseModel):
    """报错 / data 诊断的单轮请求体 — ``/ask`` 端点入参.

    跟 :class:`ChatQuery` 共享 ``route_url + payload + model`` 字段, 区别只
    在不带对话历史 (用单字段 ``user_question`` 代替 ``messages``). 命名上跟
    端点名 ``/ask`` + pipeline 函数 ``ask_bot`` 三段对齐, 不再用旧的 "doc"
    前缀 (那时候只有一个端点, 怎么叫都行; 现在跟 ``ChatQuery`` 摆一起,
    ``AskQuery`` 对称性更好).


    前端的典型入口: 业务客户在 portal 上点了某条数据, 想问'为什么是这个状态'
    或'怎么改'. 前端把命中的接口路径 + 这条数据的字段快照打包过来, 必要时
    带上客户额外补的一句话. 字段拆开是为了:

    - 把"前端必须传这三块"固化成 schema 契约, 避免漏传或拼字符串姿势各异;
    - 让 pipeline 能针对性地用 ``route_url`` (含 customer / endpoint 强信号)
      做检索拼接, 不用在客户的自由文本里二次解析.
    """

    # `model` 字段会撞 Pydantic v2 的保留命名空间 `model_*`, 这里关掉警告;
    # 字段语义就是"调哪个 LLM 模型", 业务上不想换名.
    model_config = ConfigDict(protected_namespaces=())

    route_url: str = Field(
        ...,
        min_length=1,
        description="前端命中的接口路径, 含 customer / endpoint, 如 "
        "'/boct/BYD/portalDownload'. 用于定位哪个客户哪条业务流.",
    )
    payload: dict[str, Any] = Field(
        ...,
        description="前端那条数据的字段快照, 含 processStatus / processRemark / "
        "messageType 等状态字段及关键业务字段; 报错诊断的核心事实来源.",
    )
    user_question: str | None = Field(
        default=None,
        description="客户额外补充的一句话 (可选). 不填时, 助手基于 route_url + "
        "payload 主动诊断当前状态/报错原因 + 下一步.",
    )
    model: str | None = Field(
        default=None,
        description="LLM model name (内部用, 一般不传, 走默认模型).",
    )


class ChatMessage(BaseModel):
    """对话历史里的单条消息.

    只允许 ``user`` / ``assistant`` 两种 role: ``system`` 由服务端固定注入
    (含 SYSTEM_PROMPT + outline + 当轮检索结果), 不开放给前端覆盖.
    """

    role: Literal["user", "assistant"] = Field(
        ...,
        description="消息发出方; system 由服务端控制, 不在白名单内.",
    )
    content: str = Field(
        ...,
        max_length=CHAT_MAX_CONTENT_CHARS,
        description=(
            "消息文本. 允许空字符串 (首轮可用空 user 让助手基于 "
            "route_url + payload 自检). 长度上限由 settings.CHAT_MAX_CONTENT_CHARS "
            "控制 (默认 8000 字符), 防误把整段日志贴进来."
        ),
    )


class ChatQuery(BaseModel):
    """多轮 chat 请求体 — ``/chat`` 端点入参.

    跟 :class:`AskQuery` 同样锚定到一条数据 (``route_url`` + ``payload``),
    但额外带前端持有的完整对话历史. 设计上服务端 stateless: 不存 session,
    前端每次把整段 ``messages`` 传回, 末条必须是 ``user``, 由服务端对它
    重做 RAG 检索 + 拼最终 LLM messages.

    过往轮次保持纯文本原样回放 (不重复检索, 也不把当时的检索结果再注入)
    — 这样 history token 量稳定, prefix cache 友好, 也避免重检索结果跟
    历史里的 assistant 回答自相矛盾.
    """

    # ``json_schema_extra.example`` 注 OpenAPI: FastAPI ``/docs`` 直接渲染成
    # "Try it out" 的默认 body, 前端对接看一眼就懂; 比 description 里堆字段
    # 解释直观得多. 用最小可跑的两轮 (一来一回 + 当前追问) 作为示例.
    model_config = ConfigDict(
        protected_namespaces=(),
        json_schema_extra={
            "example": {
                "route_url": "/boct/BYD/portalDownload",
                "payload": {
                    "dataId": "12345",
                    "processStatus": "Validation Error",
                    "processRemark": "BoctAbstractValidationService 校验失败",
                },
                "messages": [
                    {"role": "user", "content": "为什么这条数据失败了?"},
                    {
                        "role": "assistant",
                        "content": "这条 POD 数据在校验阶段被剔除, 原因是 ...",
                    },
                    {"role": "user", "content": "那我应该怎么改?"},
                ],
            }
        },
    )

    route_url: str = Field(
        ...,
        min_length=1,
        description="前端命中的接口路径 (跟 AskQuery 同语义); 整段对话共享一份.",
    )
    payload: dict[str, Any] = Field(
        ...,
        description="前端那条数据的字段快照 (跟 AskQuery 同语义); 整段对话共享一份.",
    )
    messages: list[ChatMessage] = Field(
        ...,
        min_length=1,
        max_length=CHAT_MAX_TURNS,
        description=(
            "对话历史, user/assistant 交替, 末条必须是 user (即客户当前这一问). "
            "首轮就是 ``[{role: user, content: '...'}]`` 一条. 总条数上限由 "
            "settings.CHAT_MAX_TURNS 控制 (默认 20 ≈ 10 轮来回); 真要超意味着客户"
            "其实该重新选一条数据问."
        ),
    )
    model: str | None = Field(
        default=None,
        description="LLM model name (内部用, 一般不传, 走默认模型).",
    )

    @model_validator(mode="after")
    def _validate_history_shape(self) -> "ChatQuery":
        """末条必须是 user; user / assistant 严格交替.

        放在 schema 层是因为这两条都是"协议契约"级别的硬约束 — 让 422 在
        FastAPI validation 阶段就抛出, pipeline 拿到的 ``messages`` 永远
        是合法形状, 不用再防御.
        """
        if self.messages[-1].role != "user":
            raise ValueError("messages 末条必须是 role=user (当前这一问)")
        for i in range(1, len(self.messages)):
            if self.messages[i].role == self.messages[i - 1].role:
                raise ValueError(
                    f"messages[{i}] 和 messages[{i - 1}] role 相同, "
                    "user / assistant 必须严格交替"
                )
        return self
