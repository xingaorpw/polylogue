"""房间：发言记录 + @提及路由 + 给每个模型组装上下文。

这里最重要的两个决定：

1. **模型只被点名时才说话。** 否则 N 个模型 M 轮就是 N×M 次调用，
   每次带全量历史，成本指数上涨，而且它们会互相附和。
2. **别人说的话按 user 角色喂给自己**，只有自己的历史是 assistant。
   这是群聊场景的标准做法，否则模型会以为整段对话都是自己说的。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .providers import Message, Usage

__all__ = ["Turn", "Room", "MENTION_RE"]

# @名字。允许字母数字下划线连字符，这样 @gemini / @deepseek-chat 都能抓。
MENTION_RE = re.compile(r"@([A-Za-z0-9_-]+)")

USER = "user"
ALL = "all"


@dataclass
class Turn:
    speaker: str
    content: str
    role: str = "user"
    usage: Usage = field(default_factory=Usage)
    cost: float | None = None
    latency_s: float = 0.0
    ts: float = field(default_factory=time.time)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class Room:
    def __init__(self, members: list[str], roles: dict[str, str] | None = None):
        if not members:
            raise ValueError("房间里至少要有一个模型")
        self.members = list(members)
        self.roles = dict(roles or {})
        self.turns: list[Turn] = []

    # ---------------------------------------------------------------- 记录

    def add_user(self, text: str) -> Turn:
        turn = Turn(speaker=USER, content=text, role="user")
        self.turns.append(turn)
        return turn

    def add_model(
        self,
        name: str,
        text: str,
        *,
        usage: Usage | None = None,
        cost: float | None = None,
        latency_s: float = 0.0,
        error: str | None = None,
    ) -> Turn:
        turn = Turn(
            speaker=name,
            content=text,
            role="assistant",
            usage=usage or Usage(),
            cost=cost,
            latency_s=latency_s,
            error=error,
        )
        self.turns.append(turn)
        return turn

    # ---------------------------------------------------------------- 路由

    def mentions(self, text: str) -> list[str]:
        """从一段话里找出被点名的成员，按出现顺序去重。"""
        found: list[str] = []
        for raw in MENTION_RE.findall(text):
            name = raw.lower()
            if name == ALL:
                for m in self.members:
                    if m not in found:
                        found.append(m)
                continue
            if name == USER:
                continue
            # 大小写不敏感匹配成员名
            for m in self.members:
                if m.lower() == name and m not in found:
                    found.append(m)
        return found

    def mentions_user(self, text: str) -> bool:
        return USER in [m.lower() for m in MENTION_RE.findall(text)]

    def next_speakers(self, turn: Turn) -> list[str]:
        """这一轮之后，谁该开口。

        用户发言：点名谁就谁；没点名就全员（这是「同时问多个模型」的场景）。
        模型发言：只叫它点名的人，且不能是自己。
        谁都没点：返回空，对话停下来等用户。
        """
        if turn.speaker == USER:
            named = self.mentions(turn.content)
            return named or list(self.members)

        named = [m for m in self.mentions(turn.content) if m != turn.speaker]
        return named

    # ---------------------------------------------------------------- 上下文

    def context_for(self, name: str) -> list[Message]:
        """给某个模型看的对话历史。

        自己的发言是 assistant，别人的发言（含用户）都包成 user 并标上说话人。
        """
        out: list[Message] = []
        for turn in self.turns:
            if not turn.ok:
                continue
            if turn.speaker == name:
                out.append(Message(role="assistant", content=turn.content, speaker=name))
            else:
                prefix = "用户" if turn.speaker == USER else turn.speaker
                out.append(
                    Message(role="user", content=f"[{prefix}] {turn.content}", speaker=turn.speaker)
                )
        return out

    def system_for(self, name: str) -> str:
        others = [m for m in self.members if m != name]
        role = self.roles.get(name, "").strip()
        lines = [
            f"你是 {name}，正在一个多方会商的房间里。",
            f"房间里还有：{', '.join(others)}，以及一个人类用户（标识 user）。" if others else "房间里还有一个人类用户（标识 user）。",
            "",
            "规则：",
            "1. 只回应与你有关的部分。不要复述别人已经说过的内容。",
            "2. 要请别人发言，写 @名字；要请用户决定，写 @user。",
            "3. 如果你认为讨论已经可以收敛，直接说结论，并且不要 @ 任何人。",
            "4. 保持简短。这是多方对话，不是写论文。",
        ]
        if role:
            lines.append(f"5. 你在这个房间里的角色：{role}")
        return "\n".join(lines)

    # ---------------------------------------------------------------- 统计

    @property
    def total_usage(self) -> Usage:
        total = Usage()
        for turn in self.turns:
            total = total + turn.usage
        return total

    def total_cost(self) -> tuple[float, bool]:
        """返回 (已知成本合计, 是否有查不到价格的调用)。"""
        total = 0.0
        unknown = False
        for turn in self.turns:
            if turn.role != "assistant" or not turn.ok:
                continue
            if turn.cost is None:
                unknown = True
            else:
                total += turn.cost
        return total, unknown

    def render(self) -> str:
        """纯文本转录，给 CLI 和导出用。"""
        lines: list[str] = []
        for turn in self.turns:
            if turn.error:
                lines.append(f"[{turn.speaker}] (调用失败: {turn.error})")
            else:
                lines.append(f"[{turn.speaker}] {turn.content}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"
