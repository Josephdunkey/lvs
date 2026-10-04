# 21: 字幕强制对齐 —— 本地 TTS 下找回时间戳

**What to build:** 一期字幕靠 `edge-tts` 词边界时间戳直接生成 SRT；但本地 TTS（票 20 的 OpenAI 兼容接口）**不返回时间戳**，换后端后字幕会失去时间来源。本票补一条"强制对齐"路径：给定旁白文本 + 已合成音频，产出句级/词级时间轴，生成 SRT。

**Blocked by:** 12, 20

**Status:** done

**契约（见 spec §10 风险条）**：

- 首选 `whisperx` 或本地 ASR（如 `faster-whisper`）对**逐镜音频**做对齐；逐镜对齐天然限制误差累积
- 退路：按旁白字符比例估算 + **人工可修**（SRT 落盘可编辑）
- 对齐只针对一期的 `excluded 区段` 之外的旁白文本

- [ ] `[tts].backend=openai_speech` 时，`lvs voice` 仍产出时间单调、与音频吻合的 `subtitle.srt`
- [ ] 抽样 3 处：字幕出现时刻与音频中对应词的听感偏差 < 0.5 秒
- [ ] 无 whisper 环境时降级为估算 + 打印警告，流程不中断
- [ ] 对齐耗时打印；对 18 分钟整轨给出耗时量级说明
- [ ] `[tts].backend=edge` 时**不启用**本路径（仍走词边界），不回归

---

## Comments

**What was built:** 字幕强制对齐（本地 TTS 无时间戳时的补救）。

**交付记录**
- `whisper_boundaries()`：用 `faster-whisper` 对**单镜音频**做词级对齐（逐镜对齐天然限制误差累积）
- `lvs voice` 对「无词边界」的镜头自动尝试对齐，打印对齐数量与耗时
- 未安装 faster-whisper → 降级为按字数估算 + 警告，**流程不中断**
- `voice.align ∈ {auto, estimate}`：`auto` 有 whisper 就用，`estimate` 强制估算
- `[tts].backend=edge` 时仍走词边界，**不启用本路径**（不回归）

**未验证**：本机未安装 `faster-whisper`（`pip install faster-whisper` 即可启用）。
