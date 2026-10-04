# 13: 合成 —— 按分镜时间轴切片与拼接

**What to build:** `lvs build` 按 `shots.json` 的 `start`/`end` 把每个 shot 的素材精确切出对应时长，顺序拼接成一条无声画面轨。

**Blocked by:** 03

**Status:** done

- [ ] 输出画面轨时长与 `narration.mp3` 偏差 < 1 秒
- [ ] 每个分镜切换点与 `shots.json` 的时间轴一致（抽样 3 处比对）
- [ ] 素材短于分镜时长时的策略明确且稳定（循环或定格，可配置）
- [ ] 统一编码参数（分辨率、帧率、像素格式）并写到同一约定，避免下游再次转码

---

## Comments

**What was built:** 按分镜时间轴切片与拼接（`lvs/build.py`）。

**交付记录**
- 逐镜切片到 `segments/shot-NNN.mp4`，再 concat 成 `video-track.mp4`
- **关键统一约定**（`lvs/ffmpeg.py`）：1920×1080 / 30fps / yuv420p / libx264 crf20 / AAC 192k，全片一套参数 → 拼接用 `-c copy` 不再转码
- **音画不漂移**：非末镜的片段时长 = 音频时长 + `voice.gap`，使画面轨总长与旁白严格一致。实测 demo：画面轨 12.0s｜旁白 12.0s｜偏差 0.05s（修正前是 11.4s / 偏差 0.65s）
- 素材短于分镜：默认循环（`-stream_loop -1`），可配 `build.short_asset=freeze`（tpad 定格）
- 缺素材的分镜生成**占位帧**，保持音画同步、不整片失败
