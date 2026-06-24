# Bosch AI Framework — Monorepo 迁移计划

**基准：flatten-plan.md（最新）**，结合 plan.md 命名 + reorg-plan.md 的 agents 分组 + 用户反馈。

---

## 目标结构（已实现）

```
bosch-ai-framework/
├── pyproject.toml              # uv workspace 根配置
├── infra/                      # 基础设施（来自 ainfra，8 个平铺文件）
│   ├── __init__.py
│   ├── llm.py                  # LiteLLM 网关
│   ├── auth.py                 # 鉴权
│   ├── tasks.py                # 异步任务
│   ├── tools.py                # 工具函数
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

## 完成情况

### ✅ 已完成

- [x] **Monorepo 基础结构** — uv workspace 配好 5 个 member
- [x] **infra 迁移** — `ainfra/infra/*.py` → `infra/*.py`，8 个文件平铺
- [x] **document 迁移** — `apdfi/apdfi/*` → `document/*`，`server.py` → `main.py`
- [x] **rag 迁移** — `bapee/bapee/*` → `rag/*`，`server.py` → `main.py`
- [x] **forecast 迁移** — `fcst/fcst/*` → `forecast/*`，含 alembic
- [x] **analytics 迁移** — `abi/abi/*` → `analytics/*`
- [x] **导入路径修复** — `apdfi`→`document`、`bapee`→`rag`、`fcst`→`forecast`、`abi`→`analytics`
- [x] **pyproject.toml** — 根 workspace + 5 个 member 各有自己的 pyproject.toml，依赖 `infra`
- [x] **CF manifest.yml** — 4 个 agent 各有 manifest.yml，gunicorn 指向 `main:app`
- [x] **deploy.sh** — `deployment/cf/deploy.sh`，支持单 agent / 全部
- [x] **README 文档** — 开发 / 部署 / workspace 用法
- [x] **uv sync 验证** — `uv sync --all-packages` 跑通，136 包安装成功
- [x] **import 验证** — infra ✅ / document ✅ / forecast ✅ / analytics ✅ / rag ⚠️

### 验证中修复的问题

| 问题 | 修复 |
|------|------|
| `uv sync --dev` 只装 dev-deps，不装 agent 外部依赖 | 改为 `uv sync --all-packages` |
| agent 的 `"infra"` 依赖找不到 workspace member | 每个 agent 加 `[tool.uv.sources] infra = { workspace = true }` |
| `uv dev-dependencies` 已废弃 | 根 pyproject.toml 改为 `[dependency-groups] dev` |
| document 缺 `pymupdf4llm`（`from pdf.pipeline.vlm import ask_vlm` 失败） | document/pyproject.toml 补上 |
| rag import 链路正确但 Windows torch DLL 失败 | 非代码问题，Linux/CF 部署不受影响 |

---

### ❌ 未完成

- [ ] **服务启动验证** — import 过了但没实际 `uv run python run.py` 启动服务看 HTTP 响应
- [ ] **forecast alembic 路径** — `fcst.db` → `forecast.db` 改了 `alembic.ini`，但 `alembic/env.py` 和 `database.py` 里的硬编码路径没查
- [ ] **CF 部署验证** — manifest.yml 没在 CF 上实际 `cf push` 跑过
- [ ] **BTP service binding** — rag manifest.yml 里 service instance 名还是 `bapee-*`，如果需要新建 rag 专属实例要改
- [ ] **原始目录 `__pycache__`** — 迁移时已删 agent 内的，但原始目录残留 `.pyc` 未清理

---

## 架构演进方向（未开始）

### RAG Agent 通用化
- AST/KB 构建（`rag/rag/tools/build_kb_ast.py`）拆为独立仓库
- 传入 git 地址即可用通用 RAG agent 分析
- RAG Agent 只负责检索/问答，KB 构建由外部工具提供

### Function Generator 框架化
- `forecast/core/agent.py` + `orchestrator.py` 抽取通用函数生成框架
- forecast 降级为子 agent / 技能预设之一
- 未来可扩展其他业务场景

### Analytics 服务
- 当前是骨架（API 路由 + agent 核心），AI BI / NL2SQL 逻辑待实现

### 代码去重
- `document/chat/state.py` `registry.py` vs `rag/` 类似模块 — 确认是否有重复
- `document/excel/date_normalize.py` 是否该提到 infra
- 各 agent 的 `auth.py` `llm.py` 是否可用 infra 版本统一

---

## 验证清单

```
[x] uv sync --all-packages（136 packages）
[x] from infra.llm / .auth / .settings / .tasks / .utils import OK
[x] from document.main import app OK
[~] from rag.main import app              import 链正确，Windows torch DLL 问题
[x] from forecast.main import app OK
[x] from analytics.main import app OK
[ ] uv run python run.py 启动 document    服务能响应 HTTP
[ ] uv run python run.py 启动 rag         服务能响应 HTTP
[ ] uv run uvicorn 启动 forecast          服务能响应 HTTP
[ ] uv run uvicorn 启动 analytics         服务能响应 HTTP
[ ] cf push 每个 agent 部署成功
[ ] 原始目录 ainfra/apdfi/bapee/fcst/abi 未被修改
```
