"""本地 Web 工作台。标准库 http.server + SSE，不需要 npm、不需要构建。

一个进程里可以同时存在多个房间，每个房间有自己的成员、预算和转录。
同一时刻只有一个房间在跑会商，其余房间排队 —— 这是有意的：
并行跑多个房间会让成本失控，而成本失控正是这类工具最容易死的地方。

启动时如果没有任何可用凭据，不报错退出，而是进入「引导模式」：
服务照常起来，界面显示填 key 的向导。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from . import bootstrap, config
from .credentials import available_providers, discover, mask, save_key
from .guard import Budget
from .pricing import PRICE_TABLE_DATE
from .providers import PRESETS, ProviderError, build_provider, resolve_name
from .room import Room
from .session import Session

__all__ = ["serve", "App", "RoomState"]

STATIC = Path(__file__).parent / "static"


def ensure_utf8_streams() -> None:
    """Windows 下 stdout 可能是 cp1252，打印中文会抛 UnicodeEncodeError。

    这个异常会直接杀掉服务线程 —— 用户看到的是「启动后立刻连不上」。
    这里把编码改成 utf-8 并允许替换，保证打印永远不会抛。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def _out(text: str) -> None:
    """打印兜底：即使 reconfigure 不可用，也不能因为编码问题让服务挂掉。"""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


@dataclass
class RoomState:
    id: str
    name: str
    room: Room
    providers: dict
    budget: Budget
    cancel: threading.Event = field(default_factory=threading.Event)

    def summary(self) -> dict:
        cost, unknown = self.room.total_cost()
        return {
            "id": self.id,
            "name": self.name,
            "members": self.room.members,
            "roles": dict(self.room.roles),
            "turns": len(self.room.turns),
            "tokens": self.room.total_usage.total,
            "cost": cost,
            "cost_unknown": unknown,
            "budget": {
                "max_rounds": self.budget.max_rounds,
                "max_calls": self.budget.max_calls,
                "max_cost_usd": self.budget.max_cost_usd,
            },
            "compression": self.room.last_compression,
        }

    def stats(self) -> list[dict]:
        """每个模型的 token / 花费明细。"""
        rows: dict[str, dict] = {}
        for turn in self.room.turns:
            if turn.role != "assistant":
                continue
            row = rows.setdefault(
                turn.speaker,
                {"speaker": turn.speaker, "turns": 0, "tokens": 0, "cost": 0.0, "unknown": False},
            )
            row["turns"] += 1
            row["tokens"] += turn.usage.total
            if turn.error:
                row["failed"] = row.get("failed", 0) + 1
                continue
            if turn.cost is None:
                row["unknown"] = True
            else:
                row["cost"] += turn.cost
        return sorted(rows.values(), key=lambda r: -r["tokens"])


class QuietServer(ThreadingHTTPServer):
    """浏览器关标签页、客户端提前断开都会让 wfile.write 抛异常。

    这些不是服务端的问题，但默认实现会把整段 traceback 打到 stderr，
    在 CI 日志里看着像挂了。这里只吞掉「对端断开」，其它异常照常抛。
    """

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class App:
    def __init__(self) -> None:
        self.rooms: dict[str, RoomState] = {}
        self.lock = threading.Lock()
        self.run_lock = threading.Lock()  # 同一时刻只跑一个房间
        self.skipped: list[str] = []
        self.origin = ""

    def add_room(self, state: RoomState) -> None:
        with self.lock:
            self.rooms[state.id] = state

    def get(self, room_id: str) -> RoomState | None:
        return self.rooms.get(room_id)

    def remove(self, room_id: str) -> bool:
        with self.lock:
            return self.rooms.pop(room_id, None) is not None

    def default_room(self) -> RoomState | None:
        for state in self.rooms.values():
            return state
        return None


def _budget_from_config() -> Budget:
    cfg = config.load().get("budget") or {}
    return Budget(
        max_rounds=int(cfg.get("max_rounds") or 6),
        max_calls=int(cfg.get("max_calls") or 24),
        max_cost_usd=cfg.get("max_cost_usd"),
    )


def _make_state(
    name: str,
    members: list,
    roles: dict[str, str] | None = None,
    max_rounds: int = 6,
    max_calls: int = 24,
    max_cost_usd: float | None = None,
) -> RoomState:
    """按请求建房间。成员可以是 ["gemini"] 或 [{"name","provider","model"}]。"""
    normalized = []
    for item in members:
        if isinstance(item, str):
            normalized.append({"name": item, "provider": resolve_name(item), "model": None})
        elif isinstance(item, dict) and item.get("name"):
            normalized.append(item)

    providers, active, skipped = bootstrap.build_all(normalized)
    if not active:
        detail = "；".join(skipped) if skipped else "成员为空"
        raise ValueError("没有可用模型：" + detail)

    room = Room(active, roles={k: v for k, v in (roles or {}).items() if k in active})
    return RoomState(
        id=uuid.uuid4().hex[:12],
        name=name or "未命名房间",
        room=room,
        providers=providers,
        budget=Budget(max_rounds=max_rounds, max_calls=max_calls, max_cost_usd=max_cost_usd),
    )


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
    app = App()
    custom = custom or {}
    models = models or {}

    resolved, origin = bootstrap.resolve_members(members)
    app.origin = origin

    providers, active, skipped = bootstrap.build_all(resolved, custom, models)
    app.skipped = skipped

    # 有成员就建默认房间；一个都没有就进引导模式，服务照常起来
    if active:
        app.add_room(
            RoomState(
                id=uuid.uuid4().hex[:12],
                name="默认房间",
                room=Room(active, roles={k: v for k, v in (roles or {}).items() if k in active}),
                providers=providers,
                budget=budget or _budget_from_config(),
            )
        )

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        # ---------------------------------------------------------- 工具

        def _json(self, code: int, obj) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _text(self, code: int, body: str, ctype: str) -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length) or b"{}")

        def _route(self) -> list[str]:
            return [p for p in urlparse(self.path).path.split("/") if p]

        def _info(self) -> dict:
            cfg = config.load()
            keys = discover()
            sources = bootstrap.credential_sources()

            def usable(name: str) -> bool:
                # 本地模型（ollama / lmstudio）不需要 key，现在就能用
                needs_key = bool(PRESETS[name]["env"]) if name in PRESETS else True
                return (name in keys) or not needs_key

            return {
                "providers": [
                    {
                        "name": name,
                        "model": PRESETS[name]["model"] if name in PRESETS else "",
                        "env": PRESETS[name]["env"] if name in PRESETS else "",
                        "available": usable(name),
                        "masked": mask(keys.get(name, "")),
                        "source": sources.get(name, ""),
                    }
                    for name in sorted(PRESETS)
                ],
                "detected": bootstrap.detect(),
                "presets": bootstrap.MODEL_PRESETS,
                "price_table": PRICE_TABLE_DATE,
                "skipped": app.skipped,
                "origin": app.origin,
                "onboarding": app.default_room() is None,
                "default_room": app.default_room().id if app.default_room() else None,
                "rooms": [r.summary() for r in app.rooms.values()],
                "context": cfg.get("context") or {},
            }

        # ---------------------------------------------------------- GET

        def do_GET(self) -> None:
            parts = self._route()

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

            if parts == ["api", "rooms"]:
                self._json(200, {"rooms": [r.summary() for r in app.rooms.values()]})
                return

            if len(parts) == 3 and parts[:2] == ["api", "rooms"]:
                state = app.get(parts[2])
                if not state:
                    self._json(404, {"error": "房间不存在"})
                    return
                self._json(
                    200,
                    {
                        **state.summary(),
                        "turns": [
                            {
                                "speaker": t.speaker,
                                "content": t.content,
                                "role": t.role,
                                "tokens": t.usage.total,
                                "cost": t.cost,
                                "error": t.error,
                            }
                            for t in state.room.turns
                        ],
                    },
                )
                return

            if len(parts) == 4 and parts[:2] == ["api", "rooms"] and parts[3] == "stats":
                state = app.get(parts[2])
                if not state:
                    self._json(404, {"error": "房间不存在"})
                    return
                cost, unknown = state.room.total_cost()
                self._json(
                    200,
                    {
                        "total_tokens": state.room.total_usage.total,
                        "total_cost": cost,
                        "cost_unknown": unknown,
                        "by_model": state.stats(),
                        "compression": state.room.last_compression,
                    },
                )
                return

            if len(parts) == 4 and parts[:2] == ["api", "rooms"] and parts[3] == "export":
                state = app.get(parts[2])
                if not state:
                    self._json(404, {"error": "房间不存在"})
                    return
                cost, unknown = state.room.total_cost()
                comp = state.room.last_compression or {}
                lines = [
                    f"# {state.name}",
                    "",
                    f"- 成员：{', '.join(state.room.members)}",
                    f"- tokens：{state.room.total_usage.total}",
                    f"- 估算成本：${cost:.4f}" + ("（含无价格模型，实际更高）" if unknown else ""),
                ]
                if comp.get("compressed"):
                    lines.append(f"- 上下文压缩：较早的 {comp['older_turns']} 条已压缩")
                lines += ["", "---", ""]
                for turn in state.room.turns:
                    if turn.error:
                        lines.append(f"**[{turn.speaker}]** ✗ {turn.error}")
                    else:
                        lines.append(f"**[{turn.speaker}]** {turn.content}")
                    lines.append("")
                self._text(200, "\n".join(lines), "text/markdown; charset=utf-8")
                return

            self._json(404, {"error": "not found"})

        # ---------------------------------------------------------- POST

        def do_POST(self) -> None:
            parts = self._route()

            if parts == ["api", "setup"]:
                self._handle_setup()
                return

            if parts == ["api", "settings"]:
                self._handle_settings()
                return

            if parts == ["api", "rooms"]:
                try:
                    data = self._body()
                except ValueError:
                    self._json(400, {"error": "bad json"})
                    return
                try:
                    state = _make_state(
                        data.get("name") or "未命名房间",
                        data.get("members") or [],
                        roles=data.get("roles"),
                        max_rounds=int(data.get("max_rounds") or 6),
                        max_calls=int(data.get("max_calls") or 24),
                        max_cost_usd=data.get("max_cost_usd"),
                    )
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                app.add_room(state)
                self._json(201, state.summary())
                return

            if len(parts) == 4 and parts[:2] == ["api", "rooms"] and parts[3] == "stop":
                state = app.get(parts[2])
                if not state:
                    self._json(404, {"error": "房间不存在"})
                    return
                state.cancel.set()
                self._json(200, {"ok": True})
                return

            if len(parts) == 4 and parts[:2] == ["api", "rooms"] and parts[3] == "say":
                state = app.get(parts[2])
                if not state:
                    self._json(404, {"error": "房间不存在"})
                    return
                try:
                    data = self._body()
                    text = (data.get("text") or "").strip()
                except (ValueError, TypeError):
                    self._json(400, {"error": "bad json"})
                    return
                if not text:
                    self._json(400, {"error": "empty"})
                    return
                self._stream(state, text)
                return

            self._json(404, {"error": "not found"})

        def _handle_setup(self) -> None:
            """首次引导：存 key、选档位、建房间。"""
            try:
                data = self._body()
            except ValueError:
                self._json(400, {"error": "bad json"})
                return

            provider = (data.get("provider") or "").strip()
            api_key = (data.get("api_key") or "").strip()
            preset = data.get("preset") or "省钱"

            if api_key:
                if not provider:
                    provider = "openrouter"
                save_key(provider, api_key)

            rows = data.get("members")
            if not rows:
                rows = bootstrap.MODEL_PRESETS.get(preset) or bootstrap.MODEL_PRESETS["省钱"]
                # 用户自己填的 key 属于哪家，就把这家的模型换进去
                if provider and provider != "openrouter":
                    rows = bootstrap.SINGLE_PROVIDER_DEFAULTS.get(provider, rows)

            try:
                state = _make_state("默认房间", rows)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return

            app.add_room(state)
            config.update(onboarded=True, members=rows)
            self._json(201, {"room": state.summary(), "info": self._info()})

        def _handle_settings(self) -> None:
            """改成员 / 预算 / 压缩参数。"""
            try:
                data = self._body()
            except ValueError:
                self._json(400, {"error": "bad json"})
                return

            changes: dict = {}
            if "members" in data:
                changes["members"] = data["members"]
            if "budget" in data and isinstance(data["budget"], dict):
                changes["budget"] = data["budget"]
            if "context" in data and isinstance(data["context"], dict):
                changes["context"] = data["context"]

            if changes:
                config.update(**changes)

            # 成员变了就重建房间（旧房间的 provider 对不上新模型）
            new_room = None
            if "members" in data and data["members"]:
                try:
                    new_room = _make_state("默认房间", data["members"])
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                for rid in list(app.rooms):
                    app.remove(rid)
                app.add_room(new_room)

            self._json(
                200,
                {
                    "ok": True,
                    "room": new_room.summary() if new_room else None,
                    "info": self._info(),
                },
            )

        # ---------------------------------------------------------- 删除

        def do_DELETE(self) -> None:
            parts = self._route()
            if len(parts) == 3 and parts[:2] == ["api", "rooms"]:
                if app.remove(parts[2]):
                    self._json(200, {"ok": True})
                else:
                    self._json(404, {"error": "房间不存在"})
                return
            self._json(404, {"error": "not found"})

        # ---------------------------------------------------------- SSE

        def _stream(self, state: RoomState, text: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def emit(obj: dict) -> None:
                try:
                    self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def on_turn(turn) -> None:
                if turn.error:
                    emit({"type": "error", "speaker": turn.speaker, "message": turn.error})
                    return
                emit(
                    {
                        "type": "turn",
                        "speaker": turn.speaker,
                        "content": turn.content,
                        "tokens": turn.usage.total,
                        "cost": turn.cost,
                    }
                )

            state.cancel.clear()

            acquired = app.run_lock.acquire(timeout=180)
            if not acquired:
                emit({"type": "error", "speaker": "system", "message": "另一个房间正在会商，等待超时"})
                emit({"type": "done", "stop": None, "tokens": 0, "cost": 0.0})
                return

            try:
                cfg = config.load().get("context") or {}
                session = Session(
                    state.room,
                    state.providers,
                    budget=state.budget,
                    on_turn=on_turn,
                    should_stop=state.cancel.is_set,
                    keep_recent=cfg.get("keep_recent", 8) if cfg.get("enabled", True) else None,
                    context_budget=cfg.get("budget_tokens", 6000) if cfg.get("enabled", True) else None,
                )
                try:
                    result = session.say(text)
                except Exception as exc:  # noqa: BLE001 - 任何异常都要让前端看到
                    emit({"type": "error", "speaker": "system", "message": f"内部错误：{exc}"})
                    emit({"type": "done", "stop": None, "tokens": 0, "cost": 0.0})
                    return

                cost, unknown = state.room.total_cost()
                emit(
                    {
                        "type": "done",
                        "stop": result.stop.message if result.stop else None,
                        "stop_code": result.stop.code if result.stop else None,
                        "tokens": state.room.total_usage.total,
                        "cost": cost,
                        "cost_unknown": unknown,
                        "stats": state.stats(),
                        "compression": state.room.last_compression,
                    }
                )
            finally:
                app.run_lock.release()

    server = QuietServer((host, port), Handler)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}"

    ensure_utf8_streams()
    _out(f"polylogue 工作台 → {url}")
    if active:
        _out(f"房间成员：{', '.join(active)}（{origin}）")
    else:
        _out("没有检测到可用的 API key —— 打开上面的地址，页面上填一个就能用。")
    if skipped:
        _out("跳过的成员：")
        for item in skipped:
            _out(f"  - {item}")
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
