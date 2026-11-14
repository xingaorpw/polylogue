"""上下文压缩：解决「轮次一多 token 就爆炸」。

问题：每一轮都把完整转录发给每个模型。3 个模型跑 10 轮，第 10 轮每个模型
要吃的上下文是第一轮的十几倍，成本是平方级增长。

做法（不需要额外调用模型，零成本）：
- 最近 `keep_recent` 条发言**逐字保留** —— 当前讨论的细节不能丢
- 更早的发言压成摘要：每条只留**首句 + 提到的名字**
  （首句通常是结论，@名字决定交接关系，这两样是不能丢的信息）
- 压缩前后都算 token，差值直接展示给用户

为什么不调用模型做摘要：那本身就要花钱、要延迟，而且摘要模型看到的
还是同一份超长上下文 —— 治不了根，只是把成本挪了个位置。
"""

from __future__ import annotations

import re

__all__ = ["estimate_tokens", "summarize", "compress", "CJK_RE"]

# 中日韩字符 + 假名 + 谚文 + 全角标点：这些大约 1 token / 字。
# 只写 \u4e00-\u9fff 会漏掉日文和韩文，那两种文字会掉进「4 字符 1 token」
# 分支，低估 4 倍以上，预算就形同虚设。
CJK_RE = re.compile(
    r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff00-\uffef]"
)
# emoji 在主流分词器里通常是 2~3 个 token，按 2 算（偏保守）
EMOJI_RE = re.compile(r"[\U0001f300-\U0001faff\u2600-\u27bf]")

_SENT_END = re.compile(r"[。！？!?\n]")


def estimate_tokens(text: str) -> int:
    """粗估 token 数。不追求精确，只要求「方向对、不低估」。

    中文按 1 字 1 token（真实分词器通常 1.2~1.6 字/token，这里偏高，
    用作预算是安全的）；其余按 4 字符 1 token。
    """
    if not text:
        return 0
    emoji = len(EMOJI_RE.findall(text))
    rest = EMOJI_RE.sub("", text) if emoji else text
    cjk = len(CJK_RE.findall(rest))
    other = len(rest) - cjk
    return int(cjk + emoji * 2 + other / 4 + 0.5)


def _first_sentence(text: str, limit: int = 60) -> str:
    """取首句。首句一般是结论，后面多是展开，压缩时优先丢展开。"""
    text = text.strip()
    if not text:
        return ""
    m = _SENT_END.search(text)
    head = text[: m.start()] if m else text
    head = head.strip()
    if len(head) > limit:
        head = head[:limit].rstrip() + "…"
    elif m and m.start() < len(text) - 1:
        head += "…"
    return head


def _mentions(text: str) -> list[str]:
    """提取 @名字，去重保序。交接关系是压缩时必须保住的信息。"""
    seen, out = set(), []
    for m in re.finditer(r"@([A-Za-z0-9_-]+)", text):
        name = m.group(1)
        if name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def summarize(turns: list) -> str:
    """把一组较早的发言压成多行摘要。"""
    return "\n".join(_line_for(t) for t in turns if _line_for(t))


def _line_for(turn) -> str:
    """单条发言压成一行：首句 + @名字。"""
    if getattr(turn, "error", None):
        return f"[{turn.speaker}] 调用失败"
    head = _first_sentence(turn.content or "")
    if not head:
        return ""
    tags = _mentions(turn.content or "")
    if tags:
        head += "  →" + " ".join(f"@{n}" for n in tags)
    return f"[{turn.speaker}] {head}"


def compress(
    turns: list,
    keep_recent: int = 8,
    budget_tokens: int | None = None,
) -> tuple[str | None, list, dict]:
    """返回 (摘要文本或 None, 要逐字保留的发言, 统计)。

    两级控制：
    1. `keep_recent` —— 最近若干条逐字保留，更早的压成「首句 + @名字」
    2. `budget_tokens` —— 硬上限。压完还超，先丢最老的摘要行，摘要行丢完
       再从 recent 最老的开始丢。这一级保证上下文**不会无限增长**，
       而不是「大概会小一点」。

    统计里的数字是给界面展示用的 —— 用户应该能看到「压缩到底省了多少」，
    而不是被要求相信它有用。
    """
    if keep_recent < 0:
        keep_recent = 0

    raw_all = sum(estimate_tokens(t.content or "") for t in turns)

    # 条数不多、token 也没超预算：什么都不用做。
    # 注意这里必须同时看 budget —— 只看条数的话，8 条超长发言能发出
    # 预算两倍的上下文，而统计还写着「没压缩」。
    if len(turns) <= keep_recent and (budget_tokens is None or raw_all <= budget_tokens):
        return None, list(turns), {
            "compressed": False,
            "older_turns": 0,
            "kept_turns": len(turns),
            "raw_tokens": raw_all,
            "digest_tokens": 0,
            "dropped": 0,
        }

    if len(turns) <= keep_recent:
        # 条数够少，但每条太长：全部按 recent 处理，再从最老的开始丢
        older: list = []
        recent = list(turns)
    else:
        older = turns[:-keep_recent] if keep_recent else list(turns)
        recent = turns[-keep_recent:] if keep_recent else []

    lines = [line for line in (_line_for(t) for t in older) if line]
    raw = sum(estimate_tokens(t.content or "") for t in older)
    recent_tokens = sum(estimate_tokens(t.content or "") for t in recent)
    dropped_recent = 0

    def omitted() -> int:
        """真正没进摘要的发言条数。

        `len(older) - len(lines)` 是被 _line_for 判成空内容的那几条 ——
        它们同样「没被模型看到」，不报出来就会少报丢失的历史。
        """
        return len(older) - len(lines) + dropped_recent

    def build() -> str:
        n = omitted()
        head = f"（更早的 {n} 条发言已省略）\n" if n else ""
        return head + "\n".join(lines)

    if budget_tokens is not None:
        while estimate_tokens(build()) + recent_tokens > budget_tokens:
            if lines:
                lines.pop(0)  # 先丢最老的摘要行
            elif recent:
                # 摘要行丢完了还超预算，只能从 recent 最老的开始丢 ——
                # 否则「硬上限」就是假的（recent 自身超预算时永远不收敛）
                recent = recent[1:]
                dropped_recent += 1
                recent_tokens = sum(estimate_tokens(t.content or "") for t in recent)
            else:
                break

    digest = build()
    digest_tokens = estimate_tokens(digest)

    # 压不小就别压。短发言加上 "[名字] " 前缀和省略号之后，
    # 摘要可能比原文还长 —— 那就成了纯粹的负优化，不如原样保留。
    # 只要真的丢了东西就不能回退：丢掉的历史没法用原文补回来。
    # 注意必须要求 older 非空：older 为空时 raw=0，`0 >= 0` 会把
    # 「只剩 recent、而且 recent 已经按预算裁过」误判成「不用压」，
    # 于是把裁掉的历史又原样还回去。
    if older and omitted() == 0 and digest_tokens >= raw:
        return None, list(turns), {
            "compressed": False,
            "skipped": True,
            "reason": "压缩后不会更短，已跳过",
            "older_turns": 0,
            "kept_turns": len(turns),
            "raw_tokens": raw,
            "digest_tokens": 0,
            "dropped": 0,
        }

    total = digest_tokens + recent_tokens
    stats = {
        "compressed": True,
        "older_turns": len(older),
        "kept_turns": len(recent),
        "raw_tokens": raw,
        "digest_tokens": digest_tokens,
        "dropped": omitted(),
        # 负数会让界面显示「省了 -10」，比不显示更糟
        "saved_tokens": max(0, raw - digest_tokens),
        "total_tokens": total,
    }
    if budget_tokens is not None:
        stats["budget_tokens"] = budget_tokens
        # 摘要头 + speaker 前缀 + system prompt 都算不进 build()，
        # 所以留一点余量；超了要如实报，不能假装达标
        stats["over_budget"] = total > budget_tokens
    return digest, recent, stats
