# Just_codding

Just_codding 是一个由 Web UI 驱动的 AI 自动化代码开发 Agent。它使用 FastAPI 提供后端 API，使用 Vue 3 提供工作台，并通过独立角色、工作流状态机、RAG 检索和人工验收，把“需求澄清 → 分析 → 编码 → 审查 → 验收”串成可追踪的开发流程。

项目目前支持两类核心任务：

- **DEV：增量开发**。从自然语言需求出发，分析现有代码、规划并实现功能，经过审查后等待人工验收。
- **DEBUG：BUG 修复**。根据复现步骤、现象和日志定位根因，必要时补充日志，再修复并进行多轮审查。

仓库还包含技能注册、任务记忆、RAG 来源审计、Trace/质量标注，以及 SWE-bench 和 FeatureBench 评测适配器。

## 核心特性

- Vue 3 工作台、实时进度面板和结果/质量面板
- FastAPI HTTP API + WebSocket 进度推送
- DEV / DEBUG 两套 LangGraph 风格状态工作流
- 需求分析师、开发者、审查者、调试侦探的角色上下文隔离
- Plan/Build 分离、Token 预算控制和人工验收门禁
- 项目路径隔离、文件读写校验、Shell 命令白名单
- 关键词与向量检索结合的 RAG，以及任务长期记忆
- SQLite 保存会话、Trace、产物、审查和评测数据
- 可选的 SWE-bench / FeatureBench 容器化评测入口

## 系统结构

```text
浏览器
  └─ frontend/                 Vue 3 + Vite 工作台
       │ HTTP / WebSocket
       ▼
  backend/main.py              FastAPI API 与 WebSocket
       ├─ backend/graph/       DEV / DEBUG 工作流与节点
       ├─ backend/agents/      角色提示词
       ├─ backend/rag/         代码索引、检索和记忆
       ├─ backend/tools/       文件、搜索、Shell 工具
       ├─ backend/db/          SQLite 数据层
       └─ backend/evaluation/  SWE-bench / FeatureBench 评测
```

一次典型的 DEV 工作流如下：

```text
需求澄清 → 需求分析 → Plan → Build → 代码审查 → 人工验收 → 产物与记忆归档
```

DEBUG 工作流会在修复前增加诊断环节，并要求至少两轮审查（逻辑和规范）。Agent 不会自动执行 `git commit`，最终代码提交由开发者自行决定。

## 环境要求

- Python 3.11 或更高版本
- Node.js 18 或更高版本，npm
- 可访问所配置的 OpenAI 兼容模型 API
- （可选）Docker：运行 SWE-bench / FeatureBench 容器评测时需要

## 快速开始

### 1. 获取代码并安装后端依赖

```bash
git clone https://github.com/Jack3y0929/coding_agent.git
cd coding_agent

python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt
```

### 2. 配置模型 API

在项目根目录创建 `.env`。`.env` 已被 `.gitignore` 忽略，不能提交密钥：

```dotenv
DEEPSEEK_API_KEY=你的模型API密钥
DEEPSEEK_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DEEPSEEK_MODEL=Qwen3.7-Flash

# 可选：逗号分隔的备用模型
# DEEPSEEK_FALLBACK_MODELS=model-a,model-b
# API_TIMEOUT=180
# MAX_RETRIES=2
```

配置项虽然沿用 `DEEPSEEK_*` 命名，但后端使用 OpenAI 兼容协议，`DEEPSEEK_BASE_URL` 可以指向其他兼容服务。

### 3. 启动后端

在项目根目录执行：

```bash
python -m backend.main
```

后端默认监听 `http://127.0.0.1:8000`。健康检查：

```text
GET http://127.0.0.1:8000/health
```

预期返回：

```json
{"status":"ok"}
```

### 4. 安装并启动前端

另开一个终端，在项目根目录执行：

```bash
npm install --prefix frontend
npm run dev --prefix frontend
```

浏览器打开 `http://127.0.0.1:5173`。Vite 已配置 `/api` 和 `/ws` 代理到本地 FastAPI 服务。

生产构建与预览：

```bash
npm run build --prefix frontend
npm run preview --prefix frontend
```

## 使用流程

1. 在工作台选择“我要开发”或“我要修 BUG”，也可以让系统根据描述自动识别。
2. 输入需求或 BUG 描述，回答 Agent 的澄清问题。
3. 观察进度面板中的分析、开发、审查和等待状态。
4. DEV 任务中直接验证新功能；DEBUG 任务中按复现步骤重复验证修复结果。
5. 在人工验收面板选择通过或不通过。不通过时补充问题，工作流会回到修复循环。
6. 验收通过后查看总结、Trace 和质量信息，再由开发者手动执行 Git 提交。

## 主要 API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/health` | 服务健康检查 |
| `POST` | `/api/workflow/run` | 创建工作流会话 |
| `GET` | `/api/workflow/status/{session_id}` | 查询会话状态和产物 |
| `GET` | `/api/workflow/events/{session_id}` | 查询工作流事件 |
| `GET` | `/api/workflow/quality/{session_id}` | 查询质量与审查信息 |
| `POST` | `/api/workflow/resume` | 提交澄清、日志或人工决策，继续工作流 |
| `POST` | `/api/workflow/accept/{session_id}` | 人工验收通过 |
| `POST` | `/api/workflow/reject/{session_id}` | 人工验收不通过 |
| `WS` | `/ws/progress/{session_id}` | 实时接收进度事件 |
| `GET` | `/api/skills` | 查看已加载技能 |
| `POST` | `/api/skills/register` | 从项目 `skills/` 注册技能 |
| `GET` | `/api/traces` | 查询历史 Trace |
| `POST` | `/api/evaluations/run` | 生成历史任务评测报告 |

创建工作流请求示例：

```bash
curl -X POST http://127.0.0.1:8000/api/workflow/run \
  -H "Content-Type: application/json" \
  -d '{"workflow_type":"dev","description":"给审批模块增加批量通过功能","project_path":"D:/path/to/target-project"}'
```

`workflow_type` 可填 `dev`、`debug`，或省略后由意图识别模块自动选择。`project_path` 必须指向允许访问的真实项目目录。

## 项目目录

```text
backend/
  main.py                 FastAPI 入口、路由和 WebSocket
  worker.py               后台工作流进程
  graph/                  工作流状态、节点和审查逻辑
  agents/                 角色提示词
  rag/                    代码索引、检索和记忆
  tools/                  安全文件与 Shell 工具
  db/                     SQLite 模型和 CRUD
  evaluation/             SWE-bench / FeatureBench 适配器
frontend/
  src/                    Vue 组件和前端状态
agents/                   项目级角色提示词副本
tasks/                    需求、示例和流程产物
docs/                     设计与开发记录
tests/                    后端测试
```

运行时会生成以下本地数据，它们默认不会提交到 Git：

- `just_codding.db`：SQLite 会话、Trace、记忆和评测数据
- `.workflow_tasks/`：后台工作流任务文件
- `.swebench/`、`.featurebench/`：评测工作目录
- `logs-*.out`、`logs-*.err`：本地服务日志

## 开发与验证

后端测试：

```bash
pytest
```

检查前端构建：

```bash
npm run build --prefix frontend
```

检查 Python 语法：

```bash
python -m compileall backend
```

评测入口位于 `backend/evaluation/`，具体数据集、Docker 和并发参数通过 `backend/config.py` 中的环境变量配置。评测会产生较大的本地数据和容器开销，建议单独准备工作目录。

## 安全与约束

- 不要把 `.env`、API Key、SQLite 数据库、日志和评测缓存提交到仓库。
- Agent 的文件访问必须位于目标项目范围内，Shell 执行受白名单约束。
- 每个工作流有 Token/费用预算，预算耗尽时会以失败状态结束并保留已有产物。
- 节点之间只传递正式产物，不共享其他角色的完整对话历史。
- 代码生成后必须经过审查和人工验收；本项目不会自动提交 Git 代码。

## 许可证

当前仓库未声明正式开源许可证。如需公开分发，请先补充 `LICENSE` 文件并明确第三方数据集、模型和评测工具的使用条款。
