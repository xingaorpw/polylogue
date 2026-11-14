"""本地 Web 工作台。标准库 http.server + SSE，不需要 npm、不需要构建。

一个进程里可以同时存在多个房间，每个房间有自己的成员、预算和转录。
同一时刻只有一个房间在跑会商，其余房间排队 —— 这是有意的：
并行跑多个房间会让成本失控，而成本失控正是这类工具最容易死的地方。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .guard import Budget
from .pricing import PRICE_TABLE_DATE, cost_of
from .providers import PRESETS, ProviderError, build_provider, resolve_name
from .room import Room
from .session import Session

__all__ = ["serve", "App"]

STATIC = Path(__file__).parent / "static"


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
            "roles": {k: v for k, v in self.room.roles.items()},
            "turns": len(self.room.turns),
            "tokens": self.room.total_usage.total,
            "cost": cost,
            "cost_unknown": unknown,
            "budget": {
                "max_rounds": self.budget.max_rounds,
                "max_calls": self.budget.max_calls,
                "max_cost_usd": self.budget.max_cost_usd,
            },
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
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class App:
    def __init__(self) -> None:
        self.rooms: dict[str, RoomState] = {}
        self.lock = threading.Lock()
        self.run_lock = threading.Lock()  # 同一时刻只跑一个房间

    def add_room(self, state: RoomState) -> None:
        with self.lock:
            self.rooms[state.id] = state

    def get(self, room_id: str) -> RoomState | None:
        return self.rooms.get(room_id)

    def remove(self, room_id: str) -> bool:
        with self.lock:
            return self.rooms.pop(room_id, None) is not None


def _available_providers() -> list[dict]:
    out = []
    for name, preset in PRESETS.items():
        env = preset["env"]
        if not env:
            available = True  # 本地模型，不需要 key
        elif name == "gemini":
            available = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
        else:
            available = bool(os.environ.get(env))
        out.append(
            {
                "name": name,
                "kind": preset.get("kind", "openai"),
                "env": env,
                "model": preset["model"],
                "available": available,
            }
        )
    return out


def _make_state(
    name: str,
    members: list[dict],
    roles: dict[str, str] | None = None,
    max_rounds: int = 6,
    max_calls: int = 24,
    max_cost_usd: float | None = None,
) -> RoomState:
    """按请求建房间。成员可以是 ["gemini"] 或 [{"name":"gemini","model":"..."}]。"""
    providers = {}
    active: list[str] = []
    errors: list[str] = []

    for item in members:
        if isinstance(item, str):
            pname, model = item, None
        else:
            pname = (item or {}).get("name", "")
            model = (item or {}).get("model") or None
        if not pname:
            continue
        key = resolve_name(pname)
        try:
            providers[key] = build_provider(key, model=model)
            active.append(key)
        except ProviderError as exc:
            errors.append(str(exc))

    if not active:
        raise ValueError("没有任何可用模型：" + "；".join(errors) if errors else "成员为空")

    room = Room(active, roles={k: v for k, v in (roles or {}).items() if k in active})
    return RoomState(
        id=uuid.uuid4().hex[:12],
        name=name or "未命名房间",
        room=room,
        providers=providers,
        budget=Budget(max_rounds=max_rounds, max_calls=max_calls, max_cost_usd=max_cost_usd),
    )


def serve(
    members: list[str],
    *,
    port: int = 8765,
    host: str = "127.0.0.1",
    budget: Budget | None = None,
    roles: dict[str, str] | None = None,
    custom: dict[str, str] | None = None,
    on_ready: Callable[[str], None] | None = None,
) -> int:
    app = App()
    custom = custom or {}

    # 启动时建一个默认房间；建不起来的成员记下来，页面上提示
    providers = {}
    active: list[str] = []
    skipped: list[str] = []
    for name in members:
        try:
            providers[name] = build_provider(name, base_url=custom.get(name))
            active.append(name)
        except ProviderError as exc:
            skipped.append(f"{name}: {exc}")

    if not active:
        raise SystemExit("没有任何模型可用：\n  " + "\n  ".join(skipped))

    default = RoomState(
        id=uuid.uuid4().hex[:12],
        name="默认房间",
        room=Room(active, roles={k: v for k, v in (roles or {}).items() if k in active}),
        providers=providers,
        budget=budget or Budget(),
    )
    app.add_room(default)

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
                self._json(
                    200,
                    {
                        "providers": _available_providers(),
                        "price_table": PRICE_TABLE_DATE,
                        "skipped": skipped,
                        "default_room": default.id,
                        "rooms": [r.summary() for r in app.rooms.values()],
                    },
                )
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
                    },
                )
                return

            if len(parts) == 4 and parts[:2] == ["api", "rooms"] and parts[3] == "export":
                state = app.get(parts[2])
                if not state:
                    self._json(404, {"error": "房间不存在"})
                    return
                cost, unknown = state.room.total_cost()
                lines = [
                    f"# {state.name}",
                    "",
                    f"- 成员：{', '.join(state.room.members)}",
                    f"- tokens：{state.room.total_usage.total}",
                    f"- 估算成本：${cost:.4f}" + ("（含无价格模型，实际更高）" if unknown else ""),
                    "",
                    "---",
                    "",
                ]
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

            # 全局只允许一个房间在跑，其余排队等待
            acquired = app.run_lock.acquire(timeout=180)
            if not acquired:
                emit({"type": "error", "speaker": "system", "message": "另一个房间正在会商，等待超时"})
                emit({"type": "done", "stop": None, "tokens": 0, "cost": 0.0})
                return

            try:
                session = Session(
                    state.room,
                    state.providers,
                    budget=state.budget,
                    on_turn=on_turn,
                    should_stop=state.cancel.is_set,
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
                    }
                )
            finally:
                app.run_lock.release()

    server = QuietServer((host, port), Handler)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}"
    if on_ready:
        on_ready(url)

    print(f"polylogue 工作台 → {url}")
    print(f"默认房间成员：{', '.join(default.room.members)}")
    if skipped:
        print("跳过的成员：")
        for item in skipped:
            print(f"  - {item}")
    print(f"价格表版本：{PRICE_TABLE_DATE}（估算，以各家官网为准）")
    print("Ctrl+C 退出")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0
