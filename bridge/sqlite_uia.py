"""SQLite 收消息 + UI Automation 发消息的现有后端。"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from backend_base import BridgeBackend


class SqliteUiaBackend(BridgeBackend):
    """当前默认后端；不注入、不 Hook 微信。"""

    name = "sqlite_uia"
    capabilities = frozenset({
        "receive_text", "receive_media", "send_text", "send_image",
        "send_file", "send_quote",
    })

    def __init__(self, logger: Optional[Callable[..., None]] = None):
        self._log = logger or (lambda *args: None)
        self.db = None
        self._listener_type = None
        self._gui_type = None

    def connect(self) -> None:
        from wechatauto import WeChatDB, WeChatGUI
        from wechatauto.db import Listener

        self._listener_type = Listener
        self._gui_type = WeChatGUI
        self.db = WeChatDB()
        self._log("SQLite 后端已连接")

    def account_id(self) -> str:
        return str(getattr(self.db, "wxid", "") or "")

    def self_info(self) -> Dict[str, Any]:
        if self.db is None:
            return {}
        return self.db.get_self_info() or {}

    def start_listener(self, callback: Callable[[Any, Any], None]) -> Any:
        if self.db is None or self._listener_type is None:
            raise RuntimeError("SQLite 后端尚未连接")
        listener = self._listener_type(self.db, interval=0.4)
        listener.add_all(callback)
        listener.start()
        return listener

    def stop_listener(self, listener: Any) -> None:
        if listener is not None:
            listener.stop()

    def make_gui(self) -> Any:
        if self._gui_type is None:
            raise RuntimeError("SQLite 后端尚未连接")
        return self._gui_type()
