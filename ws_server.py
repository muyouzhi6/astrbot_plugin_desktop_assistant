"""
独立 WebSocket 服务器模块

使用 websockets 库创建独立的 WebSocket 服务器，监听端口 6190。
这个方案不依赖 AstrBot 主应用，避免了框架兼容性问题。

桌面客户端连接地址: ws://服务器IP:6190
"""

import asyncio
import json
import traceback
from typing import Optional, Callable, Any, Dict, Set
from urllib.parse import parse_qs, urlparse

try:
    import websockets
    from websockets.server import serve, WebSocketServerProtocol
    from websockets.exceptions import ConnectionClosed
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    WebSocketServerProtocol = None

from astrbot.api import logger


class StandaloneWebSocketServer:
    """
    独立 WebSocket 服务器
    
    使用 websockets 库在指定端口运行，不依赖 AstrBot 主应用。
    支持客户端认证、心跳检测、消息分发等功能。
    
    稳定性增强：
    - 主动健康检查：定期检测所有连接是否存活
    - 死连接清理：自动清理超时未响应的连接
    - 连接状态广播：主动通知客户端连接状态
    """
    
    # 心跳配置常量 - 与客户端保持一致
    PING_INTERVAL = 30  # 心跳间隔（秒）
    PING_TIMEOUT = 20   # 心跳超时（秒）
    
    # 健康检查配置
    HEALTH_CHECK_INTERVAL = 60  # 健康检查间隔（秒）
    CLIENT_INACTIVE_TIMEOUT = 120  # 客户端不活跃超时（秒），超过此时间无心跳则清理
    
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 6190,
        on_client_connect: Optional[Callable[[str], Any]] = None,
        on_client_disconnect: Optional[Callable[[str], Any]] = None,
        on_message: Optional[Callable[[str, dict], Any]] = None,
    ):
        """
        初始化 WebSocket 服务器
        
        Args:
            host: 监听地址，默认 0.0.0.0（所有网卡）
            port: 监听端口，默认 6190
            on_client_connect: 客户端连接回调
            on_client_disconnect: 客户端断开回调
            on_message: 消息接收回调
        """
        self.host = host
        self.port = port
        self.on_client_connect = on_client_connect
        self.on_client_disconnect = on_client_disconnect
        self.on_message = on_message
        
        # 活跃连接: session_id -> websocket
        self.connections: Dict[str, WebSocketServerProtocol] = {}
        
        # 客户端最后活跃时间: session_id -> timestamp
        self._last_activity: Dict[str, float] = {}
        
        # 客户端心跳计数: session_id -> count
        self._heartbeat_counts: Dict[str, int] = {}
        
        # 服务器状态
        self._server = None
        self._running = False
        self._server_task: Optional[asyncio.Task] = None
        self._health_check_task: Optional[asyncio.Task] = None
        
        # 统计信息
        self._total_connections: int = 0
        self._total_messages: int = 0
        self._total_disconnections: int = 0
        
    @property
    def is_running(self) -> bool:
        """服务器是否正在运行"""
        return self._running and self._server is not None
    
    def get_connected_client_ids(self) -> list:
        """获取所有已连接客户端的 session_id"""
        return list(self.connections.keys())
    
    def get_active_clients_count(self) -> int:
        """获取活跃客户端数量"""
        return len(self.connections)
    
    async def start(self):
        """启动 WebSocket 服务器"""
        if not WEBSOCKETS_AVAILABLE:
            logger.error("❌ websockets 库未安装，无法启动 WebSocket 服务器")
            logger.error("   请运行: pip install websockets>=12.0")
            return False
        
        if self._running:
            logger.warning("WebSocket 服务器已在运行中")
            return True
        
        try:
            # 创建服务器 - 心跳参数与客户端保持一致
            self._server = await serve(
                self._handle_connection,
                self.host,
                self.port,
                ping_interval=self.PING_INTERVAL,  # 心跳间隔 30 秒
                ping_timeout=self.PING_TIMEOUT,    # 心跳超时 20 秒（增加容错）
                close_timeout=10,                   # 关闭超时 10 秒
            )
            
            self._running = True
            
            # 启动健康检查任务
            self._health_check_task = asyncio.create_task(self._health_check_loop())
            
            logger.info("=" * 60)
            logger.info("✅ WebSocket 服务器启动成功！")
            logger.info(f"   监听地址: {self.host}:{self.port}")
            logger.info(f"   桌面客户端连接地址: ws://服务器IP:{self.port}")
            logger.info(f"   健康检查间隔: {self.HEALTH_CHECK_INTERVAL}s")
            logger.info(f"   客户端超时时间: {self.CLIENT_INACTIVE_TIMEOUT}s")
            logger.info("=" * 60)
            
            return True
            
        except OSError as e:
            if "address already in use" in str(e).lower() or e.errno == 10048:
                logger.error(f"❌ 端口 {self.port} 已被占用！")
                logger.error("   请检查是否有其他程序占用该端口，或修改配置使用其他端口")
            else:
                logger.error(f"❌ WebSocket 服务器启动失败: {e}")
            return False
        except Exception as e:
            logger.error(f"❌ WebSocket 服务器启动失败: {e}")
            logger.error(traceback.format_exc())
            return False
    
    async def stop(self):
        """停止 WebSocket 服务器"""
        if not self._running:
            return
        
        self._running = False
        
        # 停止健康检查任务
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
            self._health_check_task = None
        
        # 关闭所有连接（发送关闭通知）
        for session_id, ws in list(self.connections.items()):
            try:
                # 先发送关闭通知
                await self._send_json(ws, {
                    "type": "server_closing",
                    "message": "Server shutting down"
                })
                await ws.close(1001, "Server shutting down")
            except Exception:
                pass
        self.connections.clear()
        self._last_activity.clear()
        self._heartbeat_counts.clear()
        
        # 关闭服务器
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        
        logger.info("WebSocket 服务器已停止")
    
    async def _handle_connection(self, websocket: WebSocketServerProtocol):
        """
        处理 WebSocket 连接
        
        支持两种连接方式：
        1. ws://服务器IP:6190/ws/client?session_id=xxx&token=xxx (标准路径)
        2. ws://服务器IP:6190?session_id=xxx&token=xxx (根路径兼容)
        """
        # 解析 URL 路径和参数
        full_path = websocket.path if hasattr(websocket, 'path') else "/"
        
        # 分离路径和查询参数
        if "?" in full_path:
            path_part, query_string = full_path.split("?", 1)
        else:
            path_part = full_path
            query_string = ""
        
        params = parse_qs(query_string)
        
        # 验证路径（支持 /ws/client 和 / 两种路径）
        valid_paths = ["/ws/client", "/", ""]
        if path_part not in valid_paths:
            logger.warning(f"WebSocket 连接拒绝: 无效路径 '{path_part}'，支持的路径: {valid_paths}")
            await websocket.close(1008, f"Invalid path: {path_part}")
            return
        
        session_id = params.get("session_id", [None])[0]
        token = params.get("token", [None])[0]
        
        logger.info(f"收到 WebSocket 连接请求: path={path_part}, session_id={session_id}, token={'*' * 6 if token else 'None'}")
        
        # 验证参数
        if not session_id or not token:
            logger.warning("WebSocket 连接拒绝: 缺少 session_id 或 token")
            await websocket.close(1008, "Missing session_id or token")
            return
        
        # TODO: 验证 token 有效性（当前信任本地连接）
        
        # 记录连接和活跃时间
        import time
        self.connections[session_id] = websocket
        self._last_activity[session_id] = time.time()
        self._heartbeat_counts[session_id] = 0
        self._total_connections += 1
        logger.info(f"✅ 客户端已连接: session_id={session_id}，当前连接数: {len(self.connections)}")
        
        # 发送连接确认消息
        await self._send_json(websocket, {
            "type": "connection_status",
            "status": "connected",
            "session_id": session_id,
            "server_time": time.time(),
            "health_check_interval": self.HEALTH_CHECK_INTERVAL,
            "inactive_timeout": self.CLIENT_INACTIVE_TIMEOUT
        })
        
        # 触发连接回调
        if self.on_client_connect:
            try:
                result = self.on_client_connect(session_id)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"连接回调执行失败: {e}")
        
        try:
            # 消息循环
            async for message in websocket:
                try:
                    data = json.loads(message)
                    await self._handle_message(session_id, websocket, data)
                except json.JSONDecodeError:
                    logger.warning(f"收到无效 JSON 消息: {message[:100]}...")
                except Exception as e:
                    logger.error(f"处理消息失败: {e}")
                    logger.error(traceback.format_exc())
                    
        except ConnectionClosed as e:
            # 区分正常关闭和异常关闭
            if e.code == 1000:
                logger.info(f"客户端正常断开: session_id={session_id}")
            elif e.code == 1001:
                logger.info(f"客户端正在离开: session_id={session_id}")
            elif e.code == 1006:
                logger.warning(f"客户端异常断开（网络问题）: session_id={session_id}")
            else:
                logger.info(f"客户端断开连接: session_id={session_id}, code={e.code}, reason={e.reason}")
        except Exception as e:
            logger.error(f"WebSocket 连接错误: {e}")
            logger.error(traceback.format_exc())
        finally:
            # 清理连接和相关记录
            self.connections.pop(session_id, None)
            self._last_activity.pop(session_id, None)
            self._heartbeat_counts.pop(session_id, None)
            self._total_disconnections += 1
            logger.info(f"客户端已移除: session_id={session_id}，剩余连接数: {len(self.connections)}")
            
            # 触发断开回调
            if self.on_client_disconnect:
                try:
                    result = self.on_client_disconnect(session_id)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.error(f"断开回调执行失败: {e}")
    
    async def _handle_message(
        self,
        session_id: str,
        websocket: WebSocketServerProtocol,
        data: dict
    ):
        """处理客户端消息"""
        import time
        msg_type = data.get("type", "")
        
        # 更新客户端活跃时间
        self._last_activity[session_id] = time.time()
        self._total_messages += 1
        
        # 心跳消息 - 立即响应
        if msg_type == "heartbeat":
            self._heartbeat_counts[session_id] = self._heartbeat_counts.get(session_id, 0) + 1
            await self._send_json(websocket, {
                "type": "heartbeat_ack",
                "timestamp": time.time(),
                "server_time": time.time(),
                "heartbeat_count": self._heartbeat_counts[session_id]
            })
            return
        
        # 触发消息回调
        if self.on_message:
            try:
                result = self.on_message(session_id, data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"消息回调执行失败: {e}")
                logger.error(traceback.format_exc())
    
    async def _health_check_loop(self):
        """
        健康检查循环
        
        定期检查所有连接的健康状态，清理死连接
        """
        import time
        
        while self._running:
            try:
                await asyncio.sleep(self.HEALTH_CHECK_INTERVAL)
                
                if not self._running:
                    break
                
                current_time = time.time()
                dead_connections = []
                
                # 检查所有连接
                for session_id, ws in list(self.connections.items()):
                    last_activity = self._last_activity.get(session_id, 0)
                    inactive_time = current_time - last_activity
                    
                    # 检查连接是否超时
                    if inactive_time > self.CLIENT_INACTIVE_TIMEOUT:
                        logger.warning(
                            f"客户端 {session_id} 超时 ({inactive_time:.0f}s > {self.CLIENT_INACTIVE_TIMEOUT}s)，标记为死连接"
                        )
                        dead_connections.append(session_id)
                        continue
                    
                    # 检查 WebSocket 连接状态
                    try:
                        if hasattr(ws, 'open') and not ws.open:
                            logger.warning(f"客户端 {session_id} WebSocket 已关闭，标记为死连接")
                            dead_connections.append(session_id)
                            continue
                    except Exception as e:
                        logger.warning(f"检查客户端 {session_id} 状态失败: {e}")
                        dead_connections.append(session_id)
                        continue
                    
                    # 发送健康检查探测（可选，减少日志噪音）
                    # 客户端会通过心跳响应来证明存活
                
                # 清理死连接
                for session_id in dead_connections:
                    await self._cleanup_dead_connection(session_id)
                
                # 输出健康状态摘要（仅在有连接时）
                if self.connections:
                    logger.debug(
                        f"健康检查完成: 活跃连接 {len(self.connections)}，"
                        f"清理死连接 {len(dead_connections)}，"
                        f"总连接 {self._total_connections}，总断开 {self._total_disconnections}"
                    )
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"健康检查异常: {e}")
                logger.error(traceback.format_exc())
    
    async def _cleanup_dead_connection(self, session_id: str):
        """
        清理死连接
        
        Args:
            session_id: 要清理的客户端 session_id
        """
        ws = self.connections.get(session_id)
        if ws:
            try:
                # 尝试发送关闭通知
                await self._send_json(ws, {
                    "type": "connection_timeout",
                    "message": "Connection timed out due to inactivity"
                })
                await ws.close(1000, "Connection timeout")
            except Exception as e:
                logger.debug(f"关闭死连接 {session_id} 失败（可能已断开）: {e}")
        
        # 清理记录
        self.connections.pop(session_id, None)
        self._last_activity.pop(session_id, None)
        self._heartbeat_counts.pop(session_id, None)
        self._total_disconnections += 1
        
        # 触发断开回调
        if self.on_client_disconnect:
            try:
                result = self.on_client_disconnect(session_id)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"断开回调执行失败: {e}")
        
        logger.info(f"已清理死连接: session_id={session_id}")
    
    async def send_to_client(self, session_id: str, data: dict) -> bool:
        """
        发送消息给指定客户端
        
        Args:
            session_id: 客户端 session_id
            data: 要发送的数据（字典）
            
        Returns:
            是否发送成功
        """
        websocket = self.connections.get(session_id)
        if not websocket:
            logger.warning(f"发送失败: 客户端未连接 session_id={session_id}")
            return False
        
        return await self._send_json(websocket, data)
    
    async def broadcast(self, data: dict) -> int:
        """
        广播消息给所有客户端
        
        Args:
            data: 要发送的数据（字典）
            
        Returns:
            成功发送的客户端数量
        """
        success_count = 0
        for session_id, websocket in list(self.connections.items()):
            if await self._send_json(websocket, data):
                success_count += 1
            else:
                # 发送失败，移除连接
                self.connections.pop(session_id, None)
        return success_count
    
    async def _send_json(self, websocket: WebSocketServerProtocol, data: dict) -> bool:
        """发送 JSON 数据"""
        try:
            await websocket.send(json.dumps(data, ensure_ascii=False))
            return True
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            return False
    
    def is_client_connected(self, session_id: str) -> bool:
        """
        检查客户端是否已连接且活跃
        
        Args:
            session_id: 客户端 session_id
            
        Returns:
            客户端是否连接且活跃
        """
        if session_id not in self.connections:
            return False
        
        ws = self.connections[session_id]
        # 检查 WebSocket 连接状态
        if ws is None:
            return False
        
        # 检查连接是否仍然打开
        try:
            return ws.open if hasattr(ws, 'open') else True
        except Exception:
            return False
    
    def get_client_last_activity(self, session_id: str) -> float:
        """
        获取客户端最后活跃时间
        
        Args:
            session_id: 客户端 session_id
            
        Returns:
            最后活跃时间戳，如果不存在返回 0
        """
        return self._last_activity.get(session_id, 0)
    
    def get_server_stats(self) -> dict:
        """
        获取服务器统计信息
        
        Returns:
            包含连接统计的字典
        """
        import time
        current_time = time.time()
        
        # 计算每个连接的活跃时间
        connection_details = {}
        for session_id in self.connections:
            last_activity = self._last_activity.get(session_id, 0)
            heartbeat_count = self._heartbeat_counts.get(session_id, 0)
            connection_details[session_id] = {
                "inactive_seconds": current_time - last_activity if last_activity else None,
                "heartbeat_count": heartbeat_count
            }
        
        return {
            "is_running": self._running,
            "active_connections": len(self.connections),
            "total_connections": self._total_connections,
            "total_disconnections": self._total_disconnections,
            "total_messages": self._total_messages,
            "connection_details": connection_details
        }
    
    async def ping_client(self, session_id: str) -> bool:
        """
        主动 ping 指定客户端
        
        Args:
            session_id: 客户端 session_id
            
        Returns:
            是否发送成功
        """
        import time
        ws = self.connections.get(session_id)
        if not ws:
            return False
        
        try:
            await self._send_json(ws, {
                "type": "server_ping",
                "timestamp": time.time()
            })
            return True
        except Exception as e:
            logger.error(f"Ping 客户端 {session_id} 失败: {e}")
            return False