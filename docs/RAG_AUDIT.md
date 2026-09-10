# RAG 中间产物审计指南

## 存了什么、存在哪里

所有 RAG 分块统一存放在项目根目录 `just_codding.db` 的 `rag_embeddings` 表：

- `file`：Python 源码。按 AST 函数、类方法、类整体切块；无法解析时退化为按内容整块。
- `memory`：`tasks/` 与 `agents/` 下的 Markdown，按空行段落切块；也包含 `memory://task/{id}` 任务总结。
- `error`：`memory://error/{id}` 历史错误模式。
- `spec`：`spec://规则名` 红线规则，共 8 条。

每行保留：来源类型、来源路径、项目路径、SHA-256 文件哈希、mtime、大小、符号、块序号、块内容、384 维 float32 向量 BLOB、token 估算、创建/更新时间。

任务完成后的产物会按需写入 `long_term_memory` 表并同步进入 RAG：

- DEV：任务总结、需求文档、开发计划、实现说明。
- DEBUG：诊断报告、修复总结、实现说明。

空产物会自动跳过，不会写入 RAG。RAG 检索命中记录写入 `trace_events`，事件类型为 `rag_retrieval`。

## 每个角色分别查什么

- `ANALYZE`：查 `memory`，top 3。
- `DEVELOP` / `FIX`：查 `file` top 8 + `memory` top 2。
- `REVIEW`：查 `spec` top 5，再融合 `error` top 3。
- `DIAGNOSE`：查 `file` top 8，再融合 `error` top 3。

每路先做 SQL `LIKE` 关键词检索，再做向量余弦语义检索，最后用 RRF 融合排序。

## 人怎么查

1. 查来源清单：`GET /api/rag/sources?project_path=...&source_type=file`
2. 查具体分块：`GET /api/rag/chunks?source_path=backend/main.py&project_path=...`
3. 查某次工作流实际查询词与命中：打开任务 Trace，查看 `rag_retrieval` 事件。
4. 临时排查可直接用 SQLite 只读连接查看 `rag_embeddings`；不要在运行中手改该表。

## AI 怎么查

- 工作流内：不要读取数据库，直接调用 `hybrid_retrieve_for_role(role, query, project_path)`。
- 工作流外/审计场景：调用 `/api/rag/sources` 和 `/api/rag/chunks`。
- 复盘一次运行：读取对应 Trace 的 `rag_retrieval` 事件，得到查询词、角色、命中来源、块序号。
