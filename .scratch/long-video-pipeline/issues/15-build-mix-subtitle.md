# 15: 合成 —— 混音与烧字幕

**What to build:** `lvs build` 把无声画面轨与 `narration.mp3` 混流，再按配置的样式把 `subtitle.srt` 烧录进画面，输出最终 `final.mp4`。

**Blocked by:** 13, 12

**Status:** ready-for-agent

- [ ] `final.mp4` 同时包含画面、音频、烧录字幕，可用播放器正常播放
- [ ] 字幕样式（字体、字号、底部边距、描边）可配置
- [ ] 中文字体渲染正常，不出现方块或乱码
- [ ] 音画同步：抽样 3 处，旁白与画面切换对得上
- [ ] 输出为 H.264 + AAC，兼容常见播放器与 B 站上传
