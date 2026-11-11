"""桥接后端工厂。"""

from __future__ import annotations

from typing import Optional

from backend_base import BridgeBackend
from native_hook import NativeHookBackend
from sqlite_uia import SqliteUiaBackend


def create_backend(name: str = "sqlite_uia", logger=None) -> BridgeBackend:
    """创建底层后端；auto 当前安全地选择 SQLite + UIA。"""
    normalized = str(name or "sqlite_uia").strip().lower()
    if normalized in ("sqlite_uia", "sqlite", "uia", "auto"):
        return SqliteUiaBackend(logger=logger)
    if normalized in ("native_hook", "hook"):
        return NativeHookBackend(logger=logger)
    raise ValueError("不支持的桥接后端：%s" % normalized)
