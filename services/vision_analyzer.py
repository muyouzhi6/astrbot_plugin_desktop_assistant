"""
视觉分析服务 - 使用多模态 LLM 分析桌面截图

该服务封装了多模态 LLM 调用逻辑，用于分析截图内容并返回文本描述。
支持三种识图模式：auto（自动检测）、chat（对话模型）、dedicated（独立模型）。
"""

import base64
import os
from typing import Optional
from dataclasses import dataclass
from enum import Enum

from astrbot import logger


class VisionMode(Enum):
    """识图模式枚举"""
    AUTO = "auto"           # 自动检测，优先对话模型，失败时提示配置
    CHAT = "chat"           # 强制使用对话模型
    DEDICATED = "dedicated" # 使用独立配置的识图模型


@dataclass
class VisionAnalysisResult:
    """视觉分析结果"""
    success: bool
    description: str
    image_path: Optional[str] = None
    error_message: Optional[str] = None
    
    @classmethod
    def error(cls, message: str) -> "VisionAnalysisResult":
        """创建错误结果"""
        return cls(success=False, description="", error_message=message)
    
    @classmethod
    def success_result(cls, description: str, image_path: str) -> "VisionAnalysisResult":
        """创建成功结果"""
        return cls(success=True, description=description, image_path=image_path)


class VisionAnalyzer:
    """
    视觉分析器 - 使用多模态 LLM 分析图片
    
    该类封装了调用多模态 LLM 分析图片的逻辑，
    用于将截图转换为文本描述，供主 LLM 使用。
    
    支持三种识图模式：
    - auto: 自动检测，优先尝试对话模型
    - chat: 强制使用对话模型（需确保支持多模态）
    - dedicated: 使用独立配置的多模态模型
    """
    
    # 默认分析提示词
    DEFAULT_ANALYSIS_PROMPT = """请分析这张桌面截图，描述以下内容：

1. **当前活动**：用户正在进行什么操作？（如浏览网页、编写代码、看视频等）
2. **打开的应用**：屏幕上可见哪些应用程序窗口？
3. **屏幕布局**：窗口的大致布局是怎样的？
4. **关键内容**：如果有明显的文字、图片或重要信息，请简要描述

请用简洁的中文描述，不要过于详细，重点关注用户可能关心的内容。"""

    def __init__(
        self,
        context,
        vision_mode: str = "auto",
        dedicated_provider_id: Optional[str] = None,
    ):
        """
        初始化视觉分析器
        
        Args:
            context: AstrBot 上下文对象，用于调用 LLM API
            vision_mode: 识图模式，可选值: "auto" | "chat" | "dedicated"
            dedicated_provider_id: 独立识图模型的 Provider ID（dedicated 模式必填）
        """
        self.context = context
        
        # 安全解析 vision_mode
        try:
            self.vision_mode = VisionMode(vision_mode)
        except ValueError:
            logger.warning(f"VisionAnalyzer: 无效的 vision_mode '{vision_mode}'，使用默认值 'auto'")
            self.vision_mode = VisionMode.AUTO
            
        self.dedicated_provider_id = dedicated_provider_id
        
        # 配置验证
        if self.vision_mode == VisionMode.DEDICATED and not dedicated_provider_id:
            logger.warning(
                "VisionAnalyzer: vision_mode 设置为 'dedicated'，"
                "但未配置 dedicated_provider_id，将降级为 'auto' 模式"
            )
            self.vision_mode = VisionMode.AUTO
            
        logger.info(f"VisionAnalyzer 初始化完成: mode={self.vision_mode.value}, "
                   f"dedicated_provider={dedicated_provider_id or '未配置'}")
    
    async def _get_vision_provider_id(self, umo: Optional[str] = None) -> tuple[Optional[str], bool]:
        """
        根据配置的识图模式获取实际使用的 Provider ID
        
        Args:
            umo: unified_message_origin，用于获取会话关联的 provider
        
        Returns:
            tuple[Optional[str], bool]: (provider_id, is_dedicated)
            - provider_id: 实际使用的 Provider ID，可能为 None
            - is_dedicated: 是否使用独立模型
        """
        if self.vision_mode == VisionMode.DEDICATED:
            return self.dedicated_provider_id, True
        
        # AUTO 或 CHAT 模式：使用对话模型
        try:
            chat_provider_id = await self.context.get_current_chat_provider_id(umo)
            return chat_provider_id, False
        except Exception as e:
            logger.error(f"获取对话模型 Provider ID 失败: {e}")
            return None, False
    
    async def analyze_image(
        self,
        image_path: str,
        prompt: Optional[str] = None,
        provider_id: Optional[str] = None,
        umo: Optional[str] = None,
    ) -> VisionAnalysisResult:
        """
        分析图片内容
        
        Args:
            image_path: 图片文件路径
            prompt: 自定义分析提示词，如果为 None 则使用默认提示词
            provider_id: 指定的 LLM provider ID（会覆盖 vision_mode 配置）
            umo: unified_message_origin，用于获取会话关联的 provider
        
        Returns:
            VisionAnalysisResult: 分析结果
        """
        # 检查文件是否存在
        if not os.path.exists(image_path):
            return VisionAnalysisResult.error(f"图片文件不存在: {image_path}")
        
        # 用于错误处理的标志
        is_dedicated = False
        
        try:
            # 确定使用的 Provider ID
            if provider_id:
                # 显式指定了 provider_id，直接使用
                actual_provider_id = provider_id
                is_dedicated = True
            else:
                # 根据 vision_mode 获取 provider_id
                actual_provider_id, is_dedicated = await self._get_vision_provider_id(umo)
            
            if not actual_provider_id:
                return VisionAnalysisResult.error(
                    "无法获取识图模型 Provider ID，请检查配置"
                )
            
            logger.info(f"使用 Provider '{actual_provider_id}' 进行视觉分析")
            
            # 使用自定义或默认提示词
            analysis_prompt = prompt or self.DEFAULT_ANALYSIS_PROMPT
            
            # 调用多模态 LLM
            llm_response = await self.context.llm_generate(
                chat_provider_id=actual_provider_id,
                prompt=analysis_prompt,
                image_urls=[image_path],
            )
            
            if llm_response and llm_response.completion_text:
                return VisionAnalysisResult.success_result(
                    description=llm_response.completion_text,
                    image_path=image_path
                )
            else:
                return VisionAnalysisResult.error("LLM 未返回有效的分析结果")
                
        except Exception as e:
            error_msg = str(e)
            logger.error(f"视觉分析失败: {error_msg}")
            
            # AUTO 模式下，如果对话模型不支持多模态，提供友好提示
            if self.vision_mode == VisionMode.AUTO and not is_dedicated:
                error_lower = error_msg.lower()
                if any(keyword in error_lower for keyword in ["image", "vision", "multimodal", "不支持"]):
                    return VisionAnalysisResult.error(
                        "当前对话模型不支持识图功能。\n"
                        "解决方案：请在插件配置中设置 vision_mode 为 'dedicated'，"
                        "并配置一个支持多模态的 LLM Provider（如 GPT-4o、Claude 3）"
                    )
            
            return VisionAnalysisResult.error(f"分析过程出错: {error_msg}")
    
    async def analyze_desktop_screenshot(
        self,
        image_path: str,
        umo: Optional[str] = None,
    ) -> VisionAnalysisResult:
        """
        专门用于分析桌面截图的方法
        
        使用针对桌面截图优化的提示词进行分析。
        
        Args:
            image_path: 截图文件路径
            umo: unified_message_origin
        
        Returns:
            VisionAnalysisResult: 分析结果
        """
        desktop_prompt = """你现在看到的是用户电脑桌面的实时截图。请简洁地描述：

1. 用户当前在做什么？
2. 屏幕上有哪些主要的应用或内容？
3. 有什么值得注意的信息吗？

请用口语化的方式回答，就像你真的能"看到"用户的屏幕一样。"""
        
        return await self.analyze_image(
            image_path=image_path,
            prompt=desktop_prompt,
            umo=umo,
        )
    
    def encode_image_base64(self, image_path: str) -> Optional[str]:
        """
        将图片编码为 base64 字符串
        
        Args:
            image_path: 图片文件路径
        
        Returns:
            base64 编码的图片字符串，失败返回 None
        """
        try:
            with open(image_path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            logger.error(f"图片编码失败: {e}")
            return None
