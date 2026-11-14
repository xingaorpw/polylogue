"""护栏：轮次、调用次数、成本、以及「它们是不是在互相点头」。

没有这层的多模型群聊是不可用的 —— 模型会互相附和，
一轮接一轮，最后账单比答案长。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Budget", "StopReason", "novelty", "STOP_TEXT", "MAX_UNKNOWN_CALLS"]

_WORD_RE = re.compile(r"[A-Za-z]{2,}|[\u4e00-\u9fff]")

# 查不到价格的调用最多放行几次。查不到价格时成本是「不知道」，
# 金额恒为 0，任何基于金额的上限都会失效 —— 只能按次数兜底。
MAX_UNKNOWN_CALLS = 4

# 出现这些短语，认为这个模型主张收敛
STOP_TEXT = (
    "可以定了",
    "没有更多补充",
    "我同意这个结论",
    "讨论可以结束",
    "没有异议",
    "conclusion reached",
    "no further comments",
)


@dataclass
class StopReason:
    code: str
    message: str


@dataclass
class Budget:
    max_rounds: int = 6
    max_calls: int = 24
    max_cost_usd: float | None = None
    max_tokens: int | None = None

    def check(
        self,
        *,
        rounds: int,
        calls: int,
        cost: float,
        cost_unknown: bool,
        tokens: int,
        unknown_calls: int = 0,
    ) -> StopReason | None:
        """超了任何一项就返回停止原因。"""
        if rounds >= self.max_rounds:
            return StopReason("max_rounds", f"到达轮次上限（{self.max_rounds} 轮）")
        if calls >= self.max_calls:
            return StopReason("max_calls", f"到达调用次数上限（{self.max_calls} 次）")
        if self.max_cost_usd is not None:
            if cost >= self.max_cost_usd:
                return StopReason("max_cost", f"到达成本上限（${self.max_cost_usd:.4f}）")
            # 有查不到价格的调用时，成本是下界，不能当成安全
            if cost_unknown and cost * 3 >= self.max_cost_usd:
                return StopReason(
                    "max_cost_uncertain",
                    f"有调用查不到价格，已知成本 ${cost:.4f} 已接近上限，停止以免超支",
                )
            # 全都查不到价格时 cost 恒为 0，上面那条永远不成立 ——
            # 按未知调用的次数兜底，否则成本上限等于不存在。
            if cost_unknown and unknown_calls >= MAX_UNKNOWN_CALLS:
                return StopReason(
                    "max_cost_uncertain",
                    f"已有 {unknown_calls} 次调用查不到价格，无法保证不超 "
                    f"${self.max_cost_usd:.4f}，已停止。换用价格表里的模型，或把成本上限调大",
                )
        if self.max_tokens is not None and tokens >= self.max_tokens:
            return StopReason("max_tokens", f"到达 token 上限（{self.max_tokens}）")
        return None


def _bag(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def novelty(text: str, previous: list[str]) -> float:
    """新发言相对已有发言的新信息比例，0~1。

    用词集合的差集算 —— 简单，但足以抓出「四个模型说同一句话」这种情况。
    """
    fresh = _bag(text)
    if not fresh:
        return 0.0
    seen: set[str] = set()
    for item in previous:
        seen |= _bag(item)
    if not seen:
        return 1.0
    return len(fresh - seen) / len(fresh)


def looks_converged(text: str) -> bool:
    lowered = text.lower()
    return any(phrase.lower() in lowered for phrase in STOP_TEXT)
