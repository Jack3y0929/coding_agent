"""FeatureBench 公开需求提示词。"""

from backend.evaluation.featurebench.models import FeatureBenchTask


def task_prompt(task: FeatureBenchTask) -> str:
    """不向 Agent 暴露测试列表、metadata 或任何参考修复。"""
    return (
        f"FeatureBench 任务：{task.instance_id}\n\n"
        f"功能需求：\n{task.problem_statement}\n\n"
        "验收要求：完整实现上述功能，保持已有功能兼容，并留下可应用的 Git diff。"
    )
