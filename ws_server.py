"""
独立的 WebSocket 服务器

由于 AstrBot 不支持插件直接注册 WebSocket 路由，
此模块提供一个独立运行的 WebSocket 服务器。
"""

import asyncio
import json
from typing import Optional, Any, Dict

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("ws_server")

# 使用 websockets 库
WEBSOCKETS_AVAILABLE = False
try:
    import websockets
    from websockets.asyncio.server import serve, ServerConnection
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    try:
        # 尝试旧版本的导入方式
        import websockets
        from websockets import serve
        WEBSOCKETS_AVAILABLE = True
    except ImportError:
        logger.warning("websockets 库未安装，WebSocket 服务器将无法启动")
        logger.warning("请运行: pip install websockets")


class WebSocketServer:
    """独立的 WebSocket 服务器"""
    
    def __init__(self, client_manager, host: str = "0.0.0.0", port: int = 6190):
        """
        初始化 WebSocket 服务器
        
        Args:
            client_manager: ClientManager 实例
            host: 监听地址
            port: 监听端口（默认 6190，与 AstrBot 的 6185 不冲突）
        """
        self.client_manager = client_manager
        self.host = host
        self.port = port
        self._server = None
        self._running = False
        
    async def start(self):
        """启动 WebSocket 服务器"""
        if not WEBSOCKETS_AVAILABLE:
            logger.error("websockets 库未安装，无法启动 WebSocket 服务器")
            return False
            
        try:
            self._server = await websockets.serve(
                self._handle_connection,
                self.host,
                self.port,
                ping_interval=30,
                ping_timeout=10,
            )
            self._running = True
            logger.info(f"✅ WebSocket 服务器已启动: ws://{self.host}:{self.port}")
            logger.info(f"   桌面客户端请连接到此地址，路径: /ws/client?session_id=xxx&token=xxx")
            return True
        except Exception as e:
            logger.error(f"WebSocket 服务器启动失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False
            
    async def stop(self):
        """停止 WebSocket 服务器"""
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("WebSocket 服务器已停止")
            
    async def _handle_connection(self, websocket):
        """
        处理 WebSocket 连接
        
        Args:
            websocket: WebSocket 连接
        """
        # 获取客户端地址（用于日志）
        client_address = "unknown"
        try:
            if hasattr(websocket, 'remote_address'):
                client_address = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"
            elif hasattr(websocket, 'transport'):
                peername = websocket.transport.get_extra_info('peername')
                if peername:
                    client_address = f"{peername[0]}:{peername[1]}"
        except Exception:
            pass
            
        logger.info(f"📡 WebSocket 新连接请求: 来自 {client_address}")
        
        # 解析查询参数
        # 从 websocket.path 或 websocket.request.path 获取路径
        session_id = None
        token = None
        
        try:
            # 获取请求路径
            if hasattr(websocket, 'path'):
                path = websocket.path
            elif hasattr(websocket, 'request') and hasattr(websocket.request, 'path'):
                path = websocket.request.path
            else:
                path = "/"
                
            logger.debug(f"WebSocket 连接请求路径: {path}")
            
            if "?" in path:
                query_string = path.split("?", 1)[1]
                params = {}
                for p in query_string.split("&"):
                    if "=" in p:
                        key, value = p.split("=", 1)
                        params[key] = value
                session_id = params.get("session_id")
                token = params.get("token")
        except Exception as e:
            logger.warning(f"解析 WebSocket 查询参数失败: {e}")
            
        if not session_id:
            logger.warning(f"WebSocket 连接拒绝: 缺少 session_id")
            await websocket.close(1008, "Missing session_id")
            return
            
        # 注册连接
        await self._register_connection(websocket, session_id)
        
        try:
            async for message in websocket:
                await self._handle_message(websocket, session_id, message)
        except websockets.exceptions.ConnectionClosed as e:
            logger.info(f"客户端断开连接: session_id={session_id}, code={e.code}")
        except Exception as e:
            logger.error(f"WebSocket 连接错误: {e}")
        finally:
            self._unregister_connection(session_id)
            
    async def _register_connection(self, websocket, session_id: str):
        """注册客户端连接"""
        # 获取客户端地址
        client_address = "unknown"
        try:
            if hasattr(websocket, 'remote_address'):
                client_address = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"
        except Exception:
            pass
            
        self.client_manager.active_connections[session_id] = websocket
        logger.info(f"✅ 客户端已连接:")
        logger.info(f"   - 来源地址: {client_address}")
        logger.info(f"   - Session ID: {session_id[:20]}...")
        logger.info(f"   - 当前连接数: {len(self.client_manager.active_connections)}")
        
        # 发送欢迎消息
        try:
            await websocket.send(json.dumps({
                "type": "connected",
                "message": "已连接到桌面助手服务器",
                "session_id": session_id
            }))
        except Exception as e:
            logger.warning(f"发送欢迎消息失败: {e}")
            
    def _unregister_connection(self, session_id: str):
        """注销客户端连接"""
        if session_id in self.client_manager.active_connections:
            del self.client_manager.active_connections[session_id]
            logger.info(f"❌ 客户端已断开: session_id={session_id[:20]}...")
            logger.info(f"   - 剩余连接数: {len(self.client_manager.active_connections)}")
            
    async def _handle_message(self, websocket, session_id: str, message: str):
        """
        处理收到的消息
        
        Args:
            websocket: WebSocket 连接
            session_id: 客户端会话 ID
            message: 收到的消息
        """
        try:
            data = json.loads(message)
            msg_type = data.get("type")
            
            if msg_type == "heartbeat":
                await websocket.send(json.dumps({"type": "heartbeat_ack"}))
                
            elif msg_type == "desktop_state":
                # 处理客户端桌面状态上报
                state_data = data.get("data", {})
                state = self.client_manager.update_client_state(session_id, state_data)
                
                # 触发回调
                if self.client_manager.on_desktop_state_update:
                    try:
                        result = self.client_manager.on_desktop_state_update(state)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as e:
                        logger.error(f"桌面状态回调执行失败: {e}")
                
                # 确认收到
                await websocket.send(json.dumps({
                    "type": "desktop_state_ack",
                    "timestamp": state.timestamp,
                }))
                
            elif msg_type == "screenshot_response":
                # 处理客户端截图响应
                response_data = data.get("data", {})
                self.client_manager.handle_screenshot_response(session_id, response_data)
                logger.debug(f"收到截图响应: session_id={session_id[:20]}...")
                
            elif msg_type == "command_result":
                # 处理通用命令执行结果
                command = data.get("command")
                if command == "screenshot":
                    response_data = data.get("data", {})
                    self.client_manager.handle_screenshot_response(session_id, response_data)
                    
            else:
                logger.debug(f"收到未知消息类型: {msg_type}")
                
        except json.JSONDecodeError as e:
            logger.warning(f"JSON 解析失败: {e}")
        except Exception as e:
            logger.error(f"处理消息失败: {e}")


def patch_client_manager_for_websockets(client_manager):
    """
    为 ClientManager 添加 websockets 库支持
    
    原来的 ClientManager 是为 Starlette WebSocket 设计的，
    这里添加对 websockets 库的支持。
    """
    
    async def patched_send_message(session_id: str, message: dict):
        """发送消息到指定客户端"""
        if session_id not in client_manager.active_connections:
            logger.warning(f"发送消息失败: 客户端未连接 session_id={session_id}")
            return
            
        websocket = client_manager.active_connections[session_id]
        try:
            # 检查是 websockets 库的连接还是 Starlette 的连接
            if hasattr(websocket, 'send') and not hasattr(websocket, 'send_json'):
                # websockets 库
                await websocket.send(json.dumps(message))
            elif hasattr(websocket, 'send_json'):
                # Starlette WebSocket
                await websocket.send_json(message)
            else:
                # 尝试通用发送
                await websocket.send(json.dumps(message))
        except Exception as e:
            logger.error(f"发送消息异常: {e}")
            # 移除失效连接
            if session_id in client_manager.active_connections:
                del client_manager.active_connections[session_id]
    
    client_manager.send_message = patched_send_message
    return client_manager