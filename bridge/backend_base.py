"""微信桥接统一后端接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict


class BridgeBackend(ABC):
    """底层收发后端；历史数据和群成员查询仍由上层 SQLite 数据对象负责。"""

    name = "unknown"
    capabilities = frozenset()

    @abstractmethod
    def connect(self) -> None:
        """连接底层微信通道。"""

    @abstractmethod
    def account_id(self) -> str:
        """返回当前微信账号 wxid。"""

    @abstractmethod
    def self_info(self) -> Dict[str, Any]:
        """返回当前账号信息。"""

    @abstractmethod
    def start_listener(self, callback: Callable[[Any, Any], None]) -> Any:
        """启动实时入站消息监听。"""

    @abstractmethod
    def stop_listener(self, listener: Any) -> None:
        """停止实时入站消息监听。"""

    def send_image(self, target: str, path: str) -> bool:
        raise NotImplementedError

    def send_file(self, target: str, path: str) -> bool:
        raise NotImplementedError

    def send_quote(self, target: str, original_id: Any, text: str) -> bool:
        raise NotImplementedError

    def make_gui(self) -> Any:
        """创建发送通道对象。"""

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities
