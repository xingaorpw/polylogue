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
    names = [p.strip() for p in spec.split(",") if p.strip()]
    if not names:
        raise SystemExit("--models 不能为空")
    return names


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


def _build_providers(members: list[str], models: dict[str, str], custom: list[str]) -> dict:
    custom_map = {}
    for item in custom:
        name, _, url = item.partition("=")
        if url:
            custom_map[name.strip()] = url.strip()

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


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    p = argparse.ArgumentParser(
        prog="polylogue",
        description="多个模型在一个房间里会商，可以互相点名说话。",
    )
    p.add_argument("prompt", nargs="?", help="要问的话；不给就进交互模式")
    p.add_argument("--version", action="version", version=f"polylogue {__version__}")
    p.add_argument("--models", default="gemini,grok,deepseek", help="房间成员，逗号分隔")
    p.add_argument("--model", action="append", default=[], metavar="NAME=MODEL", help="覆盖某个成员的模型名")
    p.add_argument("--provider", action="append", default=[], metavar="NAME=URL", help="自定义 OpenAI 兼容端点")
    p.add_argument("--roles", help="角色分配：gemini=critic,grok=builder，或 critic,builder")
    p.add_argument("--max-rounds", type=int, default=6)
    p.add_argument("--max-calls", type=int, default=24)
    p.add_argument("--max-cost", type=float, default=None, help="美元上限")
    p.add_argument("--sequential", action="store_true", help="同一轮按顺序问（默认并行）")
    p.add_argument("--quiet", action="store_true", help="不打印每个模型的名字前缀")
    p.add_argument("--web", action="store_true", help="启动本地 Web 界面")
    p.add_argument("--port", type=int, default=8765, help="Web 界面端口，默认 8765")
    p.add_argument("--list-providers", action="store_true", help="列出支持的 provider 后退出")
    args = p.parse_args(argv)

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

    roles = _parse_roles(args.roles, members)

    if args.web:
        from .web import serve

        return serve(
            members,
            port=args.port,
            budget=Budget(
                max_rounds=args.max_rounds,
                max_calls=args.max_calls,
                max_cost_usd=args.max_cost,
            ),
            roles=roles,
        )

    providers = _build_providers(members, models, args.provider)
    room = Room(members, roles=roles)
    session = Session(
        room,
        providers,
        budget=Budget(max_rounds=args.max_rounds, max_calls=args.max_calls, max_cost_usd=args.max_cost),
        parallel=not args.sequential,
        on_turn=None if args.quiet else _print_turn,
    )

    print(f"房间成员：{', '.join(members)}")
    print(f"价格表版本：{PRICE_TABLE_DATE}（估算，以各家官网为准）")

    if args.prompt:
        result = session.say(args.prompt)
        if args.quiet:
            print(room.render())
        _footer(room, result.stop)
        return 0

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
        result = session.say(line)
        _footer(room, result.stop)

    print("\n" + room.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
