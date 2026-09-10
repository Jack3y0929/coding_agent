# Just_codding Agent 开发指南

## 项目概览
- **项目名称**：Just_codding — AI自动化代码开发Agent
- **项目定位**：Web UI驱动的Python全自动代码开发助手，编排DeepSeek API模拟多角色开发团队协作
- **技术栈**：Python全栈（FastAPI后端 + Vue 3前端 + LangGraph编排引擎 + DeepSeek API），版本控制使用Git
- **工作区**：backend（后端）、frontend（前端）、agents（角色prompt）、tasks（产出物存档）

## AI 代理要求
**所有AI代理在推理和工作过程中必须全程使用中文**：
- 分析代码、编写注释、生成文档、与用户交流均使用中文
- 代码中的字符串常量、日志消息可根据业务需求决定语言
- 确保所有输出对中文用户友好

### 红线规则（必须严格遵守）
所有AI代理在开发Just_codding项目时必须遵守以下8条红线规则，违规将导致技术债务：

1. **Plan/Build模式严格分离**
   - Plan模式下严禁写文件或执行修改性shell命令
   - Build模式下写文件需路径校验，shell命令需白名单校验
   - Agent自主判断切换时机：Plan模式规划充分后通过 `save_checkpoint` 工具请求切换

2. **角色上下文隔离**
   - 每个LangGraph节点（需求分析师/开发者/审查者/侦探）必须使用独立对话上下文
   - 节点间仅传递正式产出文档（需求概括/代码变更/审查报告/问题分析报告），禁止传递对话历史
   - 每个角色通过RAG独立检索所需上下文

3. **Token预算硬上限**
   - 单次工作流总费用不得超过5元（约100万token）
   - 每次AI API调用前后必须预估和累加token消耗
   - 超限时立即中断当前节点，输出已完成总结，不静默失败

4. **Shell命令白名单**
   - Agent执行的任何shell命令必须匹配预定义白名单
   - 白名单仅包含编译/检查/读取类命令（如 `cargo check`、`npm run build`、`pytest`）
   - 不在白名单内的命令拒绝执行并记录日志
   - `execute_shell` 会自动把当前任务的 `project_path` 作为工作目录（`cwd`），命令中禁止再次拼接 `cd`、`dir`、`ls`、`pwd`、`Set-Location` 或其他目录切换命令
   - 禁止使用 `&&`、`||`、`;`、管道、重定向等 shell 组合语法；源码浏览必须使用 `read_file` / `search_code`
   - 验证命令必须能从项目根目录直接执行。前端位于子目录时使用 `npm run build --prefix frontend`、`npm run lint --prefix frontend` 或 `npm run typecheck --prefix frontend`，不要使用项目根目录下的 `npm run build`
   - Python 测试使用 `pytest` 或 `python -m pytest`；Git 检查使用白名单中的 `git status`、`git diff`、`git log --oneline`
   - 需求文档中的 `validation_commands` 只能填写上述白名单命令及其安全参数；不要让模型自行生成 `cd /d ... && ...`、`ls -la ...` 等平台相关命令

5. **写文件路径校验**
   - `write_file` 操作必须在项目源码目录内，禁止写入系统目录或非项目配置文件
   - 写入前检查目标路径是否在允许的目录范围内

6. **RAG索引增量更新**
   - 每次工作流完成后必须增量更新代码索引，只重新索引变更的文件
   - 禁止全量重建索引
   - 长期记忆（任务总结、错误模式）实时写入

7. **审查轮次强制**
   - DEV工作流（增量开发）：至少1轮代码审查
   - DEBUG工作流（BUG修复）：至少2轮代码审查
   - 第1轮审查侧重逻辑正确性，第2轮侧重代码规范
   - 审查不通过时自动触发修复循环，最多3次

8. **最终验收必须人工**
   - 代码产出后必须进入 `HUMAN_ACCEPT` 节点等待人工确认
   - 严禁Agent自动提交代码（git commit）
   - 人工验收不通过时回到修复循环

### 多角色Agent系统

Just_codding通过LangGraph编排4个AI角色协同工作，每个角色使用独立的system_prompt和独立的对话上下文：

| 角色 | LangGraph节点 | Prompt文件 | 职责 |
|---|---|---|---|
| 需求分析师 | ANALYZE | `agents/analyst_prompt.md` | 接收澄清文档，产出需求概括文档 |
| 侦探型调试 | DIAGNOSE | `agents/debugger_prompt.md` | 接收BUG描述+日志，产出问题分析报告 |
| 开发者 | DEVELOP / FIX | `agents/developer_prompt.md` | 接收需求概括/审查报告，产出代码变更 |
| 审查者 | REVIEW | `agents/reviewer_prompt.md` | 接收需求概括+代码，产出审查报告 |

**节点间数据传递规则（严格遵守）：**
```
INPUT_GATE → 产出"澄清文档"
ANALYZE   ← 接收"澄清文档"+ RAG注入 → 产出"需求概括文档"
DEVELOP   ← 接收"需求概括文档"+ RAG注入 → 产出"代码变更+实现说明"
REVIEW    ← 接收"需求概括文档+代码变更+实现说明"+ RAG注入 → 产出"审查报告"
FIX       ← 接收"审查报告"+ "之前代码变更" → 产出"修复后代码变更"
DIAGNOSE  ← 接收"BUG描述+日志"+ RAG注入 → 产出"问题分析报告"
```

**严禁行为：**
- 禁止在DEVELOP节点中传入ANALYZE节点的对话历史
- 禁止在REVIEW节点中传入DEVELOP节点的推理过程
- 禁止在任何节点中传入用户澄清阶段以外的非正式对话

## 代码风格指南

### Python规范
- **类型注解**：所有函数参数和返回值必须有类型注解（使用 `typing` 模块）
- **异步优先**：IO操作（API调用、数据库读写、文件读写）使用 `async/await`
- **错误处理**：使用自定义异常类层级，不在业务逻辑中捕获 `BaseException`
- **日志**：使用 `logging` 模块，按模块分logger，关键步骤必须打INFO日志
- **Pydantic模型**：API请求/响应体使用Pydantic BaseModel定义
- **禁止**使用其他语言关键字作为标识符（如 `type`、`class`、`async`、`function`）

### Vue 3规范
- 使用Composition API（`<script setup>`），禁止Options API
- 组件命名：PascalCase，文件名与组件名一致
- 状态管理：简单状态用 `ref`/`reactive`，跨组件用 `provide`/`inject`
- 不上Pinia（除非确实需要全局状态管理）

### FastAPI规范
- 路由路径：RESTful风格（`/api/workflow/run`、`/api/workflow/status/{id}`）
- 路由注册：使用 `APIRouter` 模块化注册
- 进度推送：使用WebSocket，路径 `/ws/progress/{session_id}`
- 健康检查：必须提供 `/health` 端点返回 `{"status": "ok"}`

## 项目结构
```
just_codding/
├── backend/                    # FastAPI 后端
│   ├── main.py                 # 入口，路由注册，WebSocket端点
│   ├── graph/                  # LangGraph 工作流
│   │   ├── dev_workflow.py     # DEV增量开发状态图
│   │   └── debug_workflow.py   # DEBUG BUG修复状态图
│   ├── agents/                 # 角色 system_prompt 文件
│   │   ├── analyst_prompt.md   # 需求分析师（基于cat07-req改编）
│   │   ├── debugger_prompt.md  # 侦探调试（基于cat07-debug改编）
│   │   ├── developer_prompt.md # 开发者（新建）
│   │   └── reviewer_prompt.md  # 审查者（新建）
│   ├── rag/                    # RAG 检索增强系统
│   │   ├── indexer.py          # 代码索引构建（AST分块+向量化）
│   │   ├── retriever.py        # 混合检索（关键词+语义+RRF融合）
│   │   └── embedding.py        # 向量化封装（sentence-transformers）
│   ├── tools/                  # Function Calling 工具集
│   │   ├── file_tools.py       # read_file / write_file（含路径校验）
│   │   ├── shell_tools.py      # execute_shell（白名单校验）
│   │   └── search_tools.py     # search_code（正则搜索）
│   ├── db/                     # SQLite 数据层
│   │   └── models.py           # 表结构定义 + CRUD操作
│   ├── budget.py               # Token预算管理器
│   └── config.py               # 全局配置
├── frontend/                   # Vue 3 前端
│   ├── App.vue                 # 根组件
│   ├── components/
│   │   ├── Workbench.vue       # 工作台首页（选择工作流+输入描述）
│   │   ├── ProgressPanel.vue   # 进度面板（WebSocket实时更新）
│   │   └── SummaryPanel.vue    # 总结面板（完成后展示）
│   ├── index.html
│   ├── package.json
│   └── vite.config.js
├── agents/                     # 角色prompt（与backend/agents/内容一致）
│   ├── analyst_prompt.md
│   ├── debugger_prompt.md
│   ├── developer_prompt.md
│   └── reviewer_prompt.md
├── tasks/                      # 工作流产出物存档
├── AGENTS.md                   # 本文件
└── requirements.txt
```

## 参考文档
- `tasks/T20260514_需求概括.md` — 完整需求概括文档（必读基础文档）
- `agents/analyst_prompt.md` — 需求分析师角色prompt，含微信聊天风格、澄清流程、产出规范
- `agents/debugger_prompt.md` — 侦探型调试角色prompt，含根因定位、论据追溯、日志打点策略
- `agents/developer_prompt.md` — 开发者角色prompt，含Plan/Build模式切换、代码规范
- `agents/reviewer_prompt.md` — 审查者角色prompt，含四维审查框架（逻辑/规范/边界/安全）
- [DeepSeek API文档](https://platform.deepseek.com/api-docs/) — API调用规范
- [LangGraph文档](https://langchain-ai.github.io/langgraph/) — 状态机编排框架

---

**最后更新**: 2026-09-07
**文档作者**: 林栖(cat07)
**文档版本**: 1.0
