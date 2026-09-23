# MinKing AI

多上游的 OpenAI 兼容转发服务。同一把 API Key 可以调用 Codex、Grok、Antigravity 和 WorkBuddy。这个仓库包含网关、管理台、用户门户、官网，以及 Windows 桌面客户端。

当前源码版本：网关 `0.8.27`，桌面端 `0.2.17`。

桌面端是托盘里的配置切换器，用来把本机编程工具接到 MinKing 云或切回官方登录。它不是聊天窗口。

## 组成

| 部分 | 位置 | 作用 |
| --- | --- | --- |
| 网关 | `app/`，`python -m app` | `/v1/*` 客户端接口，默认端口 `8787` |
| 官网 | `/` | 产品介绍、模型说明、客户端下载 |
| 管理台 | `/admin` | 上游账号、API Key、计费、卡密、调用明细 |
| 用户门户 | `/portal`，公网反代下也挂在 `/v1/portal` | 注册登录、余额、试玩、流水、卡密、工具接入 |
| 桌面端 | `clients/minking-desktop` | Windows 托盘程序 `MinKingAI.exe` |

公网示例入口是 `https://ceshi.007ka.cn/maliang/v1`。门户页面是 `https://ceshi.007ka.cn/maliang/v1/portal`。

## 网关

支持的上游：

- Codex（ChatGPT 订阅账号）
- Grok
- Antigravity（Gemini / Claude）
- WorkBuddy（例如 `workbuddy/glm-5.2`、`workbuddy/deepseek-v4-flash`）

客户端接口：

- `GET /v1/models`
- `POST /v1/responses`
- `POST /v1/chat/completions`
- `POST /v1/messages`（Anthropic Messages。`ANTHROPIC_BASE_URL` 不要带末尾 `/v1`）
- `POST /v1/images/generations`、`POST /v1/images/edits`
- `POST /v1/videos` 及视频编辑、延长、remix
- `/mcp` 上的 `generate_image`、`edit_image`

鉴权使用管理台或门户发的 Key：

```http
Authorization: Bearer sk-ts-...
```

每把 Key 有一个美元钱包。售价是官方目录价乘以倍率。未定价的模型不会出现在客户端目录里。余额为 0 时，生成请求返回 `402`。给 Key 或邮箱钱包加款必须大于 0，且不超过 10000 美元。

调用按上游账号的近期成功率调度，连续失败会短暂冷却。不会在一次请求里跨供应商换号。Codex 重放会去掉只属于上一次登录的服务端条目 id，并保留 `call_id` 和可见内容。

日志和数据库不保存提示词、回复、图片、完整 API Key、Session ID、Cookie、密码或 `auth.json` 内容。

## 管理台

管理员用会话登录，用户名固定为 `admin`。旧的 `X-Admin-Token` 已停用。

可以做这些事：

- 导入或网页登录上游账号，导出凭据，把失效账号或模型重新标为有效
- 创建、轮换、删除 API Key，并按 Key 指定上游路由
- 查看调用明细、资金流水，给钱包加款
- 发放和查看卡密
- 上传官网用的 `MinKingAI.exe` 和客户端技能包
- 在瓷白、石墨、深海三套主题之间切换

管理台写接口校验同源和 `CSRF`。登录失败有次数限制。

## 用户门户

门户和桌面端使用同一批账号。页面包括工作台统计、调用记录、模型目录、试玩、流水、卡密兑换和工具接入。网页上的工具接入会打开桌面端，不直接改本机配置。试玩调用消耗当前 Key 的余额。

桌面端注册要带这台电脑的标识。同一台 Windows 电脑只领取一次新用户奖励；重复注册仍可建号，但奖励为 0。浏览器注册按邮箱计算，读不到这台电脑的硬件标识。

卡密兑换按上海日历日计算：每个账号最多 10 次（成功和失败都算），同一个 IP 最多 40 次。

## 桌面端

支持把这些本机工具接到 MinKing 云，或恢复官方登录：

| 工具 | 云端写入 |
| --- | --- |
| Codex | `~/.codex/config.toml` 里的 MinKing 供应商。已有的其它配置会保留 |
| Grok | `%USERPROFILE%\.grok\config.toml` |
| WorkBuddy / CodeBuddy | `models.json` 中追加 MinKing 模型 |
| Claude Code | `ANTHROPIC_API_KEY` 和 `ANTHROPIC_BASE_URL` |
| ZCode | `provider_config.json`，每个模型一条供应商 |

官方 `auth.json`、Cookie 和 token 不会上传。第一次接入前会留下快照，之后每次接入再保留一份备份。回到官方配置时可以选择要恢复的版本。Codex 的「一键同步会话」在原对话上更新路由，不另建一份会话。

桌面登录态用 Windows DPAPI 保护。关闭窗口后程序留在托盘，从托盘选择退出才会结束。

## 本地运行网关

需要 Python 3.11 或更高版本。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
python -m app
```

启动前在 `.env` 里把 `ADMIN_INITIAL_PASSWORD` 设成至少 12 位的随机密码。打开 `http://127.0.0.1:8787/admin`，用 `admin` 和这个密码登录。登录成功后从 `.env` 删除 `ADMIN_INITIAL_PASSWORD`。密码以 Argon2id 存在数据库里。

没有管理员、也没有这个初始密码时，服务会拒绝启动。

生产存储可以用 MySQL 8。`MYSQL_HOST` 留空时使用 `data/gateway.sqlite3`，测试也走 SQLite。

如果管理台挂在子路径后面，设置 `APP_ROOT_PATH`（例如 `/maliang`）。Nginx 应去掉这个前缀再转发到 `127.0.0.1:8787`，`proxy_pass` 末尾保留 `/`。直连时把 `APP_ROOT_PATH` 留空。

忘记管理员密码时，在服务器上交互执行：

```bash
.venv/bin/python scripts/reset_admin_password.py
```

脚本不回显密码。重置成功后，已有管理会话会全部失效。

## 运行桌面端

依赖放在仓库根目录的 `.venv` 里。Windows 需要 WebView2。

```powershell
cd clients\minking-desktop
.\run.cmd
```

打包：

```powershell
.\build_exe.cmd
```

成功后的程序是 `clients\minking-desktop\dist\MinKingAI.exe`。macOS 打包由 `.github/workflows/build-minking-desktop.yml` 在推送到 `main` 且改动桌面端时触发。

默认云端地址可以在窗口设置里改。桌面登录不使用网站 Cookie，请求带 `Authorization: Bearer`。

## 测试

不访问真实上游：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --ignore=tests/live
```

真实上游探测会消耗额度和余额，只在明确要做联调时单独运行 `tests/live`。

## 不要提交的内容

`.gitignore` 已排除本地运行文件。不要把下面这些放进仓库：

- `.env` 和任何备份
- `.venv/`
- `data/`（数据库、上游凭据、安装包）
- `output/`、打包目录、`__pycache__`
