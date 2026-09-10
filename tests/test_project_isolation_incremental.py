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

    asyncio.run(scenario())
