"""兼容入口：后端实现已拆分到独立模块。"""

from backend_base import BridgeBackend
from backend_factory import create_backend
from native_hook import NativeHookBackend
from sqlite_uia import SqliteUiaBackend

__all__ = [
    "BridgeBackend",
    "NativeHookBackend",
    "SqliteUiaBackend",
    "create_backend",
]
