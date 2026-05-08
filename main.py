"""
桌面悬浮球助手 - AstrBot 平台适配器插件 (服务端)

提供桌面感知和主动对话功能的服务端适配器。
支持通过 QQ (NapCat/OneBot11) 远程控制桌面端截图。

架构说明：
- 使用独立端口模式 (端口 6190) 运行 WebSocket 服务器
- 不依赖 AstrBot 主应用，避免框架兼容性问题
- 桌面客户端连接地址: ws://服务器IP:6190?session_id=xxx&token=xxx
"""

import asyncio
import hashlib
import time
import traceback
import uuid
from typing import Optional

import jwt
from astrbot import logger
from astrbot.api import star, llm_tool
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import PermissionType
from astrbot.api.message_components import Image, Plain
from astrbot.core.star.register import register_command
from astrbot.core.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
)
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.platform.register import (
    register_platform_adapter,
    platform_registry,
    platform_cls_map,
)

from .services.desktop_monitor import DesktopMonitorService, DesktopState
from .services.proactive_dialog import (
    ProactiveDialogService,
    ProactiveDialogConfig,
    TriggerEvent,
    TriggerType,
)
from .services.vision_analyzer import VisionAnalyzer, VisionAnalysisResult
from .ws_handler import ClientManager, MessageHandler, ClientDesktopState, ScreenshotResponse
from .ws_server import StandaloneWebSocketServer

# ============================================================================
# 全局实例
# ============================================================================

# 全局 WebSocket 客户端管理器
client_manager = ClientManager()

# 全局消息处理器
message_handler = MessageHandler(client_manager)

# 全局 WebSocket 服务器实例
ws_server: Optional[StandaloneWebSocketServer] = None

# WebSocket 服务器默认配置
WS_DEFAULT_HOST = "0.0.0.0"
WS_DEFAULT_PORT = 6190


def _message_chain_to_text(message) -> str:
    """将消息链转换为纯文本，用于客户端显示
    
    兼容多种输入类型：
    - str: 直接返回
    - bytes/bytearray: 解码为 UTF-8
    - MessageChain: 遍历 chain 提取文本
    - 带有 text/content/message 属性的对象
    - 其他类型: 尝试转换为字符串
    """
    if message is None:
        return ""
    
    # 1) 直接字符串
    if isinstance(message, str):
        return message.strip()
    
    # 2) bytes/bytearray
    if isinstance(message, (bytes, bytearray)):
        try:
            return message.decode("utf-8", errors="ignore").strip()
        except Exception:
            return ""
    
    # 3) MessageChain 兼容（有 chain 属性）
    chain = getattr(message, "chain", None)
    if chain:
        parts = []
        for comp in chain:
            if isinstance(comp, Plain):
                parts.append(comp.text)
            elif isinstance(comp, Image):
                parts.append("[图片]")
            elif hasattr(comp, "text") and comp.text:
                parts.append(str(comp.text))
            elif hasattr(comp, "type"):
                parts.append(f"[{comp.type}]")
        result = "".join(parts).strip()
        if result:
            return result
    
    # 4) 常见字段名（适配各种消息格式）
    for key in ("text", "content", "message", "plain_text"):
        val = getattr(message, key, None)
        if val is None and isinstance(message, dict):
            val = message.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    
    # 5) 尝试调用 get_plain_text 方法（AstrBot MessageChain 的方法）
    if hasattr(message, "get_plain_text"):
        try:
            result = message.get_plain_text()
            if isinstance(result, str) and result:
                return result.strip()
        except Exception:
            pass
    
    # 6) 最后尝试转换为字符串（限制长度避免巨大对象）
    try:
        result = str(message)
        # 避免返回类似 "<MessageChain object at 0x...>" 的无用字符串
        # 也限制长度避免意外巨大对象
        if result and len(result) < 100000 and not result.startswith("<") and not result.endswith(">"):
            return result.strip()
    except Exception:
        pass
    
    return ""


# ============================================================================
# 插件主类
# ============================================================================

class Main(star.Star):
    """
    桌面悬浮球助手插件主类
    
    提供：
    1. 平台适配器模式：桌面监控和主动对话
    2. 命令模式：支持通过 /screenshot 命令远程截图
    3. 独立端口模式：在端口 6190 运行 WebSocket 服务器
    4. LLM 视觉分析：支持 LLM 主动调用截图并分析内容
    """
    
    def __init__(self, context: star.Context, config: dict) -> None:
        super().__init__(context)
        global ws_server
        
        self.context = context
        self.config = config
        self._jwt_secret = None
        try:
            dashboard_config = self.context.get_config().get("dashboard", {})
            self._jwt_secret = dashboard_config.get("jwt_secret")
        except Exception as e:
            logger.error(f"读取 Dashboard JWT 配置失败: {e}")
        
        # 从配置中读取 WebSocket 服务器设置（如果有的话）
        ws_host = config.get("ws_host", WS_DEFAULT_HOST)
        ws_port = config.get("ws_port", WS_DEFAULT_PORT)
        try:
            ws_port = int(ws_port)
        except (TypeError, ValueError):
            logger.warning(f"无效的 ws_port 配置: {ws_port}，将使用默认端口 {WS_DEFAULT_PORT}")
            ws_port = WS_DEFAULT_PORT
        
        # 创建 WebSocket 服务器
        ws_server = StandaloneWebSocketServer(
            host=ws_host,
            port=ws_port,
            on_client_connect=message_handler.on_client_connect,
            on_client_disconnect=message_handler.on_client_disconnect,
            on_message=message_handler.handle_message,
            token_validator=self._validate_ws_token,
        )
        
        # 将服务器引用设置到客户端管理器
        client_manager.set_ws_server(ws_server)
        
        # 设置配置同步回调
        message_handler.on_config_sync = self._handle_config_sync
        # 设置聊天消息回调
        message_handler.on_chat_message = self._handle_chat_message

        # 同步截图保留配置
        self._configure_screenshot_retention(config)
        
        # 从配置读取识图模式设置（直接从 config 读取，符合 _conf_schema.json 规范）
        vision_mode = config.get("vision_mode", "auto")
        dedicated_provider_id = config.get("dedicated_provider_id", "")
        
        # 初始化视觉分析器
        self.vision_analyzer = VisionAnalyzer(
            context=context,
            vision_mode=vision_mode,
            dedicated_provider_id=dedicated_provider_id or None,
        )
        
        logger.info("桌面悬浮球助手插件已加载（独立端口模式）")
        
        # 手动创建并注册平台适配器实例
        # 因为 PlatformManager.initialize() 在插件加载之前执行，
        # 所以需要在这里手动创建适配器实例并添加到 platform_insts
        try:
            platform_config = {
                "type": "desktop_assistant",
                "enable": True,
                "id": "desktop_assistant",
                "ws_host": config.get("ws_host", WS_DEFAULT_HOST),
                "ws_port": config.get("ws_port", WS_DEFAULT_PORT),
            }
            # 创建适配器实例
            self._adapter = DesktopAssistantAdapter(
                platform_config=platform_config,
                event_queue=self.context.platform_manager.event_queue,
            )
            # 添加到平台实例列表
            self.context.platform_manager.platform_insts.append(self._adapter)
            logger.info("desktop_assistant 平台适配器已手动注册到 platform_insts")
        except Exception as e:
            logger.error(f"手动注册 desktop_assistant 平台适配器失败: {e}")
        
        # 启动 WebSocket 服务器（在后台任务中启动）
        asyncio.create_task(self._start_ws_server())
        
        # 启动过期请求清理任务
        asyncio.create_task(client_manager.start_cleanup_task())

    def _configure_screenshot_retention(self, config: dict):
        """同步截图文件保留策略"""
        max_screenshots = config.get("max_screenshots")
        screenshot_max_age_hours = config.get("screenshot_max_age_hours")
        client_manager.configure_screenshot_retention(
            max_screenshots=max_screenshots,
            max_age_hours=screenshot_max_age_hours,
        )

    async def _handle_chat_message(self, session_id: str, data: dict):
        """处理客户端聊天消息"""
        content = str(data.get("content", "")).strip()
        image_base64 = data.get("image_base64")
        image_path = None
        if image_base64:
            image_path = client_manager.save_base64_image(image_base64, "chat_image")
        if not content and not image_path:
            return
        logger.info(
            f"收到客户端聊天消息: session_id={session_id}, content_len={len(content)}"
        )

        adapter = None
        for platform in self.context.platform_manager.platform_insts:
            try:
                meta = platform.meta()
                if meta.name == "desktop_assistant":
                    adapter = platform
                    break
            except Exception:
                continue

        if not adapter:
            logger.warning("未找到 desktop_assistant 平台适配器，无法处理聊天消息")
            return

        sender_id = data.get("sender_id") or "desktop_user"
        sender_name = data.get("sender_name") or "桌面用户"
        selected_provider = data.get("selected_provider")
        selected_model = data.get("selected_model")

        try:
            adapter.handle_user_message(
                session_id=session_id,
                text=content,
                sender_id=sender_id,
                sender_name=sender_name,
                selected_provider=selected_provider,
                selected_model=selected_model,
                image_path=image_path,
            )
        except Exception as e:
            logger.error(f"处理客户端聊天消息失败: {e}")
    
    async def terminate(self):
        """插件终止时的清理操作"""
        global ws_server
        
        logger.info("正在清理桌面悬浮球助手插件...")
        
        # 停止过期请求清理任务
        try:
            await client_manager.stop_cleanup_task()
        except Exception as e:
            logger.error(f"停止清理任务失败: {e}")
        
        # 停止 WebSocket 服务器
        if ws_server:
            try:
                await ws_server.stop()
                ws_server = None
            except Exception as e:
                logger.error(f"停止 WebSocket 服务器失败: {e}")
        
        # 从全局注册表中移除平台适配器，避免重载时的冲突
        adapter_name = "desktop_assistant"
        
        # 从 platform_cls_map 中移除
        if adapter_name in platform_cls_map:
            del platform_cls_map[adapter_name]
            logger.debug(f"已从 platform_cls_map 中移除适配器: {adapter_name}")
        
        # 从 platform_registry 中移除
        for pm in platform_registry[:]:  # 使用切片复制列表，避免迭代时修改
            if pm.name == adapter_name:
                platform_registry.remove(pm)
                logger.debug(f"已从 platform_registry 中移除适配器: {adapter_name}")
                break
        
        logger.info("桌面悬浮球助手插件清理完成")
    
    async def _start_ws_server(self):
        """启动 WebSocket 服务器"""
        global ws_server
        
        if ws_server:
            success = await ws_server.start()
            if not success:
                logger.error("WebSocket 服务器启动失败，远程截图功能将不可用")
    
    async def _handle_config_sync(self, session_id: str, config_data: dict):
        """
        处理客户端配置同步
        
        将客户端发送的配置应用到 AstrBot 核心配置。
        
        Args:
            session_id: 客户端会话 ID
            config_data: 客户端配置数据
        """
        try:
            # 处理语音相关配置
            voice_config = config_data.get("voice", {})
            
            if voice_config:
                # 获取 AstrBot 核心配置
                astrbot_config = self.context.get_config()
                
                # 同步 TTS dual_output 设置
                if "dual_output" in voice_config:
                    dual_output = voice_config["dual_output"]
                    
                    # 更新 AstrBot 核心的 provider_tts_settings
                    if "provider_tts_settings" in astrbot_config:
                        old_value = astrbot_config["provider_tts_settings"].get("dual_output", False)
                        astrbot_config["provider_tts_settings"]["dual_output"] = dual_output
                        
                        logger.info(
                            f"TTS dual_output 配置已同步: {old_value} -> {dual_output} "
                            f"(来自客户端 {session_id[:16]}...)"
                        )
                    else:
                        logger.warning("AstrBot 配置中未找到 provider_tts_settings")
                
                # 可以扩展其他配置项的同步
                # if "enable_tts" in voice_config:
                #     ...
                
        except Exception as e:
            logger.error(f"处理配置同步失败: {e}")
            import traceback
            traceback.print_exc()

    async def _validate_ws_token(self, token: str) -> bool:
        """验证 WebSocket 连接的 token"""
        if not token:
            return False
        token = token.removeprefix("Bearer ").strip()
        if not token:
            return False

        if token.startswith("abk_"):
            return await self._validate_openapi_key(token)

        if not self._jwt_secret:
            logger.warning("JWT secret 未配置，跳过 WebSocket token 校验")
            return True
        try:
            jwt.decode(token, self._jwt_secret, algorithms=["HS256"])
            return True
        except jwt.ExpiredSignatureError:
            logger.warning("WebSocket token 已过期")
            return False
        except jwt.InvalidTokenError:
            logger.warning("WebSocket token 无效")
            return False
        except Exception as e:
            logger.error(f"WebSocket token 校验异常: {e}")
            return False

    async def _validate_openapi_key(self, raw_key: str) -> bool:
        """验证 AstrBot OpenAPI API Key，允许 OpenAPI 模式复用远控 WebSocket。"""
        try:
            db = self.context.get_db()
            key_hash = hashlib.pbkdf2_hmac(
                "sha256",
                raw_key.encode("utf-8"),
                b"astrbot_api_key",
                100_000,
            ).hex()
            api_key = await db.get_active_api_key_by_hash(key_hash)
            if not api_key:
                logger.warning("WebSocket OpenAPI API Key 无效或已过期")
                return False

            scopes = api_key.scopes if isinstance(api_key.scopes, list) else ["chat", "config", "file", "im"]
            if "*" not in scopes and "chat" not in scopes:
                logger.warning("WebSocket OpenAPI API Key 缺少 chat scope")
                return False

            await db.touch_api_key(api_key.key_id)
            return True
        except Exception as e:
            logger.error(f"WebSocket OpenAPI API Key 校验异常: {e}")
            return False
    
    # ========================================================================
    # 命令处理器：远程截图
    # ========================================================================
    
    @register_command("screenshot", alias={"截图", "jietu"})
    @filter.permission_type(PermissionType.ADMIN)
    async def screenshot_command(self, event: AstrMessageEvent):
        """远程截图：通过 QQ 发送此命令让桌面端执行截图并返回图片（仅管理员可用）"""
        logger.info("📸 收到截图命令，正在处理...")
        
        try:
            # 1. 检查 WebSocket 服务器状态
            if not ws_server or not ws_server.is_running:
                yield event.plain_result(
                    "❌ WebSocket 服务器未运行。\n\n"
                    "请检查服务器日志获取详细错误信息。\n"
                    "可能是端口 6190 被占用。"
                )
                return
            
            # 2. 检查是否有客户端连接
            client_count = client_manager.get_active_clients_count()
            logger.info(f"WebSocket 服务状态: 正常, 当前连接数: {client_count}")

            if client_count == 0:
                # 没有客户端连接，提供详细的诊断建议
                yield event.plain_result(
                    "❌ 没有已连接的桌面客户端。\n\n"
                    "请执行以下检查：\n"
                    "1. 桌面客户端程序是否已打开？\n"
                    "2. 桌面客户端左上角是否显示'已连接'？\n\n"
                    "调试信息：\n"
                    f"• 连接模式: 独立端口 (6190)\n"
                    f"• 服务状态: 正常运行\n"
                    f"• 当前连接数: 0"
                )
                return
            
            # 3. 执行截图
            async for result in self._do_remote_screenshot(event, None, silent=True):
                yield result

        except Exception as e:
            logger.error(f"截图命令执行异常: {e}")
            traceback.print_exc()
            yield event.plain_result(f"❌ 截图命令执行异常: {str(e)}")
    
    @llm_tool("view_desktop_screen")
    async def view_desktop_screen_tool(self, event: AstrMessageEvent):
        """
        获取用户电脑桌面的截图并直接发送给用户。
        
        当用户明确要求"发送截图"、"截个图给我看看"时使用此函数。
        此函数会将截图直接发送给用户，而不会返回内容描述。
        
        注意：如果你需要"看"屏幕内容来帮助用户，请使用 analyze_desktop_screen 工具。
        
        使用场景举例：
        - 用户说"截个图发给我"
        - 用户说"把屏幕截图发过来"
        - 用户需要保存当前屏幕状态
        
        返回：桌面截图图片（直接发送给用户）
        
        权限要求：仅管理员可用
        """
        # 检查管理员权限
        if not event.is_admin():
            yield event.plain_result("❌ 权限不足：截图功能仅限管理员使用，以保护用户隐私。")
            return
        
        async for result in self._do_remote_screenshot(event, None, silent=False):
            yield result
    
    @llm_tool("analyze_desktop_screen")
    async def analyze_desktop_screen_tool(self, event: AstrMessageEvent) -> str:
        """
        分析用户当前电脑桌面屏幕内容，返回屏幕上显示内容的描述。
        
        当你需要了解用户正在做什么、理解屏幕上的内容时，调用此函数。
        此函数会获取桌面截图并分析其内容，返回文字描述供你参考。
        
        注意：此函数不会向用户发送截图，只会返回内容描述。
        如果用户明确要求"发送截图"，请使用 view_desktop_screen 工具。
        
        使用场景举例：
        - 用户问"我在干什么"或"我桌面上是什么"
        - 用户说"帮我看看这个怎么操作"
        - 用户说"你能看到我的屏幕吗"
        - 需要根据用户当前操作提供上下文相关的帮助
        
        返回：屏幕内容的文字描述
        
        权限要求：仅管理员可用
        """
        # 检查管理员权限
        if not event.is_admin():
            return "❌ 权限不足：截图功能仅限管理员使用，以保护用户隐私。"
        
        logger.info("🔍 收到桌面分析请求，正在获取截图...")
        
        try:
            # 1. 检查 WebSocket 服务器状态
            if not ws_server or not ws_server.is_running:
                return "❌ 无法分析桌面：WebSocket 服务器未运行。请检查服务器日志获取详细错误信息。"
            
            # 2. 检查客户端连接
            connected_clients = client_manager.get_connected_client_ids()
            if not connected_clients:
                return "❌ 无法分析桌面：没有已连接的桌面客户端。请确保桌面端程序已启动并连接到服务器。"
            
            # 3. 获取截图
            response: ScreenshotResponse = await client_manager.request_screenshot(
                session_id=None,
                timeout=30.0
            )
            
            if not response.success or not response.image_path:
                error_msg = response.error_message or "未知错误"
                return f"❌ 无法获取截图: {error_msg}"
            
            logger.info(f"📸 截图已获取: {response.image_path}")
            
            # 4. 使用多模态 LLM 分析截图
            umo = event.unified_msg_origin
            analysis_result: VisionAnalysisResult = await self.vision_analyzer.analyze_desktop_screenshot(
                image_path=response.image_path,
                umo=umo,
            )
            
            if analysis_result.success:
                logger.info("✅ 桌面分析完成")
                return analysis_result.description
            else:
                return f"❌ 分析失败: {analysis_result.error_message}"
                
        except Exception as e:
            logger.error(f"桌面分析异常: {e}")
            traceback.print_exc()
            return f"❌ 分析过程出错: {str(e)}"
    
    async def _do_remote_screenshot(
        self,
        event: AstrMessageEvent,
        target_session_id: Optional[str] = None,
        silent: bool = False
    ):
        """
        执行远程截图
        
        Args:
            event: 消息事件
            target_session_id: 目标客户端 session_id
            silent: 静默模式，只返回图片不返回额外信息
        """
        # 检查是否有已连接的客户端
        connected_clients = client_manager.get_connected_client_ids()
        
        logger.info(f"📊 当前连接状态: 已连接客户端数量 = {len(connected_clients)}")
        if connected_clients:
            logger.info(f"   客户端列表: {[c[:20] + '...' for c in connected_clients]}")
        else:
            logger.warning("   ⚠️ 没有任何客户端连接！")
        
        if not connected_clients:
            # 提供更详细的诊断信息
            ws_status = "✅ 正常" if (ws_server and ws_server.is_running) else "❌ 异常"
            
            logger.warning("截图请求失败：没有已连接的桌面客户端")
            
            yield event.plain_result(
                f"❌ 没有已连接的桌面客户端，无法执行截图。\n\n"
                f"📊 诊断信息：\n"
                f"• WebSocket 服务状态: {ws_status}\n"
                f"• 端口模式: 独立端口 (6190)\n"
                f"• 已连接客户端: 0\n\n"
                f"📝 排查步骤：\n"
                f"1. 确认桌面客户端程序已启动\n"
                f"2. 检查桌面客户端是否配置了正确的服务器地址\n"
                f"3. 尝试重启桌面客户端\n\n"
                f"💡 使用 `.桌面状态` 命令可查看更详细的连接信息"
            )
            return
        
        try:
            # 请求截图
            response: ScreenshotResponse = await client_manager.request_screenshot(
                session_id=target_session_id,
                timeout=30.0
            )
            
            if response.success and response.image_path:
                # 截图成功，发送图片
                yield event.image_result(response.image_path)
                # 静默模式下不发送额外信息
                if not silent:
                    yield event.plain_result(
                        f"✅ 截图成功！\n"
                        f"• 分辨率: {response.width}x{response.height}\n"
                        f"• 客户端: {response.session_id[:16]}..."
                    )
            else:
                # 截图失败
                error_msg = response.error_message or "未知错误"
                yield event.plain_result(f"❌ 截图失败: {error_msg}")
                
        except Exception as e:
            logger.error(f"远程截图异常: {e}")
            logger.error(traceback.format_exc())
            yield event.plain_result(f"❌ 截图请求异常: {str(e)}")
    
    @register_command("desktop_status", alias={"桌面状态", "zhuomian"})
    async def desktop_status_command(self, event: AstrMessageEvent):
        """查看当前连接的桌面客户端状态"""
        connected_clients = client_manager.get_connected_client_ids()
        
        # 构建 WebSocket 服务器状态
        if ws_server and ws_server.is_running:
            ws_status = f"✅ 正常 (端口 {ws_server.port})"
        else:
            ws_status = "❌ 未运行"
        
        if not connected_clients:
            yield event.plain_result(
                f"📊 桌面客户端状态\n\n"
                f"🌐 WebSocket 服务: {ws_status}\n\n"
                f"❌ 当前没有已连接的客户端。\n\n"
                f"请确保桌面端程序已启动并配置正确的服务器地址。"
            )
            return
        
        # 构建状态信息
        status_lines = ["📊 桌面客户端状态\n"]
        status_lines.append(f"🌐 WebSocket 服务: {ws_status}")
        status_lines.append(f"✅ 已连接客户端数量: {len(connected_clients)}\n")
        
        for i, session_id in enumerate(connected_clients, 1):
            state = client_manager.get_client_state(session_id)
            status_lines.append(f"\n【客户端 {i}】")
            status_lines.append(f"• Session: {session_id[:20]}...")
            
            if state:
                status_lines.append(f"• 活动窗口: {state.active_window_title or '未知'}")
                status_lines.append(f"• 进程: {state.active_window_process or '未知'}")
                if state.received_at:
                    status_lines.append(f"• 最后更新: {state.received_at.strftime('%H:%M:%S')}")
        
        yield event.plain_result("\n".join(status_lines))


# ============================================================================
# 消息事件类
# ============================================================================

class DesktopMessageEvent(AstrMessageEvent):
    """桌面助手消息事件"""
    
    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        is_proactive: bool = False
    ):
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.is_proactive = is_proactive  # 是否为主动对话触发的消息
        
    async def send(self, message: MessageChain):
        """发送消息"""
        # 通过 WebSocket 发送消息到客户端
        try:
            msg_data = {
                "type": "message",
                "content": str(message),  # 暂时转换为字符串，后续优化为结构化数据
                "session_id": self.session_id
            }
            # 尝试直接发送给对应的 session
            await client_manager.send_message(self.session_id, msg_data)
        except Exception as e:
            logger.error(f"WebSocket 发送消息失败: {e}")
            
        await super().send(message)


# ============================================================================
# 平台适配器
# ============================================================================

@register_platform_adapter(
    adapter_name="desktop_assistant",
    desc="桌面悬浮球助手 (服务端) - 提供桌面感知和主动对话功能",
    default_config_tmpl={
        "type": "desktop_assistant",
        "enable": True,
        "id": "desktop_assistant",
        # WebSocket 配置
        "ws_host": "0.0.0.0",
        "ws_port": 6190,
        # 桌面监控配置
        "enable_desktop_monitor": True,
        "monitor_interval": 60,
        "max_screenshots": 20,
        "screenshot_max_age_hours": 24,
        # 主动对话配置
        "enable_proactive_dialog": True,
        "proactive_min_interval": 300,
        "proactive_max_interval": 900,
        "proactive_probability": 0.3,
        "window_change_enabled": True,
        "window_change_probability": 0.2,
        "scheduled_greetings_enabled": True,
    },
    adapter_display_name="桌面悬浮球助手",
    support_streaming_message=True
)
class DesktopAssistantAdapter(Platform):
    """桌面悬浮球助手平台适配器"""
    
    def __init__(
        self,
        platform_config: dict,
        platform_settings: Optional[dict] = None,
        event_queue: Optional[asyncio.Queue] = None,
    ):
        if event_queue is None and isinstance(platform_settings, asyncio.Queue):
            event_queue = platform_settings
            platform_settings = {}
        if event_queue is None:
            raise TypeError("event_queue is required")

        super().__init__(platform_config, event_queue)
        self.config = platform_config
        self.settings = platform_settings or {}
        
        self._running = False
        self._pending_replies: dict[str, float] = {}
        self._pending_reply_ttl = 120.0
        
        # 平台元数据 - ID 必须固定，确保 Context.send_message() 路由正确
        self.metadata = PlatformMetadata(
            name="desktop_assistant",
            description="桌面悬浮球助手",
            id="desktop_assistant",  # 强制固定，不允许配置覆盖
        )
        
        # 会话 ID
        self.session_id = f"desktop_assistant!user!{uuid.uuid4().hex[:8]}"
        
        # 桌面监控和主动对话服务
        self.desktop_monitor: Optional[DesktopMonitorService] = None
        self.proactive_dialog: Optional[ProactiveDialogService] = None
        
        logger.info("桌面悬浮球助手适配器已初始化")
        
    def meta(self) -> PlatformMetadata:
        """返回平台元数据"""
        return self.metadata
        
    async def send_by_session(
        self,
        session: MessageSesion,
        message_chain: MessageChain,
    ):
        """通过会话发送消息"""
        # 调试日志 - 验证分段消息路由
        logger.debug(f"[send_by_session] platform_name={session.platform_name}, session_id={session.session_id}, content={str(message_chain)[:50]}...")
        
        # 通过 WebSocket 发送消息到客户端
        try:
            msg_data = {
                "type": "message",
                "content": str(message_chain),
                "session_id": session.session_id
            }
            await client_manager.send_message(session.session_id, msg_data)
        except Exception as e:
            logger.error(f"WebSocket 发送消息失败: {e}")
            
        await super().send_by_session(session, message_chain)
                
    def run(self):
        """返回适配器运行协程"""
        return self._run()
        
    async def _run(self):
        """适配器主运行协程"""
        logger.info("桌面悬浮球助手适配器启动中...")
        
        try:
            self._running = True
            self.status = self.status.__class__.RUNNING
            
            # 启动桌面监控和主动对话服务
            await self._start_monitor_services()
            
            # 保持运行，等待客户端连接或其他事件
            while self._running:
                await asyncio.sleep(1)
            
        except Exception as e:
            logger.error(f"桌面悬浮球助手运行错误: {e}")
            logger.error(traceback.format_exc())
            
    async def _start_monitor_services(self):
        """启动桌面监控和主动对话服务"""
        client_manager.configure_screenshot_retention(
            max_screenshots=self.config.get("max_screenshots"),
            max_age_hours=self.config.get("screenshot_max_age_hours"),
        )
        # 桌面监控服务（接收客户端上报的数据）
        if self.config.get("enable_desktop_monitor", True):
            self.desktop_monitor = DesktopMonitorService(
                proactive_min_interval=self.config.get("proactive_min_interval", 300),
                proactive_max_interval=self.config.get("proactive_max_interval", 900),
                on_state_change=self._on_desktop_state_change,
            )
            
            # 设置 WebSocket 客户端管理器的桌面状态回调
            client_manager.on_desktop_state_update = self._on_client_desktop_state
            
            await self.desktop_monitor.start()
            logger.info("桌面监控服务已启动（等待客户端连接）")
            
            # 主动对话服务
            if self.config.get("enable_proactive_dialog", True):
                proactive_config = ProactiveDialogConfig(
                    random_enabled=True,
                    random_probability=self.config.get("proactive_probability", 0.3),
                    random_min_interval=self.config.get("proactive_min_interval", 300),
                    random_max_interval=self.config.get("proactive_max_interval", 900),
                    window_change_enabled=self.config.get("window_change_enabled", True),
                    window_change_probability=self.config.get("window_change_probability", 0.2),
                    scheduled_enabled=self.config.get("scheduled_greetings_enabled", True),
                )
                
                self.proactive_dialog = ProactiveDialogService(
                    desktop_monitor=self.desktop_monitor,
                    config=proactive_config,
                    on_trigger=self._on_proactive_trigger,
                )
                await self.proactive_dialog.start()
                logger.info("主动对话服务已启动")
                
    async def _on_client_desktop_state(self, client_state: ClientDesktopState):
        """处理客户端上报的桌面状态"""
        if self.desktop_monitor:
            await self.desktop_monitor.handle_client_state(client_state)
    
    async def _on_desktop_state_change(self, state: DesktopState):
        """桌面状态变化回调"""
        logger.debug(f"桌面状态更新: session={state.session_id}, window={state.window_title}")
        
    async def _on_proactive_trigger(self, event: TriggerEvent):
        """主动对话触发回调"""
        logger.info(f"主动对话触发: type={event.trigger_type.value}")
        
        try:
            # 构建主动对话消息
            message_parts = []
            message_str = ""
            
            # 根据触发类型构建不同的提示
            if event.trigger_type == TriggerType.SCHEDULED:
                hint = event.context.get("message_hint", "")
                if hint:
                    message_str = hint
                    message_parts.append(Plain(f"[系统提示] {hint}"))
            elif event.trigger_type == TriggerType.WINDOW_CHANGE:
                current_window = event.context.get("current_window", "未知窗口")
                message_str = f"我看到你切换到了 {current_window}，有什么可以帮助你的吗？"
                message_parts.append(Plain(f"[桌面感知] 检测到窗口切换: {current_window}"))
            elif event.trigger_type == TriggerType.RANDOM:
                message_str = "我在这里陪着你呢，有什么需要帮助的吗？"
                message_parts.append(Plain("[主动问候] 随机触发"))
            elif event.trigger_type == TriggerType.IDLE:
                idle_duration = event.context.get("idle_duration", 0)
                message_str = f"你已经休息了 {int(idle_duration / 60)} 分钟了，需要我帮你做点什么吗？"
                message_parts.append(Plain(f"[空闲检测] 空闲 {int(idle_duration / 60)} 分钟"))
            
            # 添加截图（如果有）
            if event.has_screenshot and event.desktop_state and event.desktop_state.screenshot_path:
                message_parts.append(Image.fromFileSystem(event.desktop_state.screenshot_path))
                if not message_str:
                    message_str = "[桌面截图]"
                    
            if not message_parts:
                return
                
            # 构建 AstrBotMessage
            abm = AstrBotMessage()
            abm.self_id = "desktop_assistant"
            abm.sender = MessageMember("proactive_system", "主动对话系统")
            abm.type = MessageType.FRIEND_MESSAGE
            abm.session_id = self.session_id
            abm.message_id = str(uuid.uuid4())
            abm.timestamp = int(time.time())
            abm.message = message_parts
            abm.message_str = message_str
            abm.raw_message = event
            
            # 创建消息事件并提交（标记为主动对话）
            msg_event = DesktopMessageEvent(
                message_str=message_str,
                message_obj=abm,
                platform_meta=self.metadata,
                session_id=self.session_id,
                is_proactive=True
            )
            
            self.commit_event(msg_event)
            logger.info(f"已提交主动对话事件: {message_str[:50]}...")
            
        except Exception as e:
            logger.error(f"处理主动对话触发失败: {e}")
            logger.error(traceback.format_exc())

    def handle_user_message(
        self,
        session_id: str,
        text: str,
        sender_id: str = "desktop_user",
        sender_name: str = "桌面用户",
        selected_provider: Optional[str] = None,
        selected_model: Optional[str] = None,
        image_path: Optional[str] = None,
    ):
        """处理客户端输入的文本消息"""
        if not text and not image_path:
            return

        self._pending_replies[session_id] = time.time()
        message_parts = []
        if text:
            message_parts.append(Plain(text))
        if image_path:
            message_parts.append(Image.fromFileSystem(image_path))

        abm = AstrBotMessage()
        abm.self_id = "desktop_assistant"
        abm.sender = MessageMember(str(sender_id), sender_name)
        abm.type = MessageType.FRIEND_MESSAGE
        abm.session_id = session_id
        abm.message_id = str(uuid.uuid4())
        abm.timestamp = int(time.time())
        abm.message = message_parts
        if message_parts:
            abm.message_str = _message_chain_to_text(MessageChain(message_parts)) or text or "[图片]"
        else:
            abm.message_str = text
        abm.raw_message = {"source": "desktop_assistant_ws"}

        msg_event = DesktopMessageEvent(
            message_str=text,
            message_obj=abm,
            platform_meta=self.metadata,
            session_id=session_id,
            is_proactive=False,
        )
        
        # 调试日志 - 确认 unified_msg_origin 的实际值
        logger.info(f"[DesktopAssistant] unified_msg_origin={msg_event.unified_msg_origin}, platform_meta.id={self.metadata.id}")

        if selected_provider:
            msg_event.set_extra("selected_provider", selected_provider)
        if selected_model:
            msg_event.set_extra("selected_model", selected_model)

        self.commit_event(msg_event)

    def _has_pending_reply(self, session_id: str) -> bool:
        ts = self._pending_replies.get(session_id)
        if not ts:
            return False
        if time.time() - ts > self._pending_reply_ttl:
            self._pending_replies.pop(session_id, None)
            return False
        return True

    def _clear_pending_reply(self, session_id: str) -> None:
        self._pending_replies.pop(session_id, None)
            
    async def terminate(self):
        """终止适配器"""
        global ws_server
        
        logger.info("正在停止桌面悬浮球助手...")
        
        self._running = False
        
        # 停止过期请求清理任务
        try:
            await client_manager.stop_cleanup_task()
        except Exception as e:
            logger.error(f"停止清理任务失败: {e}")
        
        # 停止主动对话服务
        if self.proactive_dialog:
            try:
                await self.proactive_dialog.stop()
            except Exception as e:
                logger.error(f"停止主动对话服务失败: {e}")
                
        # 停止桌面监控服务
        if self.desktop_monitor:
            try:
                await self.desktop_monitor.stop()
            except Exception as e:
                logger.error(f"停止桌面监控服务失败: {e}")
        
        # 停止 WebSocket 服务器
        if ws_server:
            try:
                await ws_server.stop()
            except Exception as e:
                logger.error(f"停止 WebSocket 服务器失败: {e}")
        
        self.status = self.status.__class__.STOPPED
        logger.info("桌面悬浮球助手已停止")
