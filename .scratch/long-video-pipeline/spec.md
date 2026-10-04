# Spec: 长视频生成流水线（LongVideoStudio）

> 状态：草案，待用户确认
> 基线参考：`D:\MoneyPrinterTurbo`（仅参考其服务分层，不 fork）
> 目标平台：Windows 11 / RTX 4060 Laptop 8 GB / 16 GB RAM / Python 3.13

---

## 1. 背景与问题

MoneyPrinterTurbo 是"**短视频**"范式：一个主题 → 一段 LLM 生成的短文案 → 少量 Pexels 素材随机拼接 → 成片。它的分镜概念只有一组"搜索词"（`generate_terms`），没有逐镜画面控制。

而我们的内容源是**已成稿的长视频拍摄稿**（14–19 分钟，如 `D:\fanshu\资治通鉴\10-语料\知识视频素材库\05-拍摄稿\*.md`），每篇**自带**分镜意图：

- 正文分六段，每段带时间码（`0:30–1:00`）
- 段内散布 `[画面位]` 行，描述该处该出现什么画面
- 文末有 `三、画面位清单` 表格（`时间 | 画面 | 素材建议`）

**核心问题**：如何把这样一篇结构化讲稿，逐分镜地变成成片——每个分镜的画面自己可控（实拍素材 or 本地生成），配音与字幕逐字对齐，最后合成。

## 2. 目标

1. 输入一篇拍摄稿 `.md`，输出一集成片 `.mp4`
2. 自动拆镜：讲稿 → 结构化分镜列表，每镜含**时间区间、旁白文本、画面描述、生图提示词、素材关键词**
3. 每个分镜可**独立选择**画面来源：`pexels`（下载实拍）、`local`（本地生图）或 `library`（本地素材库），默认自动判定、可手动覆盖
4. **可在 `lvs assets` 时先翻找本地素材库**：用户已经下好/生成好的素材，命中即复用，不必重复下载或生图（素材库文件夹后续指定，缺失则该步自动跳过）
5. 本地生图**离线可用**（ComfyUI + Z-Image Turbo 主力 / SDXL 备用）
6. 配音 + 字幕（SRT），字幕与音频时间轴对齐
7. 用 ffmpeg 合成最终视频（含烧录字幕）
8. **交互形态：CLI 优先**，每一步的中间产物落盘可查、可改、可重跑

## 3. 非目标（本期不做）

- 图形界面（GUI 是后期补齐项，见 §13）
- 视频生成模型（不走 text-to-video，本期画面来源只有"实拍下载"、"本地素材库复用"与"静态图 + 运动"）
- 多用户 / 云端部署 / 任务队列（Redis 等）
- 自动发布到 B 站
- 修改 `D:\fanshu` 下的内容资产（只读引用）

## 4. 领域词汇（CONTEXT 词汇表，初版）

| 术语 | 含义 |
|---|---|
| **拍摄稿** | 输入：一篇结构化讲稿 Markdown（含时间码、`[画面位]`、画面位清单表） |
| **分镜 / shot** | 流水线最小单位：一个时间区间 + 一段旁白 + 一个画面意图 |
| **画面位** | 拍摄稿里的 `[画面位]` 行，描述该处画面内容（分镜的原始依据） |
| **画面位清单** | 拍摄稿文末的表格，分镜的时间/画面/素材建议来源 |
| **素材来源 / source** | 单个分镜的画面取得方式：`pexels`、`local` 或 `library` |
| **素材库 / library** | 用户本机已下好/已生成的素材文件夹（图片 + 视频），配置指定；`lvs assets` 时优先翻找 |
| **素材命中 / library hit** | 某分镜的关键词在素材库索引中匹配到可用文件，该镜不再下载/生图 |
| **旁白 / narration** | 该分镜对应的讲稿文本（要被 TTS 念出来的部分） |
| **重参与点** | 讲稿中的互动设计点（如"如果你是收货的那个账簿…"），不参与字幕 |
| **成片** | 最终输出：画面 + 音频 + 烧录字幕 |

## 5. 输入契约

**主输入**：拍摄稿 `.md`，需存在以下可解析结构（缺失则降级，不报错）：

| 结构 | 用途 | 缺失时降级 |
|---|---|---|
| `## 二、正文讲稿` 下的 `### 【第N段 · …】起–止` | 段落与时间码 | 退化为按空行分段、总时长按字数估算 |
| 段内 `[画面位] …` | 画面描述 | 退化为用该段首句当画面描述 |
| `## 三、画面位清单` 表格 | 分镜时间/画面/素材建议 | 完全依赖 LLM 拆镜 |
| `## 一、传达层` 的标题/封面文案 | 元数据 | 留空 |

**排除**：`[画面位]`、`[重参与点…]`（含其后续整块）、`（转场）`/`（停顿）` 等行内提示、`> 备选：…` 引用块、其他独立 `[…]` 标记（如 `[冷断句]`、`[最大翻转前 5 秒铺垫]`），以及 `四、留存曲线自检`、`五、史实核验与红线` 两个整章，**都不进字幕、不进 TTS**。

> ⚠️ **例外（实现期修正）**：`一、传达层` 章内的 **`【冷开场】` 是 0:00–0:30 的旁白**，不能整章排除，否则会丢掉片头。
> 因此：`【冷开场】` → 抽为 `parse.json` 的 `cold_open`（**作为旁白**）；`【标题】`/`【封面文案】` → 归入 `meta`（元数据，不进旁白）。
> 该决定记入 `parse.json` 的 `notes`，见票据 05 与 `lvs/parse.py` 模块注释。
>
> 行内提示（`（转场）`/`（停顿）`）按**只剥离标记、保留其余文字**处理 —— 它们是提示词，不是整段。

## 6. 数据模型（落盘 JSON）

任务目录：`.work/<task>/`（不进 git）

```
.work/<task>/
├─ source.md            输入副本
├─ parse.json           解析结果（段落/时间码/画面位/排除区段）
├─ shots.json           ★ 分镜列表（人工可编辑的真相源）
├─ audio/
│   ├─ shot-001.mp3 …   逐镜音频
│   └─ narration.mp3    拼接后整轨
├─ subtitle.srt         全程字幕
├─ assets/
│   ├─ library/shot-001.mp4     从本地素材库命中的文件（复制/软链进来）
│   ├─ pexels/shot-001.mp4
│   └─ local/shot-001.png
├─ library-index.json   本地素材库索引（若配置了素材库）
├─ shots-motion/shot-001.mp4   静态图的运动化结果
└─ final.mp4            成片
```

`shots.json` 单条结构：

```json
{
  "id": 1,
  "start": 0.0,
  "end": 4.2,
  "narration": "公元前256年。",
  "visual": "竹简三笔记载并排",
  "source": "local",
  "prompt": "ancient Chinese bamboo slips on a desk, three columns, ink brush, cinematic, muted tones",
  "keywords": ["bamboo slips", "ancient china"],
  "library_asset": null,
  "style": "historical-documentary",
  "status": "pending"
}
```

新增字段说明：

| 字段 | 含义 |
|---|---|
| `source` | `pexels` / `local` / `library`。为 `library` 时**必须**从素材库命中，否则该镜标记失败 |
| `library_asset` | 素材库命中的文件路径；为空表示"未命中，走 source 分支"。用户可手工填入以**钉死**某文件 |
| `resolved_by` | 运行时写入：实际来源 `library` / `pexels` / `local`，便于排查（不参与人工编辑校验） |

## 7. 流水线六阶段（与 CLI 子命令一一对应）

| # | 阶段 | 命令 | 产物 |
|---|---|---|---|
| 1 | 解析 | `lvs parse <script.md>` | `parse.json` |
| 2 | 拆镜 + 提示词 | `lvs shots` | `shots.json` |
| 3 | 素材获取 | `lvs assets` | `assets/*` + `shots-motion/*` |
| 4 | 配音 + 字幕 | `lvs voice` | `audio/*` + `subtitle.srt` |
| 5 | 合成 | `lvs build` | `final.mp4` |
| — | 一键 | `lvs run <script.md>` | 全流程 |
| — | 素材库索引 | `lvs library index/scan` | `library-index.json`（辅助命令，被 `lvs assets` 自动调用） |

`lvs shots` 用 LLM（沿用 `config.toml` 里的 DeepSeek/OpenAI 兼容接口）把讲稿转 `shots.json`，**一次性输出 JSON**，失败重试 1 次后落盘原始响应以便排查。

### 7.1 编排与状态 —— 为什么不照抄 MoneyPrinterTurbo

MoneyPrinterTurbo 的编排是：**FastAPI `BackgroundTasks` + 全局内存/Redis 状态字典 + 一个从 5% 走到 100% 的线性 `start()` 函数**。它适合"短视频、一次跑完、崩了重来"的场景，但有三个硬伤，长视频（100–200 镜、生图逐镜耗时）**不能**沿用：

| MoneyPrinterTurbo 做法 | 长视频下的问题 | 我们的做法 |
|---|---|---|
| 状态只在内存/Redis，进程一挂就没了 | 跑 40 分钟崩了，无法知道做到哪 | **文件即状态**：每阶段产物 + `manifest.json` 落盘 |
| 单个 `start()` 从头跑到尾，无阶段边界 | 改一个分镜要整条重跑 | **阶段化**：`parse/shots/assets/voice/build` 可单跑、可重跑 |
| 无幂等，产物覆盖写 | 重复下载/生图，浪费几小时 | **幂等**：产物存在且完整即跳过，`--force` 才重做 |
| 失败即 `TASK_STATE_FAILED` 整体退出 | 一个素材下载失败，整集作废 | **失败隔离**：单个 shot 失败只标记该 shot，其余继续 |

**核心约定（对应票据 16）**：

1. **`.work/<task>/manifest.json`** 记录每个阶段的状态与产物校验（`done_at`、`outputs[]`、`hash`）。阶段入口先读 manifest，已 `done` 且产物齐全 → 跳过。
2. **断点续跑**：删掉某阶段产物再 `lvs run`，只重跑该阶段及其**下游**，上游不动。
3. **单 shot 粒度**：`assets`/`voice` 阶段以 shot 为单位记录产物，缺哪个补哪个。
4. **不做后台线程池**：CLI 前台顺序执行，日志落 `.work/<task>/logs/`，中断即停、可续。
5. **绝不做自动抢占**：GPU 冲突（§11）时**显式报错**，绝不偷偷杀进程。

## 8. 素材来源决策与本地素材库（阶段 3）

### 8.1 素材解析优先级（`lvs assets` 的总规则）

对每个 shot，按**固定顺序**尝试取得素材，命中即停：

```
① library_asset 已钉死？           → 直接用该文件（用户手工指定或翻库命中，最高优先）
② source=library？                 → 只查素材库
     未命中：source_mode=library（策略选的）→ 回退本地生图（resolved_by=local，票 41）
             其余情况                      → 标记该镜失败
③ source=graphic（图文/图表 beat）？ → 本地排版成卡片（票 28/34），不调 ComfyUI
④ 允许翻库（默认允许）且素材库命中？ → 用命中文件，resolved_by=library
⑤ 否则按 source 分支：
     source=pexels → 检索下载实拍视频
     source=local  → 调 ComfyUI 生图
```

- 第 ③ 步的"允许翻库"由 `--no-library` 关闭；素材库未配置（目录为空/不存在）时该步**静默跳过**。
- **幂等**：已存在的产物跳过；`library_asset` 一旦写入，只要文件仍在就不重复解析。
- `resolved_by` 写回 `shots.json`，便于事后统计"这一集有多少镜是靠素材库省下来的"。

**来源策略（`source_mode`，票 41）会改写上面的顺序**：它是任务级意向
（`auto`/`pexels`/`local`/`library`），在 `lvs assets` 开始时落成逐镜 `source`。

- `auto`（默认）：完全按上表走。
- `pexels` / `local`：**只**走那一支 —— 第 ④ 步的"翻库优先"也一并关掉。
  选了下载却端出库里的文件，画面是哪来的没法解释。
- `library`：第 ② 步，未命中则**回退本地生图**（第 ⑤ 步的 local 支），
  且回退后 `source` 仍是 `library`（意向不变），只把 `resolved_by` 记成 `local` ——
  这样 `branches_for(mode=library)` 同时认两支，回退产物可复用、重跑仍幂等。
  回退的镜必须在结束报告里列出来。
- 策略**只作用于实拍镜**：图文/图表 beat（③）与手工钉死的镜（①）不受影响。
- 与 `--no-library` 冲突时报错停下（策略要库、参数关库 → 无从取素材）。

### 8.2 自动判定 `source`

LLM 在生成 `shots.json` 时给出 `source`：
- 写实场景、自然/城市/人物实拍类 → `pexels`
- 抽象概念、古籍、地图动画、数据图表、示意 → `local`
- `library` **不由 LLM 判定**（LLM 不知道你本地有什么），仅由运行时翻库命中或用户手工指定

**手动覆盖**：改 `shots.json` 里的 `source` 字段后重跑 `lvs assets`（幂等，已存在的产物跳过）。

### 8.3 本地素材库（library）—— 可后续指定文件夹

**背景**：你可能已经下好了或生成好了大量素材，散落在某个文件夹里，不想重复下载/生图。

**配置**（`config.toml`，默认留空即功能关闭）：

```toml
[library]
dirs = ["D:\\素材库"]        # 可多个；不存在/为空则该功能自动跳过
recursive = true
min_score = 1                # 关键词命中阈值
```

**索引**：`lvs library index`（`lvs assets` 会自动触发）扫描 `dirs` 下所有图片（`png/jpg/jpeg/webp/bmp`）与视频（`mp4/mov/webm/mkv`），生成 `.work/<task>/library-index.json`：

```json
{
  "root": ["D:\\素材库"],
  "generated_at": "2026-02-14T10:00:00",
  "entries": [
    {
      "path": "D:\\素材库\\竹简\\bamboo-01.jpg",
      "type": "image",
      "width": 1920, "height": 1080,
      "duration": null,
      "tags": ["bamboo", "竹简", "ancient"],
      "mtime": 1739500000,
      "size": 512000
    }
  ]
}
```

**检索**：命中评分 = 该 shot 的 `keywords` 与 entry `tags` 的**加权重合度**（文件名分词 + 所在文件夹名 + 可选的同名 sidecar `.json`/`.txt` 里的 `tags`）。分数 ≥ `min_score` 取最高者。

**索引策略**：
- 索引按 `dirs` 的 mtime 做**增量**（目录树变化才重扫），结果缓存在 `.work/_library/`（跨任务复用），`--reindex` 强制重建。
- 索引**不进 git**，素材库本身只在配置里引用，**绝不修改**用户素材文件（命中时是**复制/软链**到 `assets/library/`，不改原文件）。

### 8.4 两个分支的实现

- `pexels` 分支：复用 MoneyPrinterTurbo 的 `search_videos` / `save_video` 逻辑（按 `keywords` 检索，横屏 16:9）
- `local` 分支：调 ComfyUI HTTP API（见 §9），产 PNG → 再运动化

## 9. 本地生图（阶段 3 的 local 分支）

**栈**：ComfyUI（宿主程序）+ 模型：

| 优先级 | 模型 | 显存 | 理由 |
|---|---|---|---|
| 主力 | **Z-Image Turbo**（6B, Apache 2.0） | int8_convrot **6.2G** | 8 步出图、中文理解强、可商用 |
| 备用 | **SDXL**（3.5B） | 6.9G | LoRA/ControlNet 生态最强，适合固定角色/风格 |
| 暂缓 | Qwen-Image（20B） | ≥12G | 8G 卡上仅 Q2_K/Q3_K 勉强，质量明显下降 |

**主力模型实际是 3 个文件（分体式）**，装到 `D:\ComfyUI\models\`：

| 文件 | 目录 | 角色 |
|---|---|---|
| `z_image_turbo_int8_convrot.safetensors`（6.2G） | `diffusion_models/` | UNET → `UNETLoader` |
| `qwen_3_4b_fp8_mixed.safetensors`（5.63G） | `text_encoders/` | 文本编码器 → `CLIPLoader`（`type=lumina2`） |
| `ae.safetensors`（0.34G） | `vae/` | VAE（Flux ae） |

> 显存账：6.2G（UNET）与 5.63G（编码器）**不同时驻留** —— ComfyUI 按需加载/卸载，
> 峰值 ≈ max(单文件) + 激活，8G 卡可跑（必要时加 `--lowvram`）。

- 调用方式：ComfyUI 启动后监听 `127.0.0.1:8188`，通过 `/prompt` + `/history` HTTP API 提交工作流 JSON
- **工作流是「带占位符的 JSON 模板」**（`workflows/zimage_turbo.json`、`workflows/sdxl.json`），
  占位符形如 `{{PROMPT}}`/`{{SEED}}`/`{{CKPT}}`/`{{CLIP}}`/`{{VAE}}`。**换模型 = 换模板 + 改 `[comfyui]` 配置**，不改代码
- Z-Image 官方参数已内置为默认：`res_multistep` + `simple` + 8 步 + `cfg=1` + `ModelSamplingAuraFlow shift=3`；
  负向条件用 `ConditioningZeroOut`（cfg=1 时无意义）；潜空间用 `EmptySD3LatentImage`（Flux 制式：16 通道 /8）
- 工作流 JSON 与模型清单纳入版本管理（`workflows/`、`models.lock.json`）
- 模型权重体积大（数十 GB），**不进 git**，用 `scripts/fetch_models.py` 按清单拉取（实测 ModelScope ~5 MB/s）
- 静态图 → 视频片段：Ken Burns（缓慢推拉/平移），用 ffmpeg `zoompan` 滤镜实现，避免引入重型依赖

## 10. 配音与字幕（阶段 4）

**配音后端做成可插拔**，对外统一接口为 **OpenAI 兼容的 `POST /v1/audio/speech`**（`{input, voice, response_format}`）：

| 期 | 后端 | 说明 |
|---|---|---|
| 一期 | `EdgeTTSBackend` | 免费、现成，逐分镜合成 → 拼接。**不是** OpenAI 协议，由适配器包装成同一接口 |
| 二期 | `OpenAISpeechBackend` | 指向本地服务，见下 |

**二期接入路径（已确认可行）**：`D:\faster-qwen3-tts-main\examples\openai_server.py` 已经把本地 Qwen3-TTS 暴露为 OpenAI 兼容 `/v1/audio/speech`（支持 `--voices voices.json` 多音色、流式 wav/pcm、`/health`）。二期只需：
1. 起该 server（模型指向 `D:\qwentts\models\...`，`ref_audio` 走声音克隆）
2. 把配置里的 TTS `base_url` 指向它

即：**换后端 = 换 base_url，不改流水线代码。**

**字幕（一期）**：`edge-tts` 词边界时间戳 → SRT；失败回退 `faster-whisper`

> ⚠️ 二期风险：OpenAI 兼容接口**不返回**时间戳，换成本地 TTS 后字幕时间轴会失去来源，需要补"强制对齐"（如 `whisperx` / 本地 ASR 对齐）。这条列入二期待验证。

关键设计：**逐分镜合成**，每个分镜音频时长即该镜画面时长——这天然解决了"画面与旁白同步"。

## 11. 硬约束：8 GB 显存串行调度

实测（来自 `D:\qwentts\README.md`）：Qwen3-TTS 1.7B 单模型占 4.45 GB；本地生图（Z-Image/SDXL）约 6–8 GB。**两者不能同时驻留** 8 GB 卡。

因此流水线**必须按阶段串行使用 GPU**，禁止交错：

```
[阶段3 生图] → 释放显存 → [阶段4 TTS] → 释放显存 → [阶段5 ffmpeg 合成(不吃显存)]
```

实现要求：冲突时**显式报错并提示先停另一个服务**，不做自动抢占。

**判据必须是「探测对端服务」，不能是「空闲显存 ≥ X」**（真机踩坑，见票 17 修正）：
ComfyUI 一旦出过图就常驻约 6 GB，此时空闲只剩 ~2.6 GB —— **这是正常状态**，不是冲突。
若用显存数字当闸门，第 2 个分镜起就会被全部拦下，实际跑批必然全废。故：

| 阶段 | 真冲突判据 | 动作 |
|---|---|---|
| 生图（`assets` local 分支 / `lvs image`） | `tts.backend=openai_speech` 且其 `/health` 可达 | 报错，提示停本地 TTS |
| 本地 TTS（`lvs voice`） | `comfyui.base_url` 的 `/system_stats` 可达 | 报错，提示关 ComfyUI |
| `lvs voice` 且 `tts.backend=edge` | ——（云端合成，不占本地显存） | 放行 |

显存数字（`nvidia-smi`）仅用于**报告**与低余量**告警**：生图阶段只提示不阻断；
本地 TTS 阶段仍是硬阈值。`[gpu].skip_check=true` 可整体关闭。

## 12. 依赖

| 依赖 | 用途 | 备注 |
|---|---|---|
| **ffmpeg** | 合成、烧字幕、zoompan | ✅ **已解决**：走 `imageio-ffmpeg` 自带的 ffmpeg 7.1（含 libass/zoompan/drawtext/x264/aac/mp3），**无需系统安装**；查找顺序 PATH → `.tools/` → `imageio-ffmpeg` |
| ComfyUI | 本地生图宿主 | ✅ 装在 `D:\ComfyUI`（独立 venv），模型见 §9 |
| `edge-tts` | 一期配音/字幕 | 联网，含词级时间戳 |
| `faster-whisper` | 字幕回退 | 可选（未装则按字数估算） |
| LLM API | 拆镜 + 提示词 | 复用 `config.toml`（DeepSeek） |
| `Pillow` / `requests` | 基础 | — |
| **ffprobe / PyAV（可选）** | 读素材库图片尺寸/视频时长 | 缺则用 `ffmpeg -i` stderr 解析兜底，再缺则索引降级 |

**不引入**：moviepy（基线用它，但长视频下内存与稳定性差，改用 ffmpeg 原生 + 少量 Python）

## 13. 阶段划分

**Phase 1（MVP）—— 17 张票（`issues/01..18`）—— ✅ 全部交付**

| # | 内容 | 票 | 状态 |
|---|---|---|---|
| 1 | `lvs parse` 解析拍摄稿 | 04,05 | done |
| 2 | `lvs shots` 拆镜 + 提示词 + 自动 source 判定 | 06,07,08 | done |
| 3 | `lvs assets` —— `pexels` 分支 + **本地素材库优先命中** | 09,18 | done |
| 4 | `lvs voice` edge-tts 逐镜配音 + SRT | 10,11,12 | done |
| 5 | `lvs build` ffmpeg 合成（含 zoompan + 烧字幕） | 13,14,15 | done |
| 6 | `lvs run` 一键串起来（幂等 + 断点续跑） | 03,16 | done |
| 7 | 环境引导：检测 ffmpeg | 01 | done |
| 8 | GPU 串行调度守卫 | 17 | done |
| 9 | CLI 骨架 | 02 | done |

**Phase 2（`issues/19..21`）**

| # | 内容 | 票 | 状态 |
|---|---|---|---|
| 10 | ComfyUI + Z-Image Turbo 接入 `local` 分支 | 19 | **done**（真机出图已验证） |
| 11 | `OpenAISpeechBackend`：指向本地 Qwen3-TTS 的 OpenAI 兼容服务 | 20 | implemented（需用户手动起服务） |
| 12 | 本地 TTS 下的字幕强制对齐（`faster-whisper`） | 21 | implemented（需装 `faster-whisper`） |

> 「implemented」= 代码完成、单测通过、错误路径清晰；「待验证」= 依赖用户手动启动的外部服务，尚未真机跑通。
> 票 19 已从 implemented 升为 done（真机跑通）。

**Phase 3**
13. GUI —— **已交付**（票 36/37/40/41）：`lvs gui` 本地 Web 界面，是 CLI 的**薄壳**（只经由
    `python -m lvs <阶段>` 子进程驱动，不 import 阶段内部逻辑）。只监听 127.0.0.1；
    只写 `.work/<task>/` 与 `config.toml` 的白名单子集；全局串行；进度数盘上产物。
    页面：任务列表 / 任务工作台（流水线 + 实时日志 + 分镜栅格）/ 素材库 / 设置与体检。
    详见 CONTEXT 的 D2、D30、D31。
14. 逐镜预览 / 批量重生成（界面里已有单镜「换一张」「自己选一张图」，批量还没做）
15. 留存曲线自检的对齐（讲稿里的"目标留存"与成片实际时长对照）

## 14. 风险与待验证

| 风险 | 说明 | 应对 | 现状 |
|---|---|---|---|
| 拍摄稿格式不完全统一 | `05-拍摄稿` 与 `05-成稿` 结构不同 | 解析器容错 + 降级路径（§5） | 已实现 |
| LLM 拆镜质量 | 分镜粒度、提示词可用性 | `shots.json` 可人工编辑，失败落盘原文 | 已实现（含 `--no-llm` 启发式降级） |
| Pexels 命中率 | 历史题材实拍素材少 | 自动判定偏向 `local`；关键词英译 | 已实现；**api_key 待填** |
| 8 GB 显存 | 生图/TTS 互斥 | §11 串行调度 | 已实现（`lvs/guard.py`） |
| 长视频耗时 | 270 镜 × 生图 | 幂等 + 断点续跑，逐阶段验收 | 已实现 |
| 编排脆弱 | 长任务中途崩溃导致全部重来 | §7.1 文件即状态 + 阶段化 + 失败隔离 | 已实现 |
| 素材库误命中 | 关键词碰巧撞上不相关文件 | `min_score` 阈值 + `library_asset` 可钉死 | 已实现 |
| 素材库体积大 | 数万文件扫描慢 | 增量索引 + 缓存到 `.work/_library/` | 已实现 |
| 史实准确性 | 讲稿有"史实核验与红线" | 画面描述**只做视觉转译，不改写史实** | 已写入提示词红线 |
| ~~ffmpeg 未安装~~ | —— | —— | **已解决**：改用 `imageio-ffmpeg` 自带的 ffmpeg 7.1（含 libass/zoompan/drawtext/x264/aac/mp3），无需系统安装 |
| ComfyUI 依赖冲突 | `comfyui-workflow-templates` 要求 Python <3.12；`comfy_kitchen` 要求 torch ≥2.7 | 剔除前者；ComfyUI 独立 venv 升 torch | **已解决**（见 README） |

## 15. 验收标准（Phase 1）

给定 `K005-36个邑3万口天子最后一次结账.md`：

- [x] `lvs parse` 产出 `parse.json`，段落数 = 6，排除区段无泄漏
- [x] `lvs shots` 产出 `shots.json`，字段完整、`narration` 不丢字不重复
      - **修正**：原写「60–200 镜」，实测 **270 镜**。原因是 K005 讲稿本身是"鼓点式短句"
        （279 行旁白、平均 15.6 字/句）。分镜粒度由讲稿决定，**改判据为「≈ 每句一镜」**，不再设硬区间
- [ ] `lvs assets` 对 `pexels` 分镜全部落到 `assets/pexels/*.mp4`（**待填 api_key**）
- [x] 配置了素材库后，命中分镜落到 `assets/library/*` 且 `resolved_by=library`；未配置素材库时该步静默跳过、不影响流程
- [x] `lvs voice` 产出 `narration.mp3` 与 `subtitle.srt`，SRT 行数 > 0 且时间单调
- [x] `lvs build` 产出 `final.mp4`，时长与音频时长偏差 < 1 秒，字幕可见（demo 实测偏差 0.05s）
- [x] 全流程可重复执行（幂等），中断后可从断点续跑
- [x] 本地生图真机出图（票 19）：768×768/4步 15.2s、1344×768/8步 正常；2 镜真实流水线出 `final.mp4`
      （画面轨 9.0s｜旁白 9.1s｜偏差 0.03s，真实 AI 画面 + 烧录字幕，0 占位帧）

## 16. 进度可视化与看板（本轮新增）

长视频单次跑几十分钟，滚动日志看不清"到哪了"。补两件只读的可视化：

- **进度条**（`lvs/progress.py`，零依赖）：`素材`/`配音`/`合成`/`拆镜` 四个长循环各挂一条。
  TTY 上单行原地刷新（`[████░░░░] 45% 122/270 ETA 12:30`）；**非 TTY**（日志/后台）退化为每个整数
  百分比打一行，不刷屏、不混入 `\r`。核心是 `track(items, prefix)` 生成器，`continue` 也不用额外照顾。
- **看板**（`lvs/board.py`）：`lvs board [--html]`。读 `manifest.json` + `shots.json`，把五个阶段渲染成
  五列卡片（状态 `done/partial/todo` + 明细 + 进度条），末尾给分镜统计（已配/未配/失败、按来源）。
  `--html` 另存自包含单文件（无外链、支持深色模式、标题转义）。**纯读取，不改任何产物**。

## 17. 评审修正记录（code-review 两轴）

对未提交工作做过一次双轴评审（Standards / Spec）。已修：

| # | 问题 | 修法 |
|---|---|---|
| 1 | **断点续跑失效**：assets 用 `outputs=[shots.json]`，且 `failed>0` 仍标 `done` → `lvs run` 整段跳过、失败镜永不补（违反 §7.1②③） | `outputs` 记**每镜产物**；有失败标 `partial`；`is_stage_done` 用 manifest 记录的 outputs 判定（D15）。新增 `tests/test_resume.py` |
| 2 | 幂等跳过跨三分支 glob，改 `source` 后误用旧分支文件 | `existing_asset` 只查与 `source`/`library_asset` 匹配的分支；build 复用同一函数 |
| 3 | 配音失败镜 `start=None`，build 用 `start or 0` 压到 t=0 → 时间轴错位 | `build.shots_with_audio()` 把无音频镜排除出画面轨并报告 |
| 4 | 违反 D11：`imagegen.DEFAULT_VALUES` 写死 Z-Image 专用采样参数 | 采样参数固化进 `workflows/*.json` 字面量；`imagegen` 只留通用旋钮 + 文件回退（D16）。移除随之失效的 `--steps/--cfg` 与 config 键 |
| 5 | 词汇表漂移：CLI 用 `SCRIPT.md`/`script`（glossary 禁用「脚本」） | 改 `拍摄稿.md`/`manuscript` |
| 6 | 死代码：`assets.RESOLVERS`、`shots.script_start/end`、`build._segment_filter_video` 死参数 | 删除 |
| 7 | 重复：`_load_shots` 两份、`_resolve_asset` 与 `existing_asset` 重复 | 抽成 `Workspace.load_shots()`；build 复用 assets 的函数 |
| 8 | issue 文档用小节名 `## 交付记录`，不符 `issue-tracker.md` 约定 | 统一改为 `## Comments` |
| 9 | **占位片段卡死**：片段缓存只看时长，占位片段（缺素材时生成）时长与目标一致 → 补上素材后重跑 build 仍复用占位黑帧 | 记 `segments/.placeholders.json`；`_segment_reusable` 对「占位片段 + 本镜已有真素材」判为不可复用 → 重渲（新增 `tests/test_build.py::SegmentCacheTest`）。登记表**逐镜增量落盘**，build 中途被打断也不丢 |

**刻意保留**（评审提到但不改）：`lvs run` 的 GPU 冲突仍是**快速失败**（`return 2`），而 `assets`/`voice`
是逐镜隔离 —— 两者语义不同（前者是前置条件不满足，后者是单件失败），已在 docstring 写明。

**第 9 条的现场验证**（30 分钟压测 `long30`）：build 首次跑时磁盘上只有 73 张真图，
其余 581 镜落成占位黑帧；随后（被限时截断的）`assets` 又产出若干真图，
**不加 `--force` 重跑 build** → 18 个「占位片段 + 本镜已有真素材」的分镜被**自动重渲**
（`shot-074`…`shot-091` 的段文件 mtime 全刷新），其余占位段**原样复用**。
即 D17 在真实数据上端到端成立：占位不再卡死，且不误伤已生成段。

**压测脚手架注意**（**非产品缺陷**，但很坑）：本项目 `.venv/Scripts/python.exe` 是**启动器 shim**，
真正干活的是它拉起的 **base 解释器 `anaconda\python.exe`**。
因此外部「停任务」时若只杀掉 shim（Git Bash 的 `timeout`、`TaskStop` 包装的 shell 都是如此），
**worker 会以孤儿身份继续跑** —— 现场表现就是「明明已经 rc=124 / 已停止，真图却还在以 ~35s/张 增长」，
持续一个多小时、与后续 build **并发**。

- 现场确认：`Get-CimInstance Win32_Process -Filter "Name='python.exe'"` 里出现
  两条 `lvs assets --task long30`（07:35 启动），而外层 shim 早已消失。
- **正确停法**：杀**整棵进程树** —— `taskkill /PID <pid> /T /F` 或 PowerShell `Stop-Process -Force`，
  杀完再核验 `anaconda\python.exe` **确实消失**（只杀 shim 不够）。
- 影响可控：即便出现孤儿，因 `assets` **以磁盘产物为状态**，重跑只会跳过已完成项，不会产出错数据；
  代价仅是 GPU 空转 + 产物上限不确定。

## 18. 画面质量与手动向导（本轮新增，票 27–30）

真机跑完 30 分钟成片后，用户报了三个画面问题 —— 逐一定位、定点修（**不加依赖**）：

### 18.1 问题与根因

| 现象（用户原话） | 真实根因 | 修法（票） |
|---|---|---|
| 运镜时画面"抖动" | ffmpeg `zoompan` 把裁切窗 `x/y` 与尺寸**取整** → 推近变成"卡几帧、跳 1 像素"的阶梯 | 超采样：预放大算、lanczos 降回（票 29） |
| 图上出现"格式转换器"几个字 | **提示词污染**：`[画面位]` 原文里是编辑设计（"…→ 中间一道『科目转换器』…"），整段被当提示词喂给了生图 | 拆 beat + 提示词清洗（票 27） |
| 图上文字错乱（"西夏文"） | 大字标题类画面本就该**排版绘制**，不该交给扩散模型"画字" | 图文 beat 走本地排版卡片，不进生图（票 27/28） |

### 18.2 提示词层改造（票 27）

- **拆 beat**：`[画面位]` 一行原为编辑设计（`A → B → C`），按 `→` 拆成 beat；
  显式 `[场景]`/`[图表]` 标记优先，无标记则关键词兜底，仍存疑则**标出待复核**。
  - 模板头（`时间轴：`）与 ≤4 字的数据碎片（`前403`）**不拆分**，保持成一个 beat。
- **`sanitize()` 提示词清洗**：剥掉**引号内字幕文本**、箭头、编辑动词（高亮/砸屏/分栏/浮现/逐行/→），
  只留可拍语言。实测真实段落：含引号/动词的提示词 **0 条**。
- **分类**（`classify`）：`scene` / `graphic`。graphic = 时间轴/分栏/对照/砸屏类；
  scene = 特写/长镜/远景/宫殿/城门类。
- 拆镜时 LLM 按 segment 批量判定（有 key 走 LLM，无 key 走**按序均分 + 大声警告**）；
  graphic → `source=graphic, prompt=""`，scene → 清洗后启发式。

### 18.3 图文卡片渲染（票 28 → 34/35 改版）

**首选 HTML/CSS + 本机无头浏览器截图**（`lvs/cardhtml.py`，Edge/Chrome，`LVS_BROWSER` 可指定），
Pillow（`lvs/graphic.py`）降为**回退**版式 —— 找不到浏览器、或截图产出坏图时用。
两条路吃的是**同一个 `cards.Card`**，所以卡上有哪些内容与渲染器无关（D27）。

版式由内容自动选，共四套（`cards.kind`）：`statement`（数据行）/ `compare`（左右对照）/
`timeline`（时间轴）/ `list`（条目）。字号不靠 Python 估宽，由页内脚本实测收敛。
卡上**只有内容**：没有段落标题、没有页脚旁白（D28）；同一条 beat 只栅格化一次（D29）。

`assets` 解析时 graphic 分支**不碰 ComfyUI**，直接排版落盘 → 大字标题类画面文字**必然正确**。

用户可选「**图文图表感**」或「**实拍剧照感**」（`shots.visual_mode`）：
前者全走排版卡片，后者把 graphic 也交给扩散模型。

### 18.4 运镜抖动（票 29）

`build.kenburns_supersample`（默认 2）：预放大 max(2,N)×、zoompan 在 2× 上算、lanczos 降回。

| 变体 | 抖动 CoV（越小越匀） | 锐度 | 折算 43 min 成片 |
|---|---|---|---|
| 1（旧） | 0.2034 | 6.74 | 12 min |
| **2（新默认）** | **0.1191（−41%）** | **6.76** | **85 min** |
| 4 | 0.0905（−55%） | 6.76 | 297 min |

锐度不降（6.74→6.76）→ 是真压掉了量化误差，不是"糊了看着不抖"。代价：build 慢 7×，回退设 `=1`。

### 18.5 手动向导 `lvs studio`（票 30）

把「素材获取」拆成**有检查点的手动流程**，与 `lvs run` 全自动**并存**：

```
上传拍摄稿 → parse → shots
  → 问：下载实拍 / 本地生图 / 图文卡片 / 翻素材库
  → 执行 → 问：继续下一步（voice → build），还是改几张图
  → 改图：按镜号重出（--prompt 改词 / --seed 换种 / 都没有则 seed+1）
```

每个提问都有参数 → 同一命令既可人机对话、也可脚本化；
**非交互终端不给参数直接报错**，不会挂住。`--yes` = 全选 + 直接继续。

关键实作（都踩过）：`assets --only` 只跑选中分支；选了「生图」不选「下载素材」时
**`route_pexels_to_local`** 把 pexels 镜改 local（否则永远缺素材）；
`clear_shot_assets` **连片段一起删**（片段缓存只看时长，否则换了图被复用）。

### 18.6 真机小样试跑与三处修复（票 31）

用 DeepSeek + 手动向导在 `small`（110 镜：46 graphic / 64 scene）上跑全链，逐镜核对产物后修掉三处：

| # | 缺陷 | 根因 | 修法 |
|---|---|---|---|
| 1 | 卡片正文出现「三段原文并排浮现」等**剪辑指令**（11/46） | 卡片正文没过清洗；且 `sanitize()` 对卡片是**反向**工具（它删的正是卡片要显示的数据） | 新增 `prompting.card_text()`：摘引号留内容 + 去动词 |
| 2 | 右下角旁白**溢出边框**被切半句 | 角标右对齐不限宽，`narration[:18]` 硬截救不了 | `graphic.ellipsize()` 二分截断 + 自动缩字号 |
| 3 | **静止卡片段整段 0 帧** → 成片画面 64.7s vs 旁白 447.3s | `mode="none"` 只挂 `fps=30`；`fps` 对**单帧输入**产不出帧，而 Ken Burns 靠 `zoompan d=` 自己复制帧。ffmpeg 打 `No filtered frames` 却 **rc=0 退出**，`ff_run` 只看返回码 → 46 个 261 字节空壳被当成"成功" | 静图补 `-loop 1`；新增 `assert_segment_rendered()` **零帧当场抛错**；单镜失败退回占位帧（D17） |

**教训（对后续仍然成立）**：

1. **返回码 0 ≠ 产物有效**。阶段产物必须**真的读一遍**（时长/可解码）才算数 —— 这与 D15「阶段产物即校验」
   是同一条原则，只是此前只校验了"文件在不在"。
2. **死代码第一次被点亮时最危险**。三处缺陷里有两处源于"新代码第一次真正执行"；
   `mode="none"` 在票 28 之前从未被调用过。
3. **两个方向相反的文本清洗**：进**生图**要**删**引号内容（会被画成伪汉字），
   上**卡片**要**留**引号内容（那是数据）。同名同形的输入，处理恰好相反，别复用同一个函数。

试跑同时暴露了 **beat 粒度**层面的两个待对齐问题（同一 beat 判定漂移、一 beat 铺满多镜产生重复卡片），
见票 32。
