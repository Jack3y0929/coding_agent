"""工具调用的结构化结果模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


ToolResultCode = Literal[
    "ok",
    "invalid_arguments",
    "file_not_found",
    "permission_denied",
    "scope_denied",
    "command_not_allowed",
    "timeout",
    "nonzero_exit",
    "internal_error",
]


class ToolResult(BaseModel):
    """工具执行结果；保留机器可判断字段并提供兼容文本表示。"""

    ok: bool
    code: ToolResultCode
    message: str = ""
    data: Any | None = None
    retryable: bool = False
    suggestion: str | None = None
    tool_name: str | None = None

    def to_context_text(self) -> str:
        """转换成受限长度前的模型可读文本。"""
        parts = [self.message]
        if self.suggestion:
            parts.append(f"建议: {self.suggestion}")
        if self.data is not None:
            parts.append(f"数据: {self.data}")
        return "\n".join(item for item in parts if item)

    def __str__(self) -> str:
        return self.to_context_text()

    def startswith(self, prefix: str | tuple[str, ...]) -> bool:
        """兼容旧调用方对字符串结果的判断。"""
        return self.to_context_text().startswith(prefix)

    def __contains__(self, item: str) -> bool:
        return item in self.to_context_text()

    def __getitem__(self, key: int | slice) -> str:
        return self.to_context_text()[key]

    def __len__(self) -> int:
        return len(self.to_context_text())


def normalize_tool_result(value: ToolResult | str, tool_name: str = "") -> ToolResult:
    """兼容旧工具和测试替身返回的字符串。"""
    if isinstance(value, ToolResult):
        if value.tool_name:
            return value
        return value.model_copy(update={"tool_name": tool_name or None})
    text = str(value)
    failed = text.startswith(("错误", "拒绝", "工具执行异常", "命令超时"))
    if not failed:
        code: ToolResultCode = "ok"
    elif "不存在" in text:
        code = "file_not_found"
    elif "无权限" in text or "不在项目范围" in text:
        code = "permission_denied"
    elif "白名单" in text or "拒绝执行" in text:
        code = "command_not_allowed"
    elif "范围校验" in text:
        code = "scope_denied"
    elif "超时" in text:
        code = "timeout"
    else:
        code = "internal_error"
    return ToolResult(
        ok=not failed,
        code=code,
        message=text,
        retryable=code in {"invalid_arguments", "file_not_found", "timeout"},
        tool_name=tool_name or None,
    )
