# MinKing 生图/生视频失败怎么处理

网关把本地参考图转成 JSON data:image;base64 再打 Grok。大约 2MB 的 PNG 会变成约 2.7MB JSON，上游返回 400；同一账号连续失败会进入 gateway_cooldown_seconds=600 冷静期，之后变成 503 no_healthy_accounts。

脚本已经在上传前把参考图压成 JPEG 并拷到无空格临时路径。调用方不要再包一层 4 次重试。

## 模型决策

| stderr 信号 | 做什么 | 不要做什么 |
|---|---|---|
| HTTP 400 / 413 / length limit / file_too_large / too large | 停。换更小的参考图或只改英文 prompt 后再打。 | 不要用同一张 PNG/同一路径再打。 |
| HTTP 503 / no_healthy_accounts / cooldown | 停。告诉用户等约 10 分钟。 | 不要轮询、不要换镜头继续砸视频接口。 |
| HTTP 502 / 504 | 脚本已自动再试 1 次。仍失败就停并汇报。 | 不要再套 3–4 次循环。 |
| HTTP 409 / video_not_ready | 继续 poll content，这是未完成。 | 不要当成生成失败。 |
| 404 unsupported_capability | 如实说当前上游没有视频能力。 | 不要改去 WorkBuddy ImageGen。 |

## 连续短剧

- 按镜 4 / 6 / 8 秒 I2V，不要一次生成超长视频。
- 下一镜用上一镜尾帧；脚本会压 JPEG，不必先手转。
- 原生视频音轨不要当真对白；对白另外配。
