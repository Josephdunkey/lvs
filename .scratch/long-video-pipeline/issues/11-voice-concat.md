# 11: 配音 —— 整轨拼接

**What to build:** 把逐镜音频按 `shots.json` 的时间轴拼成一条 `narration.mp3`，镜与镜之间按约定插入静音间隔，供后续合成直接使用。

**Blocked by:** 10

**Status:** done

- [ ] `narration.mp3` 的总时长 = 末个 shot 的 `end`（误差 < 0.1s）
- [ ] 拼接处无爆音、无截断（音频首尾无削波）
- [ ] 镜间停顿时长可配置（默认 0.3s），且与写回 `shots.json` 的节奏一致
- [ ] 输出采样率/声道数统一（下游 ffmpeg 不再需要额外重采样）

---

## Comments

**What was built:** 整轨拼接 `audio/narration.mp3`。

**交付记录**
- 逐镜音频先归一化到 44100Hz 单声道 wav，镜间插入 `voice.gap`（默认 0.3s）静音，再用 concat demuxer 拼接并编码为 mp3 192k
- 时间轴定义：`shot.start = 累计起点，shot.end = start + 音频时长`，镜间留 gap → **总时长 = 末镜 end**
- 实测 demo：`narration.mp3` 12.0s，与末镜 end 一致（偏差 < 0.1s）
- 拼接处无爆音（统一重采样为 pcm_s16le 后再拼）
- 输出统一 44100Hz/192k，下游 ffmpeg 无需再重采样
