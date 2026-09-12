import asyncio

from backend.db.models import Database, init_db
from backend.rag.indexer import CodeIndexer
from backend.rag.memory_service import build_dev_memory_entries


class FakeEmbedding:
    dimension = 4

    def encode_to_bytes(self, text: str) -> bytes:
        return b"\x00" * 16


def test_dev_memory_keeps_result_facts_without_full_plan() -> None:
    entries = build_dev_memory_entries({
        "description": "增加登录功能",
        "summary": "任务完成",
        "implementation_note": "新增鉴权中间件",
        "plan": "这是不应进入长期记忆的完整计划正文",
        "requirement_doc": "这是不应进入长期记忆的完整需求正文",
        "code_changes": {"auth.py": "..."},
    })
    joined = "\n".join(item["content"] for item in entries)
    assert "新增鉴权中间件" in joined
    assert "完整计划正文" not in joined
    assert "完整需求正文" not in joined


def test_long_term_memory_deduplicates_and_records_metadata(tmp_path) -> None:
    async def scenario() -> None:
        db_path = str(tmp_path / "memory.db")
        project_path = str(tmp_path / "project")
        await init_db(db_path)
        async with Database(db_path) as db:
            await db.create_session("s1", "dev", project_path, "测试")
            await db.create_session("s2", "dev", project_path, "测试")
        indexer = CodeIndexer(db_path)
        indexer._engine = FakeEmbedding()
        entries = [{
            "category": "task",
            "memory_type": "lesson",
            "title": "实现经验",
            "content": "使用安全路径校验",
        }]
        first = await indexer.index_long_term_memory_entries(
            entries, source_session_id="s1", project_path=project_path,
        )
        second = await indexer.index_long_term_memory_entries(
            entries, source_session_id="s2", project_path=project_path,
        )
        assert first == second
        async with Database(db_path) as db:
            memories = await db.search_long_term_memory("安全路径", category="task")
            assert len(memories) == 1
            assert memories[0]["memory_type"] == "lesson"
            assert memories[0]["content_hash"]
            rows = await db.get_all_rag_embeddings("memory", project_path)
            assert len(rows) == 1
            assert rows[0]["memory_id"] == memories[0]["id"]

    asyncio.run(scenario())
