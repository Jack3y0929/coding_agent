# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import json
from datetime import datetime, timezone
from typing import Any, List, Optional

import aiosqlite

from backend.config import DB_PATH

logger = logging.getLogger("db.models")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def init_db(db_path: str = DB_PATH) -> None:
    """初始化数据库表"""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL CHECK(type IN ('dev', 'debug')),
                status TEXT NOT NULL DEFAULT 'running'
                    CHECK(status IN ('running', 'paused', 'done', 'failed')),
                project_path TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                role TEXT NOT NULL CHECK(role IN ('system', 'user', 'assistant', 'tool')),
                content TEXT DEFAULT '',
                tool_calls TEXT DEFAULT NULL,
                token_count INTEGER DEFAULT 0,
                node TEXT DEFAULT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                type TEXT NOT NULL CHECK(type IN (
                    'clarification', 'requirement', 'code_changes',
                    'review', 'diagnosis', 'output_summary', 'plan'
                )),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS long_term_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL CHECK(category IN ('task', 'error', 'preference')),
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                embedding BLOB DEFAULT NULL,
                source_session_id TEXT REFERENCES sessions(id),
                relevance_score REAL DEFAULT 0.0,
                created_at TEXT NOT NULL,
                last_recalled_at TEXT DEFAULT NULL
                ,project_path TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS rag_embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL CHECK(source_type IN ('file', 'memory', 'error', 'spec')),
                source_path TEXT NOT NULL,
                project_path TEXT NOT NULL DEFAULT '',
                scope_type TEXT NOT NULL DEFAULT 'project',
                scope_key TEXT NOT NULL DEFAULT '',
                file_hash TEXT DEFAULT NULL,
                content_hash TEXT DEFAULT NULL,
                embedding_model TEXT DEFAULT NULL,
                embedding_model_version TEXT DEFAULT NULL,
                embedding_dimension INTEGER DEFAULT NULL,
                mtime REAL DEFAULT NULL,
                size INTEGER DEFAULT NULL,
                symbol TEXT DEFAULT NULL,
                chunk_index INTEGER NOT NULL DEFAULT 0,
                content TEXT NOT NULL,
                embedding BLOB DEFAULT NULL,
                token_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS workflow_traces (
                trace_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                workflow_type TEXT NOT NULL,
                project_path TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'running',
                started_at TEXT NOT NULL,
                finished_at TEXT DEFAULT NULL,
                duration_ms INTEGER DEFAULT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                error_message TEXT DEFAULT NULL
            );

            CREATE TABLE IF NOT EXISTS trace_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT NOT NULL REFERENCES workflow_traces(trace_id),
                event_type TEXT NOT NULL,
                stage TEXT DEFAULT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                duration_ms INTEGER DEFAULT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trace_labels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT NOT NULL REFERENCES workflow_traces(trace_id),
                outcome TEXT NOT NULL,
                requirement_fit TEXT DEFAULT NULL,
                code_quality TEXT DEFAULT NULL,
                review_effectiveness TEXT DEFAULT NULL,
                diagnosis_effectiveness TEXT DEFAULT NULL,
                adoption TEXT DEFAULT NULL,
                fix_effectiveness TEXT DEFAULT NULL,
                issue_types TEXT NOT NULL DEFAULT '[]',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS evaluation_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_path TEXT DEFAULT NULL,
                workflow_type TEXT DEFAULT NULL,
                start_at TEXT DEFAULT NULL,
                end_at TEXT DEFAULT NULL,
                metrics TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trace_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT NOT NULL REFERENCES workflow_traces(trace_id),
                metric_name TEXT NOT NULL,
                score REAL DEFAULT NULL,
                source TEXT NOT NULL CHECK(source IN ('rule', 'human')),
                evidence_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            -- Worker 与 API 进程之间传递人工恢复信号。
            CREATE TABLE IF NOT EXISTS workflow_controls (
                session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                decision TEXT DEFAULT NULL,
                new_logs TEXT DEFAULT NULL,
                feedback TEXT NOT NULL DEFAULT '{}',
                intent_update TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts(session_id);
            CREATE INDEX IF NOT EXISTS idx_rag_source ON rag_embeddings(source_path);
        """)
        await _migrate_schema(db)
        await db.commit()
        await db.executescript("""
            CREATE INDEX IF NOT EXISTS idx_rag_project_source ON rag_embeddings(project_path, source_path);
            CREATE INDEX IF NOT EXISTS idx_rag_scope_type_key ON rag_embeddings(scope_type, scope_key, source_type);
            CREATE INDEX IF NOT EXISTS idx_rag_content_embedding ON rag_embeddings(content_hash, embedding_model, embedding_model_version);
            CREATE INDEX IF NOT EXISTS idx_rag_type ON rag_embeddings(source_type);
            CREATE INDEX IF NOT EXISTS idx_ltm_category ON long_term_memory(category);
            CREATE INDEX IF NOT EXISTS idx_trace_session ON workflow_traces(session_id, started_at);
            CREATE INDEX IF NOT EXISTS idx_trace_status ON workflow_traces(status, started_at);
            CREATE INDEX IF NOT EXISTS idx_trace_events_trace ON trace_events(trace_id, id);
            CREATE INDEX IF NOT EXISTS idx_trace_labels_trace ON trace_labels(trace_id, id);
            CREATE INDEX IF NOT EXISTS idx_trace_evaluations_trace ON trace_evaluations(trace_id, metric_name);
            CREATE INDEX IF NOT EXISTS idx_workflow_controls_created ON workflow_controls(created_at);
        """)
    logger.info("数据库初始化完成")


async def _migrate_schema(db: aiosqlite.Connection) -> None:
    """为已有 SQLite 数据库补充偏好记忆字段，重复执行安全。"""
    cursor = await db.execute("PRAGMA table_info(long_term_memory)")
    columns = {row[1] for row in await cursor.fetchall()}
    migrations = {
        "scope_type": "TEXT NOT NULL DEFAULT 'global'",
        "scope_value": "TEXT NOT NULL DEFAULT ''",
        "source_type": "TEXT NOT NULL DEFAULT 'workflow'",
        "source_trace_id": "TEXT DEFAULT NULL",
        "confidence": "REAL NOT NULL DEFAULT 0.5",
        "occurrence_count": "INTEGER NOT NULL DEFAULT 1",
        "status": "TEXT NOT NULL DEFAULT 'active'",
        "supersedes_memory_id": "INTEGER DEFAULT NULL",
        "last_verified_at": "TEXT DEFAULT NULL",
    }
    for name, definition in migrations.items():
        if name not in columns:
            await db.execute(f"ALTER TABLE long_term_memory ADD COLUMN {name} {definition}")
    cursor = await db.execute("PRAGMA table_info(rag_embeddings)")
    rag_columns = {row[1] for row in await cursor.fetchall()}
    rag_migrations = {
        "project_path": "TEXT NOT NULL DEFAULT ''",
        "file_hash": "TEXT DEFAULT NULL",
        "mtime": "REAL DEFAULT NULL",
        "size": "INTEGER DEFAULT NULL",
        "symbol": "TEXT DEFAULT NULL",
        "scope_type": "TEXT NOT NULL DEFAULT 'project'",
        "scope_key": "TEXT NOT NULL DEFAULT ''",
        "content_hash": "TEXT DEFAULT NULL",
        "embedding_model": "TEXT DEFAULT NULL",
        "embedding_model_version": "TEXT DEFAULT NULL",
        "embedding_dimension": "INTEGER DEFAULT NULL",
    }
    for name, definition in rag_migrations.items():
        if name not in rag_columns:
            await db.execute(f"ALTER TABLE rag_embeddings ADD COLUMN {name} {definition}")
    await db.execute(
        "UPDATE rag_embeddings SET scope_type = CASE WHEN project_path = '' THEN 'global' ELSE 'project' END "
        "WHERE scope_type = 'project' AND (scope_key = '' OR scope_key IS NULL)"
    )
    cursor = await db.execute("PRAGMA table_info(long_term_memory)")
    memory_columns = {row[1] for row in await cursor.fetchall()}
    if "project_path" not in memory_columns:
        await db.execute("ALTER TABLE long_term_memory ADD COLUMN project_path TEXT NOT NULL DEFAULT ''")


class Database:
    """异步数据库操作类"""

    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = db_path

    async def __aenter__(self) -> "Database":
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys=ON")
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._conn.close()

    # ---- Session 操作 ----

    async def create_session(self, session_id: str, workflow_type: str,
                             project_path: str = "", description: str = "") -> None:
        await self._conn.execute(
            "INSERT INTO sessions (id, type, status, project_path, description, created_at) "
            "VALUES (?, ?, 'running', ?, ?, ?)",
            (session_id, workflow_type, project_path, description, _now()),
        )
        await self._conn.commit()

    async def get_session(self, session_id: str) -> Optional[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_session_status(self, session_id: str, status: str) -> None:
        await self._conn.execute(
            "UPDATE sessions SET status = ? WHERE id = ?", (status, session_id)
        )
        await self._conn.commit()

    async def update_session_workflow_type(self, session_id: str, workflow_type: str) -> None:
        """在意图澄清后同步会话的实际执行流程类型。"""
        await self._conn.execute(
            "UPDATE sessions SET type = ? WHERE id = ?", (workflow_type, session_id)
        )
        await self._conn.commit()

    # ---- Message 操作 ----

    async def add_message(self, session_id: str, role: str, content: str = "",
                          token_count: int = 0, node: Optional[str] = None,
                          tool_calls: Optional[str] = None) -> int:
        cursor = await self._conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_calls, token_count, node, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, role, content, tool_calls, token_count, node, _now()),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def get_messages_by_session(self, session_id: str, limit: int = 50,
                                      node: Optional[str] = None) -> list[dict[str, Any]]:
        if node:
            cursor = await self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? AND node = ? "
                "ORDER BY id DESC LIMIT ?",
                (session_id, node, limit),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ---- Artifact 操作 ----

    async def add_artifact(self, session_id: str, artifact_type: str,
                           content: str) -> int:
        cursor = await self._conn.execute(
            "INSERT INTO artifacts (session_id, type, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, artifact_type, content, _now()),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def get_artifacts_by_session(self, session_id: str) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM artifacts WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_latest_artifact(self, session_id: str,
                                  artifact_type: str) -> Optional[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM artifacts WHERE session_id = ? AND type = ? "
            "ORDER BY id DESC LIMIT 1",
            (session_id, artifact_type),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # ---- 长期记忆操作 ----

    async def insert_long_term_memory(self, category: str, title: str, content: str,
                                      embedding: Optional[bytes] = None,
                                      source_session_id: Optional[str] = None,
                                      project_path: str = "") -> int:
        cursor = await self._conn.execute(
            "INSERT INTO long_term_memory (category, title, content, embedding, "
            "source_session_id, project_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (category, title, content, embedding, source_session_id, project_path, _now()),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def search_long_term_memory(self, keyword: str,
                                      category: Optional[str] = None,
                                      limit: int = 10) -> list[dict[str, Any]]:
        if category:
            cursor = await self._conn.execute(
                "SELECT * FROM long_term_memory WHERE category = ? AND "
                "(title LIKE ? OR content LIKE ?) ORDER BY created_at DESC LIMIT ?",
                (category, f"%{keyword}%", f"%{keyword}%", limit),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT * FROM long_term_memory WHERE title LIKE ? OR content LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (f"%{keyword}%", f"%{keyword}%", limit),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_long_term_memory_by_category(self, category: str,
                                               limit: int = 20) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM long_term_memory WHERE category = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (category, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def update_memory_recall(self, memory_id: int) -> None:
        await self._conn.execute(
            "UPDATE long_term_memory SET last_recalled_at = ? WHERE id = ?",
            (_now(), memory_id),
        )
        await self._conn.commit()

    # ---- RAG Embedding 操作 ----

    async def insert_rag_embedding(self, source_type: str, source_path: str,
                                   chunk_index: int, content: str,
                                   embedding: Optional[bytes] = None,
                                   token_count: int = 0,
                                   project_path: str = "",
                                   file_hash: Optional[str] = None,
                                   mtime: Optional[float] = None,
                                   size: Optional[int] = None,
                                   symbol: Optional[str] = None,
                                   scope_type: str = "project",
                                   scope_key: str = "",
                                   content_hash: Optional[str] = None,
                                   embedding_model: Optional[str] = None,
                                   embedding_model_version: Optional[str] = None,
                                   embedding_dimension: Optional[int] = None) -> int:
        now = _now()
        cursor = await self._conn.execute(
            "INSERT INTO rag_embeddings (source_type, source_path, project_path, scope_type, scope_key, file_hash, content_hash, "
            "embedding_model, embedding_model_version, embedding_dimension, mtime, size, symbol, chunk_index, content, embedding, token_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source_type, source_path, project_path, scope_type, scope_key, file_hash, content_hash,
             embedding_model, embedding_model_version, embedding_dimension, mtime, size, symbol,
             chunk_index, content, embedding, token_count, now, now),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def update_rag_embedding(self, embed_id: int, content: str,
                                   embedding: bytes, token_count: int) -> None:
        await self._conn.execute(
            "UPDATE rag_embeddings SET content = ?, embedding = ?, "
            "token_count = ?, updated_at = ? WHERE id = ?",
            (content, embedding, token_count, _now(), embed_id),
        )
        await self._conn.commit()

    async def delete_rag_by_source(self, source_path: str, project_path: Optional[str] = None) -> None:
        if project_path is None:
            await self._conn.execute("DELETE FROM rag_embeddings WHERE source_path = ?", (source_path,))
        else:
            await self._conn.execute("DELETE FROM rag_embeddings WHERE source_path = ? AND project_path = ?",
                                     (source_path, project_path))
        await self._conn.commit()

    async def get_rag_file_metadata(self, source_path: str, project_path: str) -> Optional[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT project_path, source_path, file_hash, mtime, size, embedding_model, embedding_model_version FROM rag_embeddings "
            "WHERE source_path = ? AND project_path = ? ORDER BY id LIMIT 1",
            (source_path, project_path),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_cached_embedding(self, content_hash: str, embedding_model: str,
                                   embedding_model_version: str) -> Optional[bytes]:
        cursor = await self._conn.execute(
            "SELECT embedding FROM rag_embeddings WHERE content_hash = ? AND embedding_model = ? "
            "AND embedding_model_version = ? AND embedding IS NOT NULL LIMIT 1",
            (content_hash, embedding_model, embedding_model_version),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def get_rag_sources(self, project_path: str, source_type: Optional[str] = "file") -> list[dict[str, Any]]:
        sql = "SELECT DISTINCT source_path, project_path, file_hash, mtime, size FROM rag_embeddings WHERE project_path = ?"
        params: list[Any] = [project_path]
        if source_type:
            sql += " AND source_type = ?"
            params.append(source_type)
        cursor = await self._conn.execute(sql, tuple(params))
        return [dict(row) for row in await cursor.fetchall()]

    async def has_source_path(self, source_path: str) -> bool:
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM rag_embeddings WHERE source_path = ?",
            (source_path,),
        )
        row = await cursor.fetchone()
        return row[0] > 0 if row else False

    async def get_all_rag_embeddings(self,
                                     source_type: Optional[str] = None,
                                     project_path: Optional[str] = None,
                                     scope_types: Optional[list[str]] = None,
                                     scope_key: str = "",
                                     embedding_model: Optional[str] = None,
                                     embedding_model_version: Optional[str] = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if source_type:
            clauses.append("source_type = ?"); params.append(source_type)
        if project_path is not None:
            clauses.append("(project_path = ? OR scope_type = 'global' OR (scope_type = 'team' AND scope_key = ?))")
            params.extend([project_path, scope_key])
        if scope_types:
            placeholders = ",".join("?" for _ in scope_types)
            clauses.append(f"scope_type IN ({placeholders})")
            params.extend(scope_types)
        if embedding_model:
            clauses.append("embedding_model = ?"); params.append(embedding_model)
        if embedding_model_version:
            clauses.append("embedding_model_version = ?"); params.append(embedding_model_version)
        sql = "SELECT * FROM rag_embeddings" + ((" WHERE " + " AND ".join(clauses)) if clauses else "")
        cursor = await self._conn.execute(sql, tuple(params))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def list_rag_sources(self,
                               source_type: Optional[str] = None,
                               project_path: Optional[str] = None,
                               limit: int = 200,
                               offset: int = 0) -> list[dict[str, Any]]:
        """列出 RAG 已索引来源，不含内容和向量，供人工/AI 审计。"""
        clauses: list[str] = []
        params: list[Any] = []
        if source_type:
            clauses.append("source_type = ?")
            params.append(source_type)
        if project_path is not None:
            clauses.append("project_path = ?")
            params.append(project_path)
        condition = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = (
            f"SELECT source_type, source_path, project_path, file_hash, mtime, size, "
            f"COUNT(*) AS chunk_count, COUNT(DISTINCT symbol) AS symbol_count, "
            f"SUM(token_count) AS token_count, MIN(created_at) AS created_at, "
            f"MAX(updated_at) AS updated_at FROM rag_embeddings{condition} "
            f"GROUP BY source_type, source_path, project_path, file_hash, mtime, size "
            f"ORDER BY updated_at DESC, source_path LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])
        cursor = await self._conn.execute(sql, tuple(params))
        return [dict(row) for row in await cursor.fetchall()]

    async def get_rag_chunks(self,
                             source_path: str,
                             source_type: Optional[str] = None,
                             project_path: Optional[str] = None,
                             include_embedding: bool = False) -> list[dict[str, Any]]:
        """查看指定来源的 RAG 分块中间产物。"""
        clauses = ["source_path = ?"]
        params: list[Any] = [source_path]
        if source_type:
            clauses.append("source_type = ?")
            params.append(source_type)
        if project_path is not None:
            clauses.append("project_path = ?")
            params.append(project_path)
        cursor = await self._conn.execute(
            f"SELECT * FROM rag_embeddings WHERE {' AND '.join(clauses)} ORDER BY chunk_index, id",
            tuple(params),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        if not include_embedding:
            for row in rows:
                row["embedding"] = None
        return rows

    async def get_rag_by_source(self, source_path: str, project_path: Optional[str] = None) -> list[dict[str, Any]]:
        params: tuple[Any, ...] = (source_path,) if project_path is None else (source_path, project_path)
        condition = "source_path = ?" + (" AND project_path = ?" if project_path is not None else "")
        cursor = await self._conn.execute(
            f"SELECT * FROM rag_embeddings WHERE {condition} ORDER BY chunk_index", params,
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def keyword_search_rag(self, keywords: str,
                                 source_type: Optional[str] = None,
                                 limit: int = 20,
                                 project_path: Optional[str] = None,
                                 scope_types: Optional[list[str]] = None,
                                 scope_key: str = "",
                                 embedding_model: Optional[str] = None,
                                 embedding_model_version: Optional[str] = None) -> list[dict[str, Any]]:
        """关键词搜索 RAG 索引"""
        kw_list = [kw.strip() for kw in keywords.split() if kw.strip()]
        if not kw_list:
            parts = ["content LIKE ?"]
            params: list[Any] = [f"%{keywords}%"]
        else:
            parts = ["content LIKE ?" for _ in kw_list]
            params = [f"%{kw}%" for kw in kw_list]

        condition = " OR ".join(parts)
        if source_type:
            condition = f"({condition}) AND source_type = ?"
            params.append(source_type)
        if project_path is not None:
            condition = f"({condition}) AND (project_path = ? OR scope_type = 'global' OR (scope_type = 'team' AND scope_key = ?))"
            params.extend([project_path, scope_key])
        if scope_types:
            placeholders = ",".join("?" for _ in scope_types)
            condition = f"({condition}) AND scope_type IN ({placeholders})"
            params.extend(scope_types)
        if embedding_model:
            condition = f"({condition}) AND embedding_model = ?"
            params.append(embedding_model)
        if embedding_model_version:
            condition = f"({condition}) AND embedding_model_version = ?"
            params.append(embedding_model_version)

        sql = (f"SELECT * FROM rag_embeddings WHERE {condition} "
               f"ORDER BY updated_at DESC LIMIT ?")
        params.append(limit)

        cursor = await self._conn.execute(sql, tuple(params))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


def get_session(db_path: str = DB_PATH) -> Database:
    """创建 Database 实例（用于 async with 上下文管理）"""
    return Database(db_path)


# ---- 独立函数（供快速调用） ----

async def create_session(session_id: str, workflow_type: str,
                         project_path: str = "", description: str = "") -> None:
    db = Database()
    await db.__aenter__()
    try:
        await db.create_session(session_id, workflow_type, project_path, description)
    finally:
        await db.__aexit__()


async def get_artifacts_by_session(session_id: str) -> list[dict[str, Any]]:
    db = Database()
    await db.__aenter__()
    try:
        return await db.get_artifacts_by_session(session_id)
    finally:
        await db.__aexit__()


async def update_session_status(session_id: str, status: str) -> None:
    db = Database()
    await db.__aenter__()
    try:
        await db.update_session_status(session_id, status)
    finally:
        await db.__aexit__()
