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

# 中日韩字符 + 全角标点：这些大约 1 token / 字
CJK_RE = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")

_SENT_END = re.compile(r"[。！？!?\n]")


def estimate_tokens(text: str) -> int:
    """粗估 token 数。不追求精确，只要求「方向对、不低估」。

    中文按 1 字 1 token（真实分词器通常 1.2~1.6 字/token，这里偏高，
    用作预算是安全的）；其余按 4 字符 1 token。
    """
    if not text:
        return 0
    cjk = len(CJK_RE.findall(text))
    other = len(text) - cjk
    return int(cjk + other / 4 + 0.5)


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
    2. `budget_tokens` —— 硬上限。压完还超，就从最老的那行开始丢，
       并在摘要开头注明丢了多少条。这一级保证上下文**不会无限增长**，
       而不是「大概会小一点」。

    统计里的数字是给界面展示用的 —— 用户应该能看到「压缩到底省了多少」，
    而不是被要求相信它有用。
    """
    if keep_recent < 0:
        keep_recent = 0

    if len(turns) <= keep_recent:
        return None, list(turns), {
            "compressed": False,
            "older_turns": 0,
            "kept_turns": len(turns),
            "raw_tokens": sum(estimate_tokens(t.content or "") for t in turns),
            "digest_tokens": 0,
            "dropped": 0,
        }

    older = turns[:-keep_recent] if keep_recent else list(turns)
    recent = turns[-keep_recent:] if keep_recent else []

    lines = [line for line in (_line_for(t) for t in older) if line]
    raw = sum(estimate_tokens(t.content or "") for t in older)
    recent_tokens = sum(estimate_tokens(t.content or "") for t in recent)

    dropped = 0
    if budget_tokens is not None:
        def build() -> str:
            head = f"（更早的 {dropped} 条发言已省略）\n" if dropped else ""
            return head + "\n".join(lines)

        while lines and estimate_tokens(build()) + recent_tokens > budget_tokens:
            lines.pop(0)  # 丢最老的
            dropped += 1

    digest = (f"（更早的 {dropped} 条发言已省略）\n" if dropped else "") + "\n".join(lines)
    digest_tokens = estimate_tokens(digest)

    # 压不小就别压。短发言加上 "[名字] " 前缀和省略号之后，
    # 摘要可能比原文还长 —— 那就成了纯粹的负优化，不如原样保留。
    if digest_tokens >= raw and not dropped:
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

    return digest, recent, {
        "compressed": True,
        "older_turns": len(older),
        "kept_turns": len(recent),
        "raw_tokens": raw,
        "digest_tokens": digest_tokens,
        "saved_tokens": raw - digest_tokens,
        "dropped": dropped,
        "total_tokens": digest_tokens + recent_tokens,
    }
