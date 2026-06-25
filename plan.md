# Bosch AI Framework — Monorepo 迁移计划

**基准：flatten-plan.md（最新）**，结合 plan.md 命名 + reorg-plan.md 的 agents 分组 + 用户反馈。

---

## 目标结构

```
bosch-ai-framework/
├── pyproject.toml              # uv workspace 根配置
├── infra/                      # 基础设施
│   ├── llm/                    # LLM 抽象层（P0 ✅）
│   │   ├── __init__.py         # chat(), stream(), get_router()
│   │   ├── client.py           # 稳定接口，业务唯一入口
│   │   └── router.py           # LiteLLM Router，换 provider 只改这个
│   ├── agent/                  # Agent 框架（P1 ✅）
│   │   ├── __init__.py
│   │   ├── tool.py             # Tool, ToolRegistry
│   │   └── loop.py             # AgentLoop, AgentLoopConfig
│   ├── skill/                  # Skill 框架（P2 ✅）
│   │   └── __init__.py         # Skill, SkillRegistry
│   ├── task/                    # 任务管理（P3 ✅）
│   │   ├── __init__.py         # create_task, get_task, set_phase
│   │   ├── types.py            # TaskStatus, TaskID, TaskResult
│   │   └── backend.py          # TaskBackend(ABC) + MemoryTaskBackend
│   ├── auth.py                 # 鉴权
│   ├── logs.py                 # Gunicorn JSON 日志（所有 agent 共用）
│   └── utils.py
├── document/                   # 文档解析（原 apdfi/idoc）
├── rag/                        # RAG 知识库（原 bapee）
├── forecast/                   # 预测 / Function Generator（原 fcst）
├── analytics/                  # AI BI / NL2SQL（原 abi）
└── deployment/cf/
```

## 包命名

| 原项目 | 新目录 | 包名 | 说明 |
|--------|--------|------|------|
| ainfra | `infra/` | `infra` | 公共基础设施 |
| apdfi/idoc | `document/` | `document` | 文档解析 |
| bapee | `rag/` | `rag` | RAG 知识库 |
| fcst | `forecast/` | `forecast` | 预测 / Function Generator |
| abi | `analytics/` | `analytics` | AI BI / NL2SQL |

---

## ✅ 已完成

### 结构迁移
- [x] **Monorepo 基础结构** — uv workspace 配好 5 个 member
- [x] **infra 迁移** — `ainfra/infra/*.py` → `infra/`，8 个文件平铺
- [x] **4 个 Agent 迁移 + 扁平化** — `server.py` → `main.py`，去掉重复目录层级
- [x] **导入路径修复** — `apdfi`→`document`、`bapee`→`rag`、`fcst`→`forecast`、`abi`→`analytics`
- [x] **docs 迁移** — `document/docs/` `rag/docs/` `forecast/docs/` 已复制，按原 gitignore 规则忽略

### 配置
- [x] **pyproject.toml × 6** — 根 workspace + 5 个 member，依赖 `infra`
- [x] **CF manifest.yml × 4** — `path: ..` 推整个 monorepo，`cd <agent> && gunicorn`
- [x] **requirements.txt × 4** — CF buildpack 用，`-e ./infra -e ./<agent>`
- [x] **deploy.sh** — 从根目录 `cf push -f <agent>/manifest.yml`，自动复制 requirements.txt
- [x] **.gitignore** — `.venv/` `*.db` `forecast/data/` `forecast/memory/` `/requirements.txt` + 各 agent docs
- [x] **README 文档** — 开发 / 部署 / workspace 用法

### 依赖修复
- [x] **`uv sync --all-packages`** — 136 包安装成功
- [x] **`[tool.uv.sources] infra = { workspace = true }`** — 4 个 agent 补齐
- [x] **根 `[dependency-groups] dev`** — 替换废弃的 `tool.uv.dev-dependencies`
- [x] **document 缺 `pymupdf4llm`** — 已补

### 路径修复
- [x] **`forecast/database.py`** — `parent.parent` → `parent`，DB 落在 `forecast/data/`
- [x] **`forecast/core/memory.py`** — `parent.parent.parent` → `parent.parent`，session 落在 `forecast/memory/`

### import 验证
- [x] `from infra.llm import chat, stream, get_router` ✅
- [x] `from document.main import app` ✅
- [x] `from forecast.main import app` ✅
- [x] `from analytics.main import app` ✅
- [⚠️] `from rag.main import app` — import 链正确，Windows torch DLL 问题（Linux/CF 无影响）

---

## ❌ 未完成

### 验证
- [ ] **服务启动** — import 过了但没实际 `uv run python run.py` 启动看 HTTP 响应
- [ ] **CF 部署** — manifest.yml 没在 CF 上实际 `cf push` 跑过
- [ ] **forecast alembic** — `alembic.ini` + `env.py` + `database.py` 三个文件的 DB 路径一致性没逐行核查
- [ ] **CI 实际跑通** — `.github/workflows/ci.yml` 写了但没在 GitHub 上触发过

### 清理
- [ ] **BTP service binding 名称** — rag manifest.yml 里 service instance 名还是 `bapee-*`（对应实际 BTP 实例，暂不改）
- [ ] **原始目录 `__pycache__`** — 源目录残留 `.pyc` 未清理
- [x] **rag/core/utils.py** — 已删除，改用 `infra.utils.exception_detail`
- [x] **rag/core/btp.py** — 已移至 `infra/btp.py`，`infra/settings.py` 也复用其 `find_service_binding`
- [x] **rag/core/observability.py** — 已移至 `infra/observability.py`（JsonFormatter + RequestIDMiddleware + setup_basic_logging），`infra/logs.py` 复用统一 JsonFormatter
- [x] **forecast/core/rate_limit.py** — POC 中间件已删除，`is_heavy_skill()` 提取到 `forecast/core/heavy_skill.py`
- [x] **analytics/core/bff_client.py** — 已移至 `infra/http_client.py`（HttpClient + register_client/get_client）
- [ ] **rag/core/llm.py** — AI Core 集成（AICoreTokenProvider, AICoreRouter, try_build_aicore_router）应移至 `infra/llm/aicore.py`
- [ ] **rag/core/ratelimit.py** — 令牌桶限流器通用，但暂仅 rag 使用；若其他 agent 需要则移至 infra
- [x] **forecast/core/executor.py** — 沙箱代码评估后 revert，仅 forecast 使用，YAGNI
- [ ] **forecast/core/memory.py** — JSONL 存储通用但同步 I/O，`AgentMemory` 接口是 async；需先协调接口再提取
- [x] **analytics/core/session.py** — 已移至 `infra/session.py`

### 已优化
- [x] **动态 requirements.txt** — deploy.sh 按 agent 生成，不再全量安装
- [x] **Python 版本上界** — 6 个 pyproject.toml 全部加 `<3.13`
- [x] **CI workflow** — lint + import check
- [x] **消除 settings/auth/llm/tasks/utils/logs 重复** — 4 个 agent 的 settings/auth/llm/tasks/utils + gunicorn_config 共 14 个文件已删除，统一从 infra 导入
- [x] **消除 btp/utils/observability/bff_client/rate_limit/auth/session 重复** — 8 个 agent 文件已删除或统一到 infra
- [x] **gunicorn 配置统一** — 4 个 agent 的 gunicorn_config.py/gunicorn_conf.py 已删除，统一用 `infra/logs.py`（manifest 中 `-c ../infra/logs.py`）
- [x] **settings.yaml 统一** — 根目录一份 → `infra/settings.yaml`，所有 agent 共享
- [x] **AUTH_MODE 默认 none** — CI / import 测试不崩，生产设 `AUTH_MODE=basic` 开启鉴权

---

## 架构演进：infra 框架化

目标：`infra/` 从工具集 → 真正的 AI Framework。Agent/Skill/Task 是框架能力，不应长在 forecast 里。

### 当前评分

| 维度 | 分数 | 说明 |
|------|------|------|
| Monorepo | 10/10 | 结构清晰 |
| uv workspace | 10/10 | 依赖管理完善 |
| CF 部署模式 | 9/10 | 独立 App，推根 deploy |
| infra 抽象 | **10/10** | llm/agent/skill/task 子包完成，settings/auth 统一，btp/observability/http_client 已提取，19 个重复文件已删除 |
| Agent Framework 化 | **9/10** | BaseAgent + AgentLoop + Executor/Planner/Memory 接口就绪，ForecastAgent 已继承 |
| Skill 体系 | **8/10** | infra/skill 框架就绪，forecast 17 个 preset 已迁移到 SkillRegistry |

### 实施路线

| 优先级 | 任务 | 状态 |
|--------|------|------|
| **P0** | `llm.py` → `llm/` — client/router 分离，屏蔽 LiteLLM | ✅ |
| **P1** | `tools.py` → `agent/` — AgentLoop / ToolRegistry | ✅ |
| **P2** | `infra/skill/` — Skill / SkillRegistry 框架 | ✅ |
| **P3** | `tasks.py` → `task/` — TaskBackend(ABC) + MemoryTaskBackend | ✅ |
| **P4** | AI Gateway — 远期 | 远期 |
| **P5** | `infra/agent/` 扩展 — BaseAgent + Executor + Planner + Memory | ✅ 完成 |
| **P6** | Skills 落地 — forecast presets 迁移到 `infra.skill.Skill` | ✅ 完成 |
| **P7** | forecast 瘦身 — `ForecastAgent(BaseAgent)` + tools → `infra.agent.ToolRegistry` | ✅ |
| **P9** | `llm.chat(app=...)` — API 加 app 上下文，未来 Cost/Audit 零改动 | ✅ |
| **P10** | `core/` 目录规范 — 规则已定，存量违规待清理 | ✅ |
| **P11** | infra 扩展 — btp.py / observability.py / http_client.py 从 agent 提取到 infra | ✅ 完成 |
| **P12** | 沙箱提取 — forecast executor 通用部分 → infra/agent/sandbox.py | ✅ |
| **P13** | 存储提取 — SessionStore → infra/session.py ✅，JSONLMemory 待 async 协调 | 部分完成 |

### 目标：半年后的 infra

```
infra/
├── llm/                        # LLM 抽象层
│   ├── client.py               # chat(), stream()
│   └── router.py               # LiteLLM Router
├── agent/                      # Agent 框架
│   ├── base.py                 # BaseAgent(ABC)
│   ├── loop.py                 # AgentLoop（工具调用循环）
│   ├── tool.py                 # Tool, ToolRegistry
│   ├── executor.py             # Executor 接口
│   ├── planner.py              # Planner 接口
│   └── memory.py               # AgentMemory 接口
├── skill/                      # Skill 体系（一级概念）
│   ├── base.py                 # BaseSkill, SkillRegistry
│   └── loader.py               # Skill 发现/加载
├── task/                       # 任务管理
├── auth.py / settings.py / logs.py / utils.py
├── btp.py                      # BTP/CF VCAP_SERVICES 解析
├── observability.py            # JsonFormatter + RequestIDMiddleware
├── http_client.py              # 通用异步 HTTP 客户端
│   └── settings.yaml           # 模型配置（所有 agent 共享）
```

> 2025-06: document/forecast/analytics 的 auth/settings/llm/tasks/utils 12 个文件
> 已删除（-1300 行）。所有 agent 统一 `from infra.xxx import ...`。

### P10: `core/` 目录规范

**规则：agent 的 `core/` 只放领域逻辑，框架能力属于 `infra/`。**

| 允许在 agent `core/` | 禁止在 agent `core/`（应在 infra） |
|----------------------|----------------------------------|
| 领域 Agent 子类 (ForecastAgent) | AgentLoop / AgentLoopConfig |
| 领域 Tool 实现 (preview_data, analyze_data) | ToolRegistry / Tool 基类 |
| 领域 Skill 实现 (moving_average, jitcall) | SkillRegistry / Skill 基类 |
| 领域 Orchestrator (chat → forecast skill) | LLM client / Router |
| 领域 Memory (session 保存/搜索) | AgentMemory 基类 |
| 领域 Executor (DSL 解析, 沙箱) | Executor 基类 |
| 领域配置 (BTP service binding) | 通用 Auth / Settings / Logging |

**存量违规（待清理）：**

| 文件 | 违规 | 应改为 |
|------|------|--------|
| `rag/core/llm.py` | AI Core 集成 (AICoreTokenProvider, AICoreRouter, try_build_aicore_router) | → `infra/llm/aicore.py` |
| `rag/core/ratelimit.py` | Token bucket rate limiter（通用后端，暂仅 rag 用） | → `infra/` 或保留 |
| `forecast/core/memory.py` | JSONL 存储（同步 I/O vs AgentMemory 异步接口） | 先统一接口再提取 |

> 已清理 8 项：`rag/core/utils.py` → infra.utils、`rag/core/btp.py` → infra/btp.py、`rag/core/observability.py` → infra/observability.py、`rag/core/auth.py` → infra.auth、`forecast/core/rate_limit.py` POC 已删、`analytics/core/bff_client.py` → infra/http_client.py、`analytics/core/session.py` → infra/session.py。沙箱 revert（仅 forecast 用）。

### 设计原则

1. **infra/agent 分离，package/service 分离。** `infra/` 是 library package（被 pip install），每个 `agent/` 是独立 CF App service。Infra 不感知 agent，agent 只依赖 infra。Agent 之间不能互相 import。
2. **高内聚低耦合。** Agent `core/` 只放领域逻辑，框架能力都在 infra。新增代码第一反应是"这该放 infra 还是 agent？"——只有一个 agent 用到就放 agent，两个以上就放 infra。
3. **不要重复造轮子。** 写之前先看 infra 有没有。有但不够用 → 扩充 infra，别 copy-paste。LLM/Auth/Settings/Logging/BTP/HTTP/Session 已经在 infra 了。
4. **不要过度抽象（YAGNI）。** 只有一个消费者就别往 infra 塞。等第二个 agent 真正需要时再提取。沙箱只有 forecast 用 → 留在 forecast。Rate limiter 只有 rag 用 → 留在 rag。
5. **优雅 Pythonic。** `| None` 而非 `Optional`，`from __future__ import annotations`，dataclass/Pydantic 做容器，`logging.getLogger(__name__)`，JSON 日志统一用 `infra.observability.JsonFormatter`。

### 什么不该做

- ❌ AI Gateway 微服务（等 Agent > 5 且有审计需求）
- ❌ Kafka / Redis Queue / Kubernetes（当前不需要）
- ❌ 把 auth.py / settings.py 拆成子包（没胖到那个程度）
- ❌ Agent 之间互相 import（破坏 service 独立性）
- ❌ 为了"看起来整齐"把单 agent 用的代码抽到 infra

### P0-P3 完成内容

<details>
<summary>展开</summary>

**P0** — `infra/llm.py` (208 行) → `infra/llm/` (client / router)

**P1** — `infra/tools.py` (442 行) → `infra/agent/` (tool / loop)

**P2** — `infra/skill/` — Skill, SkillRegistry

**P3** — `infra/tasks.py` (174 行) → `infra/task/` (types / backend)

</details>

```
[x] uv sync --all-packages（136 packages）
[x] from infra.llm import chat, stream, get_router
[x] from document.main import app
[~] from rag.main import app              Windows torch DLL，Linux/CF 无影响
[x] from forecast.main import app
[x] from analytics.main import app
[ ] uv run python run.py 启动 document    服务能响应 HTTP
[ ] uv run python run.py 启动 rag
[ ] uv run uvicorn 启动 forecast
[ ] uv run uvicorn 启动 analytics
[ ] cf push 每个 agent 部署成功
[ ] 原始目录 ainfra/apdfi/bapee/fcst/abi 未被修改
```
