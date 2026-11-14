"""本地配置。

所有需要用户「配一次」的东西都落在这一个文件里：`~/.polylogue/config.json`。
目标是不用环境变量、不用命令行参数、不用改代码 —— 界面上填一次，之后一直生效。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

__all__ = ["CONFIG_PATH", "load", "save", "update", "reset", "DEFAULTS"]

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
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)  # 原子写，断电不会留半个文件
    try:
        os.chmod(CONFIG_PATH, 0o600)  # 里面有 key
    except OSError:
        pass


def update(**changes) -> dict:
    data = _deep_merge(load(), changes)
    save(data)
    return data


def reset() -> None:
    if CONFIG_PATH.is_file():
        CONFIG_PATH.unlink()
