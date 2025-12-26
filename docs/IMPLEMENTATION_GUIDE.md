# LLM 视觉集成实现指南

本文档提供详细的代码实现指南，供开发者参考实施。

## 目录

1. [快速开始](#快速开始)
2. [多模态模型配置](#多模态模型配置)
3. [新增文件](#新增文件)
4. [修改现有文件](#修改现有文件)
5. [测试验证](#测试验证)

---

## 快速开始

### 修改概要

为解决 LLM 工具调用截图后无法"看到"图片内容的问题，需要：

1. **新增** `services/vision_analyzer.py` - 视觉分析服务
2. **修改** `main.py` - 添加 `analyze_desktop_screen` 工具

### 预期效果

| 用户指令 | 触发工具 | 行为 |
|----------|----------|------|
| "我桌面上是什么？" | `analyze_desktop_screen` | LLM 获取截图描述后回复 |
| "发个截图给我" | `view_desktop_screen` | 直接发送截图给用户 |
| `/screenshot` | 命令处理器 | 直接发送截图给用户 |

---

## 多模态模型配置

由于部分用户的对话模型可能不支持多模态（如纯文本 LLM），本插件提供灵活的识图模型配置方案，允许使用独立的多模态模型进行截图分析。

### 配置概述

在 [`metadata.yaml`](../metadata.yaml) 的 `config_schema` 中添加视觉模型配置：

```yaml
# 视觉模型配置
vision_model:
  type: "group"
  label: "识图模型配置"
  properties:
    vision_mode:
      type: "select"
      label: "识图模式"
      description: "选择使用哪个模型进行截图分析"
      default: "auto"
      options:
        - value: "auto"
          label: "自动检测（推荐）"
        - value: "chat"
          label: "使用对话模型"
        - value: "dedicated"
          label: "使用独立模型"
    dedicated_provider_id:
      type: "string"
      label: "独立识图模型 Provider ID"
      description: "当识图模式为「使用独立模型」时生效，填写支持多模态的 LLM Provider ID"
      default: ""
```

### 三种识图模式详解

#### 1. `auto` - 自动检测模式（默认）

**行为**：
- 优先尝试使用当前对话模型进行识图
- 如果对话模型不支持多模态或调用失败，返回友好提示，建议用户配置独立识图模型

**适用场景**：
- 不确定对话模型是否支持多模态
- 希望系统自动选择最佳方案

**配置示例**：
```yaml
vision_mode: "auto"
dedicated_provider_id: ""  # 可选，作为降级方案
```

#### 2. `chat` - 对话模型模式

**行为**：
- 强制使用当前对话模型进行识图
- 不进行额外的兼容性检查

**适用场景**：
- 确认对话模型支持多模态（如 GPT-4o、Claude 3、Gemini Pro Vision）
- 希望简化配置，统一使用同一模型

**配置示例**：
```yaml
vision_mode: "chat"
dedicated_provider_id: ""  # 此模式下忽略此配置
```

**支持多模态的常见模型**：
| 厂商 | 模型名称 |
|------|----------|
| OpenAI | GPT-4o, GPT-4-vision-preview |
| Anthropic | Claude 3 Opus/Sonnet/Haiku |
| Google | Gemini Pro Vision, Gemini 1.5 |
| 其他 | Qwen-VL, Yi-Vision 等 |

#### 3. `dedicated` - 独立模型模式

**行为**：
- 使用专门配置的多模态模型进行识图
- 对话模型和识图模型分离，互不干扰

**适用场景**：
- 对话模型不支持多模态（如 GPT-3.5、纯文本模型）
- 希望使用更强的模型专门处理视觉任务
- 成本优化：对话用便宜模型，识图用强力模型

**配置示例**：
```yaml
vision_mode: "dedicated"
dedicated_provider_id: "openai_gpt4o"  # 填写实际的 Provider ID
```

### 配置示例场景

#### 场景 1：对话模型本身支持多模态

用户使用 GPT-4o 作为对话模型，直接识图即可。

```yaml
vision_mode: "chat"
```

#### 场景 2：对话模型不支持多模态

用户使用 GPT-3.5-turbo 对话（便宜），但需要识图功能。

```yaml
vision_mode: "dedicated"
dedicated_provider_id: "gpt4_vision_provider"  # 预先配置的 GPT-4o Provider
```

#### 场景 3：自动降级方案

不确定模型能力，希望系统自动处理。

```yaml
vision_mode: "auto"
dedicated_provider_id: "backup_vision_provider"  # 可选的降级 Provider
```

### 错误处理

当识图功能不可用时，系统会返回友好的错误提示：

| 错误场景 | 提示信息 |
|----------|----------|
| `auto` 模式下对话模型不支持多模态 | "当前对话模型不支持识图，请在配置中设置独立的多模态模型（vision_mode: dedicated）" |
| `dedicated` 模式但未配置 Provider ID | "识图模式设置为「独立模型」，但未配置 dedicated_provider_id" |
| 指定的 Provider ID 无效 | "无法找到 Provider: {provider_id}，请检查配置" |
| LLM 调用失败 | "识图分析失败: {具体错误信息}" |

---

## 新增文件

### `services/vision_analyzer.py`

创建新文件 `plugins/astrbot_plugin_desktop_assistant/services/vision_analyzer.py`：

```python
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
        self.vision_mode = VisionMode(vision_mode)
        self.dedicated_provider_id = dedicated_provider_id
        
        # 配置验证
        if self.vision_mode == VisionMode.DEDICATED and not dedicated_provider_id:
            logger.warning(
                "VisionAnalyzer: vision_mode 设置为 'dedicated'，"
                "但未配置 dedicated_provider_id，将降级为 'auto' 模式"
            )
            self.vision_mode = VisionMode.AUTO
    
    async def _get_vision_provider_id(self, umo: Optional[str] = None) -> tuple[str, bool]:
        """
        根据配置的识图模式获取实际使用的 Provider ID
        
        Args:
            umo: unified_message_origin，用于获取会话关联的 provider
        
        Returns:
            tuple[str, bool]: (provider_id, is_dedicated)
            - provider_id: 实际使用的 Provider ID
            - is_dedicated: 是否使用独立模型
        """
        if self.vision_mode == VisionMode.DEDICATED:
            return self.dedicated_provider_id, True
        
        # AUTO 或 CHAT 模式：使用对话模型
        chat_provider_id = await self.context.get_current_chat_provider_id(umo)
        return chat_provider_id, False
    
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
                if "image" in error_msg.lower() or "vision" in error_msg.lower():
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
```

---

## 修改现有文件

### `main.py` 修改

#### 1. 添加导入

在文件顶部的导入部分添加：

```python
from .services.vision_analyzer import VisionAnalyzer, VisionAnalysisResult
```

#### 2. 在 `Main` 类中初始化 VisionAnalyzer

在 `__init__` 方法中添加（需要从配置读取识图模式）：

```python
def __init__(self, context: star.Context) -> None:
    # ... 现有代码 ...
    
    # 从配置读取识图模式设置
    vision_mode = self.config.get("vision_mode", "auto")
    dedicated_provider_id = self.config.get("dedicated_provider_id", "")
    
    # 初始化视觉分析器
    self.vision_analyzer = VisionAnalyzer(
        context=context,
        vision_mode=vision_mode,
        dedicated_provider_id=dedicated_provider_id or None,
    )
```

#### 3. 添加新的 LLM 工具 `analyze_desktop_screen`

在 `view_desktop_screen_tool` 方法后添加新工具：

```python
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
    """
    logger.info("🔍 收到桌面分析请求，正在获取截图...")
    
    try:
        # 1. 检查客户端连接
        connected_clients = client_manager.get_connected_client_ids()
        if not connected_clients:
            return "❌ 无法分析桌面：没有已连接的桌面客户端。请确保桌面端程序已启动并连接到服务器。"
        
        # 2. 获取截图
        response = await client_manager.request_screenshot(
            session_id=None,
            timeout=30.0
        )
        
        if not response.success or not response.image_path:
            error_msg = response.error_message or "未知错误"
            return f"❌ 无法获取截图: {error_msg}"
        
        logger.info(f"📸 截图已获取: {response.image_path}")
        
        # 3. 使用多模态 LLM 分析截图
        umo = event.unified_msg_origin
        analysis_result = await self.vision_analyzer.analyze_desktop_screenshot(
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
        import traceback
        traceback.print_exc()
        return f"❌ 分析过程出错: {str(e)}"
```

#### 4. 更新 `view_desktop_screen_tool` 的文档字符串

修改现有工具的描述，明确其用途：

```python
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
    """
    async for result in self._do_remote_screenshot(event, None, silent=False):
        yield result
```

---

## 测试验证

### 测试用例 1：分析桌面

**用户输入**：
```
我桌面上是什么？
```

**期望行为**：
1. LLM 调用 `analyze_desktop_screen` 工具
2. 工具获取截图并分析
3. LLM 收到分析结果文本
4. LLM 生成回复（如："我看到你正在使用 VS Code 编写代码..."）
5. **不会发送截图给用户**

### 测试用例 2：发送截图

**用户输入**：
```
把我的桌面截图发给我
```

**期望行为**：
1. LLM 调用 `view_desktop_screen` 工具
2. 截图直接发送给用户
3. LLM 回复（如："已发送截图"）

### 测试用例 3：命令截图

**用户输入**：
```
/screenshot
```

**期望行为**：
1. 命令处理器执行
2. 截图直接发送给用户
3. 发送截图信息

---

## 注意事项

### 多模态 LLM 要求

`analyze_desktop_screen` 功能**需要多模态 LLM 支持**。根据您配置的 `vision_mode`：

| 模式 | 要求 |
|------|------|
| `auto` | 自动检测，失败时返回配置建议 |
| `chat` | 对话模型必须支持多模态 |
| `dedicated` | 必须配置有效的多模态 Provider ID |

**支持多模态的 LLM 提供商**：
- OpenAI: GPT-4o, GPT-4-vision-preview, GPT-4-turbo
- Anthropic: Claude 3 Opus/Sonnet/Haiku
- Google: Gemini Pro Vision, Gemini 1.5 Pro/Flash
- 其他: Qwen-VL, Yi-Vision, GLM-4V 等

### 模式选择建议

```
┌─────────────────────────────────────────────────────────────┐
│              您的对话模型支持多模态吗？                       │
├─────────────────────────────────────────────────────────────┤
│  是 → vision_mode: "chat"                                   │
│  否 → vision_mode: "dedicated" + dedicated_provider_id      │
│  不确定 → vision_mode: "auto"                               │
└─────────────────────────────────────────────────────────────┘
```

### 错误处理

VisionAnalyzer 已内置智能错误处理：

1. **AUTO 模式**：自动检测错误类型，提供针对性的配置建议
2. **DEDICATED 模式**：验证 Provider ID 有效性
3. **通用错误**：返回详细错误信息便于调试

```python
# 错误处理示例 - 在 analyze_desktop_screen_tool 中
result = await self.vision_analyzer.analyze_desktop_screenshot(
    image_path=response.image_path,
    umo=umo,
)

if not result.success:
    # error_message 已包含友好的错误说明和解决建议
    return f"❌ {result.error_message}"
```

### 性能考虑

1. 截图分析会产生额外的 LLM API 调用
2. 考虑添加分析结果缓存机制（如 30 秒内相同截图不重复分析）
3. 大分辨率截图可能需要压缩后再发送给 LLM

---

## 后续优化

### 短期

- [ ] 添加截图压缩功能，减少 API 调用成本
- [ ] 添加分析结果缓存
- [ ] 优化分析提示词

### 中期

- [ ] 向 AstrBot 框架提交 Issue，请求原生支持工具返回"视觉上下文"
- [ ] 实现更精细的屏幕区域分析（如只分析活动窗口）

### 长期

- [ ] 支持实时屏幕流分析
- [ ] OCR 文字提取功能
- [ ] 用户隐私保护机制（敏感区域模糊）