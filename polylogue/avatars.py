"""Q 版头像：每个主流模型一张手绘 SVG。

为什么自己画：装图片依赖或者从网上抓图都会让这个项目多一个外部依赖/外链，
而 SVG 是纯文本，标准库就能发，离线可用，放大不糊。

风格统一：圆脸、大眼睛、腮红、微笑。每家靠「配色 + 一个标志性元素」区分，
不靠写字——小尺寸下写字看不清。

用户可以上传自己的头像覆盖，存在 config 里（base64），优先级高于内置。
"""

from __future__ import annotations

__all__ = ["AVATARS", "avatar_for", "avatar_names"]

# 统一的脸：圆脸 + 大眼 + 高光 + 腮红 + 微笑
_FACE = """
  <ellipse cx="23" cy="31" rx="5" ry="5.8" fill="#141821"/>
  <ellipse cx="41" cy="31" rx="5" ry="5.8" fill="#141821"/>
  <circle cx="24.8" cy="29" r="1.9" fill="#ffffff"/>
  <circle cx="42.8" cy="29" r="1.9" fill="#ffffff"/>
  <ellipse cx="15" cy="39" rx="4" ry="2.5" fill="#ff9db1" opacity="0.7"/>
  <ellipse cx="49" cy="39" rx="4" ry="2.5" fill="#ff9db1" opacity="0.7"/>
  <path d="M27 42.5 q5 4.8 10 0" stroke="#141821" stroke-width="2.3"
        fill="none" stroke-linecap="round"/>
"""


def _svg(top: str, bottom: str, motif: str = "", extra: str = "") -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">'
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{top}"/><stop offset="1" stop-color="{bottom}"/>'
        "</linearGradient></defs>"
        '<circle cx="32" cy="32" r="31" fill="url(#g)"/>'
        f"{motif}{_FACE}{extra}</svg>"
    )


AVATARS: dict[str, str] = {
    # OpenAI：墨绿底 + 那朵六瓣结
    "openai": _svg(
        "#10a37f",
        "#0b7a5e",
        '<g stroke="#eafff8" stroke-width="2.6" fill="none" opacity="0.95">'
        '<path d="M32 12 l4 7 -4 7 -4 -7z"/>'
        '<path d="M44 20 l-1 8 -7 4 1 -8z"/>'
        '<path d="M44 38 l-7 4 -4 -7 7 -4z"/>'
        '<path d="M32 46 l-4 -7 4 -7 4 7z"/>'
        '<path d="M20 38 l1 -8 7 -4 -1 8z"/>'
        '<path d="M20 20 l7 -4 4 7 -7 4z"/></g>',
    ),
    # Claude：珊瑚橙 + Anthropic 的星芒
    "claude": _svg(
        "#e8825f",
        "#c25a3c",
        '<g stroke="#fff3ec" stroke-width="2.4" stroke-linecap="round" opacity="0.95">'
        '<path d="M32 11 v9"/><path d="M32 44 v9"/><path d="M11 32 h9"/><path d="M44 32 h9"/>'
        '<path d="M17 17 l6.5 6.5"/><path d="M40.5 40.5 l6.5 6.5"/>'
        '<path d="M47 17 l-6.5 6.5"/><path d="M23.5 40.5 l-6.5 6.5"/></g>',
    ),
    # Gemini：蓝紫渐变 + 四角星
    "gemini": _svg(
        "#6c8cff",
        "#9d6bff",
        '<path d="M32 12 q3 12 12 15 q-9 3 -12 15 q-3 -12 -12 -15 q9 -3 12 -15z" '
        'fill="#ffffff" opacity="0.92"/>',
    ),
    # Grok：黑底 + 一道斜杠
    "grok": _svg(
        "#2b2f3a",
        "#0d1017",
        '<g stroke="#ffffff" stroke-width="3.4" stroke-linecap="round" opacity="0.95">'
        '<path d="M20 16 l24 32"/><path d="M44 16 l-10 13"/></g>',
    ),
    # DeepSeek：深蓝 + 鲸鱼尾
    "deepseek": _svg(
        "#3b6ef0",
        "#1d3fa8",
        '<g fill="#e8f0ff" opacity="0.95">'
        '<path d="M14 40 q9 -3 13 -9 q2 6 8 7 q-6 5 -13 4 z"/>'
        '<path d="M40 18 q4 -6 10 -5 q-4 3 -4 8 q-3 -3 -6 -3z"/></g>',
    ),
    # 通义千问：紫 + 云
    "qwen": _svg(
        "#9b6bff",
        "#6b3fd6",
        '<g fill="#f4ecff" opacity="0.95">'
        '<circle cx="19" cy="17" r="4.4"/><circle cx="27" cy="15" r="5.2"/>'
        '<circle cx="35" cy="17" r="4"/><rect x="17" y="16" width="19" height="5" rx="2.5"/></g>',
    ),
    # Kimi（月之暗面）：暗紫 + 弯月
    "kimi": _svg(
        "#4b3f6b",
        "#241d38",
        '<path d="M44 15 a13 13 0 1 0 6 16 a10.5 10.5 0 1 1 -6 -16z" fill="#f6f0ff" opacity="0.95"/>',
    ),
    # 智谱 GLM：蓝 + 六边形
    "glm": _svg(
        "#3f8cff",
        "#1f5fd0",
        '<path d="M32 12 l14 8 v16 l-14 8 -14 -8 v-16z" fill="none" '
        'stroke="#eaf2ff" stroke-width="2.6" opacity="0.95"/>',
    ),
    # Groq（推理托管）：橙红 + 闪电
    "groq": _svg(
        "#ff6b4a",
        "#c93a1d",
        '<path d="M35 11 l-11 21 h8 l-4 21 12 -23 h-8z" fill="#fff2ee" opacity="0.95"/>',
    ),
    # OpenRouter：多彩 + 分叉箭头
    "openrouter": _svg(
        "#5b6cff",
        "#a855f7",
        '<g stroke="#ffffff" stroke-width="2.6" fill="none" stroke-linecap="round" opacity="0.95">'
        '<path d="M16 32 h10"/><path d="M26 32 l8 -9 h8"/><path d="M26 32 l8 9 h8"/>'
        '<path d="M38 23 l4 0 -2 -3"/><path d="M38 41 l4 0 -2 3"/></g>',
    ),
    # Mistral：橙 + 三道风
    "mistral": _svg(
        "#ffb020",
        "#e07400",
        '<g stroke="#fff6e0" stroke-width="3" stroke-linecap="round" opacity="0.95">'
        '<path d="M14 18 h20"/><path d="M14 26 h26"/><path d="M14 34 h16"/></g>',
    ),
    # Together：青 + 两个相连的圆
    "together": _svg(
        "#14b8a6",
        "#0b7a6e",
        '<g fill="none" stroke="#e6fffb" stroke-width="2.8" opacity="0.95">'
        '<circle cx="24" cy="19" r="6"/><circle cx="40" cy="19" r="6"/></g>',
    ),
    # Ollama：黑白 + 羊驼耳朵
    "ollama": _svg(
        "#3a3f4b",
        "#14171e",
        '<g fill="#e8ebf2" opacity="0.95">'
        '<ellipse cx="20" cy="13" rx="4" ry="7" transform="rotate(-18 20 13)"/>'
        '<ellipse cx="44" cy="13" rx="4" ry="7" transform="rotate(18 44 13)"/></g>',
    ),
    # LM Studio：青绿 + 显示器
    "lmstudio": _svg(
        "#2dd4bf",
        "#0d9488",
        '<g fill="none" stroke="#ecfffd" stroke-width="2.6" opacity="0.95">'
        '<rect x="16" y="13" width="32" height="20" rx="3"/><path d="M26 39 h12"/></g>',
    ),
    # 兜底：不认识的就用中性灰 + 问号点
    "generic": _svg(
        "#6b7280",
        "#3f4653",
        '<circle cx="32" cy="16" r="3.6" fill="#eef1f6" opacity="0.9"/>',
    ),
}

# 别名 → 头像 key。用户可能用各种叫法。
_ALIASES = {
    "chatgpt": "openai",
    "gpt": "openai",
    "anthropic": "claude",
    "google": "gemini",
    "bard": "gemini",
    "xai": "grok",
    "x-ai": "grok",
    "qianwen": "qwen",
    "tongyi": "qwen",
    "moonshot": "kimi",
    "zhipu": "glm",
    "glm4": "glm",
    "together-ai": "together",
    "lm-studio": "lmstudio",
}


def avatar_names() -> list[str]:
    return sorted(AVATARS)


def avatar_for(name: str) -> str:
    """按 provider / 模型名 / 成员名找头像。找不到给 generic，不抛异常。"""
    key = (name or "").strip().lower()
    if key in AVATARS:
        return AVATARS[key]
    if key in _ALIASES:
        return AVATARS[_ALIASES[key]]
    # "openrouter:anthropic/claude-..." 这种带前缀的，按分隔符逐段试
    for part in key.replace("/", ":").replace("-", ":").split(":"):
        if part in AVATARS:
            return AVATARS[part]
        if part in _ALIASES:
            return AVATARS[_ALIASES[part]]
    for alias, target in _ALIASES.items():
        if alias in key:
            return AVATARS[target]
    return AVATARS["generic"]
