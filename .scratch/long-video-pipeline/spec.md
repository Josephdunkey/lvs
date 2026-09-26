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

**排除**：`[画面位]`、`[重参与点…]`、`（转场）`、`（停顿）`、`> 备选：…`、`一、传达层`/`四、留存曲线自检`/`五、史实核验` 全部章节，**都不进字幕、不进 TTS**。

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
① library_asset 已钉死？           → 直接用该文件（用户手工指定，最高优先）
② source=library？                 → 只查素材库；未命中 → 标记该镜失败
③ 允许翻库（默认允许）且素材库命中？ → 用命中文件，resolved_by=library
④ 否则按 source 分支：
     source=pexels → 检索下载实拍视频
     source=local  → 调 ComfyUI 生图 → 运动化
```

- 第 ③ 步的"允许翻库"由 `--no-library` 关闭；素材库未配置（目录为空/不存在）时该步**静默跳过**。
- **幂等**：已存在的产物跳过；`library_asset` 一旦写入，只要文件仍在就不重复解析。
- `resolved_by` 写回 `shots.json`，便于事后统计"这一集有多少镜是靠素材库省下来的"。

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
| 主力 | **Z-Image Turbo**（6B, Apache 2.0） | FP8≈8G / GGUF≈6G | 8 步出图、中文理解强、可商用 |
| 备用 | **SDXL**（3.5B） | 6–8G | LoRA/ControlNet 生态最强，适合固定角色/风格 |
| 暂缓 | Qwen-Image（20B） | ≥12G | 8G 卡上仅 Q2_K/Q3_K 勉强，质量明显下降 |

- 调用方式：ComfyUI 启动后监听 `127.0.0.1:8188`，通过 `/prompt` + `/history` HTTP API 提交工作流 JSON
- 工作流 JSON 与模型清单纳入版本管理（`workflows/`、`models.lock.json`）
- 模型权重体积大（数十 GB），**不进 git**，用 `scripts/fetch_models.py` 按清单拉取
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

实现要求：阶段入口检查显存占用（`nvidia-smi`），若目标模型与当前驻留模型冲突，**显式报错并提示先停另一个服务**，不做自动抢占。

## 12. 依赖

| 依赖 | 用途 | 备注 |
|---|---|---|
| **ffmpeg** | 合成、烧字幕、zoompan | **当前未安装，需先装** |
| ComfyUI | 本地生图宿主 | 需另行部署 + 模型下载 |
| `edge-tts` | 一期配音/字幕 | 联网 |
| `faster-whisper` | 字幕回退 | 可选 |
| LLM API | 拆镜 + 提示词 | 复用 `config.toml`（DeepSeek） |
| `Pillow` / `requests` | 基础 | — |
| **ffprobe / PyAV（可选）** | 读素材库图片尺寸/视频时长 | 缺则索引降级为"仅文件名标签" |

**不引入**：moviepy（基线用它，但长视频下内存与稳定性差，改用 ffmpeg 原生 + 少量 Python）

## 13. 阶段划分

**Phase 1（MVP，本期交付）—— 17 张票（`issues/01..17`）**
1. `lvs parse` 解析拍摄稿
2. `lvs shots` LLM 拆镜 + 提示词 + 自动 source 判定
3. `lvs assets` —— `pexels` 分支 + **本地素材库优先命中**（票 18）
4. `lvs voice` edge-tts 逐镜配音 + SRT
5. `lvs build` ffmpeg 合成（含 zoompan + 烧字幕）
6. `lvs run` 一键串起来
7. 环境引导：检测/安装 ffmpeg
8. 编排：幂等 + 断点续跑（§7.1）

**Phase 2（`issues/19..21`）**
9. ComfyUI + Z-Image Turbo 接入 `local` 分支（票 19）
10. `OpenAISpeechBackend`：起 `D:\faster-qwen3-tts-main\examples\openai_server.py`，配 `voices.json`，换 base_url 即用（票 20）
11. 本地 TTS 下的字幕强制对齐（`whisperx` 或本地 ASR）（票 21）

**Phase 3**
12. GUI（在 CLI 稳定后补）
13. 逐镜预览 / 批量重生成
14. 留存曲线自检的对齐（讲稿里的"目标留存"与成片实际时长对照）

## 14. 风险与待验证

| 风险 | 说明 | 应对 |
|---|---|---|
| 拍摄稿格式不完全统一 | `05-拍摄稿` 与 `05-成稿` 结构不同 | 解析器容错 + 降级路径（§5） |
| LLM 拆镜质量 | 分镜粒度、提示词可用性 | `shots.json` 可人工编辑，失败落盘原文 |
| Pexels 命中率 | 历史题材实拍素材少 | 自动判定偏向 `local`；关键词英译 |
| 8 GB 显存 | 生图/TTS 互斥 | §11 串行调度 |
| 长视频耗时 | 100–200 镜 × 生图 | 幂等 + 断点续跑，逐阶段验收 |
| 编排脆弱 | 长任务中途崩溃导致全部重来 | §7.1 文件即状态 + 阶段化 + 失败隔离 |
| 素材库误命中 | 关键词碰巧撞上不相关文件 | `min_score` 阈值 + `library_asset` 可钉死；命中可人工否决后重跑 |
| 素材库体积大 | 数万文件扫描慢 | 增量索引 + 缓存到 `.work/_library/` |
| 史实准确性 | 讲稿有"史实核验与红线" | 画面描述**只做视觉转译，不改写史实**；不新增讲稿没有的具体人名/数字 |

## 15. 验收标准（Phase 1）

给定 `K005-36个邑3万口天子最后一次结账.md`：

- [ ] `lvs parse` 产出 `parse.json`，段落数 = 6，排除区段无泄漏
- [ ] `lvs shots` 产出 `shots.json`，分镜数在 60–200 之间，字段完整
- [ ] `lvs assets` 对 `pexels` 分镜全部落到 `assets/pexels/*.mp4`
- [ ] 配置了素材库后，命中分镜落到 `assets/library/*` 且 `resolved_by=library`；未配置素材库时该步静默跳过、不影响流程
- [ ] `lvs voice` 产出 `narration.mp3` 与 `subtitle.srt`，SRT 行数 > 0 且时间单调
- [ ] `lvs build` 产出 `final.mp4`，时长与音频时长偏差 < 1 秒，字幕可见
- [ ] 全流程可重复执行（幂等），中断后可从断点续跑
