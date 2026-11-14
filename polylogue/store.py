"""本地数据：通讯录、分类、会话历史、搜索。

存哪儿：`~/.polylogue/`
  config.json            设置、key、通讯录、分类
  conversations/<id>.json 每个会话一个文件（转录在里面）
  avatars/<contact_id>    用户上传的头像（data URI 文本）

为什么一个会话一个文件：搜索历史要能只读需要的那些，而不是每次把全部历史
读进内存；删一个会话也只动一个文件，不会把别的会话写坏。

所有写操作都是「写临时文件再替换」，进程被杀不会留下半个 JSON。
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

from . import config

__all__ = [
    "DATA_DIR",
    "CONV_DIR",
    "AVATAR_DIR",
    "DEFAULT_CONTACTS",
    "DEFAULT_CATEGORIES",
    "ensure_dirs",
    "load_state",
    "save_state",
    "contacts",
    "categories",
    "contact_by_id",
    "add_contact",
    "update_contact",
    "remove_contact",
    "list_conversations",
    "load_conversation",
    "save_conversation",
    "delete_conversation",
    "rename_conversation",
    "create_conversation",
    "search",
    "avatar_path",
    "save_avatar",
    "load_avatar",
]


def _data_dir() -> Path:
    """从 config 的路径推出来，这样测试改 CONFIG_PATH 就能整体隔离。"""
    return Path(config.CONFIG_PATH).parent


def _conv_dir() -> Path:
    return _data_dir() / "conversations"


def _avatar_dir() -> Path:
    return _data_dir() / "avatars"


# 为了测试能 patch，暴露成模块属性；实际使用时通过函数取
DATA_DIR = _data_dir
CONV_DIR = _conv_dir
AVATAR_DIR = _avatar_dir


# 默认通讯录：每家一个，id 固定（方便测试和默认群聊引用）。
# model id 都拿真 key 在 OpenRouter 上确认过存在。
DEFAULT_CONTACTS = [
    {
        "id": "claude",
        "name": "Claude",
        "provider": "openrouter",
        "model": "anthropic/claude-haiku-4.5",
        "category": "Anthropic",
        "role": "critic",
    },
    {
        "id": "gpt",
        "name": "GPT",
        "provider": "openrouter",
        "model": "openai/gpt-4o-mini",
        "category": "OpenAI",
        "role": "builder",
    },
    {
        "id": "gemini",
        "name": "Gemini",
        "provider": "openrouter",
        "model": "google/gemini-2.5-flash",
        "category": "Google",
        "role": "explorer",
    },
    {
        "id": "grok",
        "name": "Grok",
        "provider": "openrouter",
        "model": "x-ai/grok-4.6",
        "category": "xAI",
        "role": "critic",
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "provider": "openrouter",
        "model": "deepseek/deepseek-chat",
        "category": "深度求索",
        "role": "builder",
    },
    {
        "id": "qwen",
        "name": "通义千问",
        "provider": "openrouter",
        "model": "qwen/qwen-plus",
        "category": "阿里",
        "role": "builder",
    },
    {
        "id": "kimi",
        "name": "Kimi",
        "provider": "openrouter",
        "model": "moonshotai/kimi-k2",
        "category": "月之暗面",
        "role": "explorer",
    },
    {
        "id": "glm",
        "name": "智谱 GLM",
        "provider": "openrouter",
        "model": "z-ai/glm-4.6",
        "category": "智谱",
        "role": "reviewer",
    },
]

DEFAULT_CATEGORIES = ["常用", "Anthropic", "OpenAI", "Google", "xAI", "深度求索", "阿里", "月之暗面", "智谱"]


def ensure_dirs() -> None:
    _conv_dir().mkdir(parents=True, exist_ok=True)
    _avatar_dir().mkdir(parents=True, exist_ok=True)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 通讯录


def load_state() -> dict:
    """通讯录和分类。第一次调用时写入默认值。"""
    data = config.load()
    changed = False

    if not data.get("contacts"):
        data["contacts"] = [dict(c) for c in DEFAULT_CONTACTS]
        changed = True
    if not data.get("categories"):
        data["categories"] = list(DEFAULT_CATEGORIES)
        changed = True

    if changed:
        config.update(contacts=data["contacts"], categories=data["categories"])
    return {"contacts": data["contacts"], "categories": data["categories"]}


def save_state(state: dict) -> None:
    config.update(
        contacts=state.get("contacts", []),
        categories=state.get("categories", []),
    )


def contacts() -> list[dict]:
    return load_state()["contacts"]


def categories() -> list[str]:
    return load_state()["categories"]


def contact_by_id(cid: str) -> dict | None:
    for c in contacts():
        if c.get("id") == cid:
            return c
    return None


def add_contact(
    name: str,
    provider: str,
    model: str = "",
    category: str = "常用",
    role: str = "",
    base_url: str = "",
) -> dict:
    state = load_state()
    cid = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or uuid.uuid4().hex[:8]
    if any(c.get("id") == cid for c in state["contacts"]):
        cid = f"{cid}-{uuid.uuid4().hex[:4]}"
    item = {
        "id": cid,
        "name": name.strip() or cid,
        "provider": provider,
        "model": model,
        "category": category or "常用",
        "role": role,
        "base_url": base_url or "",
    }
    state["contacts"].append(item)
    if item["category"] not in state["categories"]:
        state["categories"].append(item["category"])
    save_state(state)
    return item


def update_contact(cid: str, **changes) -> dict | None:
    state = load_state()
    for c in state["contacts"]:
        if c.get("id") == cid:
            for k in ("name", "provider", "model", "category", "role", "base_url"):
                if k in changes and changes[k] is not None:
                    c[k] = changes[k]
            if c["category"] not in state["categories"]:
                state["categories"].append(c["category"])
            save_state(state)
            return c
    return None


def remove_contact(cid: str) -> bool:
    state = load_state()
    before = len(state["contacts"])
    state["contacts"] = [c for c in state["contacts"] if c.get("id") != cid]
    if len(state["contacts"]) == before:
        return False
    save_state(state)
    path = _avatar_dir() / cid
    if path.is_file():
        path.unlink()
    return True


# ---------------------------------------------------------------- 会话


def _conv_path(cid: str) -> Path:
    """会话 id 来自 URL，必须挡住路径穿越。

    注意这里不是「净化后接着用」，而是「净化后和原值不一致就拒绝」。
    前者会把 `../evil` 悄悄变成 `evil.json`，写到调用方以为之外的文件去；
    拒绝更安全，也更容易在测试里发现传错了。
    """
    raw = str(cid or "")
    safe = re.sub(r"[^A-Za-z0-9_-]", "", raw)
    if not safe or safe != raw:
        raise ValueError("非法会话 id")
    return _conv_dir() / f"{safe}.json"


def _now() -> float:
    return time.time()


def list_conversations() -> list[dict]:
    """只读摘要，不读转录 —— 列表里有几十个会话时这很重要。"""
    out: list[dict] = []
    folder = _conv_dir()
    if not folder.is_dir():
        return out
    for path in folder.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append(
            {
                "id": data.get("id") or path.stem,
                "title": data.get("title") or "未命名",
                "kind": data.get("kind") or "group",
                "members": data.get("members") or [],
                "category": data.get("category") or "常用",
                "turns": len(data.get("turns") or []),
                "updated": data.get("updated") or 0,
                "last": _last_snippet(data),
            }
        )
    out.sort(key=lambda c: -c["updated"])
    return out


def _last_snippet(data: dict, limit: int = 40) -> str:
    for turn in reversed(data.get("turns") or []):
        text = (turn.get("content") or "").strip().replace("\n", " ")
        if text:
            return text[:limit]
    return ""


def load_conversation(cid: str) -> dict | None:
    """查会话。id 非法时返回 None，不抛 —— 这是查询，不是写入。"""
    try:
        path = _conv_path(cid)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_conversation(data: dict) -> dict:
    cid = data.get("id") or uuid.uuid4().hex[:12]
    data["id"] = cid
    data.setdefault("created", _now())
    data["updated"] = _now()
    _atomic_write(_conv_path(cid), json.dumps(data, ensure_ascii=False, indent=1))
    return data


def delete_conversation(cid: str) -> bool:
    try:
        path = _conv_path(cid)
    except ValueError:
        return False
    if path.is_file():
        path.unlink()
        return True
    return False


def rename_conversation(cid: str, title: str, category: str | None = None) -> dict | None:
    data = load_conversation(cid)
    if data is None:
        return None
    data["title"] = title.strip() or data.get("title")
    if category:
        data["category"] = category
    return save_conversation(data)


def create_conversation(
    title: str,
    members: list[str],
    *,
    kind: str = "group",
    category: str = "常用",
    cid: str | None = None,
) -> dict:
    data = {
        "id": cid or uuid.uuid4().hex[:12],
        "title": title.strip() or "未命名",
        "kind": kind,
        "members": list(members),
        "category": category or "常用",
        "created": _now(),
        "updated": _now(),
        "turns": [],
    }
    return save_conversation(data)


# ---------------------------------------------------------------- 搜索


def search(query: str, limit: int = 60) -> list[dict]:
    """在标题和全部发言里找。返回带上下文的片段。

    大小写不敏感；空查询返回空，不是返回全部 —— 调用方不该靠这个拿全量。
    """
    q = (query or "").strip().lower()
    if not q:
        return []

    hits: list[dict] = []
    for summary in list_conversations():
        data = load_conversation(summary["id"])
        if not data:
            continue
        if q in (data.get("title") or "").lower():
            hits.append(
                {
                    "conversation": summary["id"],
                    "title": summary["title"],
                    "speaker": "",
                    "snippet": data.get("title") or "",
                    "where": "标题",
                    "updated": summary["updated"],
                }
            )
        for turn in data.get("turns") or []:
            text = turn.get("content") or ""
            idx = text.lower().find(q)
            if idx < 0:
                continue
            lo = max(0, idx - 40)
            hi = min(len(text), idx + len(q) + 60)
            snippet = ("…" if lo else "") + text[lo:hi].replace("\n", " ") + ("…" if hi < len(text) else "")
            hits.append(
                {
                    "conversation": summary["id"],
                    "title": summary["title"],
                    "speaker": turn.get("speaker") or "",
                    "snippet": snippet,
                    "where": "发言",
                    "updated": summary["updated"],
                }
            )
            if len(hits) >= limit:
                return hits
    return hits[:limit]


# ---------------------------------------------------------------- 头像


def avatar_path(cid: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", cid) or "generic"
    return _avatar_dir() / safe


def save_avatar(cid: str, data_uri: str) -> bool:
    """存用户上传的头像。只接受 data URI，不接受 URL（避免变成 SSRF 入口）。"""
    if not data_uri.startswith("data:image/"):
        return False
    # 1MB 上限，别让 config 目录变成图床
    if len(data_uri) > 1_400_000:
        return False
    ensure_dirs()
    _atomic_write(avatar_path(cid), data_uri)
    return True


def load_avatar(cid: str) -> str | None:
    path = avatar_path(cid)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return text if text.startswith("data:image/") else None
