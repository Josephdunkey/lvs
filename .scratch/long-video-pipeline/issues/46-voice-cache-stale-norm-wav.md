# 46 · voice 缓存放行旧 `norm/*.wav`，导致整轨与字幕时间轴错位

Status: open
Type: bug
Severity: high（成片级事故：音画+字幕整体错位、结尾丢内容）

## 现象

任务 `NW01P1`（《挪威的森林》P1 讲书版）成片「严重字幕语音不同步」。
实测三个时长对不上：

| 产物 | 时长 |
|---|---|
| `.work/NW01P1/audio/narration.mp3`（整轨） | **1100.83s** |
| `.work/NW01P1/subtitle.srt` 时间轴跨度 | **1167.9s** |
| `.work/NW01P1/video-track.mp4` | **1168.87s** |
| `.work/NW01P1/final.mp4` | 1103.23s（被 `-shortest` 截断） |

## 根因

`audio/` 下同时存在两套逐镜产物：

- `shot-NNN.mp3` —— 本次 TTS 输出
- `norm/shot-NNN.wav` —— 归一化 + 补静音后的 wav，**拼接实际用的是这一套**（见 `audio/concat.txt`）

逐镜实测（60 镜）：

| 套 | 合计 |
|---|---|
| `norm/shot-001..060.wav` | **1083.07s** |
| `shot-001..060.mp3` | **1151.18s** |

1083.07 + 59×0.3(gap) = **1100.77s** ≈ 整轨 1100.83s → **拼接用的全是旧 wav**。

而 `shots.json` 的 `audio_duration / start / end`（整轨 1168.88s）按 **mp3** 算。
`norm/` 里还留着 **shot-061..066**（上一版的镜数）→ 确认 `norm/` 是**上一版残留**。

流程上发生的事：

1. TTS 配置变更（音色 / rate / pitch），或文本变更
2. 重跑 `lvs voice`：`mp3` 按文本 hash 判定 → 重建；`norm/*.wav` 的跳过判定**没有跟着失效** → 沿用旧 wav
3. 拼接用旧 wav（约 1083s），`shots.json` 时间轴按新 mp3（1151s）
4. `build` 按 `shots.json` 出画面轨 1168.87s，混音 `-shortest` 取到 1100.83s
   → 结尾丢 65s，**并从丢镜处开始音画/字幕整体漂移**
5. 顺带后果：旧 wav 就是旧音色，用户听到的仍是「苍老」的声线

## 影响面

- `NW01P1` 中招（已修：整目录移走 + `voice --force` + `build --force`）
- 其他任务（`NW02`–`NW05`）为新任务目录、无残留，未中招
- **任何「改了 tts 配置 / 改了讲书稿后才重跑 voice」的任务都会中招**

## 复现

```bash
# 改 config.toml 的 [tts] pitch 或 rate
.venv/Scripts/python.exe -m lvs voice --task <已跑过的任务>   # 不 --force
# → audio/norm/*.wav 与 shot-*.mp3 时长不一致
```

校验脚本（判据：两者合计差 > 0.5s 即异常）：

```python
import json, subprocess, wave, contextlib, os
d = ".work/NW01P1/audio"
# sum(wave_len(f"{d}/norm/shot-{i:03d}.wav")) + 59*gap  vs  sum(mp3_len(...))
```

## 建议修法（按优先级）

1. **缓存键带上 TTS 参数指纹**：把 `voice/rate/pitch/volume/backend` 的哈希写进
   `audio/.voice-key.json`；与当前配置不符 → 整目录（`*.mp3` + `norm/` + `narration.mp3` + `concat.txt`）全部失效。
   这是根修，成本最低。
2. **`norm/` 跟随 mp3 失效**：mp3 被重建时，同步删除对应 `norm/shot-NNN.wav`。
   现有 `--force` 语义也应删 `norm/`。
3. **拼接后自检**：`concat` 完成即断言
   `|len(narration.mp3) - (Σaudio_duration + (n-1)*gap)| < 0.5s`，
   超差直接报错退出，别把坏轨交给 `build`。
4. **`build` 侧兜底**：混音前比对 `video-track` 与 `narration` 时长，偏差 > 1s 就拒绝 `-shortest` 并告警。
   现有 `build` 已经在正常任务上打印「偏差 0.06s」，把它变成**硬校验**即可。

## Comments

- 2026-09-30 · 发现于 `NW01P1` 重做。修法 1 与 3 建议同时做：1 防复发，3 防误放行。
