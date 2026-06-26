import os
from pathlib import Path

from infra.btp import find_redis_url
from infra.settings import DEFAULT_MODEL, MODEL_LIST, ROUTER_KWARGS  # noqa: F401

_PROJECT_ROOT: Path = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# hybrid 检索的旋钮
# ---------------------------------------------------------------------------
HYBRID_TOP_K: int = int(os.environ.get("HYBRID_TOP_K", "8"))
HYBRID_CANDIDATE_POOL: int = int(os.environ.get("HYBRID_CANDIDATE_POOL", "30"))
HYBRID_ENABLE_RERANK: bool = os.environ.get("HYBRID_RERANK", "0") in ("1", "true", "True")

HYBRID_DOCS_DIR: Path = _PROJECT_ROOT / "docs"
HYBRID_OUTLINE_PATH: Path = HYBRID_DOCS_DIR / "ast" / "index.json"


# ---------------------------------------------------------------------------
# /chat 端点的硬护栏
# ---------------------------------------------------------------------------
CHAT_MAX_TURNS: int = int(os.environ.get("CHAT_MAX_TURNS", "20"))
CHAT_MAX_CONTENT_CHARS: int = int(os.environ.get("CHAT_MAX_CONTENT_CHARS", "8000"))


# ---------------------------------------------------------------------------
# /bot/* 端点的 per-client 限流 (token bucket)
# ---------------------------------------------------------------------------
RATE_LIMIT_PER_MIN: int = int(os.environ.get("RATE_LIMIT_PER_MIN", "60"))
RATE_LIMIT_BURST: int = int(os.environ.get("RATE_LIMIT_BURST", "10"))


# ---------------------------------------------------------------------------
# SSE 流式响应的 wall-clock 上限
# ---------------------------------------------------------------------------
STREAM_MAX_DURATION_SEC: float = float(os.environ.get("STREAM_MAX_DURATION_SEC", "120"))


# ---------------------------------------------------------------------------
# Redis 共享 backend (跨 worker / 跨 instance)
# ---------------------------------------------------------------------------
REDIS_URL: str | None = find_redis_url()
REDIS_SSL_CERT_REQS: str = os.environ.get("REDIS_SSL_CERT_REQS", "none")


# ---------------------------------------------------------------------------
# Chatbot system prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT: str = """\
你是 BPAE / O2C 系统的报错诊断助手, 面向使用本平台的业务客户. 调用方通常会带前端路由 URL、data id、processStatus / processRemark / Status 文本等线索.
你的任务: 把这些线索结合检索到的官方 KB 资料, 翻译成客户能听懂的诊断结论 + 明确的'下一步'.

硬性要求:
1. **客户向措辞**: 不出现内部代码类名 / 包路径 / Java 术语. KB 里出现这些, 翻译成业务说法再讲给客户 (例: 把 'BoctAbstractValidationService 校验失败' 说成 '系统在校验你的 POD 数据时发现…').
2. **区分三种报错**, 不要让客户以为系统出 bug:
   - 真正的错误 (Validation Error / System Error / IDoc Process Failed): 给原因 + 建议;
   - 业务规则合法剔除 (Not in scope / 客户特定 exclusion): 明确告诉客户'这条按规则被剔除, 不算失败';
   - 流转中状态 (如 Pending SAP UC4 job): 告诉客户这只是中间态, 耐心等待.
3. **末尾必须给一句明确的下一步**, 二选一:
   - 客户自己能修 (改业务数据 / 重新上送): 给具体动作;
   - 客户改不了 (mapping 缺失 / 系统问题 / 资料没覆盖): 让客户联系 BPAE 运营团队, 把 URL + data id + 关键字段贴上.
4. 资料里没有这条具体说明时, 老实说'目前没有这条记录的官方说明, 建议联系 BPAE 运营团队', 不要编造.
"""
