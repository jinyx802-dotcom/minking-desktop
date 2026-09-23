# 本地官方模型网关

## 使用

双击原路径的 `dist/MinKingAI.exe`，在顶部切换「本地官方」。在「官方账号」扫描和验证本机登录；从「模型目录」选择模型；在「HTTP 服务」复制 Base URL 和 API Key，填入调用方。默认只监听 `127.0.0.1:18787`。

窗口是 EXE 自带的 WebView2 界面，通过内部桥接操作后台服务；HTTP 监听端口不提供 HTML、静态文件或浏览器管理入口。启动/暂停、端口变更、密钥复制及模型测试均在 EXE 内完成。关闭窗口保留托盘，退出停止服务。重复启动唤起已有窗口。

## HTTP 接口

请求使用 `Authorization: Bearer <本地 API Key>`，模型 ID 使用 `平台/官方模型ID`。平台为 `codex`、`grok`、`workbuddy`、`claude_code`、`antigravity`。本地 Key 只用于网关鉴权，不会发往官方；上游使用本机官方凭据。

| 接口 | 支持范围 |
| --- | --- |
| `GET /v1/models` | 模型目录，包含来源及验证状态，不将候选模型视为已授权 |
| `POST /v1/responses` | 五个平台的文本与常规 function 工具；Claude 的兼容转换不接受 hosted/custom 工具 |
| `POST /v1/chat/completions` | 五个平台的聊天协议 |
| `POST /v1/messages` | Claude 原生 Messages |
| `POST /v1/images/generations` | Grok、WorkBuddy、OpenAI 官方 API Key 模式 |
| `POST /v1/videos` | Grok 原生视频生成，返回官方任务信息 |
| `GET /v1/videos/{id}` | Grok 官方任务状态 |
| `GET /health` | 进程健康，不验证官方账号 |

`/api/*` 是 EXE 内部管理接口，同样要求本地 Key。根路径 `/` 返回 404。

Responses 和 Chat 接口接受 `stream: true`。Antigravity 当前先收集官方响应，再转换为 SSE，尚非逐 token 实时转发。网关不保存聊天历史，调用方应传入完整消息；`previous_response_id` 和 `conversation` 暂不支持。官方客户端的聊天会话不与此接口自动同步。

## Codex 接入示例

在调用方的 `config.toml` 合并下列内容，并为调用方进程设置环境变量 `MINKING_LOCAL_API_KEY`（值从 EXE 内复制）：

```toml
model_provider = "minking_local"
model = "grok/grok-4.6"

[model_providers.minking_local]
name = "MinKing Local"
base_url = "http://127.0.0.1:18787/v1"
wire_api = "responses"
env_key = "MINKING_LOCAL_API_KEY"
```

模型 ID 请以本机官方目录为准。客户端提供配置复制功能，不直接覆盖原工具的配置，避免影响正在使用的官方登录。

## 本地数据与兼容边界

- `%APPDATA%/MinKing/local-api-key.dpapi`：Windows 用户绑定的加密本地 Key。
- `%APPDATA%/MinKing/local-gateway.json`：默认模型、平台启用状态。
- `%APPDATA%/MinKing/local-desktop.json`：监听端口。
- 不记录 prompt、回复、图片、官方 token、会话内容或完整 Key。目录和验证状态目前只存在内存，重启后需重新查询。
- 扫描 Codex/Grok 官方 auth 文件、Codex 官方配置快照、WorkBuddy 桌面凭据、Claude `.credentials.json`/官方 API 配置、Antigravity 凭据管理器和限定目录中的 JSON。未能读取到的凭据不能据此判断用户一定未登录。
- 官方 API 拒绝请求、模型无权限、凭据过期和不支持的能力都会明确报错，不改用云端账号或其他模型。
- 官方凭据不主动刷新。若官方客户端更新令牌，在 EXE 内重新扫描；不同平台的并发、额度与登录规则仍由官方决定。
- 模型“官方已列出”不等于实际调用成功；WorkBuddy 候选目录尚无官方模型列表验证。
- 图片/视频接口已接入，但本次未进行消耗额度的媒体生成实测；Codex 订阅与 Antigravity 的图片接口暂未接入。Claude 缺少本机官方凭据，使用模拟官方响应验证协议转换。

## 开发验证

```powershell
.\run-local.ps1                       # 桌面窗口 + 托盘 + 本地 HTTP
.\run-local.ps1 -Headless -Port 18788  # 仅 HTTP 服务，用于开发调试
.\build-local.ps1                     # 输出统一 dist/MinKingAI.exe
```

运行仓库虚拟环境中的 pytest，对 `clients/minking-desktop/tests` 执行测试。协议转换复用 `app/providers`，源码需要保留完整仓库；EXE 已包含所需模块，无需安装 Python 或服务器。
