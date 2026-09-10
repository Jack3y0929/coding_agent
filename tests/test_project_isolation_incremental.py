"""项目隔离与 SHA-256 增量索引回归测试。"""

import asyncio
import numpy as np

from backend.db.models import Database, init_db
from backend.rag.indexer import CodeIndexer
from backend.rag.retriever import hybrid_retrieve


class CountingEmbedding:
    def __init__(self) -> None:
        self.calls = 0

    def batch_encode_to_bytes(self, texts: list[str]) -> list[bytes]:
        self.calls += len(texts)
        return [np.zeros(384, dtype=np.float32).tobytes() for _ in texts]

    def encode_to_bytes(self, text: str) -> bytes:
        self.calls += 1
        return np.zeros(384, dtype=np.float32).tobytes()


def test_projects_are_isolated_and_incremental(tmp_path) -> None:
    async def scenario() -> None:
        project_a = tmp_path / "project-a"
        project_b = tmp_path / "project-b"
        project_a.mkdir()
        project_b.mkdir()
        (project_a / "same.py").write_text("def alpha():\n    return 'only-a'\n", encoding="utf-8")
        (project_b / "same.py").write_text("def beta():\n    return 'only-b'\n", encoding="utf-8")
        db_path = str(tmp_path / "rag.db")
        await init_db(db_path)
        async with Database(db_path) as db:
            await db.create_session("session-a", "dev", str(project_a), "RAG回归测试")

        indexer = CodeIndexer(db_path)
        counter = CountingEmbedding()
        indexer._engine = counter
        first = await indexer.index_project(str(project_a))
        assert first["file_count"] == 1
        calls_after_first = counter.calls
        unchanged = await indexer.index_project(str(project_a))
        assert unchanged["skipped"] == 1
        assert counter.calls == calls_after_first

        (project_a / "new.py").write_text("def newly_added():\n    return 'new'\n", encoding="utf-8")
        (project_a / "same.py").write_text("def alpha():\n    return 'changed-a'\n", encoding="utf-8")
        updated = await indexer.index_project(str(project_a))
        assert updated["reindexed"] == 1
        assert updated["file_count"] == 2
        assert counter.calls > calls_after_first

        results_a = await hybrid_retrieve("changed-a", project_path=str(project_a), top_k=10, db_path=db_path)
        assert results_a and all(item["project_path"] == str(project_a.resolve()) for item in results_a)
        assert all("only-b" not in item["content"] for item in results_a)
        assert all(item["file_hash"] for item in results_a)
        added = await hybrid_retrieve("newly_added", project_path=str(project_a), top_k=10, db_path=db_path)
        assert any(item["source_path"] == "new.py" for item in added)

        (project_a / "new.py").unlink()
        removed = await indexer.index_project(str(project_a))
        assert removed["removed"] == 1
        after_delete = await hybrid_retrieve("newly_added", project_path=str(project_a), top_k=10, db_path=db_path)
        assert all(item["source_path"] != "new.py" for item in after_delete)

        await indexer.index_project(str(project_b))
        results_b = await hybrid_retrieve("only-b", project_path=str(project_b), top_k=10, db_path=db_path)
        assert results_b and all(item["project_path"] == str(project_b.resolve()) for item in results_b)
        async with Database(db_path) as db:
            rows = await db.get_rag_by_source("same.py", str(project_a.resolve()))
            assert rows and rows[0]["project_path"] == str(project_a.resolve())
            sources = await db.list_rag_sources("file", str(project_a.resolve()))
            assert any(item["source_path"] == "same.py" and item["chunk_count"] == 1 for item in sources)
            chunks = await db.get_rag_chunks("same.py", "file", str(project_a.resolve()))
            assert len(chunks) == 1
            assert "changed-a" in chunks[0]["content"]
            assert chunks[0]["embedding"] is None

        await indexer.index_long_term_memory_entries([
            {"category": "task", "title": "需求文档", "content": "支持项目隔离"},
            {"category": "task", "title": "开发计划", "content": "修改索引器"},
            {"category": "task", "title": "实现说明", "content": "使用项目路径过滤"},
            {"category": "task", "title": "空实现说明", "content": ""},
        ], source_session_id="session-a", project_path=str(project_a))
        memory_results = await hybrid_retrieve(
            "支持项目隔离",
            source_type="memory",
            project_path=str(project_a),
            top_k=10,
            db_path=db_path,
        )
        assert any("支持项目隔离" in item["content"] for item in memory_results)
        async with Database(db_path) as db:
            memory_sources = await db.list_rag_sources("memory", str(project_a))
            assert len(memory_sources) == 3
            assert all(item["source_path"].startswith("memory://task/") for item in memory_sources)

    asyncio.run(scenario())
