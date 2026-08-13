from __future__ import annotations

import logging
from typing import Any

from backend.config import MAX_COST_YUAN

logger = logging.getLogger("budget")

try:
    import tiktoken
    _ENCODER = tiktoken.get_encoding("cl100k_base")
except Exception as exc:
    _ENCODER = None
    logger.warning("tiktoken 编码不可用，token 计数将使用估算方式: %s", exc)


class BudgetExceededError(Exception):
    """预算超限异常"""
    pass


class TokenBudget:
    """Token预算管理器：每次AI调用前检查，调用后累加"""

    DEEPSEEK_INPUT_PRICE = 2.0   # 元/百万token
    DEEPSEEK_OUTPUT_PRICE = 8.0  # 元/百万token

    def __init__(self, max_cost: float = MAX_COST_YUAN) -> None:
        self.max_cost = max_cost
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.interrupted: bool = False

    def count_tokens(self, messages: list[dict]) -> int:
        """估算消息列表的token数"""
        total = 0
        for msg in messages:
            content = msg.get("content", "") or ""
            if _ENCODER:
                total += len(_ENCODER.encode(str(content)))
            else:
                total += _estimate_tokens(str(content))
            total += 4  # 每条消息的格式开销
        return total

    def before_call(self, messages: list[dict]) -> int:
        """调用前检查预算，返回预估输入token数。超限抛异常"""
        input_tokens = self.count_tokens(messages)
        estimated_cost = self._calc_cost(
            self.total_input_tokens + input_tokens,
            self.total_output_tokens
        )
        if estimated_cost >= self.max_cost:
            self.interrupted = True
            raise BudgetExceededError(
                f"预算超限：已用token约{self.total_input_tokens + self.total_output_tokens}，"
                f"本次需{input_tokens}token，预估费用{estimated_cost:.2f}元 > {self.max_cost}元上限"
            )
        return input_tokens

    def after_call(self, input_tokens: int, output_tokens: int) -> None:
        """调用后累加token"""
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        cost = self._calc_cost(self.total_input_tokens, self.total_output_tokens)
        logger.debug(f"Token累计: 输入={self.total_input_tokens}, "
                     f"输出={self.total_output_tokens}, 费用≈{cost:.2f}元")

    def get_status(self) -> dict[str, Any]:
        """获取当前预算状态（用于Web UI进度面板）"""
        cost = self._calc_cost(self.total_input_tokens, self.total_output_tokens)
        total = self.total_input_tokens + self.total_output_tokens
        return {
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": total,
            "estimated_cost_yuan": round(cost, 2),
            "max_cost_yuan": self.max_cost,
            "usage_percent": round(cost / self.max_cost * 100, 1) if self.max_cost > 0 else 0,
            "interrupted": self.interrupted,
        }

    def get_summary(self) -> str:
        """获取预算使用摘要"""
        status = self.get_status()
        return (f"[Token预算] 已用: {status['total_tokens']} token, "
                f"费用≈{status['estimated_cost_yuan']}元 "
                f"({status['usage_percent']}%), 上限{status['max_cost_yuan']}元")

    def _calc_cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens / 1_000_000 * self.DEEPSEEK_INPUT_PRICE +
                output_tokens / 1_000_000 * self.DEEPSEEK_OUTPUT_PRICE)


def _estimate_tokens(text: str) -> int:
    """估算文本的token数（无tiktoken时的后备方案：中文字符≈1token，英文单词≈1.3token）"""
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other_chars = len(text) - chinese_chars
    return chinese_chars + int(other_chars / 3.5)
