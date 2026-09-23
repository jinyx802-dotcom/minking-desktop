---
name: minking-media
description: >
  Generate or edit images and videos through the MinKing gateway.
  Use when the user asks to 生成图片, 生图, 画一张, 修图, 改图,
  generate/edit an image, 生成视频, 生视频, 图生视频, 延长视频,
  remix 视频, image_generation, or /imagine. Do not use for ordinary coding.
---

# MinKing 生图 / 生视频

用户要出图或出视频时，**立刻跑本 skill 的脚本**。不要问比例、风格、张数、要不要高清。

发给网关的 `-Prompt` / `--prompt` **必须是英文**。用户用中文说需求时，先译成对应的英文画面/镜头描述再调用，不要把中文原文塞进 prompt。翻译要保住主体、动作、场景、数量、文字内容；不要为了文采另编一套。用户已经给了英文就原样用。对用户说话仍可用中文。

Windows 没有 Python 也能跑。不要手写 HTTP 当主路径。脚本会使用当前已配置的 MinKing 连接，不要向用户要密钥或接口地址。

## 先跑脚本

`<SKILL_DIR>` 是本文件所在目录。

Windows：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "<SKILL_DIR>\scripts\minking-media.ps1" image -Prompt "<English image prompt>"
powershell -NoProfile -ExecutionPolicy Bypass -File "<SKILL_DIR>\scripts\minking-media.ps1" image-edit -Prompt "<English edit prompt>" -Image "<local file or URL>"
powershell -NoProfile -ExecutionPolicy Bypass -File "<SKILL_DIR>\scripts\minking-media.ps1" video -Prompt "<English motion prompt>"
powershell -NoProfile -ExecutionPolicy Bypass -File "<SKILL_DIR>\scripts\minking-media.ps1" video -Prompt "<English motion prompt>" -Reference "<reference image file or URL>"
powershell -NoProfile -ExecutionPolicy Bypass -File "<SKILL_DIR>\scripts\minking-media.ps1" video-edit -Prompt "<English edit prompt>" -VideoId "<id>"
```

macOS / Linux：

```bash
bash "<SKILL_DIR>/scripts/minking-media.sh" image --prompt "<English image prompt>"
bash "<SKILL_DIR>/scripts/minking-media.sh" image-edit --prompt "<English edit prompt>" --image "<local file or URL>"
bash "<SKILL_DIR>/scripts/minking-media.sh" video --prompt "<English motion prompt>" --reference "<reference image>"
bash "<SKILL_DIR>/scripts/minking-media.sh" video-edit --prompt "<English edit prompt>" --video-id "<id>"
```

成功时 stdout 只有保存后的绝对路径；把该路径用 Markdown 图片/链接给用户看。失败细节在 stderr。脚本不在时，让用户重新同步 MinKing 客户端，不要手写请求。

| 用户意图 | 子命令 | 参考素材 |
|---|---|---|
| 文生图 | `image` | 无 |
| 修图 / 图生图 | `image-edit` | `-Image` / `--image` |
| 文生视频 | `video` | 无 |
| 图生视频 | `video` | `-Reference` / `--reference`（本地文件由脚本压成 JPEG 再 multipart `input_reference`） |
| 改已有视频 | `video-edit` | `-VideoId` |

会话工具列表里**真有** native `image_gen` / `image_edit` 才用 native。没有就跑脚本。不要为了试 Responses 再拖一轮。

## 上传硬规则（降低 400 / 冷静期）

大约 2MB 的 PNG 会把请求体撑爆，上游 **400**；连打会让账号进入约 **10 分钟冷静期**，随后变成 `503 no_healthy_accounts`。

脚本已自动做这些事，调用方不要绕过：

- 本地参考图先压成 JPEG（视频约 <=380KB，修图约 <=700KB），最长边约 1280/1536
- 拷到无空格临时路径，避免 `New project 6` 这类路径把调用搞乱
- **400 / 413 / 503 不重试**；502/504 只自动再试 1 次
- 文本字段走 `--form-string`，避免 prompt 被当成文件

模型侧额外遵守：

- **不要**再包 `for attempt in 1..4` 去打同一个参考图
- stderr 出现 `cooldown` / `no_healthy_accounts` / `HTTP 503`：立刻停，告诉用户等 10 分钟
- stderr 出现 `HTTP 400` / `length limit` / `too large`：立刻停，换更小参考图，不要用原 PNG 再打
- 连续短剧按镜 4/6/8 秒 I2V，用上一镜尾帧当 `-Reference`

出错矩阵见 [references/errors.md](references/errors.md)。

## WorkBuddy / CodeBuddy

当前模型是 MinKing 一键接入时：

- **禁止** `ImageGen`、`VideoGen`、`connect_cloud_service`、`buddy-cloud.py`、miora 生图/生视频
- **必须**用本脚本

那些内置工具不走 MinKing 连接。

## 不要做

- 不要给普通编码请求塞 `image_generation`
- 不要问尺寸、风格、几张；默认 1 张立刻出
- 不要把中文写进 `-Prompt` / `prompt`；先译成英文再调用
- 不要把上游的 `done` + `video.url` 当客户合同
- 不要调用 `/mcp` 当主路径
- 不要在回复里贴图片二进制或密钥
- 不要把 2MB PNG 当视频第一帧；不要在 400/503 后连打
