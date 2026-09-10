from __future__ import annotations

import ast
import hashlib
import logging
import os
from typing import Any, Optional

from backend.config import CHUNK_MAX_TOKENS
from backend.db.models import Database
from backend.rag.embedding import get_embedding_engine
from backend.project_scope import project_file_path, real_project_path

logger = logging.getLogger("rag.indexer")


class CodeIndexer:
    """代码索引器：AST解析Python文件，按函数/类分块，向量化后写入DB"""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._engine = get_embedding_engine()
        self._db_path = db_path

    async def _trace(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            from backend.trace import record_event
            await record_event(event_type, "index", payload)
        except Exception:
            logger.debug("索引 Trace 记录失败", exc_info=True)

    async def index_project(self, root_path: str) -> dict[str, Any]:
        """索引项目代码与研发文档；文档使用 memory 类型参与混合检索。"""
        root_path = real_project_path(root_path)
        stats = {"file_count": 0, "chunk_count": 0, "errors": 0,
                 "skipped": 0, "reindexed": 0, "removed": 0}
        await self._trace("index_started", {"project_path": root_path})
        db_context = Database(self._db_path) if self._db_path else Database()
        async with db_context as db:
            seen: set[str] = set()
            for dirpath, dirnames, filenames in os.walk(root_path):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")
                               and d not in ("node_modules", "__pycache__", "target",
                                             ".git", "dist", "build", ".venv", "venv")]
                for filename in filenames:
                    if filename.endswith(".py"):
                        filepath = os.path.join(dirpath, filename)
                        try:
                            result = await self.index_file(filepath, db, root_path)
                            seen.add(result["file"])
                            stats["file_count"] += int(not result.get("skipped"))
                            stats["chunk_count"] += result.get("chunks", 0)
                            stats["skipped"] += int(result.get("skipped", False))
                            stats["reindexed"] += int(result.get("reindexed", False))
                        except Exception as exc:
                            logger.warning(f"索引文件失败: {filepath}: {exc}")
                            stats["errors"] += 1
                    elif filename.endswith(".md") and (dirpath.endswith("tasks") or dirpath.endswith("agents")):
                        filepath = os.path.join(dirpath, filename)
                        try:
                            result = await self.index_document(filepath, db, root_path)
                            seen.add(result["file"])
                            stats["file_count"] += int(not result.get("skipped"))
                            stats["chunk_count"] += result.get("chunks", 0)
                            stats["skipped"] += int(result.get("skipped", False))
                            stats["reindexed"] += int(result.get("reindexed", False))
                        except Exception as exc:
                            logger.warning(f"索引研发文档失败: {filepath}: {exc}")
                            stats["errors"] += 1
            existing = await db.get_rag_sources(root_path, source_type=None)
            for source in existing:
                source_path = source["source_path"]
                if source_path not in seen or not os.path.isfile(os.path.join(root_path, source_path)):
                    await db.delete_rag_by_source(source_path, root_path)
                    stats["removed"] += 1
                    await self._trace("file_removed", {"project_path": root_path, "source_path": source_path})
        logger.info(f"项目索引完成: {stats}")
        return stats

    async def index_file(self, filepath: str, db: Optional[Database] = None,
                         project_path: Optional[str] = None) -> dict[str, Any]:
        """索引单个文件（AST分块+向量化+写入）"""
        root = real_project_path(project_path or os.path.dirname(os.path.abspath(filepath)))
        filepath = project_file_path(root, os.path.relpath(filepath, root))
        rel_path = os.path.relpath(filepath, root).replace(os.sep, "/")
        logger.info(f"索引文件: {rel_path}")

        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
        stat = os.stat(filepath)
        file_hash = hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()

        close_db = False
        if db is None:
            db = Database(self._db_path) if self._db_path else Database()
            await db.__aenter__()
            close_db = True
        metadata = await db.get_rag_file_metadata(rel_path, root)
        if metadata and metadata.get("file_hash") == file_hash:
            await self._trace("file_skipped_unchanged", {"project_path": root, "source_path": rel_path, "file_hash": file_hash})
            if close_db:
                await db.__aexit__()
            return {"file": rel_path, "chunks": 0, "skipped": True, "reindexed": False}

        chunks = self._parse_chunks(source, rel_path)

        if not chunks:
            # 无法解析的文件存原始内容为一整个块
            chunks = [{
                "content": source,
                "chunk_index": 0,
                "token_count": self._estimate_tokens(source),
            }]

        # 批量向量化
        texts = [chunk["content"] for chunk in chunks]
        embeddings_bytes = self._engine.batch_encode_to_bytes(texts)

        try:
            # 删除旧索引
            had_old = metadata is not None
            await db.delete_rag_by_source(rel_path, root)

            # 写入新索引
            for chunk, emb_bytes in zip(chunks, embeddings_bytes):
                await db.insert_rag_embedding(
                    source_type="file",
                    source_path=rel_path,
                    chunk_index=chunk["chunk_index"],
                    content=chunk["content"],
                    embedding=emb_bytes,
                    token_count=chunk["token_count"],
                    project_path=root, file_hash=file_hash, mtime=stat.st_mtime,
                    size=stat.st_size, symbol=chunk.get("symbol"),
                )
            await self._trace("file_reindexed" if had_old else "file_indexed", {
                "project_path": root, "source_path": rel_path, "file_hash": file_hash,
                "chunks": len(chunks),
            })
        finally:
            if close_db:
                await db.__aexit__()

        return {"file": rel_path, "chunks": len(chunks), "skipped": False, "reindexed": had_old}

    async def index_document(self, filepath: str, db: Optional[Database] = None,
                             project_path: Optional[str] = None) -> dict[str, Any]:
        """将 Markdown/文本研发文档按段落分块并增量写入 RAG。"""
        root = real_project_path(project_path or os.path.dirname(os.path.abspath(filepath)))
        filepath = project_file_path(root, os.path.relpath(filepath, root))
        rel_path = os.path.relpath(filepath, root).replace(os.sep, "/")
        with open(filepath, "r", encoding="utf-8", errors="replace") as handle:
            source = handle.read()
        stat = os.stat(filepath)
        file_hash = hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()
        close_db = False
        if db is None:
            db = Database(self._db_path) if self._db_path else Database()
            await db.__aenter__()
            close_db = True
        metadata = await db.get_rag_file_metadata(rel_path, root)
        if metadata and metadata.get("file_hash") == file_hash:
            await self._trace("file_skipped_unchanged", {"project_path": root, "source_path": rel_path, "file_hash": file_hash})
            if close_db:
                await db.__aexit__()
            return {"file": rel_path, "chunks": 0, "skipped": True, "reindexed": False}
        chunks = [part.strip() for part in source.split("\n\n") if part.strip()]
        if not chunks:
            chunks = [source]
        embeddings = self._engine.batch_encode_to_bytes(chunks)
        close_db = False
        if db is None:
            db = Database()
            await db.__aenter__()
            close_db = True
        try:
            await db.delete_rag_by_source(rel_path, root)
            for index, (content, embedding) in enumerate(zip(chunks, embeddings)):
                await db.insert_rag_embedding("memory", rel_path, index, content, embedding,
                                              self._estimate_tokens(content), root, file_hash,
                                              stat.st_mtime, stat.st_size, None)
            await self._trace("file_reindexed" if metadata else "file_indexed", {
                "project_path": root, "source_path": rel_path, "file_hash": file_hash,
                "chunks": len(chunks),
            })
        finally:
            if close_db:
                await db.__aexit__()
        return {"file": rel_path, "chunks": len(chunks), "skipped": False, "reindexed": metadata is not None}

    async def incremental_update(self, changed_files: list[str], project_path: Optional[str] = None) -> dict[str, Any]:
        """增量更新：只重索引变更的文件"""
        if project_path is None:
            if not changed_files:
                return {"file_count": 0, "chunk_count": 0, "errors": 0, "skipped": 0, "reindexed": 0}
            project_path = os.path.dirname(os.path.abspath(changed_files[0]))
        root = real_project_path(project_path)
        stats = {"file_count": 0, "chunk_count": 0, "errors": 0, "skipped": 0, "reindexed": 0}
        for filepath in changed_files:
            if not os.path.isfile(filepath):
                continue
            if not filepath.endswith(".py"):
                continue
            try:
                result = await self.index_file(filepath, project_path=root)
                stats["file_count"] += int(not result.get("skipped"))
                stats["chunk_count"] += result.get("chunks", 0)
                stats["skipped"] += int(result.get("skipped", False))
                stats["reindexed"] += int(result.get("reindexed", False))
            except Exception as exc:
                logger.warning(f"增量索引失败: {filepath}: {exc}")
                stats["errors"] += 1
        logger.info(f"增量索引完成: {stats}")
        return stats

    # ---- AST 解析分块 ----

    def _parse_chunks(self, source: str, rel_path: str) -> list[dict[str, Any]]:
        """使用AST解析Python源码，按函数/类分块"""
        try:
            tree = ast.parse(source)
        except SyntaxError:
            logger.debug(f"AST解析失败(将按行分块): {rel_path}")
            return self._fallback_chunks(source)

        chunks: list[dict[str, Any]] = []
        chunk_idx = 0

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.FunctionDef):
                chunk_text = ast.get_source_segment(source, node)
                if chunk_text and len(chunk_text) > 20:
                    chunks.append({
                        "content": chunk_text,
                        "chunk_index": chunk_idx,
                        "symbol": node.name,
                        "token_count": self._estimate_tokens(chunk_text),
                    })
                    chunk_idx += 1

            elif isinstance(node, ast.ClassDef):
                class_text = ast.get_source_segment(source, node)
                for child in node.body:
                    if isinstance(child, ast.FunctionDef):
                        method_text = ast.get_source_segment(source, child)
                        if method_text and len(method_text) > 20:
                            chunks.append({
                                "content": f"class {node.name}:\n{method_text}",
                                "chunk_index": chunk_idx,
                                "symbol": f"{node.name}.{child.name}",
                                "token_count": self._estimate_tokens(
                                    f"class {node.name}:\n{method_text}"
                                ),
                            })
                            chunk_idx += 1

                # 类本身也作为一个块
                if class_text and len(class_text) > 20:
                    chunks.append({
                        "content": class_text,
                        "chunk_index": chunk_idx,
                        "symbol": node.name,
                        "token_count": self._estimate_tokens(class_text),
                    })
                    chunk_idx += 1

        return chunks

    def _fallback_chunks(self, source: str) -> list[dict[str, Any]]:
        """无法AST解析时按行切块"""
        lines = source.split("\n")
        chunks: list[dict[str, Any]] = []
        current: list[str] = []
        current_tokens = 0
        chunk_idx = 0

        for line in lines:
            line_tokens = self._estimate_tokens(line)
            if current_tokens + line_tokens > CHUNK_MAX_TOKENS and current:
                chunk_text = "\n".join(current)
                chunks.append({
                    "content": chunk_text,
                    "chunk_index": chunk_idx,
                    "token_count": current_tokens,
                })
                chunk_idx += 1
                current = []
                current_tokens = 0
            current.append(line)
            current_tokens += line_tokens

        if current:
            chunk_text = "\n".join(current)
            chunks.append({
                "content": chunk_text,
                "chunk_index": chunk_idx,
                "token_count": current_tokens,
            })

        return chunks

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """估算文本token数（与 budget.py 保持一致）"""
        chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        other_chars = len(text) - chinese_chars
        return chinese_chars + int(other_chars / 3.5)

    # ---- 长期记忆写入 ----

    async def index_long_term_memory(self, category: str, title: str,
                                     content: str,
                                     source_session_id: str = "",
                                     project_path: str = "") -> int:
        """将任务总结/错误模式写入长期记忆并向量化"""
        embedding_bytes = self._engine.encode_to_bytes(content)

        async with Database(self._db_path) as db:
            memory_id = await db.insert_long_term_memory(
                category=category,
                title=title,
                content=content,
                embedding=embedding_bytes,
                source_session_id=source_session_id,
                project_path=project_path,
            )

            # 同时写入 rag_embeddings 表以便统一检索
            src_type = "error" if category == "error" else "memory"
            await db.insert_rag_embedding(
                source_type=src_type,
                source_path=f"memory://{category}/{memory_id}",
                chunk_index=0,
                content=content,
                embedding=embedding_bytes,
                token_count=self._estimate_tokens(content),
                project_path=project_path,
            )

        logger.info(f"长期记忆写入: [{category}] {title}")
        return memory_id

    async def index_long_term_memory_entries(
        self,
        entries: list[dict[str, Any]],
        source_session_id: str = "",
        project_path: str = "",
    ) -> list[int]:
        """批量写入非空长期记忆；空内容自动跳过，避免污染 RAG。"""
        memory_ids: list[int] = []
        for entry in entries:
            content = (entry.get("content") or "").strip()
            if not content:
                continue
            memory_id = await self.index_long_term_memory(
                category=entry["category"],
                title=entry["title"],
                content=content,
                source_session_id=source_session_id,
                project_path=project_path,
            )
            memory_ids.append(memory_id)
        return memory_ids
