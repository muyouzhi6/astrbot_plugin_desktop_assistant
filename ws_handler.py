"""
WebSocket 客户端管理器和消息处理模块

这个模块提供：
1. ClientManager: 管理所有已连接的桌面客户端
2. 数据类: ClientDesktopState, ScreenshotRequest, ScreenshotResponse
3. 消息处理逻辑

注意：这个模块不再包含 WebSocket 服务器逻辑，服务器功能已移至 ws_server.py
"""

import asyncio
import base64
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from astrbot.api import logger


@dataclass
class ClientDesktopState:
    """
    客户端上报的桌面状态
    
    桌面客户端会定期上报当前的桌面状态，包括：
    - 活动窗口信息（标题、进程名、PID）
    - 可选的截图数据
    - 运行中的应用列表
    """
    session_id: str                              # 客户端会话 ID
    timestamp: str                               # 状态时间戳
    active_window_title: Optional[str] = None   # 活动窗口标题
    active_window_process: Optional[str] = None # 活动窗口进程名
    active_window_pid: Optional[int] = None     # 活动窗口进程 PID
    screenshot_base64: Optional[str] = None     # 截图 Base64 数据
    screenshot_width: Optional[int] = None      # 截图宽度
    screenshot_height: Optional[int] = None     # 截图高度
    running_apps: Optional[list] = None         # 运行中的应用列表
    window_changed: bool = False                # 窗口是否发生变化
    previous_window_title: Optional[str] = None # 上一个窗口标题
    received_at: Optional[datetime] = None      # 服务端接收时间
    
    @classmethod
    def from_dict(cls, session_id: str, data: dict) -> "ClientDesktopState":
        """从字典创建实例"""
        return cls(
            session_id=session_id,
            timestamp=data.get("timestamp", datetime.now().isoformat()),
            active_window_title=data.get("active_window_title"),
            active_window_process=data.get("active_window_process"),
            active_window_pid=data.get("active_window_pid"),
            screenshot_base64=data.get("screenshot_base64"),
            screenshot_width=data.get("screenshot_width"),
            screenshot_height=data.get("screenshot_height"),
            running_apps=data.get("running_apps"),
            window_changed=data.get("window_changed", False),
            previous_window_title=data.get("previous_window_title"),
            received_at=datetime.now(),
        )


@dataclass
class ScreenshotRequest:
    """
    截图请求
    
    当用户发送截图命令时，会创建一个截图请求并发送给桌面客户端。
    """
    request_id: str                                     # 请求唯一 ID
    session_id: str                                     # 目标客户端会话 ID
    created_at: datetime = field(default_factory=datetime.now)  # 创建时间
    timeout: float = 30.0                               # 超时时间（秒）
    
    def is_expired(self) -> bool:
        """检查请求是否已超时"""
        elapsed = (datetime.now() - self.created_at).total_seconds()
        return elapsed > self.timeout


@dataclass
class ScreenshotResponse:
    """
    截图响应
    
    桌面客户端执行截图后返回的结果。
    """
    request_id: str                              # 对应的请求 ID
    session_id: str                              # 客户端会话 ID
    success: bool                                # 是否成功
    image_base64: Optional[str] = None           # 图片 Base64 数据
    image_path: Optional[str] = None             # 图片保存路径
    error_message: Optional[str] = None          # 错误信息
    width: Optional[int] = None                  # 图片宽度
    height: Optional[int] = None                 # 图片高度
    timestamp: datetime = field(default_factory=datetime.now)  # 响应时间


class ClientManager:
    """
    WebSocket 客户端管理器
    
    管理所有已连接的桌面客户端，提供：
    - 客户端连接/断开管理
    - 消息发送（单发/广播）
    - 桌面状态管理
    - 截图请求/响应处理
    
    这个类被 ws_server.py 中的 StandaloneWebSocketServer 使用。
    """
    
    def __init__(self):
        # 存储客户端的最新桌面状态: session_id -> ClientDesktopState
        self.client_states: Dict[str, ClientDesktopState] = {}
        
        # 桌面状态更新回调
        self.on_desktop_state_update: Optional[Callable[[ClientDesktopState], Any]] = None
        
        # 截图请求管理
        self._pending_screenshot_requests: Dict[str, ScreenshotRequest] = {}
        self._screenshot_futures: Dict[str, asyncio.Future] = {}
        
        # 截图保存目录
        self._screenshot_save_dir = "./temp/remote_screenshots"
        os.makedirs(self._screenshot_save_dir, exist_ok=True)
        
        # WebSocket 服务器引用（由 main.py 设置）
        self._ws_server = None
    
    def set_ws_server(self, ws_server):
        """设置 WebSocket 服务器引用"""
        self._ws_server = ws_server
    
    def get_active_clients_count(self) -> int:
        """获取活跃客户端数量"""
        if self._ws_server:
            return self._ws_server.get_active_clients_count()
        return 0
    
    def get_connected_client_ids(self) -> List[str]:
        """获取所有已连接客户端的 session_id 列表"""
        if self._ws_server:
            return self._ws_server.get_connected_client_ids()
        return []
    
    async def send_message(self, session_id: str, message: dict) -> bool:
        """
        发送消息给指定客户端
        
        Args:
            session_id: 目标客户端会话 ID
            message: 要发送的消息（字典格式）
            
        Returns:
            是否发送成功
        """
        if not self._ws_server:
            logger.warning("WebSocket 服务器未初始化")
            return False
        
        return await self._ws_server.send_to_client(session_id, message)
    
    async def broadcast(self, message: dict) -> int:
        """
        广播消息给所有客户端
        
        Args:
            message: 要发送的消息（字典格式）
            
        Returns:
            成功发送的客户端数量
        """
        if not self._ws_server:
            logger.warning("WebSocket 服务器未初始化")
            return 0
        
        return await self._ws_server.broadcast(message)
    
    def update_client_state(self, session_id: str, state_data: dict) -> ClientDesktopState:
        """
        更新客户端桌面状态
        
        Args:
            session_id: 客户端会话 ID
            state_data: 状态数据字典
            
        Returns:
            更新后的 ClientDesktopState 对象
        """
        state = ClientDesktopState.from_dict(session_id, state_data)
        self.client_states[session_id] = state
        logger.debug(f"客户端桌面状态已更新: session_id={session_id}, window={state.active_window_title}")
        return state
    
    def remove_client_state(self, session_id: str):
        """移除客户端状态（客户端断开时调用）"""
        self.client_states.pop(session_id, None)
        
    def get_client_state(self, session_id: str) -> Optional[ClientDesktopState]:
        """获取客户端桌面状态"""
        return self.client_states.get(session_id)
        
    def get_all_client_states(self) -> Dict[str, ClientDesktopState]:
        """获取所有客户端桌面状态"""
        return self.client_states.copy()
    
    async def request_screenshot(
        self,
        session_id: Optional[str] = None,
        timeout: float = 30.0
    ) -> ScreenshotResponse:
        """
        请求客户端截图
        
        Args:
            session_id: 目标客户端 session_id，为 None 则选择第一个可用客户端
            timeout: 超时时间（秒）
            
        Returns:
            ScreenshotResponse 对象
        """
        # 确定目标客户端
        connected_clients = self.get_connected_client_ids()
        
        if session_id is None:
            if not connected_clients:
                return ScreenshotResponse(
                    request_id="",
                    session_id="",
                    success=False,
                    error_message="没有已连接的桌面客户端"
                )
            session_id = connected_clients[0]
        
        if session_id not in connected_clients:
            return ScreenshotResponse(
                request_id="",
                session_id=session_id,
                success=False,
                error_message=f"客户端未连接: {session_id}"
            )
        
        # 创建请求
        request_id = str(uuid.uuid4())
        request = ScreenshotRequest(
            request_id=request_id,
            session_id=session_id,
            timeout=timeout
        )
        
        self._pending_screenshot_requests[request_id] = request
        
        # 创建 Future 用于等待响应
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._screenshot_futures[request_id] = future
        
        try:
            # 发送截图命令到客户端
            await self.send_message(session_id, {
                "type": "command",
                "command": "screenshot",
                "request_id": request_id,
                "params": {
                    "type": "full"  # 全屏截图
                }
            })
            
            logger.info(f"已发送截图命令到客户端: session_id={session_id}, request_id={request_id}")
            
            # 等待响应（带超时）
            response = await asyncio.wait_for(future, timeout=timeout)
            return response
            
        except asyncio.TimeoutError:
            logger.warning(f"截图请求超时: request_id={request_id}")
            return ScreenshotResponse(
                request_id=request_id,
                session_id=session_id,
                success=False,
                error_message="截图请求超时"
            )
        except Exception as e:
            logger.error(f"截图请求失败: {e}")
            return ScreenshotResponse(
                request_id=request_id,
                session_id=session_id,
                success=False,
                error_message=str(e)
            )
        finally:
            # 清理
            self._pending_screenshot_requests.pop(request_id, None)
            self._screenshot_futures.pop(request_id, None)
    
    def handle_screenshot_response(self, session_id: str, data: dict) -> Optional[ScreenshotResponse]:
        """
        处理客户端返回的截图响应
        
        Args:
            session_id: 客户端 session_id
            data: 响应数据
            
        Returns:
            ScreenshotResponse 对象，如果无对应请求则返回 None
        """
        request_id = data.get("request_id")
        if not request_id:
            logger.warning("截图响应缺少 request_id")
            return None
        
        # 检查是否有对应的等待中的请求
        if request_id not in self._screenshot_futures:
            logger.warning(f"未找到对应的截图请求: request_id={request_id}")
            return None
        
        success = data.get("success", False)
        image_base64 = data.get("image_base64")
        error_message = data.get("error_message")
        
        response = ScreenshotResponse(
            request_id=request_id,
            session_id=session_id,
            success=success,
            image_base64=image_base64,
            error_message=error_message,
            width=data.get("width"),
            height=data.get("height")
        )
        
        # 如果成功且有图片数据，保存到文件
        if success and image_base64:
            try:
                image_data = base64.b64decode(image_base64)
                filename = f"screenshot_{request_id}_{int(time.time() * 1000)}.png"
                filepath = os.path.join(self._screenshot_save_dir, filename)
                
                with open(filepath, "wb") as f:
                    f.write(image_data)
                
                response.image_path = filepath
                logger.info(f"截图已保存: {filepath}")
            except Exception as e:
                logger.error(f"保存截图失败: {e}")
        
        # 完成 Future
        future = self._screenshot_futures.get(request_id)
        if future and not future.done():
            future.set_result(response)
        
        return response


class MessageHandler:
    """
    消息处理器
    
    处理来自桌面客户端的各种消息类型。
    这个类被 main.py 使用，作为 StandaloneWebSocketServer 的消息回调。
    """
    
    def __init__(self, client_manager: ClientManager):
        """
        初始化消息处理器
        
        Args:
            client_manager: 客户端管理器实例
        """
        self.manager = client_manager
    
    async def handle_message(self, session_id: str, data: dict):
        """
        处理客户端消息
        
        Args:
            session_id: 客户端会话 ID
            data: 消息数据
        """
        msg_type = data.get("type", "")
        
        if msg_type == "desktop_state":
            # 处理桌面状态上报
            await self._handle_desktop_state(session_id, data)
            
        elif msg_type == "screenshot_response":
            # 处理截图响应
            response_data = data.get("data", {})
            self.manager.handle_screenshot_response(session_id, response_data)
            logger.debug(f"收到截图响应: session_id={session_id}")
            
        elif msg_type == "command_result":
            # 处理通用命令执行结果
            command = data.get("command")
            if command == "screenshot":
                response_data = data.get("data", {})
                self.manager.handle_screenshot_response(session_id, response_data)
                
        elif msg_type == "state_sync":
            # 处理客户端状态同步（保留向后兼容）
            pass
        
        else:
            logger.debug(f"收到未知类型消息: type={msg_type}, session_id={session_id}")
    
    async def _handle_desktop_state(self, session_id: str, data: dict):
        """处理桌面状态上报"""
        state_data = data.get("data", {})
        state = self.manager.update_client_state(session_id, state_data)
        
        # 触发回调（如果设置）
        if self.manager.on_desktop_state_update:
            try:
                result = self.manager.on_desktop_state_update(state)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"桌面状态回调执行失败: {e}")
        
        # 发送确认
        await self.manager.send_message(session_id, {
            "type": "desktop_state_ack",
            "timestamp": state.timestamp,
        })
    
    def on_client_connect(self, session_id: str):
        """客户端连接回调"""
        logger.info(f"客户端已连接: session_id={session_id}")
    
    def on_client_disconnect(self, session_id: str):
        """客户端断开回调"""
        logger.info(f"客户端已断开: session_id={session_id}")
        # 清理客户端状态
        self.manager.remove_client_state(session_id)