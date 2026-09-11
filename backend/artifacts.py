"""结构化任务文档产物：统一编号、模板和 docs 目录落盘。"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from backend.config import PROJECT_ROOT

DOCS_DIR = Path(PROJECT_ROOT) / "docs"


def allocate_document_id(prefix: str) -> str:
    """按本地日期和 docs 中已有文件分配 T/D 文档编号。"""
    if prefix not in {"T", "D"}:
        raise ValueError("文档编号前缀必须是 T 或 D")
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    date_code = datetime.now().strftime("%y%m%d")
    pattern = re.compile(rf"^{prefix}{date_code}(\d{{2}})_")
    numbers = []
    for path in DOCS_DIR.glob(f"{prefix}{date_code}*_*.md"):
        match = pattern.match(path.name)
        if match:
            numbers.append(int(match.group(1)))
    sequence = max(numbers, default=0) + 1
    return f"{prefix}{date_code}{sequence:02d}"


def _write(document_id: str, suffix: str, content: str) -> Path:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    path = DOCS_DIR / f"{document_id}_{suffix}.md"
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return path


def render_requirement(document_id: str, project_path: str, content: str) -> str:
    return f"""# {document_id} - 需求概括

**文档编号**: {document_id}_需求概括  
**需求日期**: {datetime.now():%Y-%m-%d}  
**目标项目**: `{project_path}`  
**产物类型**: DEV需求概括

---

## 1. 任务概述

### 1.1 任务目标

{content}

### 1.2 任务范围

以以上需求概括中的目标模块、变更范围和保护路径为准；未明确内容标记为“未指定”。

### 1.3 核心交付物

- 可验证的代码变更
- 对应测试或验证结果
- 本任务的开发反馈文档

## 2. 技术架构

### 2.1 数据流设计

由实现方案和源码证据确定，未确认内容不得臆测。

### 2.2 模块边界

由 RAG 检索和开发方案确认。

### 2.3 状态流转规则

由工作流和业务需求确认。

## 3. 接口/功能设计

### 3.1 输入格式

参见用户需求和已确认槽位。

### 3.2 输出格式

参见验收标准和实现说明。

### 3.3 用户交互或调用方式

未指定时由开发方案补充并标明依据。

## 4. 业务逻辑

### 4.1 主流程

按需求概括和开发方案执行。

### 4.2 异常流程

必须在开发方案和审查报告中明确。

### 4.3 边界条件

必须在验收标准中覆盖。

## 5. 数据结构与存储

由源码、RAG 和需求确认；未涉及时标记为“不适用”。

## 6. 外部依赖与集成

由项目源码和配置确认；禁止虚构依赖。

## 7. 错误处理

必须包含错误场景、返回信息和降级策略。

## 8. 技术约束

### 8.1 项目红线

遵守项目 AGENTS.md。

### 8.2 代码规范

遵守现有项目代码规范。

### 8.3 向后兼容

由验收和审查结果确认。

### 8.4 禁止修改范围

以保护路径和用户明确约束为准。

## 9. 验收标准

### 9.1 功能验收

以用户明确验收标准为准。

### 9.2 数据验收

检查数据流和持久化结果。

### 9.3 兼容性验收

执行项目允许的验证命令。

## 10. 参考文档

- `AGENTS.md`
- 项目内相关源码、测试和 RAG 检索结果
"""


def render_diagnosis(document_id: str, project_path: str, content: str) -> str:
    return f"""# {document_id} - 问题分析报告

**报告编号**: {document_id}  
**分析日期**: {datetime.now():%Y-%m-%d}  
**目标项目**: `{project_path}`  
**产物类型**: DEBUG问题分析

---

## 📋 1. 问题还原与影响范围评估

必须说明问题现象、复现步骤、发生频率、影响范围和环境信息。

{content}

## 🔍 2. 上下文信息收集

必须列出相关代码文件、关键函数、日志、运行时信息和历史修复记录。

## 🎯 3. 根因定位与论据验证

所有根因必须附带可追溯证据，并说明排除过的其他原因。

## 🛠️ 4. 逻辑修复方向建议

只输出修复方向，不在诊断阶段编写业务修复代码；标记 P0/P1/P2 优先级。

## ✅ 5. 验证标准

列出功能、回归、日志和运行时验证方式。

## ⚠️ 6. 技术风险提示

说明数据、兼容性、性能和测试覆盖风险。

## 📊 7. 影响范围与修复优先级

给出影响维度、修复优先级和预期修复效果。

## 🔗 8. 参考文档

- `AGENTS.md`
- 相关源码、日志、测试和历史任务
"""


def write_requirement(document_id: str, project_path: str, content: str) -> str:
    return str(_write(document_id, "需求概括", render_requirement(document_id, project_path, content)))


def write_diagnosis(document_id: str, project_path: str, content: str) -> str:
    return str(_write(document_id, "问题分析", render_diagnosis(document_id, project_path, content)))


def write_feedback(document_id: str, project_path: str, content: str) -> str:
    body = f"""# {document_id} - 开发反馈

**文档编号**: {document_id}_开发反馈  
**开发日期**: {datetime.now():%Y-%m-%d}  
**目标项目**: `{project_path}`

## 1. 任务完成情况
{content}

## 2. 技术实现细节
请列出文件修改清单、关键技术点和数据流。

## 3. 红线规则遵守情况
请列出路径安全、Plan/Build、预算、角色隔离和人工验收状态。

## 4. 编译和验证结果
请逐条列出实际执行的验证命令和结果。

## 5. 测试建议
请列出功能、边界、兼容性和回归测试。

## 6. 风险与注意事项
请列出已知限制和后续优化建议。

## 7. 代码质量
请说明规范、错误处理、可维护性和可测试性。

## 8. 总结
"""
    return str(_write(document_id, "开发反馈", body))


def write_prompt(document_id: str, project_path: str, content: str) -> str:
    body = f"""# {document_id} - 提示词

**文档编号**: {document_id}_提示词  
**目标项目**: `{project_path}`

## 1. 角色指令

本任务使用项目角色 Prompt，并遵守 `AGENTS.md`。

## 2. 上下文

{content}

## 3. 输入/输出规范

- 输入：正式需求概括、源码/RAG证据和工作流状态。
- 输出：可追溯的正式产物和代码变更。

## 4. 质量规则

- 角色上下文隔离
- Plan/Build严格分离
- 路径和Shell白名单校验
- Token预算和人工验收

## 5. 执行步骤

需求分析 → 方案规划 → 代码实施 → 验证 → 审查 → 人工验收。
"""
    return str(_write(document_id, "提示词", body))


def write_example_code(document_id: str, project_path: str, content: str) -> str:
    body = f"""# {document_id} - 示例代码

**文档编号**: {document_id}_示例代码  
**目标项目**: `{project_path}`

## 1. 文件修改概览

示例代码来自本次开发节点的正式代码变更。

## 2. 关键实现代码

{content}

## 3. 验证代码

验证代码和命令以实际工具执行结果为准。

## 4. 与实际实现的差异说明

未明确为完整文件的内容均视为示例片段，不替代真实代码变更。
"""
    return str(_write(document_id, "示例代码", body))
