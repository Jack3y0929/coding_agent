"""五项能力核心契约的确定性回归测试。"""

import asyncio

import numpy as np
import pytest

from backend.budget import BudgetExceededError, TokenBudget, bind_budget, get_workflow_budget, unbind_budget
from backend.intent.models import CodingIntent
from backend.intent.rules import validate_slots
from backend.rag.retriever import cosine_similarity, rrf_fusion
from backend.tools.file_tools import read_file


def test_workflow_budget_is_shared_and_hard_limited() -> None:
    budget = TokenBudget(max_cost=0.00001)
    token = bind_budget(budget)
    try:
        assert get_workflow_budget() is budget
        budget.after_call(1, 1)
        with pytest.raises(BudgetExceededError):
            budget.before_call([{"role": "user", "content": "x" * 10000}])
    finally:
        unbind_budget(token)


def test_intent_rules_clarify_incomplete_debug_task() -> None:
    intent = CodingIntent(task_type="debug", confidence=0.95, task_description="修复接口")
    result = validate_slots(intent, "debug")
    assert result.decision == "clarify"
    assert "目标模块或修改范围" in result.missing_slots
    assert "实际异常表现" in result.missing_slots


def test_cosine_and_rrf_are_deterministic() -> None:
    assert cosine_similarity(np.array([1.0, 0.0]), np.array([1.0, 0.0])) == pytest.approx(1.0)
    assert cosine_similarity(np.array([1.0, 0.0]), np.array([1.0, 0.0, 0.0])) == 0.0
    keyword = [{"id": 1, "content": "a"}, {"id": 2, "content": "b"}]
    semantic = [{"id": 2, "content": "b"}, {"id": 3, "content": "c"}]
    result = rrf_fusion(keyword, semantic, k=1)
    assert [item["id"] for item in result] == [2, 1, 3]


def test_read_file_is_bounded_and_supports_line_ranges(tmp_path) -> None:
    target = tmp_path / "large.py"
    target.write_text("".join(f"line-{i}\n" for i in range(1, 301)), encoding="utf-8")

    default_result = asyncio.run(read_file(str(target)))
    assert "1: line-1" in default_result
    assert "line-241" not in default_result
    assert "start 和 end" in default_result

    ranged_result = asyncio.run(read_file(str(target), start=250, end=255))
    assert "250: line-250" in ranged_result
    assert "255: line-255" in ranged_result
    assert "249: line-249" not in ranged_result
