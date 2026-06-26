# CLAUDE.md — Bosch AI Framework 开发指南

## 架构

```
bosch-ai-framework/          # monorepo，uv workspace
├── infra/                   # 共享 AI 框架（library package）
│   ├── llm/                 #   LLM 抽象 — 4 agents 用
│   ├── agent/               #   Agent 框架（BaseAgent/Tool/AgentLoop）— 2 agents 用
│   ├── skill/               #   Skill 注册表 — 1 agent + infra/agent 用
│   ├── task/                #   任务管理 — 2 agents 用
│   ├── auth.py              #   HTTP Basic + XSUAA — 4 agents 用
│   ├── settings.py + .yaml  #   YAML + env 配置 — 所有 agent 共享
│   ├── logs.py              #   Gunicorn JSON 日志 — 4 agents 用
│   ├── btp.py               #   BTP VCAP_SERVICES 解析 — 1 agent + infra/settings 用
│   ├── observability.py     #   JsonFormatter + RequestIDMiddleware — 1 agent + infra/logs 用
│   └── utils.py             #   exception_detail + utcnow — 3 agents 用
├── document/                # 文档解析 agent
├── rag/                     # RAG 知识库 agent（ratelimit.py 领域特有）
├── forecast/                # 预测 agent（沙箱、memory、orchestrator 均领域逻辑）
├── analytics/               # BI 分析 agent（session、http_client、chart 均领域逻辑）
└── deployment/cf/           # CF 部署脚本
```

**核心规则：infra = library package，每个 agent = 独立 service。** Infra 不感知 agent，agent 只依赖 infra，agent 之间不互相 import。

---

## 提取到 infra 的硬规则

**只有当 ≥2 个 agent 真正用到时，才从 agent 搬到 infra。** 只有一个 agent 用就留在 agent 里，等第二个 agent 需要时再搬。

搬到 infra 前先问自己：

1. 现在有几个 agent 用了？少于 2 → 不搬。
2. 第二个 agent 半年内真会用到吗？不确定 → 不搬。
3. 搬过去以后 infra 是不是仍然不依赖任何 agent？依赖了 → 不搬。

此规则无例外。宁可 infra 瘦，不要 infra 胖。

---

## 设计原则

### 1. 不要重复造轮子

写新功能前，先确认 infra 有没有。有但不够用 → 扩充 infra，不要 copy-paste。

```python
# ✅ agent 里只 import infra
from infra.llm import chat
from infra.auth import require_auth

# ❌ agent 里自建 LLM client / auth / logging
```

### 2. 不要过度抽象（YAGNI）

等有第二个消费者再提取。不是"可能会用到"就搬。

```python
# ✅ forecast 独用的沙箱 → forecast/core/executor.py
# ✅ rag 独用的 rate limiter → rag/core/ratelimit.py
# ✅ analytics 独用的 session/http_client → analytics/core/
# ❌ 先抽到 infra 再说
```

### 3. 高内聚低耦合

- Agent `core/` 只放领域逻辑。框架层（BaseAgent、ToolRegistry、LLM、Auth、Settings、Logging）必须在 infra。
- **Agent 之间不能互相 import**。每个 agent 是独立 service，互不感知。

### 4. Package 和 Service 分离

- `infra/` = library package，pip install -e 引用
- 每个 `agent/` = 独立 CF App，独立部署、独立扩缩容
- 部署：`cd <agent> && gunicorn <agent>.main:app -c ../infra/logs.py`

### 5. 优雅 Pythonic

- `| None` 不用 `Optional`，`from __future__ import annotations`
- dataclass / Pydantic 做容器，不用裸 dict
- `logging.getLogger(__name__)` 不用 print
- JSON 日志统一用 `infra.observability.JsonFormatter`

---

## 目录规范速查

| 允许在 agent `core/` | 禁止（应在 infra） |
|----------------------|-------------------|
| 领域 Agent 子类 | BaseAgent / AgentLoop |
| 领域 Tool 实现 | ToolRegistry / Tool 基类 |
| 领域 Skill 实现 | SkillRegistry / Skill 基类 |
| 领域 Orchestrator | LLM client / Router |
| 领域 Session / HTTP client（独用） | 通用 Auth / Settings / Logging |
| 领域沙箱 / rate limiter（独用） | 多 agent 共用的中间件 |

---

## 常见操作

```bash
uv sync --all-packages          # 装全部依赖
uv run pytest                   # 跑测试
uv run ruff check . && uv run ruff format .  # lint + format
./deployment/cf/deploy.sh all   # 部署全部
```

---

## 待办

- [x] **rag/core/llm.py** — 已删除，AI Core 移除，rag 统一用 infra.llm
- [x] **rag/settings.py** — 改用 infra.settings 的 MODEL_LIST/DEFAULT_MODEL/ROUTER_KWARGS
- [ ] **BTP service binding 名称** — rag manifest 里还是 `bapee-*`（对应实际 BTP 实例，暂不改）。
- [ ] **CI / CF 部署验证** — 没实际跑过。
