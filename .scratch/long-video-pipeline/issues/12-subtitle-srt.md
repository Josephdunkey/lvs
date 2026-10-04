# 12: 字幕 —— SRT 生成与 whisper 回退

**What to build:** 用 `edge-tts` 的词边界（WordBoundary）时间戳，把逐镜旁白转成 `subtitle.srt`；当词边界不可用时（非 edge 后端、或接口未返回），回退用 `faster-whisper` 对音频做对齐再产出 SRT。

**Blocked by:** 10

**Status:** done

- [ ] `subtitle.srt` 行数 > 0，时间戳单调递增，覆盖全片
- [ ] 字幕文本与 `shots.json` 的 `narration` 一致（不做改写、不丢字）
- [ ] 回退路径可被显式触发并跑通（用开关模拟词边界缺失）
- [ ] 单行字幕长度有上限，过长行被合理断行（不溢出画面）
- [ ] 字幕不包含任何 excluded 区段内容

---

## Comments

**What was built:** `subtitle.srt` 生成 + 回退路径。

**交付记录**
- 有词边界时：构 `(累计字数, 时间)` 曲线 → 按**字符位置插值**取每行字幕的起止时间，文本直接取自 `narration`（**不做改写、不丢字**，单测断言拼回等于原文）
- 无词边界时：按字数比例估算到该镜时长（`voice.align=estimate` 可强制）
- 安装了 `faster-whisper` 时：自动对缺时间戳的镜头做**逐镜词级强制对齐**（票据 21 的路径）
- 单行字幕上限 `voice.subtitle_max_chars`（默认 18），在标点处断行
- SRT 时间**单调递增**（构造时强制去重叠），行数 > 0
- 字幕只由 `shots.json` 的 `narration` 生成 → 天然不含 excluded 区段
