"""本地配置。

所有需要用户「配一次」的东西都落在这一个文件里：`~/.polylogue/config.json`。
目标是不用环境变量、不用命令行参数、不用改代码 —— 界面上填一次，之后一直生效。

这个文件里有明文 API key，所以写入路径必须做两件事：
1. 原子写（临时文件 + fsync + replace），崩在中间不会留下半个文件
2. 收紧权限，让同机的其他账户读不到
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

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
    # [{"name": "critic", "provider": "openrouter", "model": "anthropic/claude-sonnet-4"}]
    "members": [],
    "budget": {"max_rounds": 6, "max_calls": 24, "max_cost_usd": 0.5},
    # keep_recent = 逐字保留最近几条；更早的压成摘要
    "context": {"keep_recent": 8, "enabled": True},
    "theme": "dark",
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


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
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())  # 落盘再替换，断电也不会留半个文件
        except OSError:
            pass
    os.replace(tmp, path)
    harden(path)


def load() -> dict:
    """读配置。文件不存在、损坏、字段缺失都退回默认值，绝不让程序起不来。"""
    if not CONFIG_PATH.is_file():
        return json.loads(json.dumps(DEFAULTS))
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return json.loads(json.dumps(DEFAULTS))
    if not isinstance(data, dict):
        return json.loads(json.dumps(DEFAULTS))
    return _deep_merge(DEFAULTS, data)


def save(data: dict) -> None:
    write_json_atomic(CONFIG_PATH, data)


def update(**changes) -> dict:
    data = _deep_merge(load(), changes)
    save(data)
    return data


def reset() -> None:
    if CONFIG_PATH.is_file():
        CONFIG_PATH.unlink()
