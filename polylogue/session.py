"""编排：把房间、模型、护栏串成一个可以「说话 → 它们回应 → 它们互相接话」的循环。"""

from __future__ import annotations

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
        self.rounds = 0
        self.calls = 0

    # ---------------------------------------------------------------- 单次调用

    def _ask(self, name: str) -> Turn:
        provider = self.providers[name]
        messages = self.room.context_for(name)
        system = self.room.system_for(name)
        try:
            reply = provider.complete(messages, system=system)
        except ProviderError as exc:
            return self.room.add_model(name, "", error=str(exc))

        cost = cost_of(reply.model, reply.usage.prompt_tokens, reply.usage.completion_tokens)
        return self.room.add_model(
            name,
            reply.text,
            usage=reply.usage,
            cost=cost,
            latency_s=reply.latency_s,
        )

    def _ask_many(self, names: list[str]) -> list[Turn]:
        """同一轮的多个模型并行问。

        注意：这一轮里它们互相看不见对方 —— 这正是「同时问多个模型」该有的语义。
        要它们互相看见，得等下一轮。
        """
        self.calls += len(names)
        if len(names) == 1 or not self.parallel:
            turns = []
            for name in names:
                turn = self._ask(name)
                turns.append(turn)
                if self.on_turn:
                    self.on_turn(turn)
            return turns

        # 先把上下文快照下来，保证并行时各自看到的是同一份历史
        with ThreadPoolExecutor(max_workers=min(len(names), 4)) as pool:
            futures = {pool.submit(self._ask, name): name for name in names}
            turns = []
            for future, name in futures.items():
                try:
                    turns.append(future.result())
                except Exception as exc:  # noqa: BLE001 - 单个模型崩了不该拖垮整轮
                    turns.append(self.room.add_model(name, "", error=f"内部错误：{exc}"))
        # 按房间成员顺序整理，保证输出稳定
        order = {name: i for i, name in enumerate(self.room.members)}
        turns.sort(key=lambda t: order.get(t.speaker, 99))
        for turn in turns:
            if self.on_turn:
                self.on_turn(turn)
        return turns

    # ---------------------------------------------------------------- 主循环

    def say(self, text: str) -> SessionResult:
        """用户说一句，然后跑完它引发的所有模型轮次。"""
        user_turn = self.room.add_user(text)
        speakers = self.room.next_speakers(user_turn)
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

        return SessionResult(produced, StopReason("awaiting_user", "没有模型继续接话，等你发言"))

    def _stop_reason(self) -> StopReason | None:
        cost, unknown = self.room.total_cost()
        return self.budget.check(
            rounds=self.rounds,
            calls=self.calls,
            cost=cost,
            cost_unknown=unknown,
            tokens=self.room.total_usage.total,
        )
