# CLAUDE.md — Bosch AI Framework 开发指南

## 架构

```
bosch-ai-framework/          # monorepo，uv workspace
├── infra/                   # 共享 AI 框架（library package）
│   ├── llm/                 #   LLM 抽象（chat/stream/router）
│   ├── agent/               #   Agent 框架（BaseAgent/Tool/AgentLoop）
│   ├── skill/               #   Skill 注册表
│   ├── task/                #   任务管理
│   ├── auth.py              #   HTTP Basic + XSUAA 鉴权
│   ├── settings.py          #   YAML + env 配置
│   ├── logs.py              #   Gunicorn JSON 日志（-c ../infra/logs.py）
│   ├── btp.py               #   BTP/CF VCAP_SERVICES 解析
│   ├── observability.py     #   JsonFormatter + RequestIDMiddleware
│   ├── http_client.py       #   通用 HTTP 客户端
│   ├── session.py           #   会话管理（Session/SessionStore）
│   └── utils.py             #   杂项工具
├── document/                # 文档解析 agent（独立 CF App）
├── rag/                     # RAG 知识库 agent（独立 CF App）
├── forecast/                # 预测 agent（独立 CF App）
├── analytics/               # BI 分析 agent（独立 CF App）
└── deployment/cf/           # CF 部署脚本
```

**核心规则：infra = library package，每个 agent = 独立 service。** Infra 不感知任何 agent，agent 只依赖 infra。

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
# ❌ 不管有没有第二个用户，先抽到 infra 再说
```

判断标准：问自己"其他 agent 半年内真会用到吗？"答不上来就别动。

### 3. 高内聚低耦合

- **agent `core/` 目录规则**：只放领域逻辑（领域 Agent 子类、领域 Tool 实现、领域 Skill、领域编排器）。框架层的东西（BaseAgent、ToolRegistry、LLM client、Auth、Settings）必须在 infra。
- **agent 之间不能互相 import**。Agent 是独立 service，互不感知。

### 4. Package 和 Service 分离

- `infra/` = library package，被所有 agent `pip install -e` 引用
- 每个 `agent/` = 独立 CF App，独立部署、独立扩缩容、互不影响
- 部署时 `cd <agent> && gunicorn <agent>.main:app -c ../infra/logs.py`

### 5. 优雅 Pythonic

- Type hints 用 `| None` 而不是 `Optional`（3.10+）
- `from __future__ import annotations` 放文件顶
- dataclass / Pydantic 做数据容器，不用裸 dict 传来传去
- 用 `logging.getLogger(__name__)` 不用 print
- 用 `infra.observability.JsonFormatter` 输出 JSON 日志，不用自建 formatter

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
| 领域 Agent 子类 | AgentLoop / BaseAgent |
| 领域 Tool 实现 | ToolRegistry / Tool 基类 |
| 领域 Skill 实现 | SkillRegistry / Skill 基类 |
| 领域编排器 | LLM client / Router |
| 领域 Memory | AgentMemory 接口 |
| 领域 Executor | 通用 Auth / Settings / Logging |

> 存量违规：`rag/core/llm.py`（AI Core 应在 infra/llm/aicore.py）、`rag/core/ratelimit.py`（等第二个用户）、`forecast/core/memory.py`（async 接口不匹配）。

---

## 待办

- [ ] **rag/core/llm.py** — AI Core 集成 → `infra/llm/aicore.py`
- [ ] **rag/core/ratelimit.py** — 等第二个 agent 需要限流时提取到 infra
- [ ] **forecast/core/memory.py** — `AgentMemory` 接口是 async，forecast 是同步 I/O，先统一
- [ ] **BTP service binding 名称** — rag manifest 里还是 `bapee-*`（对应实际 BTP 实例，暂不改）
- [ ] **CI 实际跑通** — `.github/workflows/ci.yml` 没在 GitHub 上触发过
- [ ] **CF 部署验证** — manifest.yml 没实际 `cf push` 跑过
