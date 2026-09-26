# 13: 合成 —— 按分镜时间轴切片与拼接

**What to build:** `lvs build` 按 `shots.json` 的 `start`/`end` 把每个 shot 的素材精确切出对应时长，顺序拼接成一条无声画面轨。

**Blocked by:** 03

**Status:** ready-for-agent

- [ ] 输出画面轨时长与 `narration.mp3` 偏差 < 1 秒
- [ ] 每个分镜切换点与 `shots.json` 的时间轴一致（抽样 3 处比对）
- [ ] 素材短于分镜时长时的策略明确且稳定（循环或定格，可配置）
- [ ] 统一编码参数（分辨率、帧率、像素格式）并写到同一约定，避免下游再次转码
