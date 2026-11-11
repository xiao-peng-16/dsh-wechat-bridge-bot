"""基于 helper 原生桥的 Native Hook 后端适配。

本模块只在选择 native_hook 且桥接启动时导入 helper 的原生服务；默认
sqlite_uia 路径不会触碰 DLL。helper 项目本身不需要启动 start_web.bat/main.py。
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from backend_base import BridgeBackend


_DEFAULT_HELPER_ROOT = (
    Path(__file__).resolve().parents[2] / ".参考项目" / "helper"
)


class _NativeGui:
    """把 helper 的原生文本发送适配成当前桥接使用的 GUI 形状。"""

    def __init__(self, service: Any, backend: Any, logger: Callable[..., None]):
        self._service = service
        self._backend = backend
        self._log = logger
        self._current_chat = None

    def open_chat(self, chat: str) -> bool:
        self._current_chat = str(chat or "")
        return True

    def current_chat(self) -> Optional[str]:
        return self._current_chat

    def send_msg(self, text: str, who: Optional[str] = None, confirm: bool = False) -> bool:
        target = str(who or self._current_chat or "").strip()
        if not target:
            raise RuntimeError("Native Hook 发送缺少目标会话")
        result = bool(self._service.send_text(target, str(text)))
        detail = getattr(self._service, "last_result", None) or {}
        self._log("Native Hook WxSendText：target=%s，success=%s，report=%s" % (
            target, result, detail.get("report", detail)))
        return result

    def send_image(self, path: str, chat: Optional[str] = None) -> bool:
        if not self._backend.supports("send_image"):
            raise NotImplementedError("当前微信 profile 未验证原生图片发送")
        target = str(chat or self._current_chat or "").strip()
        if not target:
            raise RuntimeError("Native Hook 发图缺少目标会话")
        result = bool(self._service.send_image(target, path))
        self._log("Native Hook 图片发送：target=%s，success=%s，report=%s" % (
            target, result, (getattr(self._service, "last_result", None) or {}).get("report", {})))
        return result

    def quote_msg(self, text: str, who: Optional[str] = None, target_text: str = "") -> bool:
        if not self._backend.supports("send_quote"):
            raise NotImplementedError("当前微信 profile 未验证原生引用发送")
        target = str(who or self._current_chat or "").strip()
        original_id = str(target_text or "").strip()
        if not target or not original_id:
            raise ValueError("Native Hook 引用发送需要目标会话和原消息 ID")
        result = bool(self._service.send_quote_text(target, original_id, str(text)))
        detail = getattr(self._service, "last_send_result", None) or getattr(self._service, "last_result", None) or {}
        self._log("Native Hook 引用发送：target=%s，success=%s，report=%s" % (
            target, result, detail.get("report", {})))
        return result

    def send_file(self, path: str, chat: Optional[str] = None, confirm: bool = False) -> bool:
        if not self._backend.supports("send_file"):
            raise NotImplementedError("当前微信 profile 未验证原生文件发送")
        target = str(chat or self._current_chat or "").strip()
        if not target:
            raise RuntimeError("Native Hook 发文件缺少目标会话")
        result = bool(self._service.send_file(target, path))
        self._log("Native Hook 文件发送：target=%s，success=%s，report=%s" % (
            target, result, (getattr(self._service, "last_result", None) or {}).get("report", {})))
        return result


class NativeHookBackend(BridgeBackend):
    name = "native_hook"
    capabilities = frozenset({"receive_text", "send_text"})

    def __init__(self, logger: Optional[Callable[..., None]] = None,
                 helper_root: Optional[str] = None):
        if logger is None:
            self._log = lambda *args: None
        else:
            def _write_log(*args):
                try:
                    logger(args[0] % args[1:] if len(args) > 1 else args[0])
                except Exception:
                    logger(*args)
            self._log = _write_log
        configured = helper_root or os.environ.get("DSH_HELPER_ROOT")
        self.helper_root = Path(configured).expanduser().resolve() if configured else _DEFAULT_HELPER_ROOT
        self.db = None
        self._service = None
        self._gui = None
        self._handler = None
        self._on_image = None
        self._profile_version = None

    def _load_helper(self):
        if not self.helper_root.is_dir():
            raise RuntimeError("未找到 helper 参考项目：%s" % self.helper_root)
        native_dir = self.helper_root / "native"
        if not native_dir.is_dir():
            raise RuntimeError("helper 缺少 native 目录：%s" % native_dir)
        for path in (self.helper_root, native_dir):
            value = str(path)
            if value not in sys.path:
                sys.path.insert(0, value)
        try:
            from text_service import NativeTextService
        except Exception as error:
            raise RuntimeError("导入 helper 原生文本服务失败：%r" % (error,)) from error
        return NativeTextService

    def _wechat_pid(self, native_text_service: Any) -> Optional[int]:
        """按 helper 的规则筛选真正加载 Weixin.dll 的微信主进程。"""
        try:
            from run_selftest import modules, wechat_candidates
            candidates = list(wechat_candidates())
        except Exception as error:
            raise RuntimeError("Native Hook 枚举微信核心进程失败：%s" % error) from error
        if len(candidates) != 1:
            raise RuntimeError("Native Hook 需要唯一可识别微信主进程，当前候选 PID：%s" % candidates)
        pid = int(candidates[0])
        core = modules(pid).get("weixin.dll")
        if core:
            path = Path(core[1])
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                size = path.stat().st_size
                self._log("Native Hook 核心 DLL：%s，size=%d，sha256=%s",
                          path, size, digest)
                try:
                    from wxbot_framework.native_profiles import profile_for_fingerprint
                    profile = profile_for_fingerprint(size, digest)
                except Exception:
                    profile = None
                if profile is None:
                    raise RuntimeError(
                        "当前微信核心 DLL 未适配：%s（size=%d，sha256=%s）；"
                        "不能安全加载 Hook，请先建立 4.2.0.13 profile" %
                        (path, size, digest)
                    )
                self._profile_version = profile.version
                self._set_capabilities_for_profile()
                self._log("Native Hook 已匹配 profile：%s", profile.version)
            except OSError as error:
                self._log("Native Hook 读取核心 DLL 指纹失败：%s", error)
        return pid

    def connect(self) -> None:
        if self._service is not None:
            return
        try:
            from wechatauto import WeChatDB
            self.db = WeChatDB()
            native_text_service = self._load_helper()
            wxid = getattr(self.db, "wxid", None)
            pid = self._wechat_pid(native_text_service)
            self._log("Native Hook 目标：PID=%s，wxid=%s", pid, wxid)
            self._service = native_text_service(
                pid=pid,
                account=wxid,
                preserve_reports=False,
            )
            # helper 的图片/文件/引用 ABI 目前只在 4.2.0.12 做过验证。
            self._profile_version = getattr(self._service, "wechat_version", None) or self._profile_version
            self._service._dsh_backend = self
            self._set_capabilities_for_profile()
            # 回调要在 start() 前注册；否则启动瞬间到达的消息可能无人接收。
            self._log("Native Hook 后端已准备：%s", self.helper_root)
        except Exception:
            self._service = None
            self.db = None
            raise

    def account_id(self) -> str:
        if self._service is None:
            raise RuntimeError("Native Hook 后端尚未连接")
        return str(getattr(self._service, "self_wxid", "") or getattr(self.db, "wxid", "") or "")

    def self_info(self) -> Dict[str, Any]:
        if self.db is None:
            return {}
        return self.db.get_self_info() or {}

    def set_image_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        self._on_image = callback

    def _set_capabilities_for_profile(self) -> None:
        if self._profile_version == "4.2.0.12":
            self.capabilities = frozenset({
                "receive_text", "receive_image", "send_text", "send_image", "send_file", "send_quote"
            })
        else:
            self.capabilities = frozenset({"receive_text", "send_text"})

    @staticmethod
    def _adapt_message(data: Dict[str, Any]) -> Dict[str, Any]:
        wx_type = int(data.get("wx_type") or 1)
        type_name = {
            1: "文本",
            3: "图片",
            34: "语音",
            43: "视频",
            47: "动画表情",
            49: "文件/链接/卡片",
            62: "视频",
            10000: "系统消息",
            10002: "系统消息",
        }.get(wx_type, "文本")
        result = dict(data)
        result.update({
            "username": data.get("chat_id") or data.get("room_wxid") or "",
            "type": type_name,
            "content": data.get("content") or data.get("msg") or data.get("raw_content") or data.get("system_content") or "",
            "raw_content": data.get("raw_content") or data.get("system_content") or data.get("content") or "",
            "system_content": data.get("system_content") or "",
            "local_id": 0,
            "shard": "",
            "sender_username": data.get("sender_id") or data.get("from_wxid") or "",
            "create_time": 0,
        })
        if data.get("image_path"):
            result["native_image_path"] = data["image_path"]
        return result

    def start_listener(self, callback: Callable[[Any, Any], None]) -> Any:
        if self._service is None:
            raise RuntimeError("Native Hook 后端尚未连接")
        if self._handler is not None:
            raise RuntimeError("Native Hook 监听器已经启动")

        def handler(_pid: int, _protocol: int, data: Dict[str, Any], _service: Any):
            if int(data.get("wx_type") or 0) == 3:
                # 原始 Hook 事件只含 XML 元数据；等待 ImageReceiver 的下载完成回调。
                if self._on_image is None or not self.supports("receive_image"):
                    callback(self._adapt_message(data), self)
                return
            callback(self._adapt_message(data), self)

        self._handler = handler
        # helper 在 start() 内部创建 ImageReceiver，先包装回调再启动才能接管下载完成事件。
        original_deliver_image = getattr(self._service, "_deliver_image", lambda _data: True)

        def deliver_image(data: Dict[str, Any]):
            original_deliver_image(data)
            if self.supports("receive_image") and self._on_image is not None:
                self._on_image(self._adapt_message(data))

        if self._on_image is not None:
            self._service._deliver_image = deliver_image
        self._service.register_msg_handler(handler)
        # 监听器注册完成后才加载 DLL、安装 Hook 和启动轮询线程。
        try:
            self._service.start()
            self._profile_version = getattr(self._service, "wechat_version", self._profile_version)
            self._set_capabilities_for_profile()
        except Exception:
            self._handler = None
            try:
                self._service.stop()
            except Exception:
                pass
            raise
        self._log("Native Hook 后端已连接：%s", self.helper_root)
        return self

    @property
    def _thread(self):
        """兼容 wechat_bridge 的监听线程存活检查。"""
        return getattr(self._service, "_thread", None)

    def stop_listener(self, listener: Any) -> None:
        # helper 的 stop() 会等待接收线程结束、卸载 DLL 并释放进程锁。
        if self._service is not None:
            try:
                self._service.stop()
            finally:
                self._handler = None
                self._service = None
                self._gui = None

    def send_image(self, target: str, path: str) -> bool:
        if not self.supports("send_image"):
            raise NotImplementedError("当前微信 profile 未验证原生图片发送")
        if self._service is None:
            raise RuntimeError("Native Hook 后端尚未连接")
        result = bool(self._service.send_image(target, path))
        detail = getattr(self._service, "last_send_result", None) or getattr(self._service, "last_result", None) or {}
        self._log("Native Hook 图片发送：target=%s，success=%s，report=%s" % (
            target, result, detail.get("report", {})))
        return result

    def send_file(self, target: str, path: str) -> bool:
        if not self.supports("send_file"):
            raise NotImplementedError("当前微信 profile 未验证原生文件发送")
        if self._service is None:
            raise RuntimeError("Native Hook 后端尚未连接")
        result = bool(self._service.send_file(target, path))
        detail = getattr(self._service, "last_send_result", None) or getattr(self._service, "last_result", None) or {}
        self._log("Native Hook 文件发送：target=%s，success=%s，report=%s" % (
            target, result, detail.get("report", {})))
        return result

    def send_quote(self, target: str, original_id: Any, text: str) -> bool:
        if not self.supports("send_quote"):
            raise NotImplementedError("当前微信 profile 未验证原生引用发送")
        if self._service is None:
            raise RuntimeError("Native Hook 后端尚未连接")
        result = bool(self._service.send_quote_text(target, original_id, text))
        detail = getattr(self._service, "last_result", None) or {}
        self._log("Native Hook 引用发送：target=%s，success=%s，report=%s" % (
            target, result, detail.get("report", {})))
        return result

    def make_gui(self) -> Any:
        if self._service is None:
            raise RuntimeError("Native Hook 后端尚未连接")
        if self._gui is None:
            self._gui = _NativeGui(self._service, self, self._log)
        return self._gui
