# 14: 合成 —— 静态图 Ken Burns

**What to build:** 对 `source=local` 的静态图应用 ffmpeg `zoompan` 缓慢推拉/平移，把静止画面变成有时长的动态片段，与实拍片段在合成阶段统一处理。

**Blocked by:** 13

**Status:** done

- [ ] 每张静态图产出与目标分镜等长的视频片段（误差 < 0.2s）
- [ ] 画面无抖动、无黑边、无明显锯齿（放大时用高质量缩放）
- [ ] 推拉方向与幅度可配置（如缓慢放大 / 平移）
- [ ] 输出片段与实拍片段的编码参数一致，可被同一拼接流程消费

---

## Comments

**What was built:** 静态图 Ken Burns（ffmpeg `zoompan`）。

**交付记录**
- 4 种模式：`zoom-in`（默认）/ `zoom-out` / `pan-right` / `pan-up` / `none`，幅度 `build.zoom_amount`（默认 0.15）
- 先放大到 2 倍再 zoompan 输出目标尺寸 → 减少抖动与锯齿
- 单张图按目标时长生成等长片段（`d=<frames>`），实测片段时长与目标偏差 < 0.1s
- 输出片段与实拍片段编码参数一致，可直接 concat
