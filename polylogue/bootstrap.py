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
        rows: list[dict] = []
        seen: set[str] = set()
        for n in explicit:
            if n in seen:
                # `--models a,a` 不该变成两个同名成员
                continue
            seen.add(n)
            rows.append({"name": n, "provider": resolve_name(n), "model": None})
        return rows, "命令行指定"

    cfg = config.load()
    saved = cfg.get("members") or []
    # 旧格式可能是 ["claude"] 这种字符串数组：原样返回会让下游
    # item["name"] 直接 TypeError，所以先归一成 dict
    saved = [_as_member(m) for m in saved if isinstance(m, (dict, str))]
    saved = [m for m in saved if m]
    if saved:
        return saved, "上次保存的配置"

    keys = discover()
    if "openrouter" in keys:
        # 一个 key 覆盖所有厂商，直接上「省钱」档。
        # 必须深拷贝：返回的是模块级常量，调用方 append 一个成员
        # 就会污染整个进程的预设（后续 detect() 也会带着它）。
        return [dict(m) for m in MODEL_PRESETS["省钱"]], "自动（检测到 OpenRouter 凭据）"

    for provider in ("openai", "claude", "gemini", "deepseek", "grok"):
        if provider in keys and provider in SINGLE_PROVIDER_DEFAULTS:
            return [dict(m) for m in SINGLE_PROVIDER_DEFAULTS[provider]], f"自动（检测到 {provider} 凭据）"

    # 只认这五家的话，装了 qwen / kimi / glm / groq 的 key 会得到
    # 「没有可用凭据」—— 明明检测到了却用不了。这里按 PRESETS 现场拼。
    for provider in sorted(keys):
        preset = PRESETS.get(provider)
        if not preset:
            continue
        # 本地端点（ollama/lmstudio）没有 key，不该出现在「检测到凭据」里
        if not preset.get("env"):
            continue
        model = preset.get("model")
        if not model:
            continue
        return (
            [{"name": provider, "provider": provider, "model": model}],
            f"自动（检测到 {provider} 凭据）",
        )

    return [], "没有可用凭据，需要引导"


def _as_member(item) -> dict | None:
    """把成员归一成 {name, provider, model}。认不出就返回 None。"""
    if isinstance(item, str):
        name = item.strip()
        if not name:
            return None
        return {"name": name, "provider": resolve_name(name), "model": None}
    if not isinstance(item, dict):
        return None
    name = item.get("name") or item.get("provider")
    if not isinstance(name, str) or not name.strip():
        return None
    provider = item.get("provider") or name
    model = item.get("model")
    return {
        "name": name,
        "provider": resolve_name(provider) if isinstance(provider, str) else name,
        "model": model if isinstance(model, str) and model else None,
    }


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
    seen: set[str] = set()

    for raw in members:
        item = _as_member(raw)
        if not item:
            skipped.append(f"{raw!r}：成员格式不对，已忽略")
            continue
        name = item["name"]
        if name in seen:
            # 同名成员会让上游被调用两次、收两段一样的回答，token 翻倍，
            # 而且 providers 字典里第二个会覆盖第一个 —— 用户以为是两个人。
            skipped.append(f"{name}：重名成员，已忽略")
            continue
        seen.add(name)
        # 别名（anthropic/xai/google/gpt）要归一到规范名再查 key ——
        # 否则 keys.get("anthropic") 永远是 None，成员会被静默跳过
        provider = item["provider"] or name
        canonical = resolve_name(provider)
        model = item["model"] or models.get(name) or models.get(canonical)

        kwargs = {
            "model": model,
            "base_url": custom.get(canonical) or custom.get(provider) or custom.get(name),
        }
        key = keys.get(canonical) or keys.get(provider)
        if key:
            kwargs["api_key"] = key

        try:
            providers[name] = build_provider(provider, **kwargs)
            active.append(name)
        except ProviderError as exc:
            skipped.append(f"{name}（{provider}）: {exc}")

    return providers, active, skipped
