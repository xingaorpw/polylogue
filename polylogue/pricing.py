"""价格表。用来算这次会商花了多少钱。

价格会变，所以：
- 这里是「每百万 token 的美元单价」，单位写清楚
- 查不到就返回 None，界面上显示「未知」，不瞎猜
- 表里的数字来自 OpenRouter `/api/v1/models` 的 pricing 字段（2026-11 拉取），
  不是拍脑袋估的；但各家官网会调价，所以仍标注为估算

两个坑，都是真实踩过的：
1. OpenRouter 的模型 id 带 `vendor/` 前缀（`anthropic/claude-haiku-4.5`），
   而表里存的是短名。不剥前缀，所有模型都会匹配不到 → 全部显示「未知」。
2. `:free` 变体是真的 0 元，那是「已知的 0」，不是「未知」。两者不能混。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Price", "PRICES", "cost_of", "PRICE_TABLE_DATE"]

PRICE_TABLE_DATE = "2026-11"

PRICES: dict[str, tuple[float, float]] = {
    # 模型短名（或前缀）: (输入 $/M, 输出 $/M)
    # --- OpenAI
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "o3": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
    # --- Anthropic
    "claude-opus-4.6": (5.00, 25.00),
    "claude-opus-4": (5.00, 25.00),
    "claude-sonnet-4.5": (3.00, 15.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-haiku-4.5": (1.00, 5.00),
    "claude-3-5-haiku": (0.80, 4.00),
    # --- Google
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    # --- xAI
    "grok-4.6": (2.00, 6.00),
    "grok-4.5": (2.00, 6.00),
    "grok-4.3": (1.25, 2.50),
    "grok-4": (2.00, 6.00),
    "grok-3-mini": (0.30, 0.50),
    "grok-3": (3.00, 15.00),
    # --- DeepSeek
    "deepseek-chat": (0.2574, 1.0287),
    "deepseek-v3.2": (0.26, 0.38),
    "deepseek-reasoner": (0.28, 0.42),
    # --- 国内其它
    "qwen-plus": (0.26, 0.78),
    "qwen-max": (1.60, 6.40),
    "kimi-k2": (0.57, 2.30),
    "moonshot-v1-32k": (0.60, 2.50),
    "glm-4.6": (0.50, 2.00),
    "glm-4-plus": (0.70, 0.70),
    # --- 托管
    "llama-3.3-70b": (0.71, 0.71),
    "mistral-large": (2.00, 6.00),
    # --- 免费变体（这些确实 0 元）
    "ernie-5.5-flash-preview": (0.0, 0.0),
    "ling-3.2-flash": (0.0, 0.0),
    "particle-1": (0.0, 0.0),
    # --- 测试
    "mock-1": (0.0, 0.0),
}


@dataclass
class Price:
    input_per_m: float
    output_per_m: float


def _normalize(model: str) -> str:
    """把 `anthropic/claude-haiku-4.5` 变成 `claude-haiku-4.5`。

    同时去掉 `:batch` / `:free` 这类后缀，但 `:free` 要单独记下来 ——
    它是真的免费，不能当成「查不到价格」。
    """
    name = (model or "").strip().lower()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name


def _lookup(model: str) -> Price | None:
    name = _normalize(model)

    # `:free` 变体是已知的 0 元
    if name.endswith(":free"):
        return Price(0.0, 0.0)

    name = name.split(":", 1)[0]

    if name in PRICES:
        return Price(*PRICES[name])
    # 前缀匹配：grok-4.6-0301 这种带日期的版本落到 grok-4.6
    candidates = [k for k in PRICES if name.startswith(k)]
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
