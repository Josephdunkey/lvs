# 11: 配音 —— 整轨拼接

**What to build:** 把逐镜音频按 `shots.json` 的时间轴拼成一条 `narration.mp3`，镜与镜之间按约定插入静音间隔，供后续合成直接使用。

**Blocked by:** 10

**Status:** ready-for-agent

- [ ] `narration.mp3` 的总时长 = 末个 shot 的 `end`（误差 < 0.1s）
- [ ] 拼接处无爆音、无截断（音频首尾无削波）
- [ ] 镜间停顿时长可配置（默认 0.3s），且与写回 `shots.json` 的节奏一致
- [ ] 输出采样率/声道数统一（下游 ffmpeg 不再需要额外重采样）
