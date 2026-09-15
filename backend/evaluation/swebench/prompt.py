"""构造不包含官方答案的 SWE-bench Agent 输入。"""

from backend.evaluation.swebench.models import SWEbenchInstance

SYSTEM_RULES = """
你正在解决一个 SWE-bench 软件工程任务。只根据 issue 描述和仓库源码定位并修复问题。
禁止查看、搜索或猜测官方参考补丁及测试补丁；禁止修改测试来伪造通过；禁止修改仓库外文件。
所有写入必须位于当前项目目录。完成后必须留下可应用的 git diff。
这是无人值守评测：信息不足时基于源码作出最合理判断，不得请求人工输入。
""".strip()


def issue_prompt(instance: SWEbenchInstance) -> str:
    """只将公开 issue 信息交给 Agent，不包含测试名称和答案字段。"""
    return (
        f"SWE-bench 实例：{instance.instance_id}\n"
        f"目标仓库：{instance.repo}\n"
        f"基线提交：{instance.base_commit}\n\n"
        f"Issue 描述：\n{instance.problem_statement}"
    )
