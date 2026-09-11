"""正式产物的最低结构校验。"""

from __future__ import annotations


REQUIREMENT_HEADINGS = ["## 1. 任务概述", "## 2. 技术架构", "## 3. 接口/功能设计", "## 4. 业务逻辑", "## 5. 数据结构与存储", "## 6. 外部依赖与集成", "## 7. 错误处理", "## 8. 技术约束", "## 9. 验收标准", "## 10. 参考文档"]
DIAGNOSIS_HEADINGS = ["## 📋 问题还原与影响范围评估", "## 🔍 上下文信息收集", "## 🎯 根因定位与论据验证", "## 🛠️ 逻辑修复方向建议", "## 📊 影响范围与修复优先级", "## 🔗 参考文档"]


def validate_artifact(content: str, artifact_type: str) -> tuple[bool, list[str]]:
    """返回是否通过以及缺失项，不修改模型内容。"""
    headings = REQUIREMENT_HEADINGS if artifact_type == "requirement" else DIAGNOSIS_HEADINGS if artifact_type == "diagnosis" else []
    missing = [heading for heading in headings if heading not in content]
    if artifact_type == "diagnosis" and "## 诊断判定数据" not in content:
        missing.append("## 诊断判定数据")
    return not missing, missing
