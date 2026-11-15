"""凭据自动发现。

目标是「零配置」：用户机器上如果已经有可用的 key，就不要让他再填一遍。

查找顺序（先找到的赢）：
1. 环境变量 —— 最显式，用户自己设的优先
2. polylogue 自己的配置 `~/.polylogue/config.json`
3. DeepSeek Harness 的凭据 `~/.dsh/.credentials.yaml`

第 3 条是关键：很多用户已经在 DSH 里配过 OpenRouter / OpenAI 的 key，
而 OpenRouter 一个 key 就能调 Claude、GPT、Gemini、Grok、DeepSeek 全系。
复用它可以做到「装完就能用」。

安全约定：
- 只在本地读，不写日志、不上传、不回显完整值
- `mask()` 是唯一允许出现在界面/终端里的形式
- `redact()` 用来擦掉任何「可能已经混进文本里」的 key（错误信息、异常、日志）
- 用户可以用 `POLYLOGUE_NO_REUSE=1` 关掉对 DSH 的复用
- 终端输入走 `prompt_secret()`（getpass，无回显），不接受命令行参数 ——
  命令行参数会出现在进程列表和 shell 历史里
"""

from __future__ import annotations

import getpass
import json
import os
import re
import sys
from pathlib import Path

from . import config
from .providers import PRESETS

__all__ = [
    "discover",
    "available_providers",
    "mask",
    "redact",
    "prompt_secret",
    "credential_sources",
    "save_key",
    "DSH_CREDENTIALS",
]

DSH_CREDENTIALS = Path.home() / ".dsh" / ".credentials.yaml"

# 认得的 key 名 → provider 名。PRESETS 里的 env 字段是主要来源，
# 这里补上各家常见的别名。
_ALIASES = {
    "GOOGLE_API_KEY": "gemini",
    "OPENROUTER_KEY": "openrouter",
    "X_AI_API_KEY": "grok",
    "ANTHROPIC_KEY": "claude",
}

# 复用顺序：OpenRouter 放前面，因为一个 key 覆盖最多厂商
_PREFERRED = ["openrouter", "openai", "claude", "gemini", "grok", "deepseek"]

# 长得像 key 的东西。用来兜住「不知道具体值但一看就是 key」的泄漏，
# 比如上游错误响应体里回显了我们不认识的凭据。
#
# 下限给到 24：真实 key 都远超这个长度（OpenAI 48+、OpenRouter 70+、
# Groq 52+），而 `api-key-rotation-policy`、`sk-learn-pipeline-2026`
# 这类**普通标识符**只有 18–19 个字符 —— 下限 12 会把它们一起打成
# 「已隐藏」，日志变得读不懂，还会误导排查方向。
_KEY_SHAPE = re.compile(
    r"\b(?:sk|pk|gsk|xai|api)[-_][A-Za-z0-9_\-]{24,}"  # OpenAI / Anthropic / Groq / xAI / OpenRouter
    r"|\bAIza[0-9A-Za-z_\-]{35,}"  # Google
    r"|\bhf_[A-Za-z0-9]{30,}"  # HuggingFace
    r"|\bnvapi-[A-Za-z0-9_\-]{20,}"  # NVIDIA
    r"|\bpplx-[A-Za-z0-9]{20,}"  # Perplexity
)
# URL 里的 key：`?key=...` / `&api_key=...`。上游报错经常把整个请求 URL
# 回显出来，而这种 key 不带任何可识别前缀，正则兜底抓不到。
_KEY_QUERY = re.compile(
    r"([?&](?:key|api_key|apikey|token|access_token|secret)=)[^&\s\"'<>]+", re.I
)
_HIDDEN = "sk-…（已隐藏）"


def mask(value: str) -> str:
    """唯一允许出现在界面上的形式。

    只留前 4 后 2，且总长超过 16 才露这两段 —— 短 key 直接全藏，
    因为短串露 6 个字符可能就把大部分内容给出去了。
    """
    if not value:
        return ""
    value = value.strip()
    if len(value) <= 16:
        return "…"
    return f"{value[:4]}…{value[-2:]}"


def redact(text: str, secrets=None) -> str:
    """把 key 从任何要展示/记录的文本里抹掉。

    两层：
    1. 已知的 key 值 —— 精确替换
    2. 长得像 key 的串 —— 正则兜底，防止上游把别人的凭据回显进错误信息

    不传 `secrets` 时自己去发现一遍；传空列表表示只做正则兜底。
    """
    if not text or not isinstance(text, str):
        return text

    values = discover().values() if secrets is None else secrets
    for value in values:
        if isinstance(value, str) and len(value) >= 8 and value in text:
            text = text.replace(value, _HIDDEN)

    # 先处理 query 里的 key（它没有可识别前缀，只能靠位置认），
    # 再跑形状兜底 —— 顺序反了的话 `?key=sk-xxx` 会先被形状规则替换成
    # `?key=sk-…（已隐藏）`，query 规则就匹配不上了（不影响安全，但会留下
    # 半截 key 前缀在日志里）。
    text = _KEY_QUERY.sub(lambda m: m.group(1) + _HIDDEN, text)
    return _KEY_SHAPE.sub(_HIDDEN, text)


def prompt_secret(prompt: str = "粘贴 API key（输入时不显示）：") -> str:
    """从终端无回显地读一个 key。

    用 getpass 而不是 input()：终端不显示、不进 shell 历史、不进进程列表。

    为什么要先自己判 isatty：Windows 上 getpass 只要 `sys.stdin is sys.__stdin__`
    就会走 msvcrt.getwch() 读**真实键盘**，完全忽略管道 —— 输入被重定向时
    会直接卡死。所以不是终端就不交给它，自己读一行并明确告知「回显关不掉」，
    不假装安全。
    """
    try:
        interactive = sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        interactive = False

    if not interactive:
        print("（输入来自管道/重定向，这里关不掉回显）", file=sys.stderr, flush=True)
        try:
            return (sys.stdin.readline() or "").strip()
        except (AttributeError, OSError, ValueError):
            return ""

    try:
        value = getpass.getpass(prompt)
    except (EOFError, OSError):
        return ""
    return (value or "").strip()


def _parse_dsh_refs(text: str) -> dict[str, str]:
    """从 DSH 凭据里取 refs 块。

    那个块是 YAML flow mapping，但值是**不带引号**的裸串，
    所以不能用普通 YAML 解析器的思路去要求引号。这里按行取：
    键是 `大写字母数字下划线`，值是冒号后到行尾（去掉可能的尾逗号）。

    只解析 refs 块，别的段（含 secret / token）不碰。
    """
    idx = text.find("refs:")
    if idx < 0:
        return {}

    out: dict[str, str] = {}
    started = False
    for line in text[idx:].splitlines()[1:]:
        s = line.strip()
        if not s:
            continue
        if s.startswith("}"):
            break
        m = re.match(r"^([A-Za-z0-9_]+)\s*:\s*(.+)$", s)
        if not m:
            continue
        key, value = m.group(1), m.group(2).rstrip(",").strip()
        value = value.strip('"').strip("'")
        if value and value not in ("{", "}"):
            out[key] = value
            started = True
    return out if started else {}


def _from_env() -> dict[str, str]:
    found = {}
    for name, preset in PRESETS.items():
        env = preset.get("env")
        if env and os.environ.get(env):
            found[name] = os.environ[env]
    if not found.get("gemini") and os.environ.get("GOOGLE_API_KEY"):
        found["gemini"] = os.environ["GOOGLE_API_KEY"]
    return found


def _from_own_config() -> dict[str, str]:
    if not config.CONFIG_PATH.is_file():
        return {}
    try:
        data = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return {}
    keys = data.get("api_keys")
    if not isinstance(keys, dict):
        return {}
    return {k: v for k, v in keys.items() if isinstance(v, str) and v}


def _from_dsh() -> dict[str, str]:
    if os.environ.get("POLYLOGUE_NO_REUSE"):
        return {}
    if not DSH_CREDENTIALS.is_file():
        return {}
    try:
        refs = _parse_dsh_refs(DSH_CREDENTIALS.read_text(encoding="utf-8-sig", errors="replace"))
    except OSError:
        return {}

    # 环境变量名 → provider 名
    by_env = {preset["env"]: name for name, preset in PRESETS.items() if preset.get("env")}

    found = {}
    for ref_name, value in refs.items():
        provider = by_env.get(ref_name) or _ALIASES.get(ref_name)
        if provider and provider in PRESETS:
            found.setdefault(provider, value)
    return found


def credential_sources() -> dict[str, str]:
    """{provider: 来源说明}，用于在界面上告诉用户「这个 key 是哪来的」。

    顺序必须和 `discover()` 的覆盖顺序一致，否则 `--whoami` 会显示
    **env 的掩码配 config 的来源**：env 和 config 都有 openrouter 时，
    用户看到的是环境变量的 key，却被告知「来自 polylogue 配置」，
    然后照着提示去改配置，怎么改都没用。
    """
    sources = {}
    # 优先级从低到高覆盖，最后写的最优先
    for name in _from_dsh():
        sources[name] = "复用 DSH 凭据"
    for name in _from_own_config():
        sources[name] = "polylogue 配置"
    for name in _from_env():
        sources[name] = "环境变量"
    return sources


def discover() -> dict[str, str]:
    """返回 {provider: api_key}。本地模型（ollama/lmstudio）不需要 key，不在这里。"""
    merged: dict[str, str] = {}
    # 后面的覆盖前面的：环境变量优先，所以最后写
    for layer in (_from_dsh(), _from_own_config(), _from_env()):
        merged.update(layer)
    return merged


def available_providers() -> list[str]:
    """当前机器上真正可用的 provider，按「覆盖厂商多」排序。"""
    keys = discover()
    usable = [n for n in _PREFERRED if n in keys]
    usable += [n for n in sorted(keys) if n not in usable]
    # 本地模型不需要 key，永远可用
    usable += ["ollama", "lmstudio"]
    return usable


def save_key(provider: str, api_key: str) -> None:
    """写进自己的配置，不动 DSH 的文件。

    走 `config.write_json_atomic`：原子写 + fsync + 收紧权限。
    以前这里是直接 `write_text`，崩在中间会把整个 config 写坏 ——
    连带里面的通讯录和分类一起丢。

    整个读-改-写必须持 `config._WRITE_LOCK`：不持锁的话，`--login` 在慢盘上
    读到旧快照 → 另一线程 `config.update(theme=...)` 落盘 → 这里再写，
    就把对方的改动整段覆盖掉了（实测 theme 被写回 None）。
    """
    with config._WRITE_LOCK:
        data = {}
        if config.CONFIG_PATH.is_file():
            try:
                # utf-8-sig：BOM 会让 json.loads 报错，然后这里会把整份
                # 配置当成空的，用 `{"api_keys": {...}}` 覆盖掉 ——
                # 通讯录、成员、主题全丢，还报「保存成功」。
                data = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8-sig"))
            except (ValueError, OSError):
                data = {}
        if not isinstance(data, dict):
            data = {}

        keys = data.get("api_keys")
        if not isinstance(keys, dict):
            keys = {}
        if api_key:
            keys[provider] = api_key
        else:
            keys.pop(provider, None)
        data["api_keys"] = keys

        config.write_json_atomic(config.CONFIG_PATH, data)
