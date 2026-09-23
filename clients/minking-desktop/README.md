# MinKing AI 桌面端

## 0.2.3：共用服务端协议与本地工具接入

- 账号一键同步并发验证；媒体目录复用服务端适配器，候选与实际调用状态分开显示。
- Codex 图片请求构造、SSE 图片解析已抽到 `app/providers/image_protocol.py`，云端和本地共用；Grok 图片/视频及 Antigravity 图片复用现有协议函数。
- 本地接入工具支持模型选择、备份和回退，使用独立 `local-profiles` 备份目录；Claude Code 本地调用支持 Anthropic Messages 协议。
- 云端回退按钮改名「回退配置」，调用记录显示中文状态、费用和合并令牌用量。界面时间统一为本地时区的 `yyyy-mm-dd hh:mm:ss`，API 与数据库保留标准时间表示。
- 本地和云端模型目录均支持文本、图片、视频分类。
- 实测 Grok 本地图片返回图片，视频任务完成并返回视频 URL。WorkBuddy 图片官方接口返回 404；Codex / Antigravity 图片转换已接入，但本轮真实生成未验证成功。

## 0.2.2：修复云端调用测试

Responses 调试采用消息列表，Codex Responses Lite 复用已有协议转换并携带所需 reasoning 参数。客户端收集流式完成事件和输出项，兼容结束事件中 output 为空的情况；失败或截断不会显示为成功。HTTP 错误在界面展示脱敏后的具体原因，不保存请求或响应内容。

## 0.2.1：原 EXE 统一云端和本地

原客户端主窗口通过顶部「云端服务 / 本地官方」切换来源，两种来源复用模型目录和文本/图片/视频调用测试组件。云端保留登录、余额、账单、卡密、配置同步；本地包含 HTTP 服务启停/端口、独立 API Key、官方账号扫描和接入配置。本地服务在首次打开本地面板时启动，切回云端或退出云端账号不会停止它。HTTP 端口只提供 API，不提供网页，也不打开浏览器。关闭窗口后服务留在托盘；退出程序停止 HTTP 服务。

- 统一 EXE：`dist/MinKingAI.exe`，沿用原客户端位置，单文件直接启动。
- 源码启动：`./run-local.ps1` 或 `python run.py`。
- 构建本地版：`./build-local.ps1`。
- 默认 Base URL：`http://127.0.0.1:18787/v1`，Key 在 EXE 内复制。
- 调用方使用 `平台/官方模型ID`，例如 `grok/grok-4.6`。只连接官方域名，无自动跨平台模型替换。
- API Key 独立于云端 Key，Windows 下使用 DPAPI 加密保存。官方登录文件只读，不主动刷新或改写，以便与原客户端共存。失效后在官方客户端重新登录，再扫描。
- 支持现有系统的本机 HTTP CONNECT 代理，保留 TLS 校验，不采用云端配置中的 API Base URL。
- 默认进入统一窗口并记住上次选择；本地模式无需门户账号。旧 `--cloud` 参数仍可运行统一窗口。

协议矩阵、已知限制和接入示例见 [LOCAL_API.md](LOCAL_API.md)。

## 云端配置同步

托盘小工具，用来把本机 Codex / WorkBuddy / Claude Code 在「MinKing 云」和「官方登录」之间一键切换。**不是聊天窗口。**

- 邮箱验证码登录，和网站门户是同一批用户。
- 云模式：把 MinKing 的 Key 和 URL 写入本地工具配置。
- 官方模式：恢复接入前自动保存的快照。流量走你已经登录的官方账号，不经过 10.1.102.36。
- 官方 `auth.json` / Cookie / token **不会上传**到服务器。
- 桌面登录 token 用 Windows DPAPI 保护，不写进日志。

开始时本机没有 `rustc`/`cargo`。已用 `rustup-init -y` 装好 **Rust 1.98.1**，但为了当天能跑通测试和 EXE，客户端做成 **Python + pywebview + 系统托盘**（未再转 Tauri）。可 `python -m` 运行，也可用 PyInstaller 打成 EXE。

## 依赖

使用仓库里的虚拟环境：

```bat
D:\Users\DELL\Desktop\transfer-station\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

需要 Windows 上的 WebView2（Win10/11 一般已有）和可显示的托盘。

## 运行

双击 `run.cmd`，或：

```bat
cd /d D:\Users\DELL\Desktop\transfer-station\clients\minking-desktop
set PYTHONPATH=%CD%
D:\Users\DELL\Desktop\transfer-station\.venv\Scripts\python.exe -m minking_desktop
```

默认 API 根地址：

`https://ceshi.007ka.cn/maliang/v1`

可在窗口「设置」里覆盖。桌面接口为：

- `GET /portal/api/captcha`
- `POST /portal/api/desktop/auth/send-code`
- `POST /portal/api/desktop/auth/verify`
- `GET /portal/api/desktop/bootstrap`
- `POST /portal/api/desktop/key`
- `POST /portal/api/desktop/logout`

请求带 `Authorization: Bearer …`，不带网站 Cookie。

## 一键接入

| 工具 | 行为 |
| --- | --- |
| Codex | 先快照 `~/.codex/{config.toml,auth.json,.env,codex-models.json}`，再写入 `minkingapi`。`base_url` 必须带 `/v1`。 |
| Grok | 快照 `%USERPROFILE%\\.grok\\config.toml`，合并 `[model_providers.minkingapi]` 和 `minking-*` 模型，默认切到 `minking-grok-4.6`（若列表里有）。不改 `auth.json`。 |
| WorkBuddy / CodeBuddy | 同时写入 `%USERPROFILE%\\.workbuddy\\models.json` 和 `%USERPROFILE%\\.codebuddy\\models.json`，追加 MinKing 模型，不删其它模型。 |
| Claude Code | 快照 `~/.claude/settings.json`，设置 `ANTHROPIC_API_KEY` 和 `ANTHROPIC_BASE_URL`（**去掉**末尾 `/v1`，因为 Claude Code 自己会拼 `/v1/messages`）。Opus→`gpt-5.6-sol`，Sonnet→`grok-4.6`，Haiku→`gemini-3.8-flash`，Fable→`gpt-6-astra`。 |
| ZCode | 合并 `%USERPROFILE%\\.zcode\\v2\\provider_config.json`，每个勾选模型一条供应商，`providerId` 用模型 id。 |

官方首次快照在 `%APPDATA%\MinKing\profiles\<harness>\official\`。每次再接入会把当时的文件备份到 `...\profiles\<harness>\backups\<时间戳>\`（最多保留 20 份）。

带 `.complete` 标记。第一次接入才拍快照，之后不会覆盖官方快照。

托盘菜单：打开主窗口、全部回到官方、退出。关闭主窗口会藏到托盘，不会退出。再次双击 `MinKingAI.exe` 会把已有窗口拉到前台；只有托盘里选「退出」才是真正退出。如果双击没有窗口，先在托盘溢出区找 MinKing 图标，或打开任务管理器结束 `MinKingAI.exe` 后再打开。

## 协议 `minking://`

首次启动会在 **当前用户** 下注册 `minking://`。

- 应用已在运行时，再次打开 `minking://import` 会唤起主窗口。
- 若尚未运行，协议会启动 `run.py`。
- 门户上的「导入到桌面端」按钮由另一个任务接入。本客户端不在 URL 里接收 token。

## 测试

不访问现网。在本目录：

```bat
set PYTHONPATH=%CD%
D:\Users\DELL\Desktop\transfer-station\.venv\Scripts\python.exe -m pytest
```

## 打包 EXE

```bat
build_exe.cmd
```

成功后路径（onedir，需整夹一起拷贝）：

`clients\minking-desktop\dist\MinKingAI.exe`

## 数据位置

| 路径 | 内容 |
| --- | --- |
| `%APPDATA%\MinKing\settings.json` | 公共 URL、邮箱，无密钥 |
| `%APPDATA%\MinKing\token.dpapi` | DPAPI 加密的桌面登录 token |
| `%APPDATA%\MinKing\profiles\` | 各工具官方快照 |
| `%APPDATA%\MinKing\desktop.log` | 仅方法/路径/状态，不含 Key |

## 已知缺口

- 未使用 Tauri 2（本机开始时没有 Rust 工具链；已尝试静默安装 rustup）。
- 协议注册只写 HKCU，部分环境可能需要用户手动关联。
- 打 EXE 依赖 PyInstaller 和本机 WebView2。
