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
│   ├── tools.py                # → re-export from infra.agent（向后兼容）
│   ├── skill/                  # Skill 框架（P2 ✅）
│   │   └── __init__.py         # Skill, SkillRegistry
│   ├── task/                    # 任务管理（P3 ✅）
│   │   ├── __init__.py         # create_task, get_task, set_phase
│   │   ├── types.py            # TaskStatus, TaskID, TaskResult
│   │   └── backend.py          # TaskBackend(ABC) + MemoryTaskBackend
│   ├── tasks.py                # → re-export from infra.task（向后兼容）
│   ├── settings.py             # 配置加载
│   ├── logs.py                 # JSON 日志
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

### 清理
- [ ] **BTP service binding 名称** — rag manifest.yml 里 service instance 名还是 `bapee-*`
- [ ] **原始目录 `__pycache__`** — 源目录残留 `.pyc` 未清理

---

## 架构演进：infra 框架化

目标：`infra/` 从工具集 → 真正的 AI Framework，agent 只写业务差异。

### 实施路线

| 优先级 | 任务 | 状态 |
|--------|------|------|
| **P0** | `llm.py` → `llm/` 子包（client / router 分离，屏蔽 LiteLLM） | ✅ 完成 |
| **P1** | `tools.py` → `agent/` + `tool/`（AgentLoop / ToolRegistry 独立子包） | ✅ 完成 |
| **P2** | Skills 提升到 `infra/skill/`（`skill.execute()` 通用接口） | ✅ 框架就绪 |
| **P3** | `tasks.py` → `task/`（`TaskBackend(ABC)` + `MemoryTaskBackend`） | ✅ 完成 |
| **P4** | AI Gateway（Token/Cost/Rate Limit/审计统一） | 远期 |

### P0 完成内容

```
infra/llm.py (208 行)  →  infra/llm/
├── __init__.py    # 公共 API，向后兼容
├── client.py      # chat(), stream() — 稳定接口
└── router.py      # LiteLLM Router — 换 provider 只改这个
```

向后兼容：`from infra.llm import chat, chat_stream, get_router` 代码零改动。

### 设计原则

```python
# 业务永远不写：
from litellm import completion

# 而是：
from infra.llm import chat
await chat(messages=[...])

# LiteLLM → OpenAI SDK → SAP AI Core 换底层只改 router.py，业务无感知。
```

### P1 完成内容

```
infra/tools.py (442 行)  →  infra/agent/
├── __init__.py    # 公共 API: ToolRegistry, AgentLoop, AgentLoopConfig
├── tool.py        # Tool, ToolRegistry — 工具注册与执行
└── loop.py        # AgentLoop, AgentLoopConfig — 流式/非流式循环
infra/tools.py     # → re-export from infra.agent（向后兼容）
```

向后兼容：`from infra.tools import ToolRegistry, AgentLoop` 代码零改动。
新代码直接：`from infra.agent import ToolRegistry, AgentLoop`。

### P2 完成内容

```
infra/skill/
└── __init__.py    # Skill, SkillRegistry — 声明式技能注册与执行
```

- `Skill` dataclass — name + handler + description + params + category + tags + metadata
- `SkillRegistry` — register / execute / list_skills，支持参数覆盖
- `registry.execute("skill_name", input, **overrides)` 统一调度接口
- 输入名自动 normalise（kebab-case / snake_case / 空格 → 统一）

> forecast skills/presets 尚未迁到 infra.skill — 框架就绪，业务 refactor 可独立进行。

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
