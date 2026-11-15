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

from .context import compress
from .providers import Message, Usage

__all__ = ["Turn", "Room", "MENTION_RE"]

# @名字。允许字母数字下划线连字符，也允许中文/日文/韩文 ——
# 联系人名字可以叫「小助手」，正则切不出来的话交接链会静默断掉。
# 注意：真正的成员匹配不用这个正则（见 mentions），它只用来找
# 「写了 @ 但没对上任何人」的情况。
MENTION_RE = re.compile(
    r"@([A-Za-z0-9_\-\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+)"
)
# `@all` 前面不能紧贴字母数字：`a@all.com` 是邮箱，不是全员广播。
_AT_ALL = re.compile(r"(?<![A-Za-z0-9._\-])@all(?![A-Za-z0-9_\-])", re.IGNORECASE)

USER = "user"
ALL = "all"

# 中日韩文字没有词边界：`@小助手你好` 里「你」紧跟名字，但确实是在叫小助手。
_CJK_RANGES = (
    ("\u3040", "\u30ff"),  # 日文假名
    ("\u3400", "\u4dbf"),  # CJK 扩展 A
    ("\u4e00", "\u9fff"),  # CJK 基本区
    ("\uac00", "\ud7af"),  # 韩文
)


def _is_cjk(ch: str) -> bool:
    return any(lo <= ch <= hi for lo, hi in _CJK_RANGES)


def _is_word_char(ch: str) -> bool:
    """能接在名字后面、说明「这其实是另一个词」的字符。"""
    return ch.isalnum() or ch in "_-"


def _inside_email(text: str, at: int) -> bool:
    """这个 `@` 是不是邮箱的一部分（前面紧贴字母数字/点/连字符）。"""
    return at > 0 and (text[at - 1].isalnum() or text[at - 1] in "._-")


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
        # 最近一次压缩的统计，给界面展示「省了多少」
        self.last_compression: dict = {}

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
        """从一段话里找出被点名的成员，按出现顺序去重。

        直接拿成员名字去文本里找，而不是先用正则切出「@单词」再比对。
        原因：成员名可以是「小助手」「gpt-4o mini」「claude 3.5」这种，
        正则切不出来（空格、中文、点），那样模型 @ 了人也等于没 @，
        对话会静默停成 awaiting_user，用户完全看不出发生了什么。
        """
        lowered = (text or "").lower()
        hits: list[tuple[int, str]] = []
        for name in self.members:
            needle = "@" + name.lower()
            start = lowered.find(needle)
            while start >= 0:
                end = start + len(needle)
                if _inside_email(lowered, start):
                    # `bob@claude.ai` 里的 @claude 不是点名 —— 以前会把
                    # claude 拉进对话，`a@all.com` 更狠，直接全员广播 N 次付费调用。
                    start = lowered.find(needle, start + 1)
                    continue
                # 必须落在词边界：@gpt 不能命中 @gpt4。
                # 但中日韩没有词边界，`@小助手你好` 后面紧跟汉字仍然是叫小助手。
                if end >= len(lowered) or not _is_word_char(lowered[end]):
                    hits.append((start, name))
                    break
                if _is_cjk(lowered[end - 1]) and _is_cjk(lowered[end]):
                    hits.append((start, name))
                    break
                start = lowered.find(needle, start + 1)
        hits.sort(key=lambda pair: pair[0])

        found: list[str] = []
        for _, name in hits:
            if name not in found:
                found.append(name)
        if _AT_ALL.search(lowered):
            for m in self.members:
                if m not in found:
                    found.append(m)
        return found

    def unmatched_mentions(self, text: str) -> list[str]:
        """写了 @ 但对不上任何成员的名字。用来给用户一个明确的提示。"""
        text = text or ""
        out: list[str] = []
        for match in MENTION_RE.finditer(text):
            if _inside_email(text, match.start()):
                # `me@example.com` 不是点名。以前它会让整条消息一个模型都不发
                # （next_speakers 看到 @ 就以为点名失败），用户完全摸不着头脑。
                continue
            raw = match.group(1)
            low = raw.lower()
            if low in (ALL, USER):
                continue
            if not any(m.lower() == low for m in self.members) and f"@{raw}" not in out:
                out.append(f"@{raw}")
        return out

    def has_mention_syntax(self, text: str) -> bool:
        """真的写了 @ 吗（排除邮箱里的 @）。"""
        text = text or ""
        return any(not _inside_email(text, m.start()) for m in MENTION_RE.finditer(text))

    def mentions_user(self, text: str) -> bool:
        text = text or ""
        return any(
            m.group(1).lower() == USER and not _inside_email(text, m.start())
            for m in MENTION_RE.finditer(text)
        )

    def next_speakers(self, turn: Turn) -> list[str]:
        """这一轮之后，谁该开口。

        用户发言：点名谁就谁；没点名就全员（这是「同时问多个模型」的场景）。
        模型发言：只叫它点名的人，且不能是自己。
        谁都没点：返回空，对话停下来等用户。
        """
        if turn.speaker == USER:
            named = self.mentions(turn.content)
            if named:
                return named
            # 写了 @ 却一个成员都没对上（拼错、模型已下线、只写了 @user）：
            # 不要退化成全员广播 —— 一次误输入就是 N 次付费调用。
            # 用 has_mention_syntax 而不是 `"@" in content`：邮箱里的 @
            # 不算点名，否则「发到 me@example.com」会让整条消息无人应答。
            if self.has_mention_syntax(turn.content):
                return []
            return list(self.members)

        named = [m for m in self.mentions(turn.content) if m != turn.speaker]
        return named

    # ---------------------------------------------------------------- 上下文

    def context_for(
        self, name: str, keep_recent: int | None = None, budget_tokens: int | None = None
    ) -> list[Message]:
        """给某个模型看的对话历史。

        自己的发言是 assistant，别人的发言（含用户）都包成 user 并标上说话人。

        `keep_recent` 不为 None 时开启压缩：最近若干条逐字保留，更早的压成
        摘要放在最前面。这是治「轮次一多 token 就爆炸」的地方 —— 不压缩的话
        每轮都要把完整转录重发一遍，成本随轮次平方增长。
        `budget_tokens` 给硬上限，超了会继续丢最老的摘要行。
        """
        turns = self.turns
        digest = None
        if keep_recent is not None:
            digest, turns, stats = compress(self.turns, keep_recent, budget_tokens)
            self.last_compression = stats

        out: list[Message] = []
        if digest:
            out.append(
                Message(
                    role="user",
                    content="【较早的发言已压缩，摘要如下】\n" + digest,
                    speaker="summary",
                )
            )
        for turn in turns:
            if not turn.ok:
                continue
            # 空内容不能进上下文：Anthropic 不接受空 content，
            # 一条空 assistant 会让那个模型之后每一轮都 400。
            if not (turn.content or "").strip():
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
