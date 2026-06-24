# Bosch AI Platform

Monorepo — 5 个 Python 包，每个独立部署为一个 Cloud Foundry App。

## 概念

```
bosch-ai-framework/          ← 一个 Git 仓库（monorepo）
├── infra/                   ← 共享基础设施包（uv workspace member）
├── document/                ← 文档解析 Agent（独立 CF App）
├── rag/                     ← RAG 知识库 Agent（独立 CF App）
├── forecast/                ← 预测 Agent（独立 CF App）
├── analytics/               ← AI BI Agent（独立 CF App）
└── deployment/cf/           ← 统一部署脚本
```

- **infra** 是公共依赖，4 个 agent 都 `from infra.llm import ...`
- **uv workspace** 管理所有包的依赖，一条命令安装全部
- **每个 agent 一个 CF App**，独立扩缩容、独立绑定服务、互不影响

## 开发环境

### 1. 安装 uv（如果没有）

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 2. 安装全部依赖

```bash
cd bosch-ai-framework
uv sync --all-packages
```

`uv sync --all-packages` 会：
- 安装根 `pyproject.toml` 里 `[tool.uv.workspace] members` 的全部 5 个包
- 安装每个包的 `dependencies`（litellm, fastapi, pandas, torch 等）
- 自动处理 workspace 内依赖（`document` 依赖 `infra` — uv 先装 infra）

装完之后，`from infra.llm import ...` 就能在任意 agent 里直接用了。

### 3. 本地启动某个 Agent

```bash
# document
cd document && uv run python run.py                   # http://127.0.0.1:8080

# rag
cd rag && uv run python run.py                        # http://127.0.0.1:8080

# forecast
cd forecast && uv run uvicorn forecast.main:app --reload  # http://127.0.0.1:8000

# analytics
cd analytics && uv run uvicorn analytics.main:app --reload  # http://127.0.0.1:8000
```

> 同时启动多个 agent 时注意端口冲突，改 `--port` 就行。

### 4. 只改 infra 时

改完 `infra/llm.py` 不用重新安装 — `uv sync --all-packages` 是以 editable 模式装的，改完即生效，所有 agent 立刻看到新代码。

## 依赖关系

```
document ──┐
rag ────────┼──→ infra    （workspace 内依赖，uv 自动解析）
forecast ──┤
analytics ─┘
```

每个 agent 的 `pyproject.toml` 里写：

```toml
dependencies = [
    "infra",              # workspace 内依赖
    "fastapi>=0.104.0",   # 外部依赖
    ...
]
```

uv 看到 `"infra"` 会优先匹配 workspace member，不需要 publish 到 PyPI。

## 目录说明

```
bosch-ai-framework/
├── pyproject.toml              # 根 workspace 配置
├── infra/                      # 公共基础设施（来自 ainfra）
│   ├── __init__.py
│   ├── llm.py                  # LiteLLM 网关 / Router
│   ├── auth.py                 # HTTP Basic + XSUAA 鉴权
│   ├── settings.py             # YAML + env 配置加载
│   ├── tasks.py                # 异步任务管理
│   ├── tools.py                # 通用工具函数
│   ├── logs.py                 # JSON 日志
│   └── utils.py
├── document/                   # 文档解析（原 apdfi/idoc）
│   ├── main.py                 # FastAPI app 入口
│   ├── excel/                  # Excel 解析引擎 + 客户配置
│   ├── pdf/                    # PDF 字段抽取 + VLM pipeline
│   ├── chat/                   # Chat FSM 多轮对话状态机
│   └── manifest.yml            # CF 部署描述
├── rag/                        # RAG 知识库（原 bapee）
│   ├── main.py
│   ├── rag/                    # 通用 Hybrid RAG 引擎
│   ├── chatbot/                # BPAE 业务管道
│   ├── core/                   # Auth / LLM / Rate Limit / BTP
│   └── manifest.yml
├── forecast/                   # 预测 / Function Generator（原 fcst）
│   ├── main.py
│   ├── core/                   # Agent 循环 / 执行器 / 编排器
│   ├── skills/                 # 技能预设（统计 / 业务）
│   ├── routes/                 # API 路由
│   └── manifest.yml
├── analytics/                  # AI BI / NL2SQL（原 abi）
│   ├── main.py
│   ├── api/                    # Chat / Health 路由
│   ├── core/                   # Agent / Session / Chart
│   └── manifest.yml
└── deployment/
    └── cf/
        └── deploy.sh           # 一键部署全部或单个 agent
```

## 部署到 Cloud Foundry

### 前置条件

```bash
# 1. 登录 CF
cf login -a https://api.cf.eu10.hana.ondemand.com -o <org> -s <space>

# 2. 创建必要的 service instances（一次性）
#    XSUAA / Redis / AI Core 等，按需创建
```

### 部署单个 Agent

```bash
cd document
cf push
```

`cf push` 自动读取当前目录的 `manifest.yml`。每个 agent 的 manifest 各自描述：
- app name（`document` / `rag` / `forecast` / `analytics`）
- 内存 / 磁盘配额
- gunicorn 启动命令
- health check endpoint
- 绑定的 service instances

部署之后 5 个 agent 是 5 个独立的 CF App，各自有独立的 URL、独立的 scaling、独立的 service binding。一个挂了不影响其他。

### 部署全部

```bash
./deployment/cf/deploy.sh all
```

### 更新某个 Agent

```bash
cd rag
cf push --strategy rolling    # 零停机滚动更新
```

### 查看状态

```bash
cf apps                         # 所有 agent 的运行状态
cf logs rag --recent            # 查看 rag 最近日志
cf restart document             # 重启 document
```

### 扩缩容

```bash
cf scale forecast -i 3          # forecast 扩到 3 个实例
cf scale analytics -m 2G        # analytics 加内存
```

## 常用操作

```bash
# 加一个新依赖
cd document
uv add <package>

# 所有 agent 一起跑测试
uv run pytest

# 代码格式化
uv run ruff check .
uv run ruff format .

# 查看 workspace 依赖树
uv tree
```
