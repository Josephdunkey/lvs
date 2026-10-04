# 10: 配音 —— 逐镜 edge-tts 合成与时长写回

**What to build:** `lvs voice` 通过**可插拔的 TTS 后端接口**（本票只需实现 `edge-tts` 适配器）逐镜合成音频到 `audio/shot-NNN.mp3`，并把每个 shot 的实际音频时长累加，写回 `shots.json` 的 `start` / `end`。后端接口的调用形态对齐 OpenAI `/v1/audio/speech` 的语义（`input` / `voice` / 输出音频），以便二期换成 `OpenAISpeechBackend` 时零改动。

**Blocked by:** 08

**Status:** done

- [ ] 每个 shot 对应一个音频文件，命名与 shot id 对应
- [ ] `shots.json` 的 `start`/`end` 由音频时长累加得出，且严格单调递增
- [ ] 单个 shot 合成失败可重试，不影响已成功的其他 shot
- [ ] TTS 后端以接口 + 实现的方式组织，新增后端不需要改调用方代码
- [ ] 音色（voice）可通过配置指定

---

## Comments

**What was built:** 可插拔 TTS 后端 + 逐镜合成 + 时长写回（`lvs/tts.py`）。

**交付记录**
- 后端接口 `TTSBackend.synthesize(text, out, voice) -> SynthResult`，新增后端不改调用方
- `EdgeTTSBackend` 已实现；**关键修正**：edge-tts 7.x 默认只给 `SentenceBoundary`，已显式传 `boundary="WordBoundary"` 拿到**词级**时间戳（可配置）
- 逐镜产物 `audio/shot-NNN.mp3` + 时间戳 sidecar `audio/shot-NNN.json`
- 时长写回 `shots.json` 的 `start`/`end`，**严格单调递增**（按 synth 顺序累加）
- 单镜失败可重试、不影响已成功的镜；全部失败才终止
- 音色/语速/音量由 `config.toml [tts]` 指定
