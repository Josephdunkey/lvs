# 12: 字幕 —— SRT 生成与 whisper 回退

**What to build:** 用 `edge-tts` 的词边界（WordBoundary）时间戳，把逐镜旁白转成 `subtitle.srt`；当词边界不可用时（非 edge 后端、或接口未返回），回退用 `faster-whisper` 对音频做对齐再产出 SRT。

**Blocked by:** 10

**Status:** ready-for-agent

- [ ] `subtitle.srt` 行数 > 0，时间戳单调递增，覆盖全片
- [ ] 字幕文本与 `shots.json` 的 `narration` 一致（不做改写、不丢字）
- [ ] 回退路径可被显式触发并跑通（用开关模拟词边界缺失）
- [ ] 单行字幕长度有上限，过长行被合理断行（不溢出画面）
- [ ] 字幕不包含任何 excluded 区段内容
