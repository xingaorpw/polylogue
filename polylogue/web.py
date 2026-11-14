"""本地 Web 工作台。

模型：通讯录（contacts）→ 会话（conversations，dm 或 group）→ 转录（turns）。

会话落盘在 `~/.polylogue/conversations/<id>.json`，所以关掉程序再打开历史还在，
搜索也有东西可搜。内存里只在真正跑会商时构造 `Room`，跑完写回磁盘。

同一时刻只允许一个会话在跑（run_lock）。并行跑多个会让成本失控，
而成本失控正是这类工具最容易死的地方。
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

from . import avatars, bootstrap, config, credentials, store
from .credentials import discover, mask, save_key
from .guard import Budget
from .pricing import PRICE_TABLE_DATE
from .providers import PRESETS, ProviderError, build_provider, resolve_name
from .room import Room
from .session import Session

__all__ = ["serve", "App"]

STATIC = Path(__file__).parent / "static"


# 只认本机回环。DNS rebinding 会把恶意域名解析到 127.0.0.1，
# 校验 Host 是唯一能识破的办法。
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

# 请求体上限。头像 data URI 最大 1.4MB，2MB 留足余量；
# 再大就是有人在拿我们当内存垃圾桶。
MAX_BODY = 2 * 1024 * 1024


def ensure_utf8_streams() -> None:
    """Windows 下 stdout 可能是 cp1252，打印中文会抛 UnicodeEncodeError。

    这个异常会直接杀掉服务线程 —— 用户看到的是「启动后立刻连不上」。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def _out(text: str) -> None:
    # 终端输出也脱敏：错误信息里可能夹带上游回显的凭据
    text = credentials.redact(text)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


def _scrub(obj):
    """递归脱敏。

    为什么不只在错误分支里处理：接口一多，总有一个分支会漏。
    统一在出口擦一遍，新增字段也不会意外带出 key。
    """
    if isinstance(obj, str):
        return credentials.redact(obj)
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    return obj


class QuietServer(ThreadingHTTPServer):
    """浏览器关标签页、客户端提前断开都会让 wfile.write 抛异常。

    这些不是服务端问题，但默认实现会把整段 traceback 打到 stderr，
    在 CI 日志里看着像挂了。这里只吞掉「对端断开」。
    """

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class App:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.run_lock = threading.Lock()
        self.cancels: dict[str, threading.Event] = {}
        self.skipped: list[str] = []
        self.origin = ""

    def cancel_for(self, cid: str) -> threading.Event:
        with self.lock:
            return self.cancels.setdefault(cid, threading.Event())


def _budget() -> Budget:
    cfg = config.load().get("budget") or {}
    return Budget(
        max_rounds=int(cfg.get("max_rounds") or 6),
        max_calls=int(cfg.get("max_calls") or 24),
        max_cost_usd=cfg.get("max_cost_usd"),
    )


def _providers_for(member_ids: list[str]) -> tuple[dict, list[str], list[str]]:
    """按通讯录成员建 provider。返回 (providers, 名字列表, 跳过原因)。"""
    keys = discover()
    providers: dict = {}
    names: list[str] = []
    skipped: list[str] = []

    for cid in member_ids:
        contact = store.contact_by_id(cid)
        if not contact:
            skipped.append(f"{cid}: 通讯录里没有这个人")
            continue
        name = contact.get("name") or cid
        # 名字不能重复，否则 Room 里会认错人
        if name in providers:
            name = f"{name}({cid})"
        try:
            providers[name] = build_provider(
                contact.get("provider") or "openrouter",
                model=contact.get("model") or None,
                api_key=keys.get(contact.get("provider") or ""),
                # 自定义端点（自建 / 本地服务）优先用通讯录里填的
                base_url=contact.get("base_url") or None,
            )
            names.append(name)
        except ProviderError as exc:
            skipped.append(f"{name}: {exc}")
    return providers, names, skipped


def _name_map(member_ids: list[str]) -> dict[str, str]:
    """成员名 → 联系人 id，给前端找头像用。"""
    out = {}
    for cid in member_ids:
        contact = store.contact_by_id(cid)
        if contact:
            out[contact.get("name") or cid] = cid
    return out


def _room_from(conv: dict) -> tuple[Room, dict, list[str]]:
    providers, names, skipped = _providers_for(conv.get("members") or [])
    roles = {}
    for cid in conv.get("members") or []:
        contact = store.contact_by_id(cid)
        if contact and contact.get("role"):
            roles[contact.get("name") or cid] = contact["role"]

    room = Room(names, roles=roles)
    # 回填历史：这样多轮对话接得上，压缩也基于完整历史
    for turn in conv.get("turns") or []:
        if turn.get("role") == "user":
            room.add_user(turn.get("content") or "")
        elif turn.get("speaker") in names:
            room.add_model(turn["speaker"], turn.get("content") or "")
    return room, providers, skipped


def _summarize(conv: dict) -> dict:
    turns = conv.get("turns") or []
    total = sum(int(t.get("tokens") or 0) for t in turns)
    cost = sum(float(t.get("cost") or 0) for t in turns)
    unknown = any(t.get("cost") is None and not t.get("error") for t in turns if t.get("role") == "assistant")
    return {
        "id": conv.get("id"),
        "title": conv.get("title"),
        "kind": conv.get("kind") or "group",
        "members": conv.get("members") or [],
        "category": conv.get("category") or "常用",
        "turns": len(turns),
        "tokens": total,
        "cost": cost,
        "cost_unknown": unknown,
        "updated": conv.get("updated") or 0,
        "last": store._last_snippet(conv),
    }


def serve(
    members: list[str] | None = None,
    *,
    port: int = 8765,
    host: str = "127.0.0.1",
    budget: Budget | None = None,
    roles: dict[str, str] | None = None,
    custom: dict[str, str] | None = None,
    models: dict[str, str] | None = None,
    on_ready: Callable[[str], None] | None = None,
) -> int:
    del members, roles, custom, models  # 新版按通讯录组织，命令行成员参数不再使用
    app = App()
    store.ensure_dirs()

    keys = discover()
    app.origin = (
        "自动（检测到 OpenRouter 凭据）" if "openrouter" in keys else ("自动（检测到本机凭据）" if keys else "")
    )

    # 第一次进来：建一个默认群聊，让用户打开就能说话
    if store.list_conversations() == [] and keys:
        state = store.load_state()
        picked = [c["id"] for c in state["contacts"][:3]]
        if picked:
            store.create_conversation("默认群聊", picked, kind="group", category="常用")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        # ------------------------------------------------------ 工具

        def _json(self, code: int, obj) -> None:
            data = json.dumps(_scrub(obj), ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _bytes(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _text(self, code: int, body: str, ctype: str) -> None:
            self._bytes(code, body.encode("utf-8"), ctype)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            if length > MAX_BODY:
                raise ValueError("请求体太大")
            return json.loads(self.rfile.read(length) or b"{}")

        # -------------------------------------------------- 来源校验

        def _host_ok(self) -> bool:
            """Host 必须是本机回环，挡 DNS rebinding。

            否则恶意域名可以解析到 127.0.0.1，浏览器就把它当成同源，
            页面里的脚本能直接读写这个工作台。
            """
            host = (self.headers.get("Host") or "").strip().lower()
            if not host:
                return False
            if host.startswith("["):  # [::1]:8765
                name = host.split("]")[0] + "]"
            else:
                name = host.rsplit(":", 1)[0]
            return name in _LOOPBACK_HOSTS

        def _origin_ok(self) -> bool:
            """有 Origin 就必须也是本机，挡跨站请求。

            浏览器发的跨源写请求一定带 Origin；同源页面和 curl 不带或带本机。
            """
            origin = (self.headers.get("Origin") or "").strip()
            if not origin:
                return True
            parsed = urlparse(origin)
            if parsed.scheme not in ("http", "https"):
                return False
            host = (parsed.hostname or "").lower()
            return host in _LOOPBACK_HOSTS

        def _guard_write(self) -> bool:
            """写操作的三道闸。过了返回 True；没过已经回过响应，返回 False。

            第三道是关键：表单只能发 text/plain / urlencoded / multipart，
            发不出 application/json；而跨源发 application/json 会触发预检，
            我们从不回 CORS 头，浏览器会挡下来。所以「必须是 JSON」
            等于「必须过预检」，把表单 CSRF 这条路堵死。

            无 body 的写操作（DELETE）允许不带 Content-Type —— 表单也发不出
            DELETE，这条路上没有 CSRF 面。
            """
            if not self._host_ok():
                self._json(403, {"error": "只接受本机访问"})
                return False
            if not self._origin_ok():
                self._json(403, {"error": "拒绝跨站请求"})
                return False

            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                self._json(400, {"error": "Content-Length 不合法"})
                return False

            if ctype not in ("", "application/json") or (length > 0 and ctype != "application/json"):
                self._json(415, {"error": "写操作必须用 Content-Type: application/json"})
                return False
            return True

        def _parts(self) -> tuple[list[str], dict]:
            parsed = urlparse(self.path)
            # 路径段要解码，否则中文 id 永远匹配不上；解码后的 `/`
            # 会留在段内，后续 store 的路径检查会把它挡掉。
            return [unquote(p) for p in parsed.path.split("/") if p], parse_qs(parsed.query)

        # ------------------------------------------------------ 组装

        def _info(self) -> dict:
            cfg = config.load()
            state = store.load_state()
            keys = discover()
            sources = bootstrap.credential_sources()

            def usable(name: str) -> bool:
                needs_key = bool(PRESETS[name]["env"]) if name in PRESETS else True
                return (name in keys) or not needs_key

            return {
                "theme": cfg.get("theme") or "dark",
                "onboarding": not keys,
                "origin": app.origin,
                "contacts": state["contacts"],
                "categories": state["categories"],
                "conversations": [_summarize(c) for c in self._all_convs()],
                "providers": [
                    {
                        "name": name,
                        "available": usable(name),
                        "masked": mask(keys.get(name, "")),
                        "source": sources.get(name, ""),
                    }
                    for name in sorted(PRESETS)
                ],
                "detected": bootstrap.detect(),
                "presets": bootstrap.MODEL_PRESETS,
                "context": cfg.get("context") or {},
                "budget": cfg.get("budget") or {},
                "price_table": PRICE_TABLE_DATE,
                "skipped": app.skipped,
                "avatar_names": avatars.avatar_names(),
            }

        def _all_convs(self) -> list[dict]:
            out = []
            for summary in store.list_conversations():
                data = store.load_conversation(summary["id"])
                if data:
                    out.append(data)
            return out

        def _avatar_bytes(self, key: str) -> tuple[bytes, str]:
            """先看用户上传，再用内置。key 可以是联系人 id 或 provider 名。"""
            uploaded = store.load_avatar(key)
            if uploaded:
                head, _, payload = uploaded.partition(",")
                mime = head.split(":")[1].split(";")[0] if ":" in head else "image/png"
                try:
                    return base64.b64decode(payload), mime
                except (ValueError, TypeError):
                    pass
            return avatars.avatar_for(key).encode("utf-8"), "image/svg+xml; charset=utf-8"

        def _guard_read(self) -> bool:
            """读操作也要校验来源。

            DNS rebinding 不只是能改配置 —— 恶意页面可以让自己的域名解析到
            127.0.0.1，然后在浏览器眼里变成同源，直接把 /api/conversations
            读走。Host 校验是唯一能识破这一招的地方。
            """
            if not self._host_ok():
                self._json(403, {"error": "只接受本机访问"})
                return False
            if not self._origin_ok():
                self._json(403, {"error": "拒绝跨站请求"})
                return False
            return True

        # ------------------------------------------------------ GET

        def do_GET(self) -> None:
            if not self._guard_read():
                return
            parts, query = self._parts()

            if not parts:
                index = STATIC / "index.html"
                if not index.is_file():
                    self._text(500, "static/index.html missing", "text/plain; charset=utf-8")
                    return
                self._text(200, index.read_text(encoding="utf-8"), "text/html; charset=utf-8")
                return

            if parts == ["api", "info"]:
                self._json(200, self._info())
                return

            if parts[:2] == ["api", "avatar"] and len(parts) == 3:
                body, ctype = self._avatar_bytes(parts[2])
                self._bytes(200, body, ctype)
                return

            if parts == ["api", "search"]:
                q = (query.get("q") or [""])[0]
                self._json(200, {"query": q, "hits": store.search(q)})
                return

            if parts == ["api", "conversations"]:
                self._json(200, {"conversations": [_summarize(c) for c in self._all_convs()]})
                return

            if len(parts) == 3 and parts[:2] == ["api", "conversations"]:
                conv = store.load_conversation(parts[2])
                if not conv:
                    self._json(404, {"error": "会话不存在"})
                    return
                self._json(
                    200,
                    {
                        **_summarize(conv),
                        "names": _name_map(conv.get("members") or []),
                        "turns": conv.get("turns") or [],
                        "compression": conv.get("compression") or {},
                    },
                )
                return

            if len(parts) == 4 and parts[:2] == ["api", "conversations"] and parts[3] == "export":
                conv = store.load_conversation(parts[2])
                if not conv:
                    self._json(404, {"error": "会话不存在"})
                    return
                self._text(200, self._export(conv), "text/markdown; charset=utf-8")
                return

            self._json(404, {"error": "not found"})

        def _export(self, conv: dict) -> str:
            summary = _summarize(conv)
            lines = [
                f"# {conv.get('title')}",
                "",
                f"- 类型：{'单聊' if conv.get('kind') == 'dm' else '群聊'}",
                f"- 成员：{', '.join(conv.get('members') or [])}",
                f"- tokens：{summary['tokens']}",
                f"- 估算成本：${summary['cost']:.4f}" + ("（含无价格模型，实际更高）" if summary["cost_unknown"] else ""),
                "",
                "---",
                "",
            ]
            for turn in conv.get("turns") or []:
                if turn.get("error"):
                    lines.append(f"**[{turn.get('speaker')}]** ✗ {turn['error']}")
                elif turn.get("role") == "user":
                    lines.append(f"**你：** {turn.get('content')}")
                else:
                    lines.append(f"**[{turn.get('speaker')}]** {turn.get('content')}")
                lines.append("")
            return "\n".join(lines)

        # ------------------------------------------------------ POST / PATCH / DELETE

        def do_POST(self) -> None:
            if not self._guard_write():
                return
            parts, _ = self._parts()

            try:
                if parts == ["api", "setup"]:
                    self._setup()
                    return
                if parts == ["api", "settings"]:
                    self._settings()
                    return
                if parts == ["api", "conversations"]:
                    self._create_conv()
                    return
                if parts == ["api", "contacts"]:
                    self._create_contact()
                    return
                if parts[:2] == ["api", "avatar"] and len(parts) == 3:
                    self._upload_avatar(parts[2])
                    return
                if len(parts) == 4 and parts[:2] == ["api", "conversations"] and parts[3] == "say":
                    self._say(parts[2])
                    return
                if len(parts) == 4 and parts[:2] == ["api", "conversations"] and parts[3] == "stop":
                    app.cancel_for(parts[2]).set()
                    self._json(200, {"ok": True})
                    return
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return

            self._json(404, {"error": "not found"})

        def do_PATCH(self) -> None:
            if not self._guard_write():
                return
            parts, _ = self._parts()
            try:
                data = self._body()
            except ValueError:
                self._json(400, {"error": "bad json"})
                return

            if len(parts) == 3 and parts[:2] == ["api", "conversations"]:
                conv = store.rename_conversation(
                    parts[2], data.get("title") or "", data.get("category")
                )
                if not conv:
                    self._json(404, {"error": "会话不存在"})
                    return
                self._json(200, _summarize(conv))
                return

            if len(parts) == 3 and parts[:2] == ["api", "contacts"]:
                contact = store.update_contact(parts[2], **data)
                if not contact:
                    self._json(404, {"error": "联系人不存在"})
                    return
                self._json(200, contact)
                return

            self._json(404, {"error": "not found"})

        def do_DELETE(self) -> None:
            if not self._guard_write():
                return
            parts, _ = self._parts()
            if len(parts) == 3 and parts[:2] == ["api", "conversations"]:
                if not store.delete_conversation(parts[2]):
                    self._json(404, {"error": "会话不存在"})
                    return
                self._json(200, {"ok": True})
                return
            if len(parts) == 3 and parts[:2] == ["api", "contacts"]:
                if not store.remove_contact(parts[2]):
                    self._json(404, {"error": "联系人不存在"})
                    return
                self._json(200, {"ok": True})
                return
            self._json(404, {"error": "not found"})

        # ------------------------------------------------------ 具体处理

        def _setup(self) -> None:
            data = self._body()
            provider = (data.get("provider") or "openrouter").strip()
            api_key = (data.get("api_key") or "").strip()
            if api_key:
                save_key(provider, api_key)
            config.update(onboarded=True)

            # 没有会话就建一个默认群聊
            if not store.list_conversations():
                state = store.load_state()
                picked = [c["id"] for c in state["contacts"][:3]]
                if picked:
                    store.create_conversation("默认群聊", picked, kind="group", category="常用")

            self._json(201, {"info": self._info()})

        def _settings(self) -> None:
            data = self._body()
            changes = {}
            if data.get("theme") in ("dark", "light"):
                changes["theme"] = data["theme"]
            if isinstance(data.get("context"), dict):
                changes["context"] = data["context"]
            if isinstance(data.get("budget"), dict):
                changes["budget"] = data["budget"]
            if changes:
                config.update(**changes)
            self._json(200, {"ok": True, "info": self._info()})

        def _create_conv(self) -> None:
            data = self._body()
            member_ids = [m for m in (data.get("members") or []) if store.contact_by_id(m)]
            if not member_ids:
                raise ValueError("至少选一个模型")
            kind = "dm" if len(member_ids) == 1 else "group"
            title = (data.get("title") or "").strip()
            if not title:
                names = [store.contact_by_id(m)["name"] for m in member_ids]
                title = names[0] if kind == "dm" else "、".join(names[:3])
            conv = store.create_conversation(
                title, member_ids, kind=kind, category=data.get("category") or "常用"
            )
            self._json(201, _summarize(conv))

        def _create_contact(self) -> None:
            data = self._body()
            name = (data.get("name") or "").strip()
            if not name:
                raise ValueError("名字不能为空")
            contact = store.add_contact(
                name,
                data.get("provider") or "openrouter",
                model=data.get("model") or "",
                category=data.get("category") or "常用",
                role=data.get("role") or "",
                base_url=data.get("base_url") or "",
            )
            self._json(201, contact)

        def _upload_avatar(self, cid: str) -> None:
            data = self._body()
            payload = data.get("data") or ""
            if not store.save_avatar(cid, payload):
                self._json(400, {"error": "头像必须是 1MB 以内的 data:image/... 内容"})
                return
            self._json(200, {"ok": True})

        # ------------------------------------------------------ SSE

        def _say(self, cid: str) -> None:
            conv = store.load_conversation(cid)
            if not conv:
                self._json(404, {"error": "会话不存在"})
                return
            data = self._body()
            text = (data.get("text") or "").strip()
            if not text:
                self._json(400, {"error": "空消息"})
                return

            room, providers, skipped = _room_from(conv)
            if not room.members:
                self._json(400, {"error": "没有可用模型：" + ("；".join(skipped) or "通讯录为空")})
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def emit(obj: dict) -> None:
                try:
                    payload = json.dumps(_scrub(obj), ensure_ascii=False)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            conv.setdefault("turns", []).append(
                {"speaker": "你", "role": "user", "content": text, "ts": store._now()}
            )

            new_turns: list[dict] = []

            def on_turn(turn) -> None:
                record = {
                    "speaker": turn.speaker,
                    "speaker_id": _name_map(conv.get("members") or []).get(turn.speaker, ""),
                    "role": "assistant",
                    "content": turn.content,
                    "tokens": turn.usage.total,
                    "cost": turn.cost,
                    "error": turn.error,
                    "ts": store._now(),
                }
                new_turns.append(record)
                if turn.error:
                    emit({"type": "error", "speaker": turn.speaker, "message": turn.error})
                else:
                    emit(
                        {
                            "type": "turn",
                            "speaker": turn.speaker,
                            "content": turn.content,
                            "tokens": turn.usage.total,
                            "cost": turn.cost,
                        }
                    )

            cancel = app.cancel_for(cid)
            cancel.clear()

            acquired = app.run_lock.acquire(timeout=180)
            if not acquired:
                emit({"type": "error", "speaker": "system", "message": "另一个会话正在会商，等待超时"})
                emit({"type": "done", "stop": None, "tokens": 0, "cost": 0.0})
                return

            try:
                cfg = config.load().get("context") or {}
                enabled = cfg.get("enabled", True)
                session = Session(
                    room,
                    providers,
                    budget=budget or _budget(),
                    on_turn=on_turn,
                    should_stop=cancel.is_set,
                    keep_recent=(cfg.get("keep_recent", 8) if enabled else None),
                    context_budget=(cfg.get("budget_tokens", 6000) if enabled else None),
                )
                try:
                    result = session.say(text)
                except Exception as exc:  # noqa: BLE001 - 任何异常都要让前端看到
                    emit({"type": "error", "speaker": "system", "message": f"内部错误：{exc}"})
                    emit({"type": "done", "stop": None, "tokens": 0, "cost": 0.0})
                    return

                conv["turns"].extend(new_turns)
                conv["compression"] = room.last_compression
                store.save_conversation(conv)

                summary = _summarize(conv)
                emit(
                    {
                        "type": "done",
                        "stop": result.stop.message if result.stop else None,
                        "tokens": summary["tokens"],
                        "cost": summary["cost"],
                        "cost_unknown": summary["cost_unknown"],
                        "compression": room.last_compression,
                        "conversation": _summarize(conv),
                    }
                )
            finally:
                app.run_lock.release()

    server = QuietServer((host, port), Handler)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}"

    ensure_utf8_streams()
    _out(f"polylogue 工作台 → {url}")
    if keys:
        state = store.load_state()
        _out(f"通讯录：{len(state['contacts'])} 个模型（{app.origin}）")
        _out(f"会话：{len(store.list_conversations())} 个")
    else:
        _out("没有检测到可用的 API key —— 打开上面的地址，页面上填一个就能用。")
    _out(f"价格表版本：{PRICE_TABLE_DATE}（估算，以各家官网为准）")
    _out("Ctrl+C 退出")

    if on_ready:
        on_ready(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _out("\n已停止")
    finally:
        server.server_close()
    return 0
