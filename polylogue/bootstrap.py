"""启动装配：决定「用什么模型开工」。

这是「零门槛」的核心。优先级从高到低：

1. 命令行显式给的 `--models`（开发者/高级用户）
2. `~/.polylogue/config.json` 里保存的成员（用户上次选好的）
3. 本机能找到的凭据 → 自动挑一组覆盖不同厂商的模型（第一次打开就能用）
4. 什么都没有 → 进入引导模式，界面上填 key

第 3 条是关键：如果机器上已经有 OpenRouter / OpenAI 的 key，
用户不该再被问一遍「请配置」。
"""

from __future__ import annotations

from . import config
from .credentials import credential_sources, discover, mask
from .providers import PRESETS, ProviderError, build_provider, resolve_name

__all__ = [
    "MODEL_PRESETS",
    "resolve_members",
    "build_all",
    "detect",
]

# 三档预设。每个成员都来自**不同厂商**——同厂模型容易说同样的话，
# 多方会商的价值在于视角不同，不是同一个人说三遍。
#
# 这里的 model id 都是拿真 key 打过一遍确认还活着的（2026-11 实测）。
# OpenRouter 下线的模型会直接返回 404，写错一个用户第一次打开就报错，
# 所以宁可用老一点但确定能用的。
MODEL_PRESETS = {
    "省钱": [
        {"name": "critic", "provider": "openrouter", "model": "anthropic/claude-haiku-4.5"},
        {"name": "builder", "provider": "openrouter", "model": "openai/gpt-4o-mini"},
        {"name": "explorer", "provider": "openrouter", "model": "google/gemini-2.5-flash"},
    ],
    "强力": [
        {"name": "critic", "provider": "openrouter", "model": "anthropic/claude-opus-4.6"},
        {"name": "builder", "provider": "openrouter", "model": "openai/gpt-4o"},
        {"name": "explorer", "provider": "openrouter", "model": "google/gemini-2.5-pro"},
    ],
    # 免费档只放实测能出正文的模型。nvidia/nemotron 那几个
    # 会把预算全烧在思考上、content 返回空，不能放进默认档。
    "免费": [
        {"name": "critic", "provider": "openrouter", "model": "baidu/ernie-5.5-flash-preview:free"},
        {"name": "builder", "provider": "openrouter", "model": "inclusionai/ling-3.2-flash:free"},
        {"name": "explorer", "provider": "openrouter", "model": "particle-ai/particle-1:free"},
    ],
}

# 只有单家 key 时，用这家自己的模型凑一组（尽量拉开模型档次）
SINGLE_PROVIDER_DEFAULTS = {
    "openai": [
        {"name": "critic", "provider": "openai", "model": "gpt-4o"},
        {"name": "builder", "provider": "openai", "model": "gpt-4o-mini"},
    ],
    "claude": [
        {"name": "critic", "provider": "claude", "model": "claude-sonnet-4-20250514"},
        {"name": "builder", "provider": "claude", "model": "claude-3-5-haiku-20241022"},
    ],
    "gemini": [
        {"name": "critic", "provider": "gemini", "model": "gemini-2.5-pro"},
        {"name": "builder", "provider": "gemini", "model": "gemini-2.5-flash"},
    ],
    "deepseek": [
        {"name": "critic", "provider": "deepseek", "model": "deepseek-reasoner"},
        {"name": "builder", "provider": "deepseek", "model": "deepseek-chat"},
    ],
    "grok": [
        {"name": "critic", "provider": "grok", "model": "grok-3"},
        {"name": "builder", "provider": "grok", "model": "grok-3-mini"},
    ],
}


def detect() -> dict:
    """本机现在能用什么。给引导界面用。"""
    keys = discover()
    sources = credential_sources()
    return {
        "providers": [
            {
                "name": name,
                "label": PRESETS[name]["model"] if name in PRESETS else name,
                "available": True,
                "source": sources.get(name, ""),
                "masked": mask(keys.get(name, "")),
            }
            for name in keys
        ],
        "presets": {name: rows for name, rows in MODEL_PRESETS.items()},
        "onboarded": bool(config.load().get("onboarded")),
    }


def resolve_members(explicit: list[str] | None = None) -> tuple[list[dict], str]:
    """决定这次用哪些成员。返回 (成员列表, 来源说明)。

    成员是 `{"name", "provider", "model"}`；`name` 是房间里的称呼，
    `provider` 决定用哪个适配器，`model` 是具体模型 id。
    """
    if explicit:
        return (
            [{"name": n, "provider": resolve_name(n), "model": None} for n in explicit],
            "命令行指定",
        )

    cfg = config.load()
    saved = cfg.get("members") or []
    if saved:
        return saved, "上次保存的配置"

    keys = discover()
    if "openrouter" in keys:
        # 一个 key 覆盖所有厂商，直接上「省钱」档
        return MODEL_PRESETS["省钱"], "自动（检测到 OpenRouter 凭据）"

    for provider in ("openai", "claude", "gemini", "deepseek", "grok"):
        if provider in keys and provider in SINGLE_PROVIDER_DEFAULTS:
            return SINGLE_PROVIDER_DEFAULTS[provider], f"自动（检测到 {provider} 凭据）"

    return [], "没有可用凭据，需要引导"


def build_all(
    members: list[dict],
    custom: dict[str, str] | None = None,
    models: dict[str, str] | None = None,
) -> tuple[dict, list[str], list[str]]:
    """把成员列表变成真正的 provider。返回 (providers, 成功的名字, 跳过原因)。"""
    custom = custom or {}
    models = models or {}
    keys = discover()

    providers: dict = {}
    active: list[str] = []
    skipped: list[str] = []

    for item in members:
        name = item.get("name") or item.get("provider")
        provider = item.get("provider") or name
        model = item.get("model") or models.get(name)

        kwargs = {"model": model, "base_url": custom.get(provider) or custom.get(name)}
        key = keys.get(provider)
        if key:
            kwargs["api_key"] = key

        try:
            providers[name] = build_provider(provider, **kwargs)
            active.append(name)
        except ProviderError as exc:
            skipped.append(f"{name}（{provider}）: {exc}")

    return providers, active, skipped
