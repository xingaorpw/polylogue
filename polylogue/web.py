"""本地 Web 工作台。

模型：通讯录（contacts）→ 会话（conversations，dm 或 group）→ 转录（turns）。

会话落盘在 `~/.polylogue/conversations/<id>.json`，所以关掉程序再打开历史还在，
搜索也有东西可搜。内存里只在真正跑会商时构造 `Room`，跑完写回磁盘。

同一时刻只允许一个会话在跑（run_lock）。并行跑多个会让成本失控，
而成本失控正是这类工具最容易死的地方。
"""

from __future__ import annotations

import base64
import functools
import json
import sys
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

from . import avatars, bootstrap, config, credentials, store
from .credentials import discover, mask, save_key
from .guard import Budget
from .pricing import PRICE_TABLE_DATE
from .providers import PRESETS, ProviderError, Usage, build_provider, resolve_name
from .room import Room
from .session import Session

__all__ = ["serve", "App"]

STATIC = Path(__file__).parent / "static"


# 只认本机回环。DNS rebinding 会把恶意域名解析到 127.0.0.1，
# 校验 Host 是唯一能识破的办法。
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _str_field(data: dict, key: str, default: str = "") -> str:
    """从 JSON 体里取字符串字段。

    客户端传 `{"title": 123}` / `{"provider": ["x"]}` 时直接 `.strip()`
    会 AttributeError —— 那是 400 的活，不是 500。数字按字符串收下，
    其余类型退回默认值。
    """
    value = data.get(key)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return default


def _str_list(data: dict, key: str) -> list[str]:
    """取字符串列表。非列表、或元素不是字符串的，一律丢掉。"""
    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _guarded(fn):
    """兜住所有没预料到的异常，变成 500 + 栈，而不是把连接直接断掉。

    不兜的话（实测）：请求体是 `[]` 时 `data.get(...)` 抛 AttributeError，
    异常逃出 `do_POST`，`BaseHTTPRequestHandler` 只把连接关掉 —— 客户端
    收到 `RemoteDisconnected`，前端只能显示「网络错误」，终端里也没有
    任何指向具体接口的线索。502/500 至少能让人知道是服务端崩了。
    """

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except ConnectionError:
            # 客户端自己走了（关标签页 / 取消请求）。这是正常操作，不是服务端
            # 错误 —— 打成 traceback 只会让终端里全是假警报，把真问题埋掉。
            return None
        except Exception as exc:  # noqa: BLE001 - 兜底就是它的职责
            traceback.print_exc(file=sys.stderr)
            try:
                self._json(500, {"error": f"服务端内部错误：{type(exc).__name__}"})
            except Exception:  # noqa: BLE001 - 连响应都发不出去就没办法了
                pass
            return None

    return wrapper

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

    def forget_cancel(self, cid: str) -> None:
        """会话被删掉时回收它的停止信号。

        为什么不在每次说完就回收：两个请求可能同时说话，先到的那个说完了
        就把 Event 摘掉，后到的那个拿的还是旧对象 —— 用户按「停止」时
        `cancel_for` 会新建一个 Event，正在跑的那个永远收不到信号。
        所以按会话存活期管理，而不是按请求。
        """
        with self.lock:
            self.cancels.pop(cid, None)


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
    # 回填历史：这样多轮对话接得上，压缩也基于完整历史。
    #
    # 必须把 usage 和 cost 一起带回来。以前只回填文字，`room.total_cost()`
    # 永远是 (0.0, True) —— 每条消息的成本都从 0 重新算，`max_cost_usd`
    # 完全拦不住（实测 8 条消息真实花了 $1.60、上限 $0.50，一次都没停）。
    for turn in conv.get("turns") or []:
        if turn.get("role") == "user":
            room.add_user(turn.get("content") or "")
        elif turn.get("speaker") in names:
            tokens = turn.get("tokens")
            cost = turn.get("cost")
            room.add_model(
                turn["speaker"],
                turn.get("content") or "",
                # 存的只有总数，拆不出 prompt/completion —— 但上限判断只看总数
                usage=Usage(prompt_tokens=int(tokens or 0), completion_tokens=0),
                cost=cost if isinstance(cost, (int, float)) else None,
                error=turn.get("error"),
            )
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
        # 空闲连接不能无限占着一个线程：浏览器开着标签页不关、
        # 或者有人只连不发，默认实现会为每条连接永久留一个线程。
        timeout = 60

        def log_message(self, fmt, *args):
            pass

        def version_string(self) -> str:
            # 默认是 "BaseHTTP/0.6 Python/3.13.17"，把解释器版本白送给
            # 任何能连上来的人，没有任何好处。
            return "polylogue"

        def handle_one_request(self) -> None:
            # `_sent` 必须**每个请求**重置。BaseHTTPRequestHandler 一个连接
            # 只创建一个实例、在 handle() 里循环处理请求；不重置的话第一个
            # 响应把 `_sent` 置 True，同一连接上的第二个请求会走进 `_json`
            # 的早退分支，一个字节都不写 —— 客户端只能超时。
            # 浏览器必然复用连接（GET / 之后紧接着 GET /api/info），
            # 所以这是「首屏永远转圈」级别的 bug。测试全绿是因为
            # 测试每次都新建 HTTPConnection。
            self._sent = False
            self._body_cache = None
            super().handle_one_request()

        # ------------------------------------------------------ 工具

        def _json(self, code: int, obj) -> None:
            # 头已经发过了就绝不能再写一次 —— 否则会把第二个 HTTP 响应
            # 塞进已经开始的流里，客户端看到的是一段乱码。
            # 以前 SSE 中途抛 ValueError 就这么干过一次。
            if getattr(self, "_sent", False):
                return
            data = json.dumps(_scrub(obj), ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            self._sent = True

        def _bytes(self, code: int, body: bytes, ctype: str) -> None:
            if getattr(self, "_sent", False):
                return
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            self._sent = True

        def _text(self, code: int, body: str, ctype: str) -> None:
            self._bytes(code, body.encode("utf-8"), ctype)

        def _read_raw(self) -> bytes:
            """把请求体读进缓存，**只读一次**。

            为什么必须保证「每个请求的 body 都被读干净」：没读走的字节会留在
            缓冲区里，被 `BaseHTTPRequestHandler` 当成下一个请求的请求行。
            实测同一连接上 `POST /api/conversations/x/stop`（这个分支不用 body）
            之后发 `PUT`，服务端把残留的 `{}` 当请求行解析，回了
            `501 Unsupported method`。浏览器会复用连接，所以这不是理论问题。
            """
            cached = getattr(self, "_body_cache", None)
            if cached is not None:
                return cached
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                self._body_cache = b""
                raise ValueError("Content-Length 不合法")
            if length < 0:
                # `read(-1)` 会一路读到 EOF，线程永久挂住
                self._body_cache = b""
                raise ValueError("Content-Length 不合法")
            if length > MAX_BODY:
                self._body_cache = b""
                raise ValueError("请求体太大")
            try:
                self._body_cache = self.rfile.read(length) if length else b""
            except OSError:
                self._body_cache = b""
            return self._body_cache

        def _body(self) -> dict:
            raw = self._read_raw() or b"{}"
            try:
                data = json.loads(raw)
            except ValueError:
                raise ValueError("请求体不是合法 JSON")
            # 必须是对象：`[]`、`123`、`"x"` 都是合法 JSON，但后面每个
            # 接口都会 `data.get(...)`，类型不对就 AttributeError。
            if not isinstance(data, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return data

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

        def _reject(self, code: int, message: str) -> None:
            """拒绝一个写请求，并**把 body 读掉**。

            不读的话，残留的 body 字节会被当成下一个请求的请求行 ——
            实测 415 之后同一连接发 `GET /api/info` 会收到
            `501 Unsupported method`。太大或声明不合法时读不完，就直接断开。
            """
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = -1
            if 0 < length <= MAX_BODY:
                try:
                    self._read_raw()
                except ValueError:
                    self.close_connection = True
            elif length != 0:
                self.close_connection = True
            self._json(code, {"error": message})

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
                self._reject(403, "只接受本机访问")
                return False
            if not self._origin_ok():
                self._reject(403, "拒绝跨站请求")
                return False

            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                self._reject(400, "Content-Length 不合法")
                return False

            if length < 0:
                self._reject(400, "Content-Length 不合法")
                return False

            if ctype not in ("", "application/json") or (length > 0 and ctype != "application/json"):
                self._reject(415, "写操作必须用 Content-Type: application/json")
                return False

            # 在闸门里就拒绝，不读 body —— 否则要先把几 MB 收进内存再说不行
            if length > MAX_BODY:
                self._reject(413, "请求体太大")
                return False

            # 过了闸门就把 body 一次读干净：不是每个分支都会用到它
            # （`/stop`、`DELETE` 就不看 body），但**谁都不能把字节留在
            # 缓冲区里** —— 残留会被当成下一个请求的请求行。
            try:
                self._read_raw()
            except ValueError as exc:
                self._reject(400, str(exc))
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

        @_guarded
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

        @_guarded
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
                    # 会话不存在就别建 Event：`cancels` 只在删除会话时回收，
                    # 对着随机 id 反复按停止会让它无界增长（实测 200 次 → 200 条），
                    # 而且返回 200 {"ok":true} 会让前端以为真的停掉了什么。
                    if not store.load_conversation(parts[2]):
                        self._json(404, {"error": "会话不存在"})
                        return
                    app.cancel_for(parts[2]).set()
                    self._json(200, {"ok": True})
                    return
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return

            self._json(404, {"error": "not found"})

        @_guarded
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
                    parts[2],
                    _str_field(data, "title"),
                    _str_field(data, "category") or None,
                )
                if not conv:
                    self._json(404, {"error": "会话不存在"})
                    return
                self._json(200, _summarize(conv))
                return

            if len(parts) == 3 and parts[:2] == ["api", "contacts"]:
                # 只放行白名单字段：`**data` 直接把客户端键塞进参数表，
                # 传 {"cid": ...} 会 TypeError（跟位置参数撞名），
                # 传 {"id": ...} 则能改掉联系人 id，把文件名和内容改散。
                allowed = ("name", "provider", "model", "category", "role", "base_url")
                changes = {k: data[k] for k in allowed if k in data}
                contact = store.update_contact(parts[2], **changes)
                if not contact:
                    self._json(404, {"error": "联系人不存在"})
                    return
                self._json(200, contact)
                return

            self._json(404, {"error": "not found"})

        @_guarded
        def do_DELETE(self) -> None:
            if not self._guard_write():
                return
            parts, _ = self._parts()
            if len(parts) == 3 and parts[:2] == ["api", "conversations"]:
                if not store.delete_conversation(parts[2]):
                    self._json(404, {"error": "会话不存在"})
                    return
                app.forget_cancel(parts[2])  # 会话没了，停止信号也别留着
                self._json(200, {"ok": True})
                return
            if len(parts) == 3 and parts[:2] == ["api", "contacts"]:
                if not store.remove_contact(parts[2]):
                    self._json(404, {"error": "联系人不存在"})
                    return
                self._json(200, {"ok": True})
                return
            self._json(404, {"error": "not found"})

        # 没实现的方法以前落到 BaseHTTPRequestHandler 的默认 501 页面：
        # 回一个 HTML 错误页，还带 `Server: BaseHTTP/0.6 Python/3.13.17`，
        # 把解释器版本白送出去。这里统一回 405 + Allow，跟别的接口一致。
        _ALLOW = "GET, POST, PATCH, DELETE"

        def _not_allowed(self) -> None:
            body = json.dumps({"error": "不支持这个方法"}, ensure_ascii=False).encode("utf-8")
            self.send_response(405)
            self.send_header("Allow", self._ALLOW)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self) -> None:
            self._not_allowed()

        def do_HEAD(self) -> None:
            self._not_allowed()

        def do_OPTIONS(self) -> None:
            self._not_allowed()

        def do_TRACE(self) -> None:
            self._not_allowed()

        # ------------------------------------------------------ 具体处理

        def _setup(self) -> None:
            data = self._body()
            provider = _str_field(data, "provider", "openrouter") or "openrouter"
            api_key = _str_field(data, "api_key")
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
            member_ids = [m for m in _str_list(data, "members") if store.contact_by_id(m)]
            if not member_ids:
                raise ValueError("至少选一个模型")
            kind = "dm" if len(member_ids) == 1 else "group"
            title = _str_field(data, "title")
            if not title:
                names = [store.contact_by_id(m)["name"] for m in member_ids]
                title = names[0] if kind == "dm" else "、".join(names[:3])
            conv = store.create_conversation(
                title,
                member_ids,
                kind=kind,
                category=_str_field(data, "category", "常用") or "常用",
            )
            self._json(201, _summarize(conv))

        def _create_contact(self) -> None:
            data = self._body()
            name = _str_field(data, "name")
            if not name:
                raise ValueError("名字不能为空")
            contact = store.add_contact(
                name,
                _str_field(data, "provider", "openrouter") or "openrouter",
                model=_str_field(data, "model"),
                category=_str_field(data, "category", "常用") or "常用",
                role=_str_field(data, "role"),
                base_url=_str_field(data, "base_url"),
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
            text = _str_field(data, "text")
            if not text:
                self._json(400, {"error": "空消息"})
                return

            # 这里只做便宜的存在性检查就开流。模型建不起来（key 过期等）
            # 也要走 SSE 告诉用户「为什么没人回」，而不是一个 400 把话吞掉。
            if not (conv.get("members") or []):
                self._json(400, {"error": "这个对话没有成员"})
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self._sent = True  # 从这里开始只能写 SSE，不能再写 HTTP 响应

            # 客户端关掉标签页之后继续跑就是在烧钱。写失败就记一笔，
            # 下一轮边界让 Session 停 —— 不会打断正在飞的请求。
            alive = {"yes": True}

            def emit(obj: dict) -> None:
                if not alive["yes"]:
                    return
                try:
                    payload = json.dumps(_scrub(obj), ensure_ascii=False)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    alive["yes"] = False

            def finish(stop_message: str | None, conv_now: dict | None) -> None:
                summary = _summarize(conv_now) if conv_now else {"tokens": 0, "cost": 0.0}
                emit(
                    {
                        "type": "done",
                        "stop": stop_message,
                        "tokens": summary.get("tokens", 0),
                        "cost": summary.get("cost", 0.0),
                        "cost_unknown": summary.get("cost_unknown", False),
                        "compression": (conv_now or {}).get("compression"),
                        "conversation": summary,
                    }
                )

            # 停止信号属于「正在跑的那次请求」。必须在**拿到锁之后**才 clear：
            # 在等锁之前 clear 的话，用户刚点的停止会被排队中的请求抹掉，
            # 正在跑的那次照样跑满轮次（实测上游调用从 2 次涨到 12 次）。
            cancel = app.cancel_for(cid)

            acquired = app.run_lock.acquire(timeout=180)
            if not acquired:
                emit({"type": "error", "speaker": "system", "message": "另一个会话正在会商，等待超时"})
                finish(None, None)
                return

            cancel.clear()  # 现在才轮到这次请求接管这个信号
            try:
                # 拿到锁之后**重新读一遍**。两个请求同时说话时，先到的那个已经
                # 写过盘了；还拿着开头读到的旧副本去保存，会把对方的发言整段覆盖掉。
                conv = store.load_conversation(cid)
                if not conv:
                    emit({"type": "error", "speaker": "system", "message": "会话已被删除"})
                    finish(None, None)
                    return

                # 用户说的话先落盘再跑模型。模型挂了、内部异常、进程被杀，
                # 都不该让用户发现自己刚打的字没了。
                #
                # 但 Room 必须用**落盘之前**的历史来建：`session.say(text)`
                # 自己会把这句话加进 Room，重放一次就变成两条一样的 user
                # 消息 —— 每轮多付一份 prompt token，收敛判断也被污染。
                before = {
                    "members": conv.get("members") or [],
                    "turns": list(conv.get("turns") or []),
                }
                conv.setdefault("turns", []).append(
                    {"speaker": "你", "role": "user", "content": text, "ts": store._now()}
                )
                store.save_conversation(conv)

                # Room 不接受空成员列表（一个没有模型的房间没有意义）。
                # 但这里必须转成 SSE 事件 —— 头已经发了，抛出去会变成
                # 第二个 HTTP 响应塞进流里。
                try:
                    room, providers, skipped = _room_from(before)
                except ValueError as exc:
                    emit({"type": "error", "speaker": "system", "message": str(exc)})
                    finish(None, store.load_conversation(cid))
                    return
                # 前端靠 /api/info 的 skipped 显示「跳过：…」，这个字段以前
                # 从来没被赋值，那行提示永远不会出现。
                with app.lock:
                    app.skipped = list(skipped)

                name_to_id = _name_map(conv.get("members") or [])

                if not room.members:
                    detail = "；".join(skipped) or "通讯录为空"
                    emit({"type": "error", "speaker": "system", "message": f"没有可用模型：{detail}"})
                    finish(None, store.load_conversation(cid))
                    return

                new_turns: list[dict] = []

                def on_turn(turn) -> None:
                    record = {
                        "speaker": turn.speaker,
                        "speaker_id": name_to_id.get(turn.speaker, ""),
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

                def persist() -> None:
                    """把已经产生的发言写下去。

                    中途失败也要调用 —— 屏幕上已经显示出来的内容必须和磁盘一致，
                    否则刷新一下回复就没了。
                    """
                    if not new_turns:
                        return
                    fresh = store.load_conversation(cid) or conv
                    fresh.setdefault("turns", []).extend(new_turns)
                    fresh["compression"] = room.last_compression
                    store.save_conversation(fresh)
                    new_turns.clear()

                cfg = config.load().get("context") or {}
                enabled = cfg.get("enabled", True)
                session = Session(
                    room,
                    providers,
                    budget=budget or _budget(),
                    on_turn=on_turn,
                    should_stop=lambda: cancel.is_set() or not alive["yes"],
                    keep_recent=(cfg.get("keep_recent", 8) if enabled else None),
                    context_budget=(cfg.get("budget_tokens", 6000) if enabled else None),
                )
                try:
                    result = session.say(text)
                except Exception as exc:  # noqa: BLE001 - 任何异常都要让前端看到
                    persist()
                    emit({"type": "error", "speaker": "system", "message": f"内部错误：{exc}"})
                    finish(None, store.load_conversation(cid))
                    return

                persist()
                finish(
                    result.stop.message if result.stop else None,
                    store.load_conversation(cid),
                )
            except ConnectionError:
                # 客户端自己走了（关标签页 / 取消）。不是服务端问题，
                # 别在 stderr 里刷一段栈，前端也不需要收到什么。
                pass
            except Exception as exc:  # noqa: BLE001 - 头已经发了，只能走流里报
                # 走到这里说明是**开流之后**出的错（磁盘满、只读、内部 bug）。
                # 不兜的话异常会穿出 do_POST，客户端拿到一个 200 + 空流：
                # 没有 done、没有错误、屏幕上什么都没有，原因只在 stderr。
                traceback.print_exc(file=sys.stderr)
                emit(
                    {
                        "type": "error",
                        "speaker": "system",
                        "message": f"内部错误：{type(exc).__name__}: {exc}",
                    }
                )
                try:
                    finish(None, store.load_conversation(cid))
                except Exception:  # noqa: BLE001 - 连收尾都失败就只能这样了
                    pass
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
