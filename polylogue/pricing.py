"""价格表。用来算这次会商花了多少钱。

价格会变，所以：
- 这里是「每百万 token 的美元单价」，单位写清楚
- 查不到就返回 None，界面上显示「未知」，不瞎猜
- 用 --price 可以在命令行覆盖
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Price", "PRICES", "cost_of", "PRICE_TABLE_DATE"]

# 这份表是 2026-11 的公开价格，仅用于估算。以各家官网为准。
PRICE_TABLE_DATE = "2026-11"

PRICES: dict[str, tuple[float, float]] = {
    # 模型名（或前缀）: (输入 $/M, 输出 $/M)
    # --- OpenAI
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "o3": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
    # --- Anthropic
    "claude-opus-4": (15.00, 75.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00),
    # --- Google
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    # --- xAI
    "grok-4": (3.00, 15.00),
    "grok-3": (3.00, 15.00),
    "grok-3-mini": (0.30, 0.50),
    # --- DeepSeek
    "deepseek-chat": (0.28, 0.42),
    "deepseek-reasoner": (0.28, 0.42),
    # --- 国内其它
    "qwen-plus": (0.40, 1.20),
    "qwen-max": (1.60, 6.40),
    "moonshot-v1-32k": (0.60, 2.50),
    "glm-4-plus": (0.70, 0.70),
    # --- 托管
    "llama-3.3-70b": (0.59, 0.79),
    "mistral-large": (2.00, 6.00),
    # --- 测试
    "mock-1": (0.0, 0.0),
}


@dataclass
class Price:
    input_per_m: float
    output_per_m: float


def _lookup(model: str) -> Price | None:
    if model in PRICES:
        return Price(*PRICES[model])
    # 前缀匹配：grok-3-0301 这种带日期的版本落到 grok-3
    candidates = [k for k in PRICES if model.startswith(k)]
    if not candidates:
        return None
    best = max(candidates, key=len)
    return Price(*PRICES[best])


def cost_of(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """返回美元成本；查不到价格返回 None，不要返回 0 冒充「免费」。"""
    price = _lookup(model)
    if price is None:
        return None
    return (prompt_tokens / 1_000_000) * price.input_per_m + (
        completion_tokens / 1_000_000
    ) * price.output_per_m
