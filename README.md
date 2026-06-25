# Bosch AI Platform

Monorepo — 5 个 Python 包，每个独立部署为一个 Cloud Foundry App。

## 概念

```
bosch-ai-framework/          ← 一个 Git 仓库（monorepo）
├── infra/                   ← 共享框架（uv workspace member）
├── document/                ← 文档解析 Agent（独立 CF App）
├── rag/                     ← RAG 知识库 Agent（独立 CF App）
├── forecast/                ← 预测 Agent（独立 CF App）
├── analytics/               ← AI BI Agent（独立 CF App）
└── deployment/cf/           ← 统一部署脚本
```

- **infra** 是 AI 框架，4 个 agent 都 `from infra.llm import chat` / `from infra.agent import ToolRegistry`
- **uv workspace** 管理所有包的依赖，一条命令安装全部
- **每个 agent 一个 CF App**，独立扩缩容、独立绑定服务、互不影响

## 开发环境

### 1. 安装 uv

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 2. 安装全部依赖

```bash
cd bosch-ai-framework
uv sync --all-packages
```

装完 5 个 workspace member + 所有外部依赖，`from infra.llm import chat` 在任意 agent 里直接可用。

### 3. 本地启动

```bash
cd document && uv run python run.py                     # http://127.0.0.1:8080
cd rag && uv run python run.py                          # http://127.0.0.1:8080
cd forecast && uv run uvicorn forecast.main:app --reload   # http://127.0.0.1:8000
cd analytics && uv run uvicorn analytics.main:app --reload # http://127.0.0.1:8000
```

> 同时启动注意端口冲突，加 `--port` 换端口。

### 4. 改 infra 即刻生效

`uv sync` 是 editable 安装，改 `infra/llm/client.py` 不用重新装，所有 agent 立刻看到。

## 目录说明

```
bosch-ai-framework/
├── pyproject.toml              # uv workspace 根配置
├── requirements.txt            # CF buildpack 用 (pip install -e)
├── infra/                      # 公共 AI 框架
│   ├── llm/                    # LLM 抽象层（屏蔽 LiteLLM）
│   │   ├── client.py           # chat(), stream() — 稳定接口
│   │   └── router.py           # LiteLLM Router，换 provider 只改这个
│   ├── agent/                  # Agent 框架
│   │   ├── tool.py             # Tool, ToolRegistry
│   │   └── loop.py             # AgentLoop (流式/非流式)
│   ├── skill/                  # Skill 框架
│   │   └── __init__.py         # Skill, SkillRegistry
│   ├── task/                   # 任务管理
│   │   ├── types.py            # TaskStatus, TaskID, TaskResult
│   │   └── backend.py          # TaskBackend(ABC) + MemoryTaskBackend
│   ├── auth.py                 # HTTP Basic + XSUAA 鉴权
│   ├── settings.py             # YAML + env 配置
│   ├── settings.yaml           # 模型配置（所有 agent 共享）
│   ├── logs.py                 # Gunicorn JSON 日志（所有 agent 共用 -c ../infra/logs.py）
│   ├── btp.py                  # BTP/CF VCAP_SERVICES 解析
│   ├── observability.py        # JsonFormatter + RequestIDMiddleware
│   ├── http_client.py          # 通用异步 HTTP 客户端
│   └── utils.py
├── document/                   # 文档解析（原 apdfi/idoc）
│   ├── main.py                 # FastAPI 入口
│   ├── excel/                  # Excel 引擎 + 客户配置
│   ├── pdf/                    # PDF 字段抽取 + VLM
│   ├── chat/                   # Chat FSM 状态机
│   └── manifest.yml
├── rag/                        # RAG 知识库（原 bapee）
│   ├── main.py
│   ├── rag/                    # Hybrid RAG 引擎
│   ├── chatbot/                # BPAE 业务管道
│   ├── core/                   # Auth / LLM / BTP
│   └── manifest.yml
├── forecast/                   # 预测 / Function Generator（原 fcst）
│   ├── main.py
│   ├── core/                   # Agent 编排器
│   ├── skills/                 # 统计/业务预设
│   ├── routes/                 # API 路由
│   └── manifest.yml
├── analytics/                  # AI BI / NL2SQL（原 abi）
│   ├── main.py
│   ├── api/                    # Chat / Health
│   ├── core/                   # Agent / Session
│   └── manifest.yml
└── deployment/cf/
    └── deploy.sh
```

## 依赖关系

```
document ──┐
rag ────────┼──→ infra    （workspace 内依赖）
forecast ──┤
analytics ─┘
```

每个 agent 的 `pyproject.toml`：

```toml
dependencies = [
    "infra",              # workspace 内依赖，uv 自动解析
    "fastapi>=0.104.0",
    ...
]

[tool.uv.sources]
infra = { workspace = true }
```

## 部署到 Cloud Foundry

### 前置

```bash
cf login -a https://api.cf.eu10.hana.ondemand.com -o <org> -s <space>
```

### 部署

```bash
./deployment/cf/deploy.sh rag          # 只部署 rag
./deployment/cf/deploy.sh all          # 部署全部
```

**原理：** manifest `path: ..` 推整个 monorepo，CF buildpack 用根 `requirements.txt` 一把装完所有 workspace member，然后 `cd <agent> && gunicorn` 启动指定 agent。5 个 agent 是 5 个独立的 CF App。

### 更新

```bash
./deployment/cf/deploy.sh rag          # 只重启 rag，其他不受影响
```

### 常用

```bash
cf apps                                # 所有 agent 状态
cf logs rag --recent                   # 日志
cf scale forecast -i 3                 # 扩实例
```

## 常用操作

```bash
uv add <package>                       # 加依赖
uv run pytest                          # 跑测试
uv run ruff check . && uv run ruff format .   # 格式化
uv tree                                # 依赖树
```
