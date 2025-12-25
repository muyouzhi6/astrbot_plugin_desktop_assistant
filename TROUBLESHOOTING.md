# 桌面助手故障排查指南

## 📋 常见问题

### 问题 1: 发送"截图"命令后提示"没有已连接的桌面客户端"

**可能原因：**
1. 桌面客户端未启动
2. 桌面客户端连接到了错误的服务器地址
3. 服务器防火墙未开放 6190 端口
4. WebSocket 服务器未成功启动

**排查步骤：**

#### 步骤 1: 检查服务端日志

在 Linux 服务器的 AstrBot 日志中，查找以下信息：

```
✅ WebSocket 服务器已启动: ws://0.0.0.0:6190
   桌面客户端请连接到此地址，路径: /ws/client?session_id=xxx&token=xxx
```

如果没有看到这条日志，说明 WebSocket 服务器未启动。请检查：
- `websockets` 库是否已安装：`pip install websockets`
- 端口 6190 是否被占用：`netstat -tlnp | grep 6190`

#### 步骤 2: 检查服务器防火墙

确保 6190 端口已开放：

```bash
# Ubuntu/Debian
sudo ufw allow 6190/tcp

# CentOS/RHEL
sudo firewall-cmd --permanent --add-port=6190/tcp
sudo firewall-cmd --reload

# 验证端口是否开放
sudo netstat -tlnp | grep 6190
```

#### 步骤 3: 检查桌面客户端日志

启动桌面客户端后，查看控制台输出：

```
[DEBUG] 启动 WebSocket 连接:
  - 服务器: http://你的服务器IP:6185
  - WS 端口: 6190
  - Session ID: xxx

[WebSocket] 初始化:
  - 源 server_url: http://你的服务器IP:6185
  - 解析 host: 你的服务器IP
  - WebSocket 端口: 6190
  - 最终 URL: ws://你的服务器IP:6190/ws/client?token=xxx&session_id=xxx
```

**关键检查点：**
- `解析 host` 应该是你的 Linux 服务器 IP，而不是 `localhost` 或 `127.0.0.1`
- `最终 URL` 应该指向正确的服务器地址

#### 步骤 4: 检查客户端配置

桌面客户端的配置文件位置：
- Windows: `%APPDATA%\astrbot-desktop\config.yaml`
- macOS: `~/Library/Application Support/astrbot-desktop/config.yaml`
- Linux: `~/.config/astrbot-desktop/config.yaml`

检查配置中的 `server.url` 是否正确：

```yaml
server:
  url: "http://你的Linux服务器IP:6185"  # 确保这里是远程服务器地址
  ws_port: 6190
```

---

### 问题 2: 本地 AstrBot 能响应命令，但远程服务器不行

**原因分析：**

这表明桌面客户端连接到了**本地的 AstrBot**，而不是远程服务器。

**排查步骤：**

1. **检查客户端配置的服务器地址**

   打开桌面客户端的设置窗口，确认服务器地址是远程服务器的 IP，而不是 `localhost`。

2. **关闭本地的 AstrBot**

   如果本地也运行了 AstrBot，请先关闭它，然后测试：
   - 如果关闭后桌面客户端无法连接，说明配置确实指向了本地
   - 重新配置为远程服务器地址

3. **检查远程服务器的端口可达性**

   ```bash
   # 在你的本地电脑上测试远程端口
   # Windows
   telnet 远程服务器IP 6190
   
   # Linux/macOS
   nc -zv 远程服务器IP 6190
   ```

---

### 问题 3: WebSocket 连接失败，显示"连接断开/失败"

**可能原因：**
- 网络不通
- 防火墙阻止
- 服务端未运行

**排查步骤：**

1. 确认服务端 AstrBot 正在运行
2. 确认 6190 端口已开放
3. 检查网络连通性：`ping 服务器IP`

---

## 🔧 快速诊断命令

### 在远程 Linux 服务器上：

```bash
# 检查 WebSocket 服务是否运行
netstat -tlnp | grep 6190

# 检查 AstrBot 进程
ps aux | grep astrbot

# 查看 AstrBot 日志（最后 100 行）
tail -100 /path/to/astrbot/logs/astrbot.log | grep -i "websocket\|desktop\|6190"
```

### 在本地电脑上：

```bash
# 测试端口连通性（Linux/macOS）
nc -zv 服务器IP 6190

# 测试端口连通性（Windows PowerShell）
Test-NetConnection -ComputerName 服务器IP -Port 6190
```

---

## 📊 数据流说明

正确的数据流应该是：

```
QQ 客户端
    ↓ (发送"截图"命令)
NapCat (OneBot11 协议)
    ↓
远程 Linux 服务器上的 AstrBot (端口 6185)
    ↓ (插件处理命令)
astrbot_plugin_desktop_assistant
    ↓ (通过 WebSocket 下发命令)
WebSocket 服务器 (端口 6190)
    ↓
你本地电脑上的桌面客户端 (通过 ws://服务器IP:6190 连接)
    ↓ (执行截图)
截图完成，返回图片
    ↓
通过 WebSocket 返回到服务端
    ↓
服务端发送图片到 QQ
```

**关键点：**
- 桌面客户端必须连接到**远程服务器的** 6190 端口
- 不是连接到本地的 6190 端口

---

## 💡 配置示例

### 远程服务器配置

假设你的服务器 IP 是 `192.168.1.100`：

**服务端（Linux 服务器上）：**
- AstrBot 运行在端口 6185
- 插件自动在 6190 启动 WebSocket 服务器
- 确保 6185 和 6190 都已开放

**客户端（你的本地电脑上）：**

`config.yaml`:
```yaml
server:
  url: "http://192.168.1.100:6185"
  ws_port: 6190
  username: "admin"
  password: "你的密码"
```

---

## 📞 获取帮助

如果以上步骤都无法解决问题，请提供以下信息：

1. 服务端 AstrBot 日志（包含 "WebSocket" 或 "6190" 的行）
2. 桌面客户端启动时的控制台输出
3. 你的网络环境描述（本地/远程服务器 IP）
4. 执行的诊断命令及结果