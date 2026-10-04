# 15: 合成 —— 混音与烧字幕

**What to build:** `lvs build` 把无声画面轨与 `narration.mp3` 混流，再按配置的样式把 `subtitle.srt` 烧录进画面，输出最终 `final.mp4`。

**Blocked by:** 13, 12

**Status:** done

- [ ] `final.mp4` 同时包含画面、音频、烧录字幕，可用播放器正常播放
- [ ] 字幕样式（字体、字号、底部边距、描边）可配置
- [ ] 中文字体渲染正常，不出现方块或乱码
- [ ] 音画同步：抽样 3 处，旁白与画面切换对得上
- [ ] 输出为 H.264 + AAC，兼容常见播放器与 B 站上传

---

## Comments

**What was built:** 混音 + 烧字幕 → `final.mp4`。

**交付记录**
- `-filter_complex "[0:v]subtitles=…:force_style=…[v]"` + 旁白音轨，输出 H.264 + AAC + faststart
- 实测 demo：字幕**已烧录**，中文渲染正常（抽帧目视：`九鼎易主，天下再无共主。` 清晰带描边）
- 字幕样式可配（`build.subtitle_style`，libass force_style：字体/字号/描边/边距/对齐）
- 用**相对文件名** + `cwd=任务目录` 规避 Windows 盘符冒号在 filter 里的转义问题
- 烧字幕失败（缺 libass/字体）时降级输出无字幕版并明确告警，不丢成片
