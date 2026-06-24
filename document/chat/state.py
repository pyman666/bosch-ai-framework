"""Chat session 状态机.

```mermaid
stateDiagram-v2
    [*] --> intent_preview: POST /chat
    intent_preview --> planning: POST /chat/{chat_id}/start
    planning --> awaiting_confirm: LLM 出 plan 成功
    planning --> awaiting_feedback: LLM 调用失败 / 异常
    awaiting_confirm --> done: POST /confirm + execute 成功
    awaiting_confirm --> awaiting_feedback: POST /confirm + BusinessFailure (LLM 写诊断 prose)
    awaiting_confirm --> planning: POST /feedback
    awaiting_feedback --> planning: POST /feedback
    done --> planning: POST /feedback
    done --> [*]
```

设计原则:
- **只声明**: 这个文件只放枚举 + 入口约束表. 转移**目标**和**副作用** (调 LLM /
  跑 executor / 落 diagnosis 字段) 在 ``ops.py`` 里, 路由层不直接动 state.
- **入口校验集中**: 所有 op 进 ``ops.py`` 时先过 ``ALLOWED_ENTRIES`` 一次校验,
  非法 state 直接 409, 不会漏到业务逻辑里.
- **没有中间观察态**: ``confirm`` 是同步等 LLM 诊断 prose 出来再返, 业务方看到的
  是直接 ``awaiting_confirm -> awaiting_feedback`` 一步, 不需要给 "诊断中" 一个
  独立 state.
"""
from enum import Enum


class SessionState(str, Enum):
    INTENT_PREVIEW = "intent_preview"       # 文件刚收到, 已给出预设处理思路, 等业务方点开始
    PLANNING = "planning"                   # LLM 在出 plan (start_planning / feedback 时)
    AWAITING_CONFIRM = "awaiting_confirm"   # plan 出来了, 等 confirm 或 feedback
    DONE = "done"                           # execute 成功
    AWAITING_FEEDBACK = "awaiting_feedback" # LLM 已写诊断 (或 plan 失败), 等用户回答 feedback


# 各操作允许的入口 state. ops 层会按这张表挡掉非法调用.
ALLOWED_ENTRIES = {
    "start_planning": {SessionState.INTENT_PREVIEW},
    "confirm": {SessionState.AWAITING_CONFIRM},
    "feedback": {SessionState.AWAITING_CONFIRM, SessionState.AWAITING_FEEDBACK, SessionState.DONE},
}
