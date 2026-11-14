"""编排：把房间、模型、护栏串成一个可以「说话 → 它们回应 → 它们互相接话」的循环。"""

from __future__ import annotations

import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .guard import Budget, StopReason, looks_converged, novelty
from .pricing import cost_of
from .providers import Provider, ProviderError
from .room import Room, Turn

__all__ = ["Session", "SessionResult"]


class SessionResult:
    def __init__(self, turns: list[Turn], stop: StopReason | None):
        self.turns = turns
        self.stop = stop


class Session:
    def __init__(
        self,
        room: Room,
        providers: dict[str, Provider],
        *,
        budget: Budget | None = None,
        parallel: bool = True,
        on_turn: Callable[[Turn], None] | None = None,
        min_novelty: float = 0.12,
        should_stop: Callable[[], bool] | None = None,
        keep_recent: int | None = 8,
        context_budget: int | None = 6000,
    ):
        missing = [m for m in room.members if m not in providers]
        if missing:
            raise ValueError(f"这些成员没有对应的 provider：{', '.join(missing)}")

        self.room = room
        self.providers = providers
        self.budget = budget or Budget()
        self.parallel = parallel
        self.on_turn = on_turn
        self.min_novelty = min_novelty
        self.should_stop = should_stop
        # None = 不压缩（老行为）；数字 = 逐字保留最近 N 条，更早的压成摘要
        self.keep_recent = keep_recent
        # 上下文 token 硬上限。轮次再多，发给模型的上下文也不会超过这个数 ——
        # 这才是「token 不随轮次爆炸」的保证，靠启发式压缩只能让它小一点。
        self.context_budget = context_budget
        self.rounds = 0
        self.calls = 0
        # 本次会商里「查不到价格」的调用次数。不能只看累计金额 ——
        # 全都查不到价格时金额恒为 0，成本上限就完全不起作用了。
        self.unknown_calls = 0

    # ---------------------------------------------------------------- 单次调用

    def _messages_for(self, name: str):
        return self.room.context_for(
            name, keep_recent=self.keep_recent, budget_tokens=self.context_budget
        )

    def _ask_with(self, name: str, messages, system: str) -> Turn:
        """只负责发请求。上下文由调用方传进来 —— 这样并行时可以先快照。"""
        provider = self.providers[name]
        try:
            reply = provider.complete(messages, system=system)
        except ProviderError as exc:
            return self.room.add_model(name, "", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - 一个模型崩了不该拖垮整场
            # 保留 traceback，否则真实代码 bug 会被伪装成「某家模型挂了」
            traceback.print_exc(file=sys.stderr)
            return self.room.add_model(
                name, "", error=f"内部错误：{type(exc).__name__}: {exc}"
            )

        # 本地模型（ollama / lmstudio / 自建回环端点）成本是真的 0，
        # 不能算「未知」—— 否则成本上限会把它们当成不可估量的风险。
        if getattr(provider, "free", False):
            cost = 0.0
        else:
            cost = cost_of(reply.model, reply.usage.prompt_tokens, reply.usage.completion_tokens)
        return self.room.add_model(
            name,
            reply.text,
            usage=reply.usage,
            cost=cost,
            latency_s=reply.latency_s,
        )

    def _ask(self, name: str) -> Turn:
        return self._ask_with(name, self._messages_for(name), self.room.system_for(name))

    def _ask_many(self, names: list[str]) -> list[Turn]:
        """同一轮的多个模型并行问。

        注意：这一轮里它们互相看不见对方 —— 这正是「同时问多个模型」该有的语义。
        要它们互相看见，得等下一轮。
        """
        # 调用次数是硬上限：本轮有几个人就先扣几次，扣不下的直接不发。
        # 只在轮次边界检查的话，一次并行就能把上限突破 N 倍。
        remaining = self.budget.max_calls - self.calls
        if remaining <= 0:
            return []
        if len(names) > remaining:
            names = names[:remaining]
        self.calls += len(names)

        if len(names) == 1 or not self.parallel:
            turns = []
            for name in names:
                turn = self._ask(name)
                turns.append(turn)
                if self.on_turn:
                    self.on_turn(turn)
        else:
            # 先在主线程把每个人的上下文快照下来，再并行发请求。
            # 在工作线程里取上下文的话，先返回的模型已经把自己的发言写进
            # room.turns 了，后发的模型就会看见同一轮里的其他人 ——
            # 那等于串行，还会多出接话轮次和额外费用。
            jobs = [
                (name, self._messages_for(name), self.room.system_for(name)) for name in names
            ]
            turns = []
            with ThreadPoolExecutor(max_workers=len(names)) as pool:
                futures = [pool.submit(self._ask_with, *job) for job in jobs]
                for future in futures:
                    turns.append(future.result())
            # 按房间成员顺序整理，保证输出稳定
            order = {name: i for i, name in enumerate(self.room.members)}
            turns.sort(key=lambda t: order.get(t.speaker, 99))
            for turn in turns:
                if self.on_turn:
                    self.on_turn(turn)

        for turn in turns:
            if turn.ok and turn.cost is None:
                self.unknown_calls += 1
        return turns

    # ---------------------------------------------------------------- 主循环

    def say(self, text: str) -> SessionResult:
        """用户说一句，然后跑完它引发的所有模型轮次。"""
        user_turn = self.room.add_user(text)
        speakers = self.room.next_speakers(user_turn)
        if not speakers:
            # 点了一个不存在的名字：要说清楚，否则用户以为程序卡住了
            missed = self.room.unmatched_mentions(text)
            if missed:
                return SessionResult(
                    [],
                    StopReason(
                        "unknown_mention",
                        f"没找到 {'、'.join(missed)}。检查名字拼写，或者不带 @ 让所有人回答",
                    ),
                )
            return SessionResult([], StopReason("awaiting_user", "没有模型被点名，等你发言"))
        return self._run(speakers)

    def _run(self, speakers: list[str]) -> SessionResult:
        produced: list[Turn] = []

        while speakers:
            # 手动停止：在轮次边界检查，不会打断正在飞的请求
            if self.should_stop and self.should_stop():
                return SessionResult(produced, StopReason("cancelled", "已手动停止"))

            stop = self._stop_reason()
            if stop:
                return SessionResult(produced, stop)

            self.rounds += 1
            turns = self._ask_many(speakers)
            if not turns:
                # 调用次数已经用满，这一轮一个人都发不出去
                return SessionResult(
                    produced,
                    StopReason("max_calls", f"到达调用次数上限（{self.budget.max_calls} 次）"),
                )
            produced.extend(turns)

            ok_turns = [t for t in turns if t.ok]
            if not ok_turns:
                return SessionResult(produced, StopReason("all_failed", "这一轮所有模型都调用失败"))

            # 收敛判断：全部主张可以定，或新信息都很少
            if all(looks_converged(t.content) for t in ok_turns):
                return SessionResult(produced, StopReason("converged", "所有发言都表示可以收敛"))

            # 新信息判断要和「同一轮其他模型」比 —— 只跟本轮之前的历史比，
            # 会漏掉「三个模型说同一件事」这种情况，而那正是最该停的时候。
            previous = [t.content for t in self.room.turns if t not in turns and t.ok]
            fresh = [
                novelty(t.content, previous + [o.content for o in ok_turns if o is not t])
                for t in ok_turns
            ]
            if fresh and max(fresh) < self.min_novelty:
                return SessionResult(
                    produced,
                    StopReason(
                        "no_new_info",
                        f"这一轮的新信息都很少（最高 {max(fresh):.0%}），继续下去只会重复",
                    ),
                )

            # 下一轮谁说：只认这一轮里点到的名字
            nxt: list[str] = []
            for turn in ok_turns:
                for name in self.room.next_speakers(turn):
                    if name not in nxt:
                        nxt.append(name)
            speakers = [n for n in nxt if n in self.providers]

        # 一轮就结束（没人继续接话）时，上限还是要在「等你发言」之前报出来。
        # 否则这一轮已经烧掉的调用/成本被一句「等你发言」盖住，用户根本
        # 不知道刚才撞了顶。
        stop = self._stop_reason()
        if stop:
            return SessionResult(produced, stop)
        return SessionResult(produced, StopReason("awaiting_user", "没有模型继续接话，等你发言"))

    def _stop_reason(self) -> StopReason | None:
        cost, unknown = self.room.total_cost()
        return self.budget.check(
            rounds=self.rounds,
            calls=self.calls,
            cost=cost,
            cost_unknown=unknown,
            tokens=self.room.total_usage.total,
            unknown_calls=self.unknown_calls,
        )
