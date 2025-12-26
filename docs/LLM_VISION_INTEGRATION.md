# LLM 视觉集成技术文档

## 问题描述

### 当前行为
当 LLM 调用 `view_desktop_screen` 工具时，截图会**直接发送给用户**，而不是作为 LLM 的视觉输入。这导致 LLM 无法"看到"截图内容，只能告诉用户"已发送截图"。

### 期望行为
当用户询问"我桌面上是什么情况"时，LLM 应该能够：
1. 调用截图工具获取桌面截图
2. **将截图作为视觉上下文输入给 LLM**
3. LLM 分析截图内容后生成描述性回复

## 技术分析

### 问题根源

在 AstrBot 框架的 [`tool_loop_agent_runner.py`](../../../astrbot/core/agent/runners/tool_loop_agent_runner.py:360-370) 中，当工具返回图片时：

```python
elif isinstance(res.content[0], ImageContent):
    tool_call_result_blocks.append(
        ToolCallMessageSegment(
            role="tool",
            tool_call_id=func_tool_id,
            content="返回了图片(已直接发送给用户)",  # LLM 收到的是这个文本
        ),
    )
    yield MessageChain(type="tool_direct_result").base64_image(
        res.content[0].data,
    )
```

**关键发现**：框架将图片当作"直接发送给用户"的内容处理，而不是将图片数据传递给 LLM 作为多模态输入。

### 当前插件实现

在 [`main.py`](../main.py:159-176) 中：

```python
@llm_tool("view_desktop_screen")
async def view_desktop_screen_tool(self, event: AstrMessageEvent):
    """查看用户当前电脑桌面屏幕内容..."""
    async for result in self._do_remote_screenshot(event, None, silent=False):
        yield result  # 直接 yield 图片，导致图片发送给用户

async def _do_remote_screenshot(self, event, target_session_id, silent=False):
    # ...
    if response.success and response.image_path:
        yield event.image_result(response.image_path)  # 问题：使用 image_result 直接发送
```

### AstrBot 多模态支持

通过分析 AstrBot 框架，发现以下多模态相关能力：

1. **[`ProviderRequest`](../../../astrbot/core/provider/entities.py:88-110)** 支持 `image_urls` 参数
2. **[`llm_generate()`](../../../astrbot/core/star/context.py:83-122)** 方法接受 `image_urls: list[str]` 参数
3. **[`tool_loop_agent()`](../../../astrbot/core/star/context.py:124-213)** 同样支持图片输入

## 解决方案

### 方案概述

修改 `view_desktop_screen_tool` 工具的返回值策略：

| 场景 | 当前行为 | 期望行为 |
|------|----------|----------|
| LLM 工具调用 | 直接发送图片给用户 | 返回图片路径/描述，让 LLM 能"看到"图片 |
| `/screenshot` 命令 | 直接发送图片给用户 | 保持不变，直接发送图片 |

### 技术实现方案

#### 方案 A：返回文本描述 + 图片路径（推荐）

让工具返回一个包含图片路径的字符串结果，利用 AstrBot 的多模态上下文机制：

```python
@llm_tool("view_desktop_screen")
async def view_desktop_screen_tool(self, event: AstrMessageEvent):
    """
    查看用户当前电脑桌面屏幕内容。
    返回：截图的文件路径，供后续分析使用。
    """
    # 获取截图
    response = await self._get_screenshot()
    
    if response.success and response.image_path:
        # 方案 A：返回字符串结果，让 LLM 知道截图信息
        # 图片路径会被框架处理，加入下一次 LLM 请求的上下文
        return f"截图已获取，保存在: {response.image_path}，分辨率: {response.width}x{response.height}"
    else:
        return f"截图失败: {response.error_message}"
```

**优点**：简单，符合现有 llm_tool 返回值规范
**缺点**：LLM 只能看到路径文本，无法直接看到图片内容

#### 方案 B：使用 MCP ImageContent 返回（需要框架支持）

返回 MCP 标准的 `ImageContent` 类型，但需要框架修改以支持将图片作为 LLM 上下文：

```python
import mcp.types

@llm_tool("view_desktop_screen")
async def view_desktop_screen_tool(self, event: AstrMessageEvent):
    response = await self._get_screenshot()
    
    if response.success and response.image_path:
        # 读取图片为 base64
        with open(response.image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode()
        
        # 返回 ImageContent，期望框架将其作为多模态输入
        yield mcp.types.CallToolResult(
            content=[
                mcp.types.ImageContent(
                    type="image",
                    data=image_data,
                    mimeType="image/png"
                )
            ]
        )
```

**优点**：标准化的图片返回方式
**缺点**：当前框架实现会直接发送给用户，需要修改框架

#### 方案 C：自定义多模态工具调用（需要框架扩展）

扩展 AstrBot 框架，支持工具返回"视觉上下文"类型：

```python
# 需要在框架中添加新的返回类型
class VisionContextResult:
    """表示需要作为 LLM 视觉输入的图片"""
    image_path: str
    description: str

@llm_tool("view_desktop_screen")
async def view_desktop_screen_tool(self, event: AstrMessageEvent):
    response = await self._get_screenshot()
    
    if response.success:
        # 返回 VisionContextResult，框架将图片加入 LLM 上下文
        return VisionContextResult(
            image_path=response.image_path,
            description=f"桌面截图，分辨率 {response.width}x{response.height}"
        )
```

**优点**：最符合需求的解决方案
**缺点**：需要修改 AstrBot 框架

### 推荐实施路径

#### 短期方案（无需框架修改）

1. **修改 `view_desktop_screen_tool`**：返回纯文本描述
2. **新增独立的"分析桌面"工具**：调用截图后，主动调用多模态 LLM API

```python
@llm_tool("analyze_desktop_screen")
async def analyze_desktop_screen_tool(self, event: AstrMessageEvent):
    """
    分析用户当前桌面屏幕内容，返回屏幕上显示内容的描述。
    """
    # 1. 获取截图
    response = await self._get_screenshot()
    if not response.success:
        return f"无法获取截图: {response.error_message}"
    
    # 2. 使用多模态 LLM 分析截图
    context = self.context
    umo = event.unified_msg_origin
    provider_id = await context.get_current_chat_provider_id(umo)
    
    llm_response = await context.llm_generate(
        chat_provider_id=provider_id,
        prompt="请描述这张桌面截图中的内容，包括打开的应用程序、窗口标题、以及用户可能正在进行的活动。",
        image_urls=[response.image_path],
    )
    
    # 3. 返回分析结果给主 LLM
    return llm_response.completion_text
```

#### 中期方案（建议向 AstrBot 提交 PR）

向 AstrBot 框架提交功能请求或 PR，在 `tool_loop_agent_runner.py` 中添加对"视觉上下文"类型工具返回值的支持：

```python
# 修改 _handle_function_tools 方法
elif isinstance(res.content[0], ImageContent):
    # 检查是否需要作为视觉上下文
    if res.is_vision_context:  # 新增标志位
        # 将图片加入下一轮 LLM 请求的 image_urls
        self.run_context.pending_image_urls.append(
            f"base64://{res.content[0].data}"
        )
        tool_call_result_blocks.append(
            ToolCallMessageSegment(
                role="tool",
                tool_call_id=func_tool_id,
                content="[图片已加入视觉上下文，等待分析]",
            ),
        )
    else:
        # 保持原有行为：直接发送给用户
        ...
```

## 实施步骤

### 阶段一：短期修复

1. **创建 `analyze_desktop_screen` 工具**
   - 内部调用截图功能
   - 使用多模态 LLM API 分析截图
   - 返回文本描述结果

2. **修改 `view_desktop_screen` 工具的描述**
   - 明确说明该工具会直接发送截图给用户
   - 引导 LLM 在需要"看"屏幕时使用 `analyze_desktop_screen`

3. **保持 `/screenshot` 命令不变**
   - 继续直接发送图片给用户

### 阶段二：框架增强

1. **向 AstrBot 提交 Issue/PR**
   - 描述多模态工具调用需求
   - 提出 `VisionContextResult` 类型建议

2. **临时使用 Hook 机制**
   - 利用 `on_tool_end` hook 注入图片上下文
   - 实验性验证方案可行性

## 代码修改清单

### 需要修改的文件

| 文件路径 | 修改内容 | 优先级 |
|----------|----------|--------|
| [`main.py`](../main.py) | 新增 `analyze_desktop_screen` 工具 | 高 |
| [`main.py`](../main.py) | 更新 `view_desktop_screen` 工具描述 | 高 |
| [`ws_handler.py`](../ws_handler.py) | 优化截图响应处理 | 中 |

### 新增文件

| 文件路径 | 用途 |
|----------|------|
| `services/vision_analyzer.py` | 封装多模态 LLM 调用逻辑 |

## 测试用例

### 用例 1：LLM 主动分析桌面

**输入**：用户问 "我桌面上是什么情况？"

**期望行为**：
1. LLM 调用 `analyze_desktop_screen` 工具
2. 工具获取截图并用多模态 LLM 分析
3. LLM 收到分析结果，生成自然语言回复
4. **不会直接发送截图给用户**

### 用例 2：用户请求截图

**输入**：用户发送 `/screenshot` 命令

**期望行为**：
1. 直接执行截图
2. 将截图发送给用户
3. （可选）发送截图信息

### 用例 3：LLM 需要发送截图给用户

**输入**：用户说 "把我的桌面截图发给我"

**期望行为**：
1. LLM 调用 `view_desktop_screen` 工具
2. 截图直接发送给用户
3. LLM 回复 "已发送截图"

## 参考资料

- [AstrBot LLM Tool 文档](https://github.com/AstrBotDevTeam/AstrBot/wiki/LLM-Tools)
- [MCP ImageContent 规范](https://modelcontextprotocol.io/specification/types#imagecontent)
- [OpenAI Vision API](https://platform.openai.com/docs/guides/vision)

## 变更日志

| 日期 | 版本 | 变更内容 |
|------|------|----------|
| 2024-12-26 | 1.0 | 初始文档，问题分析和解决方案设计 |