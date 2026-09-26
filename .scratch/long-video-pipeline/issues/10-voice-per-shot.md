# 10: 配音 —— 逐镜 edge-tts 合成与时长写回

**What to build:** `lvs voice` 通过**可插拔的 TTS 后端接口**（本票只需实现 `edge-tts` 适配器）逐镜合成音频到 `audio/shot-NNN.mp3`，并把每个 shot 的实际音频时长累加，写回 `shots.json` 的 `start` / `end`。后端接口的调用形态对齐 OpenAI `/v1/audio/speech` 的语义（`input` / `voice` / 输出音频），以便二期换成 `OpenAISpeechBackend` 时零改动。

**Blocked by:** 08

**Status:** ready-for-agent

- [ ] 每个 shot 对应一个音频文件，命名与 shot id 对应
- [ ] `shots.json` 的 `start`/`end` 由音频时长累加得出，且严格单调递增
- [ ] 单个 shot 合成失败可重试，不影响已成功的其他 shot
- [ ] TTS 后端以接口 + 实现的方式组织，新增后端不需要改调用方代码
- [ ] 音色（voice）可通过配置指定
