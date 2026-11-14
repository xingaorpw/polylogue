"""polylogue —— 让多个模型在一个房间里会商，并且能互相点名说话。"""

__version__ = "0.3.0"

from .guard import Budget, StopReason, novelty
from .pricing import cost_of
from .providers import (
    ALIASES,
    PRESETS,
    AnthropicProvider,
    GeminiProvider,
    Message,
    MockProvider,
    OpenAICompatProvider,
    Provider,
    ProviderError,
    Reply,
    Usage,
    build_provider,
    resolve_name,
)
from .room import Room, Turn
from .session import Session, SessionResult

__all__ = [
    "__version__",
    "Room",
    "Turn",
    "Session",
    "SessionResult",
    "Budget",
    "StopReason",
    "novelty",
    "Provider",
    "ProviderError",
    "Message",
    "Usage",
    "Reply",
    "OpenAICompatProvider",
    "GeminiProvider",
    "AnthropicProvider",
    "MockProvider",
    "build_provider",
    "PRESETS",
    "ALIASES",
    "resolve_name",
    "cost_of",
]
