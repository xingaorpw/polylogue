"""本地配置。

所有需要用户「配一次」的东西都落在这一个文件里：`~/.polylogue/config.json`。
目标是不用环境变量、不用命令行参数、不用改代码 —— 界面上填一次，之后一直生效。

这个文件里有明文 API key，所以写入路径必须做两件事：
1. 原子写（临时文件 + fsync + replace），崩在中间不会留下半个文件
2. 收紧权限，让同机的其他账户读不到
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

# 进程内串行化读-改-写。Web 是 ThreadingHTTPServer，两个请求同时改
# 配置时各自拿到旧快照再写回，后写的会覆盖先写的（实测 90 次并发
# 新增只留下 17 个）。
_WRITE_LOCK = threading.RLock()

__all__ = [
    "CONFIG_PATH",
    "load",
    "save",
    "update",
    "reset",
    "DEFAULTS",
    "harden",
    "write_json_atomic",
]

CONFIG_PATH = Path.home() / ".polylogue" / "config.json"

DEFAULTS = {
    "onboarded": False,
    # {"openrouter": "sk-..."} —— 只在本地，不上传
    "api_keys": {},
    # [{"name": "critic", "provider": "openrouter", "model": "anthropic/claude-sonnet-4.5"}]
    "members": [],
    "budget": {"max_rounds": 6, "max_calls": 24, "max_cost_usd": 0.5},
    # keep_recent = 逐字保留最近几条；更早的压成摘要
    "context": {"keep_recent": 8, "enabled": True},
    "theme": "dark",
}


def _deep_merge(base: dict, override: dict) -> dict:
    """把 override 合到 base 上，**按 base 的类型校验**。

    不校验的后果（都实测过）：`{"budget": {"max_rounds": "abc"}}` 会让
    `_budget()` 每请求抛 ValueError（500）；`{"members": "openrouter"}`
    会被拆成 10 个单字母成员名，CLI 报「不认识的 provider：o」；
    `{"budget": null}` 直接把默认预算顶掉。配置文件是用户手改的，
    写错一个类型不该让整个程序变成另一副样子 —— 类型对不上就保留默认值。
    """
    out = dict(base)
    for k, v in (override or {}).items():
        if k not in out:
            # 表里没有的键（api_keys 的具体 key、将来的新字段）原样收下
            out[k] = v
            continue
        current = out[k]
        if isinstance(current, dict):
            if isinstance(v, dict):
                out[k] = _deep_merge(current, v)
            continue  # 类型不对：保留默认
        if isinstance(current, list):
            if isinstance(v, list):
                out[k] = v
            continue
        if isinstance(current, bool):
            if isinstance(v, bool):
                out[k] = v
            continue
        if isinstance(current, (int, float)):
            # bool 是 int 的子类，`true` 不该变成 1 混进数值配置
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = v
            continue
        if isinstance(current, str):
            if isinstance(v, str):
                out[k] = v
            continue
        out[k] = v
    return out


def _int_at_least(value, minimum: int, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if value >= minimum else default


def _limit_or_none(value, default):
    """成本上限：正有限浮点，或 None（表示不限）。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    value = float(value)
    # NaN 的比较恒为 False —— 拿它当上限等于「上限不存在」，
    # 用户以为自己设了 5 块钱，实际上一点护栏都没有。
    if value != value or value in (float("inf"), float("-inf")):
        return default
    return value if value > 0 else default


def _sanitize(data: dict) -> dict:
    """把数值配置收进合法范围。

    类型对不代表值对：`max_rounds=0` 让会话一句都不跑、`max_calls=-3`
    让第一次调用前就停、`max_cost=nan` 让上限彻底失效。这些都是手改
    配置文件能写出来的，不该以「保存成功」的形式变成不工作的程序。
    """
    budget = data.get("budget")
    if isinstance(budget, dict):
        budget["max_rounds"] = _int_at_least(budget.get("max_rounds"), 1, DEFAULTS["budget"]["max_rounds"])
        budget["max_calls"] = _int_at_least(budget.get("max_calls"), 1, DEFAULTS["budget"]["max_calls"])
        budget["max_cost_usd"] = _limit_or_none(
            budget.get("max_cost_usd"), DEFAULTS["budget"]["max_cost_usd"]
        )
    ctx = data.get("context")
    if isinstance(ctx, dict):
        if "keep_recent" in ctx:
            # None 是合法值：表示不做压缩，全部逐字保留
            ctx["keep_recent"] = (
                None
                if ctx["keep_recent"] is None
                else _int_at_least(ctx["keep_recent"], 0, DEFAULTS["context"]["keep_recent"])
            )
        if "budget_tokens" in ctx:
            ctx["budget_tokens"] = _int_at_least(ctx["budget_tokens"], 100, 6000)
    return data


def harden(path: Path) -> None:
    """把文件权限收到「只有当前用户能读写」。尽力而为，失败不抛。

    为什么不能只靠 chmod：Windows 上 `os.chmod` 基本是空操作，它只改只读位，
    不改 ACL —— 同机其他账户照样能读到明文 key。所以 Windows 上要额外用
    `icacls` 去掉继承，只授权当前用户。
    """
    path = Path(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

    if sys.platform != "win32":
        return

    user = os.environ.get("USERNAME") or ""
    if not user:
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def write_json_atomic(path: Path, data: dict) -> None:
    """原子写 JSON 并收紧权限。

    显式 `newline="\\n"`：Windows 上默认会把 `\\n` 翻成 `\\r\\n`，
    同一个文件在不同平台字节不一致，对比配置时会出现假的「变了」。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件名必须唯一。以前是固定的 `<名字>.tmp`，两个线程同时写
    # 同一个文件时会互相删/覆盖对方的临时文件（Windows 上直接
    # PermissionError），失败方的写入静默丢失。
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())  # 落盘再替换，断电也不会留半个文件
            except OSError:
                pass
        _replace_with_retry(tmp, path)
    except BaseException:
        # 任何异常都要清掉临时文件。它里面是**明文 API key** ——
        # json.dump 撞上不可序列化的值（TypeError）也一样会留下半个文件。
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    harden(path)
    # 写配置文件的唯一收口。缓存失效放在这里而不是 `save()` 里：
    # `credentials.save_key` 也是直接调本函数写 config 的，绕过 save()
    # 的话读缓存会在 TTL 内返回旧值 —— 刚存的 key 读不出来。
    if path == CONFIG_PATH:
        _bump_generation()


def _replace_with_retry(src: Path, dst: Path, attempts: int = 12) -> None:
    """替换目标文件。Windows 上目标正被读时 os.replace 会抛 PermissionError
    （WinError 5/32），通常几十毫秒就消失 —— 短重试比直接失败好。"""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.02 * (i + 1))


def _defaults() -> dict:
    return copy.deepcopy(DEFAULTS)


# 读缓存。`load()` 被调得非常频繁（`contact_by_id` 每次调用都会走一遍），
# 每次都读盘 + JSON 解析 + 深合并，实测 400 µs —— 一个 /api/info 里
# 调几十次就是几十毫秒。
#
# 正确性：
# - 本进程写入 → `_generation` 变化，立即失效（不依赖时间戳精度）
# - 外部改动 → 靠 (mtime_ns, size) 指纹发现，但**最多晚 TTL 才发现**
# - 返回深拷贝，调用方改不到缓存本体
#
# 为什么指纹要带 TTL：Windows 上单次 `stat()` 实测 ~160 µs（杀软实时
# 扫描），热路径每次 stat 反而比解析还贵。TTL 内直接用上次的指纹。
_CACHE_LOCK = threading.RLock()
_CACHE_TTL = 0.25
# stamp = 缓存里那份 data 对应的指纹；probe = 上次 stat 拿到的指纹。
# 两个必须分开：只存一个的话，「stat 发现变了」和「缓存对应哪一版」会
# 混在一起 —— 把新指纹写进 stamp 却留着旧 data，就会一直读到旧值。
_cache: dict = {
    "stamp": None,
    "probe": None,
    "probe_path": None,
    "data": None,
    "generation": -1,
    "checked_at": 0.0,
}
_generation = 0


def _bump_generation() -> None:
    global _generation
    _generation += 1


def _fingerprint() -> tuple | None:
    """(path, generation, mtime_ns, size)。文件不存在/读不了返回 None。"""
    now = time.monotonic()
    here = str(CONFIG_PATH)
    with _CACHE_LOCK:
        if (
            _cache["data"] is not None
            and _cache["generation"] == _generation
            and _cache["probe_path"] == here
            and now - _cache["checked_at"] < _CACHE_TTL
        ):
            return _cache["probe"]
    try:
        st = CONFIG_PATH.stat()
    except OSError:
        return None
    # 路径也要进指纹：测试和 `--config` 会切 CONFIG_PATH，
    # 只比 mtime/size 的话两个不同文件可能撞上，读到别的配置。
    stamp = (here, _generation, st.st_mtime_ns, st.st_size)
    with _CACHE_LOCK:
        _cache["checked_at"] = now
        _cache["probe"] = stamp
        _cache["probe_path"] = here
    return stamp


def load() -> dict:
    """读配置。文件不存在、损坏、字段缺失都退回默认值，绝不让程序起不来。"""
    stamp = _fingerprint()
    if stamp is None:
        return _defaults()

    with _CACHE_LOCK:
        if _cache["stamp"] == stamp and _cache["data"] is not None:
            return copy.deepcopy(_cache["data"])

    try:
        # utf-8-sig：记事本和 PowerShell 5 的 `Set-Content -Encoding utf8`
        # 都会在开头写 BOM。用 `utf-8` 读会带出一个 \ufeff，json.loads 直接
        # 报错 → 整份配置被当成损坏 → key、成员、主题**静默全丢**。
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        data = None
    if isinstance(data, dict):
        merged = _sanitize(_deep_merge(DEFAULTS, data))
    else:
        # 文件坏了也要把默认值写进缓存：否则 load() 说「默认」、
        # load_shared() 还拿着上一版缓存，同一个进程两套真相。
        merged = _defaults()
    with _CACHE_LOCK:
        _cache.update(stamp=stamp, probe=stamp, data=merged, generation=_generation)
    return copy.deepcopy(merged)


def load_shared() -> dict:
    """只读视图：返回缓存本体，**调用方绝对不能修改**。

    给热路径用（`contact_by_id` 每次 `/api/info` 会被调几十次），
    省掉深拷贝。要改配置一律走 `load()`（深拷贝）+ `save()`。
    """
    stamp = _fingerprint()
    if stamp is None:
        return DEFAULTS
    with _CACHE_LOCK:
        if _cache["stamp"] == stamp and _cache["data"] is not None:
            return _cache["data"]

    load()  # 未命中：走完整路径把缓存填上（返回值丢掉，这里只要副作用）
    with _CACHE_LOCK:
        return _cache["data"] if _cache["data"] is not None else DEFAULTS


def save(data: dict) -> None:
    # 类型不对就报错，不要写进去：写进去之后 load() 会静默回默认值，
    # 用户看到的是「保存成功」但什么都没变。
    if not isinstance(data, dict):
        raise TypeError(f"配置必须是 dict，收到 {type(data).__name__}")
    # 失效在 write_json_atomic 里做（那里是唯一写入收口）
    write_json_atomic(CONFIG_PATH, data)


def update(**changes) -> dict:
    # 读-改-写要串行：两个请求同时改设置时，后写的会覆盖先写的
    with _WRITE_LOCK:
        data = _deep_merge(load(), changes)
        save(data)
        return data


def reset() -> None:
    if CONFIG_PATH.is_file():
        CONFIG_PATH.unlink()
    # 删文件后 mtime/size 指纹对不上，但显式失效更稳（缓存里可能存着旧值）
    _bump_generation()
