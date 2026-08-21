"""多视角审查的确定性规则测试。"""

import json

from backend.graph.review import aggregate_review_reports, parse_review_report


def _report(passed: bool, findings: list[dict]) -> str:
    marker = "[审查通过]" if passed else "[审查不通过]"
    return (
        f"# 审查报告\n\n{marker}\n\n## 审查判定数据\n```json\n"
        f'{{"passed": {str(passed).lower()}, "findings": {json.dumps(findings)}}}'
        "\n```"
    )


def test_high_severity_finding_cannot_pass() -> None:
    report = _report(True, [{
        "severity": "high", "category": "logic", "file": "backend/app.py", "line": 12,
        "evidence": "空输入会绕过校验", "recommendation": "在入口校验参数",
    }])

    result = parse_review_report(report, "logic")

    assert result["passed"] is False
    assert "high_severity_finding_marked_passed" in result["parse_errors"]


def test_aggregate_merges_duplicate_findings_and_blocks_release() -> None:
    finding = {
        "severity": "high", "category": "input_validation", "file": "backend/api.py", "line": 20,
        "evidence": "未校验请求参数", "recommendation": "使用 Pydantic 校验",
    }
    logic = parse_review_report(_report(False, [finding]), "logic")
    security = parse_review_report(_report(False, [finding]), "security")
    quality = parse_review_report(_report(True, []), "quality")
    state = {
        "review_round": 0,
        "review_logic_report": _report(False, [finding]),
        "review_logic_result": logic,
        "review_logic_tokens": 10,
        "review_security_report": _report(False, [finding]),
        "review_security_result": security,
        "review_security_tokens": 20,
        "review_quality_report": _report(True, []),
        "review_quality_result": quality,
        "review_quality_tokens": 30,
    }

    aggregate = aggregate_review_reports(state, "dev", required_rounds=1)

    assert aggregate["review_passed"] is False
    assert aggregate["review_round"] == 1
    assert aggregate["review_aggregate_tokens"] == 60
    assert len(aggregate["review_findings"]) == 1
    assert aggregate["review_findings"][0]["perspectives"] == ["logic", "security"]
