"""统一的模型调用层。

Grok、DeepSeek、OpenAI、OpenRouter、Ollama 都是 OpenAI 兼容接口，
所以一个适配器覆盖它们；Gemini 是独立格式，单独一个。
两个适配器覆盖四家，不用引任何 SDK。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

__all__ = [
    "Message",
    "Usage",
    "Reply",
    "Provider",
    "ProviderError",
    "OpenAICompatProvider",
    "GeminiProvider",
    "AnthropicProvider",
    "MockProvider",
    "PRESETS",
    "ALIASES",
    "resolve_name",
    "build_provider",
]


@dataclass
class Message:
    role: str  # "user" | "assistant" | "system"
    content: str
    speaker: str = "user"  # 房间里的发言人标识，如 gemini / grok / user

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
        )


@dataclass
class Reply:
    text: str
    model: str
    usage: Usage = field(default_factory=Usage)
    latency_s: float = 0.0


class ProviderError(RuntimeError):
    """调用失败。带上 provider 名字，方便定位是哪家的问题。"""


class Provider:
    """适配器基类。"""

    name: str = "provider"
    model: str = ""

    def complete(self, messages: list[Message], system: str | None = None) -> Reply:
        raise NotImplementedError

    def _post(self, url: str, payload: dict, headers: dict[str, str], timeout: float = 120.0) -> dict:
        body = json.dumps(payload).encode("utf-8")
        req = Request(url, data=body, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:  # noqa: BLE001 - 读错误体失败不该掩盖原始错误
                pass
            raise ProviderError(f"{self.name} HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise ProviderError(f"{self.name} 连不上：{exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise ProviderError(f"{self.name} 请求失败：{exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"{self.name} 返回的不是 JSON：{exc}") from exc


class OpenAICompatProvider(Provider):
    """OpenAI 兼容接口。DeepSeek / Grok / OpenRouter / Ollama 都走这里。"""

    def __init__(self, name: str, base_url: str, api_key: str, model: str, timeout: float = 120.0):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def complete(self, messages: list[Message], system: str | None = None) -> Reply:
        payload_messages: list[dict[str, str]] = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        payload_messages.extend(m.as_dict() for m in messages)

        payload = {"model": self.model, "messages": payload_messages}
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        started = time.monotonic()
        data = self._post(f"{self.base_url}/chat/completions", payload, headers, self.timeout)
        latency = time.monotonic() - started

        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.name} 返回结构不对：{str(data)[:300]}") from exc

        raw_usage = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
            completion_tokens=int(raw_usage.get("completion_tokens") or 0),
        )
        return Reply(text=text.strip(), model=self.model, usage=usage, latency_s=latency)


class GeminiProvider(Provider):
    """Google Gemini。接口格式和 OpenAI 不一样，单独实现。"""

    BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash", timeout: float = 120.0):
        self.name = "gemini"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def complete(self, messages: list[Message], system: str | None = None) -> Reply:
        contents = []
        for m in messages:
            role = "model" if m.role == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": m.content}]})

        payload: dict[str, Any] = {"contents": contents}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        url = f"{self.BASE}/{self.model}:generateContent?key={self.api_key}"
        headers = {"Content-Type": "application/json"}

        started = time.monotonic()
        data = self._post(url, payload, headers, self.timeout)
        latency = time.monotonic() - started

        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, TypeError) as exc:
            # 被安全策略拦下时没有 candidates，单独报，不然看不出原因
            blocked = (data.get("promptFeedback") or {}).get("blockReason")
            if blocked:
                raise ProviderError(f"gemini 拒绝了这次请求：{blocked}") from exc
            raise ProviderError(f"gemini 返回结构不对：{str(data)[:300]}") from exc

        meta = data.get("usageMetadata") or {}
        usage = Usage(
            prompt_tokens=int(meta.get("promptTokenCount") or 0),
            completion_tokens=int(meta.get("candidatesTokenCount") or 0),
        )
        return Reply(text=text.strip(), model=self.model, usage=usage, latency_s=latency)


class AnthropicProvider(Provider):
    """Anthropic Claude。Messages API 和 OpenAI 不一样，而且 max_tokens 是必填。"""

    BASE = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 4096,
        timeout: float = 120.0,
    ):
        self.name = "claude"
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout

    def complete(self, messages: list[Message], system: str | None = None) -> Reply:
        payload_messages = []
        for m in messages:
            # Anthropic 只认 user / assistant
            role = "assistant" if m.role == "assistant" else "user"
            payload_messages.append({"role": role, "content": m.content})

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": payload_messages,
        }
        if system:
            payload["system"] = system

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.API_VERSION,
        }

        started = time.monotonic()
        data = self._post(self.BASE, payload, headers, self.timeout)
        latency = time.monotonic() - started

        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ProviderError(f"claude 返回结构不对：{str(data)[:300]}")
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

        raw_usage = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(raw_usage.get("input_tokens") or 0),
            completion_tokens=int(raw_usage.get("output_tokens") or 0),
        )
        return Reply(text=text.strip(), model=self.model, usage=usage, latency_s=latency)


class MockProvider(Provider):
    """测试用。可以预置回复，也能按脚本依次返回。"""

    def __init__(self, name: str = "mock", script: list[str] | None = None, model: str = "mock-1"):
        self.name = name
        self.model = model
        self.script = list(script or [])
        self.calls: list[list[Message]] = []

    def complete(self, messages: list[Message], system: str | None = None) -> Reply:
        self.calls.append(list(messages))
        if self.script:
            text = self.script.pop(0)
        else:
            text = f"{self.name} 的回复"
        return Reply(
            text=text,
            model=self.model,
            usage=Usage(prompt_tokens=10, completion_tokens=5),
            latency_s=0.0,
        )


# ---------------------------------------------------------------- 预设

# kind 决定用哪个适配器：
#   openai    —— OpenAI 兼容协议（绝大多数厂商都提供）
#   anthropic —— Claude 原生 Messages API
#   gemini    —— Google 原生 generateContent
PRESETS: dict[str, dict[str, str]] = {
    # --- 一线闭源
    "openai": {
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "env": "OPENAI_API_KEY",
        "model": "gpt-4o",
    },
    "claude": {
        "kind": "anthropic",
        "base_url": "",
        "env": "ANTHROPIC_API_KEY",
        "model": "claude-sonnet-4-20250514",
    },
    "gemini": {
        "kind": "gemini",
        "base_url": "",
        "env": "GEMINI_API_KEY",
        "model": "gemini-2.5-flash",
    },
    "grok": {
        "kind": "openai",
        "base_url": "https://api.x.ai/v1",
        "env": "XAI_API_KEY",
        "model": "grok-3",
    },
    # --- 国内
    "deepseek": {
        "kind": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "env": "DEEPSEEK_API_KEY",
        "model": "deepseek-chat",
    },
    "qwen": {
        "kind": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "env": "DASHSCOPE_API_KEY",
        "model": "qwen-plus",
    },
    "kimi": {
        "kind": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "env": "MOONSHOT_API_KEY",
        "model": "moonshot-v1-32k",
    },
    "glm": {
        "kind": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "env": "ZHIPU_API_KEY",
        "model": "glm-4-plus",
    },
    # --- 托管 / 聚合
    "groq": {
        "kind": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "env": "GROQ_API_KEY",
        "model": "llama-3.3-70b-versatile",
    },
    "openrouter": {
        "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "env": "OPENROUTER_API_KEY",
        "model": "anthropic/claude-sonnet-4",
    },
    "mistral": {
        "kind": "openai",
        "base_url": "https://api.mistral.ai/v1",
        "env": "MISTRAL_API_KEY",
        "model": "mistral-large-latest",
    },
    "together": {
        "kind": "openai",
        "base_url": "https://api.together.xyz/v1",
        "env": "TOGETHER_API_KEY",
        "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    },
    # --- 本地
    "ollama": {
        "kind": "openai",
        "base_url": "http://localhost:11434/v1",
        "env": "",
        "model": "llama3.2",
    },
    "lmstudio": {
        "kind": "openai",
        "base_url": "http://localhost:1234/v1",
        "env": "",
        "model": "local-model",
    },
}

# 常见叫法 → 规范名。用户说「加个 gpt」不该报错。
ALIASES: dict[str, str] = {
    "gpt": "openai",
    "chatgpt": "openai",
    "oai": "openai",
    "anthropic": "claude",
    "google": "gemini",
    "bard": "gemini",
    "xai": "grok",
    "x-ai": "grok",
    "aliyun": "qwen",
    "dashscope": "qwen",
    "tongyi": "qwen",
    "moonshot": "kimi",
    "zhipu": "glm",
    "bigmodel": "glm",
    "llama": "groq",
}


def resolve_name(name: str) -> str:
    key = name.strip().lower()
    return ALIASES.get(key, key)


def build_provider(
    name: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    max_tokens: int = 4096,
) -> Provider:
    """按名字建适配器。key 优先用参数，其次环境变量。"""
    key = resolve_name(name)
    preset = PRESETS.get(key)

    # 不认识的 provider：如果给了 base_url 就当 OpenAI 兼容端点
    if preset is None:
        if base_url:
            token = api_key or os.environ.get("LLM_API_KEY", "")
            return OpenAICompatProvider(
                name=key, base_url=base_url, api_key=token or "none", model=model or ""
            )
        known = ", ".join(sorted(PRESETS))
        raise ProviderError(f"不认识的 provider：{name}。可用：{known}；或用 --provider 名字=URL 自定义")

    env_name = preset["env"]
    token = api_key or (os.environ.get(env_name, "") if env_name else "")
    # Google 有两套叫法，两个都认
    if not token and key == "gemini":
        token = os.environ.get("GOOGLE_API_KEY", "")
    if env_name and not token:
        raise ProviderError(f"{name} 需要环境变量 {env_name}")

    kind = preset.get("kind", "openai")
    resolved_model = model or preset["model"]

    if kind == "anthropic":
        return AnthropicProvider(api_key=token, model=resolved_model, max_tokens=max_tokens)
    if kind == "gemini":
        return GeminiProvider(api_key=token, model=resolved_model)

    return OpenAICompatProvider(
        name=key,
        base_url=base_url or preset["base_url"],
        api_key=token or "none",
        model=resolved_model,
    )
