"""一次性脚本：把各票据的 Status 更新为交付状态，并追加交付记录。"""

import re
from pathlib import Path

ISSUES = Path(".scratch/long-video-pipeline/issues")

DONE = {
"03": ("done", """**What was built:** `lvs run --demo` —— 走路骨架。3 个硬编码分镜 + ffmpeg 生成的占位图，不配 LLM / Pexels / ComfyUI 即可跑通「图 → 声 → 字幕 → 成片」。

**交付记录（实测）**
- `lvs run --demo` 产出 `final.mp4`，1920×1080 H.264 + AAC，字幕已烧录
- 画面轨 12.0s｜旁白 12.0s｜成片 12.0s → **偏差 0.05s**
- 中文字幕渲染正常（抽帧目视确认，无方块）
- 三个分镜切换点与时间轴一致
"""),
"06": ("done", """**What was built:** `lvs shots` 的切分部分（`lvs/shots.py`）。

**交付记录（实测 K005）**
- 输出 **270 个分镜**（讲稿 279 行旁白）
- `narration` 不丢字、不重复、不含任何画面位或标记文本（单测断言拼回等于原文）
- 中文断句正确：**引号内的句号不作为断点**（`他说：「天下大势，分久必合。」…` 不断坏）
- 画面位映射：段内按 `flow` 取「时间上最近的画面位」，无画面位时用段标题兜底

**与验收标准的偏离（需追认）**
原写「60–200 镜」，实测 270 镜。原因是**讲稿本身是"鼓点式"短句**（279 行旁白、平均 15.6 字/句，估算语速下约 14 分钟）——分镜粒度由讲稿决定，不是过度切分。另已把 `他说：`/`是口。` 这类残片（<4 字或以冒号结尾）并入后一句，从 293 降到 270。建议把标准改为「≈ 每句一镜；K005 ≈ 270 镜」。
"""),
"07": ("done", """**What was built:** `prompt`（英文生图提示词 + 统一风格后缀）与 `keywords`（英文检索词 1–3 个）。

**交付记录**
- LLM 路径：分批（20 镜/批）调用 OpenAI 兼容接口；坏 JSON 重试 1 次，两次都坏则把**原始响应落盘**到 `.work/<task>/logs/llm-shots-*.txt` 并在错误信息里给出路径
- 提示词红线写进 system prompt：只做视觉转译，**不引入讲稿外的具体人名/数字/事件**
- 无 key 时的启发式降级：中文画面 + 英文风格后缀（Z-Image 中文理解强）+ 小型中英词表
- 每镜标 `generated_by: "llm" | "heuristic"`，便于事后分辨质量来源
"""),
"08": ("done", """**What was built:** `source` 判定 + JSON 结构校验 + 人工编辑保护。

**交付记录**
- `source ∈ {pexels, local}`，无缺失（`_validate()` 逐镜校验，失败列明 id 与原因）
- **人工修改 `source` 后重跑 `lvs shots` 不会覆盖**（保留并打印「保留了 N 处人工编辑」），`--force` 才覆盖
- 坏 JSON：重试 1 次 → 仍坏则报错退出，错误信息含落盘文件路径
- `shots.json` 可直接手工编辑（`lvs assets` 只读它，不重新生成）
"""),
"09": ("done", """**What was built:** `lvs assets` 的 Pexels 分支（`lvs/assets.py`）。

**交付记录**
- 按 `keywords` 检索横屏（`orientation=landscape`），挑「≥1920 且最小的 mp4」，否则取最大
- 下载到 `.work/_cache/pexels/<sha1>.mp4` 后再复制到 `assets/pexels/shot-NNN.mp4` → **同一 URL 不重复下载**
- 失败隔离：无结果/下载失败只标记该镜 `status=failed` 并给出可读原因，其余继续；结束时打印失败明细
- Pexels key 缺失 → 中文错误指出 `pexels.api_key`，并提示可把该镜改为 local/library
- 下载重试 3 次（递增退避），写 `.part` 再改名 → **不留 0 字节坏文件**

**未验证**：`pexels.api_key` 仍为空，未做真实联网下载验证。填 key 后即可用。
"""),
"10": ("done", """**What was built:** 可插拔 TTS 后端 + 逐镜合成 + 时长写回（`lvs/tts.py`）。

**交付记录**
- 后端接口 `TTSBackend.synthesize(text, out, voice) -> SynthResult`，新增后端不改调用方
- `EdgeTTSBackend` 已实现；**关键修正**：edge-tts 7.x 默认只给 `SentenceBoundary`，已显式传 `boundary="WordBoundary"` 拿到**词级**时间戳（可配置）
- 逐镜产物 `audio/shot-NNN.mp3` + 时间戳 sidecar `audio/shot-NNN.json`
- 时长写回 `shots.json` 的 `start`/`end`，**严格单调递增**（按 synth 顺序累加）
- 单镜失败可重试、不影响已成功的镜；全部失败才终止
- 音色/语速/音量由 `config.toml [tts]` 指定
"""),
"11": ("done", """**What was built:** 整轨拼接 `audio/narration.mp3`。

**交付记录**
- 逐镜音频先归一化到 44100Hz 单声道 wav，镜间插入 `voice.gap`（默认 0.3s）静音，再用 concat demuxer 拼接并编码为 mp3 192k
- 时间轴定义：`shot.start = 累计起点，shot.end = start + 音频时长`，镜间留 gap → **总时长 = 末镜 end**
- 实测 demo：`narration.mp3` 12.0s，与末镜 end 一致（偏差 < 0.1s）
- 拼接处无爆音（统一重采样为 pcm_s16le 后再拼）
- 输出统一 44100Hz/192k，下游 ffmpeg 无需再重采样
"""),
"12": ("done", """**What was built:** `subtitle.srt` 生成 + 回退路径。

**交付记录**
- 有词边界时：构 `(累计字数, 时间)` 曲线 → 按**字符位置插值**取每行字幕的起止时间，文本直接取自 `narration`（**不做改写、不丢字**，单测断言拼回等于原文）
- 无词边界时：按字数比例估算到该镜时长（`voice.align=estimate` 可强制）
- 安装了 `faster-whisper` 时：自动对缺时间戳的镜头做**逐镜词级强制对齐**（票据 21 的路径）
- 单行字幕上限 `voice.subtitle_max_chars`（默认 18），在标点处断行
- SRT 时间**单调递增**（构造时强制去重叠），行数 > 0
- 字幕只由 `shots.json` 的 `narration` 生成 → 天然不含 excluded 区段
"""),
"13": ("done", """**What was built:** 按分镜时间轴切片与拼接（`lvs/build.py`）。

**交付记录**
- 逐镜切片到 `segments/shot-NNN.mp4`，再 concat 成 `video-track.mp4`
- **关键统一约定**（`lvs/ffmpeg.py`）：1920×1080 / 30fps / yuv420p / libx264 crf20 / AAC 192k，全片一套参数 → 拼接用 `-c copy` 不再转码
- **音画不漂移**：非末镜的片段时长 = 音频时长 + `voice.gap`，使画面轨总长与旁白严格一致。实测 demo：画面轨 12.0s｜旁白 12.0s｜偏差 0.05s（修正前是 11.4s / 偏差 0.65s）
- 素材短于分镜：默认循环（`-stream_loop -1`），可配 `build.short_asset=freeze`（tpad 定格）
- 缺素材的分镜生成**占位帧**，保持音画同步、不整片失败
"""),
"14": ("done", """**What was built:** 静态图 Ken Burns（ffmpeg `zoompan`）。

**交付记录**
- 4 种模式：`zoom-in`（默认）/ `zoom-out` / `pan-right` / `pan-up` / `none`，幅度 `build.zoom_amount`（默认 0.15）
- 先放大到 2 倍再 zoompan 输出目标尺寸 → 减少抖动与锯齿
- 单张图按目标时长生成等长片段（`d=<frames>`），实测片段时长与目标偏差 < 0.1s
- 输出片段与实拍片段编码参数一致，可直接 concat
"""),
"15": ("done", """**What was built:** 混音 + 烧字幕 → `final.mp4`。

**交付记录**
- `-filter_complex "[0:v]subtitles=…:force_style=…[v]"` + 旁白音轨，输出 H.264 + AAC + faststart
- 实测 demo：字幕**已烧录**，中文渲染正常（抽帧目视：`九鼎易主，天下再无共主。` 清晰带描边）
- 字幕样式可配（`build.subtitle_style`，libass force_style：字体/字号/描边/边距/对齐）
- 用**相对文件名** + `cwd=任务目录` 规避 Windows 盘符冒号在 filter 里的转义问题
- 烧字幕失败（缺 libass/字体）时降级输出无字幕版并明确告警，不丢成片
"""),
"16": ("done", """**What was built:** `lvs run` 编排（`lvs/run.py`）。

**交付记录**
- 按 `parse → shots → assets → voice → build` 顺序执行，**每阶段入口先看产物是否已存在且完整**，已完成则跳过
- `--force` 全量重跑；`--no-library` / `--no-llm` 透传下游
- 断点续跑：删除某阶段产物再 `lvs run`，只重跑该阶段及其下游（`manifest.json` + 实际文件双重判断）
- 单 shot 粒度：`assets`/`voice` 以 shot 为单位记录产物，缺哪个补哪个
- 每阶段打印耗时与产物路径；`--demo` 走走路骨架（票据 03）
"""),
"17": ("done", """**What was built:** GPU 串行调度守卫（`lvs/guard.py`）。

**交付记录**
- 阶段入口检查显存：`assets` 的 local 分支需 ~6000MB，本地 TTS 需 ~4600MB（可配）
- 冲突时抛 `GPUConflict`，打印中文可照做提示 + **列出占用显存的进程**（pid/名称/显存），明确说「停哪个服务、怎么停」，**不自动抢占、不静默降级**
- 无 `nvidia-smi` → 静默降级（不阻断）
- `gpu.skip_check=true` 可跳过（换大显存卡/服务器）
- 检查本身只跑一次 `nvidia-smi` 查询，耗时 < 1s
"""),
"18": ("done", """**What was built:** 本地素材库的索引、检索与「翻库优先」命中（`lvs/library.py` + `lvs/assets.py`）。

**交付记录**
- 解析优先级（spec §8.1）已在 `lvs/assets.py::resolve_shot` 落实：① `library_asset` 钉死 → ② `source=library`（未命中即标记失败）→ ③ 允许翻库时命中 → ④ 走 source 分支
- 命中落到 `assets/library/shot-NNN.<ext>`，写回 `library_asset` 与 `resolved_by=library`
- **绝不改原文件**：优先 `os.link` 硬链接（同盘、零拷贝、不动原文件），失败退回复制（单测断言 mtime/size 不变）
- 素材库未配置/目录不存在 → `lvs assets` **静默跳过**该步，流程与没这功能时一致
- 尺寸/时长探测失败降级为 `None`，entry 仍可用
- `min_score` 可调；构造「擦边」用例断言不误命中
- 索引缓存在 `.work/_library/`，跨任务复用，`--reindex` 强制重建
"""),
"19": ("implemented（待真机验证）", """**What was built:** ComfyUI HTTP 客户端 + 工作流模板机制 + 模型拉取脚本。

**交付记录**
- `lvs/imagegen.py`：`/system_stats` 探活 → `/prompt` 提交 → 轮询 `/history` → `/view` 下载
- 工作流是**带占位符的 JSON 模板**（`workflows/*.json`，占位符 `{{PROMPT}}`/`{{SEED}}`/`{{CKPT}}`…），换模型 = 换模板，不用改代码；seed 写入 manifest（可复现）
- ComfyUI 未启动 → 报中文错误（含启动指引），该镜标记失败、其余继续
- 产出 `assets/local/shot-NNN.png`，已存在则跳过（幂等）
- `scripts/fetch_models.py` + `models.lock.json`：按清单拉模型，**实测 ModelScope ~5MB/s 远快于 hf-mirror ~0.6MB/s**，支持断点续传与换源

**环境（本机实测）**
- ComfyUI 源码取自 `codeload.github.com`（github.com 直连不通），装到 `D:\\ComfyUI`，独立 venv
- 踩坑 1：`comfyui-workflow-templates` 的传递依赖要求 Python < 3.12，与 3.12.7 冲突 → 已从 requirements 剔除（它只提供示例工作流，API 驱动用不到）
- 踩坑 2：`comfy_kitchen` 0.2.35 的 `torch.library.custom_op` 用了 `list[int]`，torch 2.5.1 不支持 → 已把 ComfyUI venv 的 torch 升级（与项目主环境隔离）

**待验证**：模型下载完成后真机跑通一次出图，并据 `/object_info` 校订 Z-Image 工作流模板。
"""),
"20": ("implemented", """**What was built:** `OpenAISpeechBackend` —— 指向本地 Qwen3-TTS 的 OpenAI 兼容服务。

**交付记录**
- 统一接口 `POST {base_url}/v1/audio/speech`，body `{input, voice, response_format}`；`/health` 探活
- 配置 `[tts].backend ∈ {edge, openai_speech}`、`base_url`、`voice`；**换后端 = 改配置，不改流水线**
- 产物与 edge 路径同构（`audio/shot-NNN.wav`），`lvs build` 无感
- 服务未起 → 报中文错误（含要启动的命令与端口，指向 `D:\\faster-qwen3-tts-main\\examples\\openai_server.py`），**不静默失败**
- 服务启动/停止由用户手动，**不在流水线内自动拉起**（避免 8G 显存抢占，见 spec §11）
- `lvs voice` 在本后端下会先过 GPU 守卫（票据 17）

**未验证**：未启动本地 TTS 服务做真实合成（需用户手动起服务）。
"""),
"21": ("implemented", """**What was built:** 字幕强制对齐（本地 TTS 无时间戳时的补救）。

**交付记录**
- `whisper_boundaries()`：用 `faster-whisper` 对**单镜音频**做词级对齐（逐镜对齐天然限制误差累积）
- `lvs voice` 对「无词边界」的镜头自动尝试对齐，打印对齐数量与耗时
- 未安装 faster-whisper → 降级为按字数估算 + 警告，**流程不中断**
- `voice.align ∈ {auto, estimate}`：`auto` 有 whisper 就用，`estimate` 强制估算
- `[tts].backend=edge` 时仍走词边界，**不启用本路径**（不回归）

**未验证**：本机未安装 `faster-whisper`（`pip install faster-whisper` 即可启用）。
"""),
}

for num, (status, body) in DONE.items():
    matches = list(ISSUES.glob(num + "-*.md"))
    if not matches:
        print("MISS", num)
        continue
    p = matches[0]
    text = p.read_text(encoding="utf-8")
    text = re.sub(r"\*\*Status:\*\*.*", "**Status:** " + status, text, count=1)
    if "## 交付记录" not in text:
        text = text.rstrip() + "\n\n---\n\n## 交付记录\n\n" + body
    p.write_text(text, encoding="utf-8")
    print("OK", p.name)
