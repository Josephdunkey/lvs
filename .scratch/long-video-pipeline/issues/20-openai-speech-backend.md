# 20: 本地 TTS 后端 —— OpenAISpeechBackend（接入 `D:\qwentts`）

**What to build:** 把 `lvs voice` 的配音后端从 `EdgeTTSBackend` 抽象为可插拔；新增 `OpenAISpeechBackend`，指向本机 OpenAI 兼容的语音服务，即 `D:\faster-qwen3-tts-main\examples\openai_server.py`（模型来自 `D:\qwentts\models\...`）。**换后端 = 改配置里的 `base_url` + `backend`，不改流水线代码。**

**Blocked by:** 05, 17

**Status:** ready-for-agent

**契约（见 spec §10、§11）**：

- 统一接口：`POST {base_url}/v1/audio/speech`，body `{input, voice, response_format}`；支持 `/health` 探活
- 后端选择：`config.toml [tts].backend ∈ {edge, openai_speech}`、`[tts].base_url`、`[tts].voice`
- 逐分镜合成 → 拼接，产物与 edge 路径**同构**（`audio/shot-NNN.wav` + `narration.*`），下游 `build` 无感
- 启动/停止该服务由用户手动（不在流水线内自动拉起，避免显存抢占，见 §11）

- [ ] `[tts].backend=edge` 时行为与票 05 完全一致（回归不破）
- [ ] `[tts].backend=openai_speech` 且服务就绪时，逐镜产出音频并拼接成功
- [ ] 服务未启动/探活失败：给出中文错误，指出要启动的命令与端口，**不静默失败**
- [ ] 配置了 `voices.json` 的多音色可被选用（至少一个非默认音色跑通）
- [ ] 与 `lvs build` 串联后 `final.mp4` 音画同步（偏差 < 1 秒）
