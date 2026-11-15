"""命令行入口。两种用法：一次问答，或进交互房间。"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .guard import Budget
from .pricing import PRICE_TABLE_DATE, cost_of
from .providers import ProviderError, build_provider
from .room import Room
from .session import Session

__all__ = ["main"]

ROLE_PRESETS = {
    "critic": "你是这个房间里的批评者：专门找别人方案里的漏洞、边界情况和不成立的假设。不要附和。",
    "builder": "你是这个房间里的实现者：把讨论落到具体做法、代码结构、接口和失败处理上。",
    "reviewer": "你是这个房间里的审查者：检查别人说的有没有事实错误，指出哪些是没有依据的猜测。",
    "explorer": "你是这个房间里的发散者：提出别人没想到的角度和替代方案，哪怕不成熟。",
}


def _parse_models(spec: str) -> list[str]:
    """空字符串 = 没指定，交给自动检测决定成员（不是错误）。

    去重：`--models a,a` 会真的建两个同名成员、向上游发**两次**请求、
    收两段一样的回答 —— token 翻倍，system prompt 里还会写
    「房间里还有：a」，模型自己跟自己对话。
    """
    out: list[str] = []
    for part in (spec or "").split(","):
        name = part.strip()
        if name and name not in out:
            out.append(name)
    return out


def _budget_from(args) -> "Budget":
    """把命令行参数收成 Budget，先校验。

    `--max-rounds 0` / `--max-calls -3` / `--max-cost -1` 以前都能一路走到
    会话里，变成「到达轮次上限（0 轮）」这种看起来像 bug 的输出；
    `--max-cost nan` 更糟：`nan` 的比较恒为 False，上限**彻底失效**，
    用户以为自己设了护栏，实际上一点都没有。
    """
    problems = []
    if args.max_rounds < 1:
        problems.append(f"--max-rounds 至少 1（收到 {args.max_rounds}）")
    if args.max_calls < 1:
        problems.append(f"--max-calls 至少 1（收到 {args.max_calls}）")
    cost = args.max_cost
    if cost is not None and not (cost > 0):  # nan 也走这里
        problems.append(f"--max-cost 必须大于 0（收到 {cost}）")
    if problems:
        raise SystemExit("参数不合法：" + "；".join(problems))
    return Budget(max_rounds=args.max_rounds, max_calls=args.max_calls, max_cost_usd=cost)


def _validate_port(port: int) -> int:
    if not (0 <= port <= 65535):
        raise SystemExit(f"--port 必须在 0–65535 之间（收到 {port}）")
    return port


def _parse_roles(spec: str | None, members: list[str]) -> dict[str, str]:
    """--roles "gemini=critic,grok=builder" 或 --roles critic,builder（按顺序分配）。"""
    if not spec:
        return {}
    roles: dict[str, str] = {}
    if "=" in spec:
        for pair in spec.split(","):
            if "=" not in pair:
                continue
            name, _, role = pair.partition("=")
            name, role = name.strip(), role.strip()
            text = ROLE_PRESETS.get(role, role)
            if name in members:
                roles[name] = text
    else:
        for name, role in zip(members, [r.strip() for r in spec.split(",")]):
            roles[name] = ROLE_PRESETS.get(role, role)
    return roles


def _parse_custom(items: list[str]) -> dict:
    """把 --provider NAME=URL 解析成 {name: url}。"""
    custom_map = {}
    for item in items:
        name, _, url = item.partition("=")
        if url:
            custom_map[name.strip()] = url.strip()
    return custom_map


def _build_providers(members: list[str], models: dict[str, str], custom: list[str]) -> dict:
    custom_map = _parse_custom(custom)

    providers = {}
    for name in members:
        try:
            providers[name] = build_provider(name, model=models.get(name), base_url=custom_map.get(name))
        except ProviderError as exc:
            raise SystemExit(f"建 provider 失败：{exc}")
    return providers


def _print_turn(turn) -> None:
    if turn.error:
        print(f"\n[{turn.speaker}] ✗ {turn.error}")
        return
    print(f"\n[{turn.speaker}] {turn.content}")


def _footer(room: Room, stop) -> None:
    usage = room.total_usage
    cost, unknown = room.total_cost()
    money = f"${cost:.4f}" + ("（部分模型无价格，实际更高）" if unknown else "")
    print(f"\n— {usage.total} tokens · {money} · {stop.message if stop else ''}")


def _whoami() -> int:
    """只显示掩码。这是唯一允许把凭据信息打到终端的形状。"""
    from .credentials import credential_sources, discover, mask

    keys = discover()
    if not keys:
        print("本机没有可用的 key。用 polylogue --login openrouter 录入一个。")
        return 0
    sources = credential_sources()
    print(f"{'provider':<14} {'key':<16} 来源")
    print("-" * 52)
    for name in sorted(keys):
        print(f"{name:<14} {mask(keys[name]):<16} {sources.get(name, '未知')}")
    print("\n以上都是掩码，完整 key 不会显示。")
    return 0


def _forget(provider: str) -> int:
    """删掉本机存的某个 key。

    以前的版本不管三七二十一都打印「已删除 X 的 key」并返回 0：
    `--forget anthropic`（真名是 claude）报成功但 key 还在；`--forget nosuch`
    也报成功，还顺手创建一个空的 config.json。用户以为删干净了。
    """
    from .credentials import credential_sources, discover, save_key
    from .providers import resolve_name

    name = (provider or "").strip()
    if not name:
        print("--forget 需要一个 provider 名字，例如 --forget openrouter")
        return 2

    canonical = resolve_name(name)
    keys = discover()
    present = [n for n in (canonical, name) if n in keys]
    if not present:
        print(f"本机没有 {name} 的 key，无需删除。用 --whoami 看有哪些。")
        return 2

    if canonical != name:
        print(f"（{name} 是 {canonical} 的别名）")

    source = credential_sources().get(canonical, "")
    for key_name in present:
        save_key(key_name, "")
    print(f"已从 polylogue 配置里删除 {canonical} 的 key")
    if source == "环境变量":
        # 删配置文件对环境变量毫无影响，不说清楚用户会一直以为没删掉
        print(f"注意：{canonical} 的 key 来自**环境变量**，删配置文件不起作用。")
        print(f"      要真正移除，请清掉对应的环境变量再开新终端。")
    elif source == "复用 DSH 凭据":
        print(f"注意：{canonical} 的 key 是从 DSH 凭据里复用的，删这里不影响它。")
        print("      要真正移除，请改 ~/.dsh/.credentials.yaml，或设 POLYLOGUE_NO_REUSE=1。")
    return 0


def _login(provider: str) -> int:
    """终端无回显录入 key。

    为什么不支持 `--key sk-xxx`：命令行参数会留在 shell 历史和进程列表里，
    等于把 key 抄在墙上。只能用交互式输入。
    """
    from .credentials import mask, prompt_secret, save_key
    from .providers import PRESETS

    if provider not in PRESETS:
        print(f"不认识的 provider：{provider}")
        print("可用：" + ", ".join(sorted(PRESETS)))
        return 2

    print(f"给 {provider} 录入 API key。")
    print("输入时不显示、不进 shell 历史。直接回车取消。")
    value = prompt_secret("API key: ")
    if not value:
        print("已取消，没有改动。")
        return 1

    save_key(provider, value)
    print(f"已保存 {provider}: {mask(value)}")
    print("存在 ~/.polylogue/config.json，权限已收紧到仅当前用户可读。")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            # errors="replace" 很关键：Windows 下 stdout 可能是 cp1252，
            # 打印中文会抛 UnicodeEncodeError 直接把进程干掉。
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass

    p = argparse.ArgumentParser(
        prog="polylogue",
        description="多个模型在一个房间里会商，可以互相点名说话。",
    )
    p.add_argument("prompt", nargs="?", help="要问的话；不给就进交互模式")
    p.add_argument("--version", action="version", version=f"polylogue {__version__}")
    p.add_argument(
        "--models",
        default="",
        help="房间成员，逗号分隔。不填就自动检测本机凭据 / 用上次保存的配置",
    )
    p.add_argument("--model", action="append", default=[], metavar="NAME=MODEL", help="覆盖某个成员的模型名")
    p.add_argument("--provider", action="append", default=[], metavar="NAME=URL", help="自定义 OpenAI 兼容端点")
    p.add_argument("--roles", help="角色分配：gemini=critic,grok=builder，或 critic,builder")
    p.add_argument("--max-rounds", type=int, default=6)
    p.add_argument("--max-calls", type=int, default=24)
    p.add_argument("--max-cost", type=float, default=None, help="美元上限")
    p.add_argument("--sequential", action="store_true", help="同一轮按顺序问（默认并行）")
    p.add_argument("--quiet", action="store_true", help="不打印每个模型的名字前缀")
    p.add_argument("--web", action="store_true", help="启动本地 Web 界面")
    p.add_argument(
        "--app",
        action="store_true",
        help="像桌面程序一样打开：起服务 + 开一个独立窗口（推荐）",
    )
    p.add_argument("--port", type=int, default=8765, help="Web 界面端口，默认 8765")
    p.add_argument("--list-providers", action="store_true", help="列出支持的 provider 后退出")
    p.add_argument(
        "--login",
        metavar="PROVIDER",
        nargs="?",
        const="openrouter",
        default=None,
        help="在终端无回显地录入 API key（不经过网页，也不进 shell 历史）",
    )
    p.add_argument("--forget", metavar="PROVIDER", help="删掉本机存的某个 provider 的 key")
    p.add_argument("--whoami", action="store_true", help="列出本机有哪些 key（只显示掩码）")
    args = p.parse_args(argv)

    if args.whoami:
        return _whoami()

    if args.forget is not None:
        return _forget(args.forget)

    if args.login is not None:
        return _login(args.login)

    if args.list_providers:
        from .providers import ALIASES, PRESETS

        print(f"{'名字':<12} {'适配器':<10} {'环境变量':<22} 默认模型")
        print("-" * 78)
        for name, preset in PRESETS.items():
            env = preset["env"] or "（不需要）"
            print(f"{name:<12} {preset.get('kind', 'openai'):<10} {env:<22} {preset['model']}")
        print("\n别名：" + ", ".join(f"{k}→{v}" for k, v in sorted(ALIASES.items())))
        print("自定义：--provider 名字=https://your-endpoint/v1（OpenAI 兼容，key 读 LLM_API_KEY）")
        return 0

    members = _parse_models(args.models)
    models = {}
    for item in args.model:
        name, _, model = item.partition("=")
        if model:
            models[name.strip()] = model.strip()

    budget = _budget_from(args)
    port = _validate_port(args.port)

    # 界面模式下成员是在网页的通讯录里管的，命令行这几个参数没有意义。
    # 以前是静默丢掉 —— 用户写 `--models nosuch` 启动，以为它生效了。
    if args.app or args.web:
        ignored = []
        if members:
            ignored.append("--models")
        if args.roles:
            ignored.append("--roles")
        if args.provider:
            ignored.append("--provider")
        if args.model:
            ignored.append("--model")
        if ignored:
            print(
                f"提示：界面模式下成员在网页的通讯录里管理，"
                f"{'、'.join(ignored)} 不会生效。",
                file=sys.stderr,
            )
        if args.prompt:
            print("提示：界面模式忽略命令行里要问的话，请在网页里输入。", file=sys.stderr)

    if args.app:
        from .launcher import run_app

        return run_app(
            members or None,
            port=port,
            custom=_parse_custom(args.provider),
            models=models,
        )

    if args.web:
        from .web import serve

        return serve(
            members or None,
            port=port,
            budget=budget,
            roles=_parse_roles(args.roles, members),
            custom=_parse_custom(args.provider),
            models=models,
        )

    # 命令行模式：成员从哪来、key 从哪读、坏成员怎么办，全交给 bootstrap。
    # 以前这里自己手搓了一套 —— 把 `resolve_members` 返回的 name 当 provider
    # 名（零配置直接报「不认识的 provider：critic」）、完全不用 discover()
    # 读到的 key（`--login` 存了也没用）、一个坏成员就 SystemExit 拖垮整间房。
    from .bootstrap import build_all, resolve_members

    rows, origin = resolve_members(members or None)
    providers, active, skipped = build_all(
        rows, custom=_parse_custom(args.provider), models=models
    )
    for reason in skipped:
        print(f"跳过：{reason}", file=sys.stderr)
    if not active:
        raise SystemExit(
            "没有可用模型。先跑一次 `polylogue --app` 在界面里填 key，"
            "或用 --models 指定成员。"
        )
    if origin:
        print(f"成员来源：{origin}")

    members = active
    room = Room(members, roles=_parse_roles(args.roles, members))

    def make_session() -> Session:
        return Session(
            room,
            providers,
            budget=budget,
            parallel=not args.sequential,
            on_turn=None if args.quiet else _print_turn,
        )

    session = make_session()

    def failed() -> bool:
        """这一轮是不是所有模型都失败了 —— 全失败不能返回 0。"""
        replies = [t for t in room.turns if t.role == "assistant"]
        return bool(replies) and all(t.error for t in replies)

    print(f"房间成员：{', '.join(members)}")
    print(f"价格表版本：{PRICE_TABLE_DATE}（估算，以各家官网为准）")

    if args.prompt:
        try:
            result = session.say(args.prompt)
        except KeyboardInterrupt:
            print("\n已中断", file=sys.stderr)
            return 130
        if args.quiet:
            print(room.render())
        _footer(room, result.stop)
        return 1 if failed() else 0

    # ---- 交互模式
    print("输入内容回车发言；@名字 点名；/cost 看花费；/export 导出；/quit 退出")
    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/quit", "/exit", "/q"):
            break
        if line == "/cost":
            usage = room.total_usage
            cost, unknown = room.total_cost()
            print(f"  {usage.total} tokens，已知成本 ${cost:.4f}" + ("（有模型无价格）" if unknown else ""))
            for turn in room.turns:
                if turn.role != "assistant":
                    continue
                c = "?" if turn.cost is None else f"${turn.cost:.4f}"
                print(f"  {turn.speaker:<12} {turn.usage.total:>7} tokens  {c}")
            continue
        if line == "/export":
            print(room.render())
            continue
        # 每次发言新建 Session。轮次/调用次数是**单次发言**的预算，
        # 建在 while 外面会跨句累积 —— 连说 8 句时第 7 句直接报
        # 「到达轮次上限（6 轮）」，用户以为程序坏了。web 端就是每次新建。
        try:
            result = make_session().say(line)
        except KeyboardInterrupt:
            print("\n已中断（正在发出的请求可能还会返回，按两次 Ctrl-C 强制退出）", file=sys.stderr)
            break
        _footer(room, result.stop)

    print("\n" + room.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
