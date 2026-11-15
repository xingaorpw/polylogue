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
import threading
import time
import uuid
from pathlib import Path

from . import config

# 通讯录/分类的读-改-写要串行，否则两个请求同时加联系人会互相覆盖
_STATE_LOCK = threading.RLock()

# Windows 保留设备名：这些名字当文件名会炸（NUL 写出来是黑洞，
# 带 .tmp 再 replace 会 FileExistsError），无扩展名的头像文件正好踩中
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

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
    # 临时文件名必须唯一：以前是固定的 `<名字>.tmp`，两个线程同时写
    # 同一个会话时会互相删/覆盖对方的临时文件，失败方的写入静默丢失。
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        config._replace_with_retry(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    # 自己写完立刻让摘要缓存失效。
    #
    # 只靠 (mtime_ns, size) 判断是不行的：文件系统的时钟精度可能粗到几毫秒，
    # 而「旧名字」→「新名字」这种等长改名的字节数也一样，两次写落进同一个
    # tick 时指纹完全相同 —— 界面上连续改两次名，列表一直显示旧标题。
    _bump_summaries()


# ---------------------------------------------------------------- 通讯录


def load_state() -> dict:
    """通讯录和分类。第一次调用时写入默认值。

    字段类型也要校验：`"contacts": "oops"` 这种是「有值但类型错」，
    只判真值会把它原样返回，后面 `c.get(...)` 直接 AttributeError，
    整个 /api/info 挂掉。
    """
    data = config.load()
    changed = False

    contacts = data.get("contacts")
    if not isinstance(contacts, list) or not all(isinstance(c, dict) for c in contacts):
        contacts = [dict(c) for c in DEFAULT_CONTACTS]
        changed = True

    categories = data.get("categories")
    if not isinstance(categories, list) or not all(isinstance(c, str) for c in categories):
        categories = list(DEFAULT_CATEGORIES)
        changed = True

    if changed:
        config.update(contacts=contacts, categories=categories)
    return {"contacts": contacts, "categories": categories}


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
    """按 id 查联系人。

    这是热路径：`/api/info` 里每个会话的每个成员都会查一次，几十次调用。
    走 `load_shared()` 避免每次深拷贝整个 config（0.39 ms → 微秒级），
    返回浅拷贝，调用方改不到缓存本体。
    """
    contacts = config.load_shared().get("contacts")
    if isinstance(contacts, list) and all(isinstance(c, dict) for c in contacts):
        for c in contacts:
            if c.get("id") == cid:
                return dict(c)
        return None
    # 字段类型坏了：交给 load_state() 修好默认值再查
    for c in load_state()["contacts"]:
        if c.get("id") == cid:
            return c
    return None


def _text(value, default: str = "") -> str:
    """只接受字符串。别的类型退回默认值。

    这些字段会流进 `PRESETS.get(provider)`、文件路径、正则 —— 存进一个
    dict/list 的话，下一次加载会在别处炸，而且现场看起来跟这里毫无关系。
    数字可以安全转成字符串（用户传 `{"model": 4}` 就当 "4"）。
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return default


def add_contact(
    name: str,
    provider: str,
    model: str = "",
    category: str = "常用",
    role: str = "",
    base_url: str = "",
) -> dict:
    with _STATE_LOCK:
        state = load_state()
        name = _text(name, "")
        cid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        cid = cid or uuid.uuid4().hex[:8]
        if any(c.get("id") == cid for c in state["contacts"]):
            cid = f"{cid}-{uuid.uuid4().hex[:4]}"
        item = {
            "id": cid,
            "name": name or cid,
            "provider": _text(provider, "openrouter") or "openrouter",
            "model": _text(model, ""),
            "category": _text(category, "常用") or "常用",
            "role": _text(role, ""),
            "base_url": _text(base_url, ""),
        }
        state["contacts"].append(item)
        if item["category"] not in state["categories"]:
            state["categories"].append(item["category"])
        save_state(state)
        return item


def update_contact(contact_id: str, **changes) -> dict | None:
    # 参数名故意不叫 cid：`**changes` 里出现同名键会 TypeError
    # （`update_contact(x, cid=...)` → got multiple values for argument）。
    # 叫 contact_id 之后，客户端传什么键都只是被白名单忽略掉。
    with _STATE_LOCK:
        state = load_state()
        for c in state["contacts"]:
            if c.get("id") == contact_id:
                for k in ("name", "provider", "model", "category", "role", "base_url"):
                    if k not in changes or changes[k] is None:
                        continue
                    value = _text(changes[k], "")
                    # 空名字/空 provider 会把联系人变得不可用，宁可不改
                    if not value and k in ("name", "provider"):
                        continue
                    if not value and k == "category":
                        value = "常用"
                    c[k] = value
                if c["category"] not in state["categories"]:
                    state["categories"].append(c["category"])
                save_state(state)
                return c
        return None


def _avatar_file(cid: str) -> Path | None:
    """头像文件路径。id 不合法就返回 None —— 绝不用原始 id 拼路径。

    以前这里直接 `_avatar_dir() / cid` 然后 unlink()，一个 id 为
    `../config.json` 的联系人能让「删除联系人」把配置（含 key）删掉。
    """
    raw = str(cid or "")
    safe = re.sub(r"[^A-Za-z0-9_-]", "", raw)
    if not safe or safe != raw:
        return None
    if safe.lower() in _RESERVED:
        return None
    return _avatar_dir() / safe


def remove_contact(cid: str) -> bool:
    with _STATE_LOCK:
        state = load_state()
        before = len(state["contacts"])
        state["contacts"] = [c for c in state["contacts"] if c.get("id") != cid]
        if len(state["contacts"]) == before:
            return False
        save_state(state)
    path = _avatar_file(cid)
    if path is not None and path.is_file():
        try:
            path.unlink()
        except OSError:
            pass
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
    # 长度也要挡：300 个字符会让 open() 抛 OSError(Errno 22)，
    # 那不是 ValueError，web 层兜不住，直接 500。
    if len(safe) > 120:
        raise ValueError("会话 id 太长")
    return _conv_dir() / f"{safe}.json"


def _now() -> float:
    return time.time()


def _as_float(value) -> float:
    """排序用的时间戳。字符串/None/字典都当 0，不能让一个坏文件毁掉整个列表。"""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _turns_of(data: dict) -> list[dict]:
    turns = data.get("turns")
    if not isinstance(turns, list):
        return []
    return [t for t in turns if isinstance(t, dict)]


def _summary_of(path: Path, data: dict) -> dict:
    turns = _turns_of(data)
    members = data.get("members")
    return {
        # id 以文件名为准：内容里的 id 和文件名不一致时（复制/改名/还原），
        # 用内容里的 id 会出现「列表里有、点进去 404、删也删不掉」。
        "id": path.stem,
        "title": str(data.get("title") or "未命名"),
        "kind": str(data.get("kind") or "group"),
        "members": [str(m) for m in members] if isinstance(members, list) else [],
        "category": str(data.get("category") or "常用"),
        "turns": len(turns),
        "updated": _as_float(data.get("updated")),
        "last": _last_snippet({"turns": turns}),
    }


# 每个会话文件的摘要缓存，按 (mtime_ns, size) 校验。
# 以前每次 list_conversations 都把每个会话的**完整 JSON** 解析一遍
# （40 个会话实测 20 ms，/api/info 每次刷新都要跑），而这里只需要
# 标题、条数、最后一句。文件变了指纹就对不上，外部改动也不会读到旧值。
_SUMMARY_LOCK = threading.Lock()
_SUMMARIES: dict[str, tuple[int, int, int, dict]] = {}
# 本进程内的写次数，进缓存指纹。等长改名 + 粗粒度 mtime 时只有它能把
# 新旧两次写区分开（详见 _atomic_write 的注释）。
_SUMMARY_GEN = 0


def _bump_summaries() -> None:
    global _SUMMARY_GEN
    with _SUMMARY_LOCK:
        _SUMMARY_GEN += 1


def list_conversations() -> list[dict]:
    """只读摘要，不读转录 —— 列表里有几十个会话时这很重要。"""
    out: list[dict] = []
    folder = _conv_dir()
    if not folder.is_dir():
        return out

    seen: set[str] = set()
    for path in folder.glob("*.json"):
        key = str(path)
        seen.add(key)
        try:
            st = path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            continue
        with _SUMMARY_LOCK:
            hit = _SUMMARIES.get(key)
            gen = _SUMMARY_GEN
        if hit and hit[0] == stamp[0] and hit[1] == stamp[1] and hit[2] == gen:
            out.append(dict(hit[3]))
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        summary = _summary_of(path, data)
        with _SUMMARY_LOCK:
            _SUMMARIES[key] = (stamp[0], stamp[1], gen, summary)
        out.append(dict(summary))

    # 删掉的会话要把缓存一起清掉，否则这个字典只涨不跌
    with _SUMMARY_LOCK:
        for stale in [k for k in _SUMMARIES if k not in seen]:
            del _SUMMARIES[stale]

    # 排序键加 id 做次级比较：连续建两个对话时 updated 可能落在同一 tick，
    # 只按时间排的话列表顺序会随机跳，每次刷新都不一样。
    out.sort(key=lambda c: (-c["updated"], c["id"]))
    return out


def _last_snippet(data: dict, limit: int = 40) -> str:
    for turn in reversed(_turns_of(data)):
        content = turn.get("content")
        text = str(content).strip().replace("\n", " ") if content else ""
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
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def save_conversation(data: dict) -> dict:
    cid = data.get("id")
    # id 必须是字符串：数字 id 存进去之后，两条 updated 相同、id 一个
    # int 一个 str 时排序会抛 TypeError，整个列表接口 500。
    if not isinstance(cid, str) or not cid:
        cid = uuid.uuid4().hex[:12]
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
    clean = (title or "").strip() if isinstance(title, str) else ""
    data["title"] = clean or data.get("title")
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
    clean = (title or "").strip() if isinstance(title, str) else ""
    data = {
        "id": cid or uuid.uuid4().hex[:12],
        "title": clean or "未命名",
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
        title = str(data.get("title") or "")
        if q in title.lower():
            hits.append(
                {
                    "conversation": summary["id"],
                    "title": summary["title"],
                    "speaker": "",
                    "snippet": title,
                    "where": "标题",
                    "updated": summary["updated"],
                }
            )
        for turn in _turns_of(data):
            content = turn.get("content")
            text = str(content) if content else ""
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
                    "speaker": str(turn.get("speaker") or ""),
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
    """头像文件路径。只用于读内置兜底图（"generic"）。

    用户上传一律走 `_avatar_file()`，那里会拒绝非法 id ——
    这里「净化后接着用」会让 "a b" 和 "ab" 撞到同一个文件。
    """
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(cid or "")) or "generic"
    if safe.lower() in _RESERVED:
        safe = "generic"
    return _avatar_dir() / safe


def save_avatar(cid: str, data_uri: str) -> bool:
    """存用户上传的头像。只接受 data URI，不接受 URL（避免变成 SSRF 入口）。"""
    if not isinstance(data_uri, str) or not data_uri.startswith("data:image/"):
        return False
    # 1MB 上限，别让 config 目录变成图床
    if len(data_uri) > 1_400_000:
        return False
    path = _avatar_file(cid)
    if path is None:
        # id 非法（穿越、保留名、空）：拒绝，不要悄悄写到别的文件上
        return False
    ensure_dirs()
    try:
        _atomic_write(path, data_uri)
    except OSError:
        return False
    return True


def load_avatar(cid: str) -> str | None:
    path = avatar_path(cid)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    return text if text.startswith("data:image/") else None
