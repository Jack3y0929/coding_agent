"""多视角审查的确定性规则测试。"""

import json

from backend.graph.review import aggregate_review_reports, parse_review_report
from backend.graph.dev_workflow import _changes_from_execution


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
    assert "blocking_finding_marked_passed" in result["parse_errors"]


def test_medium_severity_finding_cannot_pass() -> None:
    report = _report(True, [{
        "severity": "medium", "category": "test_coverage", "file": "tests/test_app.py", "line": 8,
        "evidence": "关键失败路径没有回归测试", "recommendation": "补充失败路径测试",
    }])

    result = parse_review_report(report, "quality")

    assert result["passed"] is False
    assert "blocking_finding_marked_passed" in result["parse_errors"]


def test_any_finding_cannot_pass_even_when_low_severity() -> None:
    report = _report(True, [{
        "severity": "low", "category": "maintainability", "file": "README.md", "line": 10,
        "evidence": "说明文字可以更简洁", "recommendation": "精简说明文字",
    }])

    result = parse_review_report(report, "quality")

    assert result["passed"] is False
    assert "passed_with_findings" in result["parse_errors"]


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


def test_aggregate_merges_same_location_cross_perspective_root_cause() -> None:
    quality_finding = {
        "severity": "high", "category": "test_coverage", "file": "hello-dev/README.md", "line": 75,
        "evidence": "README.md:75 声称已执行 dir hello-dev 且退出码为 0，但没有真实执行记录。",
        "recommendation": "删除未执行命令的伪造输出并记录真实结果。",
    }
    security_finding = {
        "severity": "medium", "category": "security", "file": "hello-dev/README.md", "line": 75,
        "evidence": "README 第 75 行把未执行的 dir hello-dev 写成退出码 0，验证记录不真实。",
        "recommendation": "不要将未执行命令写成成功证据。",
    }
    results = [
        parse_review_report(_report(False, [quality_finding]), "quality"),
        parse_review_report(_report(False, [security_finding]), "security"),
        parse_review_report(_report(True, []), "logic"),
    ]
    state = {
        "review_round": 0,
        "review_logic_report": _report(True, []), "review_logic_result": results[2], "review_logic_tokens": 0,
        "review_security_report": _report(False, [security_finding]), "review_security_result": results[1], "review_security_tokens": 0,
        "review_quality_report": _report(False, [quality_finding]), "review_quality_result": results[0], "review_quality_tokens": 0,
    }

    aggregate = aggregate_review_reports(state, "dev", required_rounds=1)

    assert len(aggregate["review_findings"]) == 1
    assert aggregate["review_findings"][0]["perspectives"] == ["quality", "security"]


def test_changes_from_execution_normalizes_absolute_and_relative_paths(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    absolute = project / "hello-dev" / "index.html"
    changes = _changes_from_execution({
        "successful_writes": [
            {"path": str(absolute), "content": "absolute"},
            {"path": "hello-dev\\index.html", "content": "relative"},
        ],
    }, str(project))

    assert list(changes) == ["hello-dev/index.html"]
    assert changes["hello-dev/index.html"] == "relative"


def test_review_payload_can_be_recovered_from_fenced_json_without_heading() -> None:
    report = (
        "[审查通过]\n\n"
        "基于已核实代码，未发现阻塞问题。\n\n"
        "```json\n"
        '{"passed": true, "findings": []}'
        "\n```"
    )

    result = parse_review_report(report, "quality")

    assert result["passed"] is True
    assert result["findings"] == []
    assert result["parse_errors"] == []
