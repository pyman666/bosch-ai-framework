# CLAUDE.md — Bosch AI Framework 开发指南

## 架构

```
bosch-ai-framework/          # monorepo，uv workspace
├── infra/                   # 共享 AI 框架（library package）
│   ├── llm/                 #   LLM 抽象（chat/stream/router）
│   ├── agent/               #   Agent 框架（BaseAgent/Tool/AgentLoop）
│   ├── skill/               #   Skill 注册表
│   ├── task/                #   任务管理（create/get/set_phase）
│   ├── auth.py              #   HTTP Basic + XSUAA 鉴权
│   ├── settings.py + .yaml  #   YAML + env 配置，所有 agent 共享
│   ├── logs.py              #   Gunicorn JSON 日志（-c ../infra/logs.py）
│   ├── btp.py               #   BTP/CF VCAP_SERVICES 解析
│   ├── observability.py     #   JsonFormatter + RequestIDMiddleware
│   └── utils.py             #   exception_detail + utcnow
├── document/                # 文档解析 agent
│   ├── chat/ excel/ pdf/    #   领域逻辑
│   └── main.py              #   FastAPI 入口
├── rag/                     # RAG 知识库 agent
│   ├── core/                #   llm.py, ratelimit.py（领域特有）
│   ├── chatbot/ rag/        #   领域逻辑
│   └── main.py
├── forecast/                # 预测 agent
│   ├── core/                #   agent.py, executor.py, memory.py, orchestrator.py...
│   ├── skills/ routes/      #   领域逻辑
│   └── main.py
├── analytics/               # BI 分析 agent
│   ├── core/                #   agent.py, tools.py, bff_client.py, session.py...
│   ├── api/ models/         #   领域逻辑
│   └── main.py
└── deployment/cf/           # CF 部署脚本
```

**核心规则：infra = library package，每个 agent = 独立 service。** Infra 不感知 agent，agent 只依赖 infra，agent 之间不互相 import。

### Infra 模块消费者矩阵

每个 infra 模块都至少被 2 个 agent 使用（否则留在 agent 内，YAGNI）：

| 模块 | 消费者 |
|------|--------|
| `llm/` | document, rag, forecast, analytics |
| `agent/` | forecast, analytics |
| `skill/` | forecast |
| `task/` | document, forecast |
| `auth.py` | document, rag, forecast, analytics |
| `settings.py` | document, rag, forecast, analytics |
| `logs.py` | document, rag, forecast, analytics（gunicorn -c） |
| `btp.py` | rag, infra/settings.py |
| `observability.py` | rag, infra/logs.py |
| `utils.py` | document, rag |

> 已删除的 YAGNI：`agent/planner.py`、`agent/memory.py`、`agent/executor.py`（纯 ABC，零实现，零引用）。

---

## 设计原则（写代码前读一遍）

### 1. 不要重复造轮子

在 agent 里写新功能前，先确认 infra 有没有。如果 infra 有类似能力但不够用 → 扩充 infra，不要 copy-paste。

```python
# ✅ agent 里只 import infra
from infra.llm import chat
from infra.auth import require_auth
from infra.observability import JsonFormatter

# ❌ agent 里自建 LLM client / auth / logging
```

### 2. 不要过度抽象

**YAGNI。** 只有一个 agent 用到的东西，长在 agent 里就好，别往 infra 塞。等第二个 agent 也需要时再提取。

```python
# ✅ forecast 独用的沙箱 → 留在 forecast/core/executor.py
# ✅ rag 独用的 rate limiter → 留在 rag/core/ratelimit.py
# ✅ analytics 独用的 session/http_client → 留在 analytics/core/
# ❌ 不管有没有第二个用户，先抽到 infra 再说
```

判断标准：问自己"其他 agent 半年内真会用到吗？"答不上来就别动。

### 3. 高内聚低耦合

- **agent `core/` 目录规则**：只放领域逻辑（领域 Agent 子类、领域 Tool 实现、领域 Skill、领域编排器）。框架层（BaseAgent、ToolRegistry、LLM client、Auth、Settings、Logging）必须在 infra。
- **agent 之间不能互相 import**。Agent 是独立 service，互不感知。

### 4. Package 和 Service 分离

- `infra/` = library package，pip install -e 引用
- 每个 `agent/` = 独立 CF App，独立部署、独立扩缩容、互不影响
- 部署时 `cd <agent> && gunicorn <agent>.main:app -c ../infra/logs.py`

### 5. 优雅 Pythonic

- `| None` 不用 `Optional`，`from __future__ import annotations`
- dataclass / Pydantic 做数据容器，不用裸 dict
- `logging.getLogger(__name__)`，不用 print
- JSON 日志统一用 `infra.observability.JsonFormatter`

---

## 常见操作

```bash
uv sync --all-packages          # 装全部依赖
uv run pytest                   # 跑测试
uv run ruff check . && uv run ruff format .  # lint + format
```

## 部署

```bash
./deployment/cf/deploy.sh rag       # 部署单个 agent
./deployment/cf/deploy.sh all       # 全部
```

## 目录规范速查

| 允许在 agent `core/` | 禁止（应在 infra） |
|----------------------|-------------------|
| 领域 Agent 子类 (ForecastAgent) | AgentLoop / BaseAgent |
| 领域 Tool 实现 (preview_data, analyze_data) | ToolRegistry / Tool 基类 |
| 领域 Skill 实现 (moving_average, jitcall) | SkillRegistry / Skill 基类 |
| 领域 Orchestrator (chat → skill) | LLM client / Router |
| 领域 Session / HTTP client（独用） | AgentMemory 接口 |
| 领域 Executor / 沙箱（独用） | 通用 Auth / Settings / Logging |

---

## 待办

- [ ] **rag/core/llm.py** — AI Core 集成（AICoreTokenProvider, AICoreRouter）应移至 `infra/llm/aicore.py`。当前仅 rag 用，等第二个 agent 接入 AI Core 时提取。
- [ ] **BTP service binding 名称** — rag manifest.yml 里 service instance 名还是 `bapee-*`（对应实际 BTP 实例，暂不改）。
- [ ] **CI 实际跑通** — `.github/workflows/ci.yml` 没在 GitHub 上触发过。
- [ ] **CF 部署验证** — manifest.yml 没实际 `cf push` 跑过。
