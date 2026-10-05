# LongVideoStudio

把一篇已成稿的**长视频拍摄稿**（Markdown），逐分镜地变成**成片**。

- 输入：一份 Markdown 拍摄稿（格式见 `docs/拍摄稿格式契约.md`）
- 输出：一集 `final.mp4`（画面 + 配音 + 烧录字幕）
- 形态：CLI（命令名 `lvs`），每阶段产物落盘、可编辑、可重跑

> **新手从这里开始**：`docs/使用说明书.md` —— 写给没用过命令行的人，一步步照做即可。
> 只想先看效果：装好依赖后跑 `lvs run --demo`（约 1 分钟，不需要任何密钥 / 模型 / 显卡）。

**它是怎么工作的**（三句话）：稿子被切成一个个"分镜"→ 每个分镜配一张图 →
把图和配音、字幕拼成 mp4。**每一道工序的半成品都存到硬盘上**，所以中断了能续、
不满意能只重做某一步。全程有 **6 道人工确认关卡**，防止白跑几小时。

---

## 快速开始

### 方式一：克隆使用（推荐）

```bash
git clone <本仓库地址> LongVideoStudio
cd LongVideoStudio

python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[tts,http]"    # Windows
# source .venv/bin/activate && pip install -e ".[tts,http]"  # macOS / Linux

cp config.example.toml config.toml      # 然后按需填写（路线 B 什么都不用填）
```

```powershell
.venv\Scripts\python.exe -m lvs doctor       # 1) 环境体检
.venv\Scripts\python.exe -m lvs run --demo   # 2) 跑通全链路（演示模式：不需要任何密钥/模型）
```

> 习惯 `lvs` 短命令的话：`scripts\lvs.cmd` 已封装好 venv，把它所在目录加进 PATH 即可。

### 方式二：`pip install`

```bash
pip install .            # 或 pip install ".[tts,http]"
lvs doctor
```

装好后，**在你的工作目录**放一份 `config.toml`（会优先读它），产物落在该目录的
`.work/` 下。生图工作流模板随包一起安装，无需额外配置。

---

## 命令

| 命令 | 阶段 | 产物 | 状态 |
|---|---|---|---|
| `lvs doctor` | 环境体检 | — | ✅ 票据 01 |
| `lvs parse <拍摄稿.md>` | 解析拍摄稿 | `parse.json` | ✅ 票据 04/05 |
| `lvs shots` | 拆镜 + 提示词 + 来源判定 | `shots.json` | ✅ 票据 06/07/08 |
| `lvs assets` | 素材获取（素材库 → Pexels / 本地生图） | `assets/*` | ✅ 票据 09/18/19 |
| `lvs voice` | 逐镜配音 + 字幕 | `audio/*`、`subtitle.srt` | ✅ 票据 10/11/12/20 |
| `lvs build` | ffmpeg 合成 | `final.mp4` | ✅ 票据 13/14/15 |
| `lvs run <拍摄稿.md>` | 一键串起全部阶段，**执行到下一道门禁为止**（人审闸门见下） | — | ✅ 票据 03/16 |
| | ↳ `--keep-going`：单阶段失败不中断（**不跳过门禁**） | | ✅ 新增 |
| `lvs studio` | **手动向导**：素材段逐步确认（取素材 → 出完再问继续/改图） | 同各阶段 | ✅ 新增 |
| `lvs library index` | 本地素材库扫描建索引 | `library-index.json` | ✅ 票据 18 |
| `lvs image` | 手动生图（单张/多张） | `.work/_imagegen/*.png` | ✅ 票据 19 |
| `lvs board` | 看板：把任务进度摊成看板 | （可选）`board.html` | ✅ 新增 |
| `lvs gui` | **本地图形界面**（⏸ **已冻结**，见下） | — | 票据 36 |
| `lvs check <拍摄稿.md>` | **格式校验**（G0）：出稿即验，不等到 parse | — | ✅ 新增 |
| `lvs migrate <拍摄稿.md>` | **拍摄稿 v2 迁移**：补 `[场景]` 标注 + `{NAME}` 槽位 | `_v2/*.md` + 报告 | ✅ 新增 |
| `lvs cast` | **定妆**（G2）：提取人物 → 出候选 → 审阅冻结 | `_cast/lock.json` + `ref-*.png` | ✅ 新增 |
| `lvs qc` | **生图巡检**（G3）：完整性 / 彩度 / 人数 / 提示词版本 | `.work/<task>/qc/*` | ✅ 新增 |
| `lvs gate` | **流水线门禁账本**：六道人审闸门的状态 / 批准 / 打回 / 跳过 | `.work/<task>/gates.json` | ✅ 新增 |
| `lvs styles` | **画面风格预设**：30 个内置（按题材分组、标注是否单色）+ 项目自定义 | — | ✅ 新增 |
| `lvs init <素材库>` | **接入新项目**：建素材库骨架 + 项目风格/配置 + 体检待办 | `config.<名>.toml`、`00-设定/风格预设.toml` | ✅ 新增 |
| `lvs publish` | **投稿物料**（B站 / 抖音）：封面**叠三行字** + 标题候选 + 简介 + 标签/话题 | `.work/<任务>/publish/*` | ✅ 新增 |
| `lvs bgm [generate\|download\|prompt\|mix]` | **背景音乐（BGM）**：本地生成可商用配乐（随文案风格）+ 压低闪避混音 | `.work/<任务>/bgm/*`、`bgm_final.mp4` | ✅ 新增 |
| `lvs clean` | **清理任务目录**（`.work/` 会越长越大）：默认**只报告**，`--yes` 才真删 | `.work/<任务>/` | ✅ 新增 |

> **六道门禁（G0–G5）**：`script` 拍摄稿 → `shots` 分镜与提示词 → `cast` 定妆参考 →
> `images` 分镜图与巡检 → `voice` 配音与字幕 → `final` 成片。
> 每道门守的是"下一步要不要花钱"，**未批准就不放行**（fail-closed）；
> 批准时记下产物的**指纹**，产物事后被改动 → 门禁自动失效要求重审。
> 全流程剧本见 `docs/全流程流水线-编排手册.md`；
> **换一本书/换一个题材怎么接进来**见 `docs/新项目接入指南.md`。

> **架构**：阶段顺序 / 开关 / 产物 / 门禁**只有一处定义**（`lvs/stage.py`）；
> 退出码与错误基类统一在 `lvs/errors.py`；产物指纹与签名统一在 `lvs/artifact.py`；
> 阶段间产物契约统一在 `lvs/handoff.py`（上下游都验同一份 schema）；
> 连续失败熔断在 `lvs/breaker.py`。
> 架构约束由 `tests/test_architecture.py` 守着（改坏了会红）。
> 详见 `docs/_archive/架构审查-整体.md`、`docs/_archive/编排框架审查-外部对标.md`。

> **退出码**（`lvs run` / 各阶段统一）：
> `0` 成功 ｜ `1` **有失败件**（逐镜隔离：某几镜没取到素材/没合出音 → `run` 会继续，最后汇总报 1）
> ｜ `2` 输入/前置条件不对（改一下再来） ｜ `3` 被人审门禁拦住（等人决定，**不是错误**）。
> `--keep-going` 管的是**硬失败**（2）也继续；逐镜部分失败本来就继续；门禁（3）**始终会停**。
> 任何未捕获的领域错误都由 `cli.main` 统一映射（打人话、不用看堆栈）；要看堆栈设 `LVS_DEBUG=1`。

> **给 Agent / 脚本用**：工作命令（`parse` / `shots` / `assets` / `voice` / `build` / `run` / `qc`）
> 都支持 `--json`，输出**统一结果信封**（`ok` / `status` / `exit_code` / `counts` / `next_hint`）；
> `lvs gate --json` 给门禁状态与下一步。信封在输出的**最后一段**（以 `{` 开头）。
> 只锁这 5 个决策字段，其余自由扩展 —— 见 `lvs/result.py`。
> 门禁带**自动判据**（G1 过契约 / G3 要 `lvs qc` 无 error / G4 音轨齐）：
> `放行 = 人已批准 AND 判据通过`，见 `lvs/criteria.py`。
> 长跑的轨迹在 `.work/<任务>/logs/run-<日期>.jsonl`（一镜一行，可关）。

> **离线跑通全链**（无需 LLM / ComfyUI / 网络，秒级）：`[comfyui].backend="placeholder"`
> + `[tts].backend="silent"`。端到端测试 `tests/test_e2e_offline.py` 走的就是这条路
> —— 它只换"像素/声音从哪来"，其余全是真实代码。

> **给 Agent / 脚本用**：工作命令（`parse` / `shots` / `assets` / `voice` / `build` / `run` / `qc`）
> 都支持 `--json`，输出**统一结果信封**（`ok` / `status` / `exit_code` / `counts` / `next_hint`）；
> `lvs gate --json` 给门禁状态与下一步。信封在输出的**最后一段**（以 `{` 开头）。
> 只锁这 5 个决策字段，其余自由扩展 —— 见 `lvs/result.py`。
> 门禁带**自动判据**（G1 过契约 / G3 要 `lvs qc` 无 error / G4 音轨齐）：
> `放行 = 人已批准 AND 判据通过`，见 `lvs/criteria.py`。
> 长跑的轨迹在 `.work/<任务>/logs/run-<日期>.jsonl`（一镜一行，可关）。

> **离线跑通全链**（无需 LLM / ComfyUI / 网络，秒级）：`[comfyui].backend="placeholder"`
> + `[tts].backend="silent"`。端到端测试 `tests/test_e2e_offline.py` 走的就是这条路
> —— 它只换"像素/声音从哪来"，其余全是真实代码。

> **`.work/` 会长大**（一个 270 镜的集子跑完几百 MB）。用 `lvs clean` 清理：
> `--task <名>` / `--keep N` / `--all` 三选一，**默认只报告**，确认后加 `--yes`。
> ⚠ 任务目录里有 `shots.json`（可能是你手改过的）与全部中间产物 —— 删掉就得从零重跑。

> **多项目支持**：`lvs` 服务的**不止一本书**。一本书 = 一个素材库 + 一份项目 config。
> `[paths].lib` 一配，定妆库 / 定妆卡 / 项目风格文件全部自动派生；
> 画面风格有 30 个内置预设，也可用 `<素材库>/00-设定/风格预设.toml` 给某本书定专属风格
> （**不需要改代码**）。接入新项目：`lvs init "<素材库路径>" --name <项目名>`。

### 出图的两道闸门（v2）

一次 `lvs assets` 之前，有两个**会拦住你**的检查点 —— 这不是"建议"，是报错：

```powershell
# G2 定妆闸门：本集出现的人物必须已有冻结参考图
lvs cast --task NW01P1 --extract          # 从拍摄稿槽位 + 定妆卡登记人物
lvs cast --task NW01P1 --render NAOKO     # 出候选 → _cast/NAOKO/v1/cand-*.png
lvs cast --task NW01P1                    # 看审阅表 + 闸门状态
lvs cast --task NW01P1 --approve NAOKO    # 审阅通过 → 冻结为 ref-*.png
# 未批准时 `lvs assets` 会拒绝执行并打印怎么修；纯空镜集可 --no-cast-gate 跳过

# G3 生图巡检：每 N 张自动查一次（不必等全批跑完）
lvs qc --task NW01P1 --fix                # 彩度超标就地转灰度，原图留 _fixed/
```

**为什么值得多这两步**：2026-10-01 实测 —— 479 张图跑了 10 小时，跑完才发现整批提示词是旧版；
另一批因否定式描述（`no text` / `空无一人`）与年代道具淋洒，1780 条提示词全部污染。这两道闸门就是为了不再重演。

常用开关：

```powershell
--task NAME          本次产物落在 .work/NAME/（默认取脚本文件名）
--force              忽略已有产物与人工编辑，强制重做
--no-llm             拆镜不调 LLM，用启发式（没有 key 也能跑）
--no-library         本次不翻本地素材库
--only pexels,local  素材阶段只跑选中分支（studio 内部也会用）
--visual graphic     画面模式：graphic=图文图表感 / photo=实拍剧照感
--source local       实拍镜来源策略：auto=逐镜判定 / pexels=下载 / local=本地生图 /
                     library=本地素材库（未命中自动回退生图）
--style NAME         画面风格预设：historical-documentary（默认，历史纪录片感）/
                     jp-youth-manga-bw（日系青年黑白漫画风）；覆盖 config [shots].style
--demo               走路骨架：3 个硬编码分镜跑通全链路
--reindex            强制重建素材库索引
```

### 实拍镜从哪儿来：`--source`

这部片子里的**实拍镜**（图文/图表卡片不受影响）素材从哪儿取，可以一次性说清，
不用每次看 LLM 逐镜判：

```powershell
lvs run 拍摄稿.md --source local      # 全片实拍镜本地生图，绝不联网下载
lvs assets --task K005 --source library   # 改用手头的素材库；库里没有就回退生图
lvs shots  --task K005 --source auto      # 交还给逐镜判定（默认）
```

- 选择记进 `shots.json` 的 `source_mode`，重跑、续跑都一致；`config.toml [shots] source_mode`
  是全局默认，命令行 `--source` 临时覆盖。
- **强制**：选了 `pexels` 就只下载，连"先翻本地库"都不会发生 —— 否则画面是哪来的没法解释。
- `library` 多一步：库里没命中就**回退本地生图**，并在结束时报出是哪几镜回退了。
- 手工钉死的镜（界面里的「自己选一张图」）**永远优先**，不受策略影响。

### 手动向导 `lvs studio`（与全自动并存）

`lvs run` 一键到底；`lvs studio` **每步停下等你确认**，适合"图我要一张张看"：

```powershell
# 交互向导（终端里跑）
.venv\Scripts\python.exe -m lvs studio 拍摄稿.md --task K005

# 它会在这些点停下问你：
#   1) 实拍镜的素材从哪儿来？ 1) 自动判定 2) Pexels 3) 本地生图 4) 本地素材库
#   2) 素材看过了，接下来？ 1) 继续下一步（配音→合成） 2) 改几张图 3) 先停这里
```

**每个提问都有参数**，所以同一条命令也能脚本化（后台/非交互终端**必须给参数**，否则直接报错，不会挂住）：

```powershell
# 全片用本地生图，出完就停
.venv\Scripts\python.exe -m lvs studio --task K005 --source local --stop

# 进阶：只重渲图文卡片（--action 是"这次跑哪几支"的开关）
.venv\Scripts\python.exe -m lvs studio --task K005 --action graphic --yes

# 改图：按镜号重出。--prompt 改词 / --seed 换种 / 都没有则 seed+1 重抽
.venv\Scripts\python.exe -m lvs studio --task K005 --redo 7,12-15 --seed 99
.venv\Scripts\python.exe -m lvs studio --task K005 --redo 20 --prompt "a lone scholar under cold moonlight"
```

小贴士：

- `--action` 与 `--source` 是两回事：前者是"这次跑哪几支"，后者是"实拍镜该用哪个来源"。
  给了 `--action` 就按它跑，不额外替你定来源。
- 只勾了「本地生图」没勾「下载素材」时，原本 `source=pexels` 的镜会**自动改成本地生图**
  （不然它们永远缺素材）。显式给了 `--source` 就不需要这条兜底了。
- 改图会**连已合成的片段一起删**再重出，否则图换了但时长没变会被缓存复用。
- `--stop` 会打印续跑命令；下次 `lvs studio --task K005` 已完成阶段自动跳过。

## 图形界面 `lvs gui`

> ⏸ **已冻结（2026-10-03）。代码保留、能用，但不再投入。**
>
> **为什么**：这个项目的主要使用方式是 **让 agent 直接驱动流水线**（"拿拍摄稿生成视频"），
> 而不是人在界面里点。证据：`.work/` 里全部任务（MM01 / NW01P1 / NW02P2 / UGE01…）
> 都是命令行产物，没有一次 GUI 痕迹；而且 25 分钟的长视频要走 G0–G5 门禁、
> 几百镜、断点续跑 —— 这套体量本来就更适合命令行。
>
> 更关键的是：**界面里没有门禁 / 巡检 / 定妆**（`grep -rlE "gate|qc|cast" lvs/gui/*.py` 零命中），
> 也就是说它连本项目最值钱的三件事都点不到。要让它真有用，得先补齐这三样，
> 那是另一件事，等真有精力再说。
>
> **后果**：`lvs gui` 照旧可用，不删；但新功能不再往里加。
> **复活的判据**：如果哪天需要"把片子给别人看 / 让别人自己跑"，再回来补 ——
> 那时优先补的是门禁/巡检/定妆，不是排版。

不想敲命令就用界面。它**只是 CLI 的薄壳**：每个动作都等同于点一次 `lvs <阶段>`，
产物与状态仍以 `.work/<task>/` 为准 —— 所以界面里跑一半、切回命令行接着跑，完全没问题。

```powershell
.venv\Scripts\python.exe -m lvs gui              # 起服务并打开浏览器（默认 127.0.0.1:8730）
.venv\Scripts\python.exe -m lvs gui --port 8800  # 换端口（被占用会自动往后找）
.venv\Scripts\python.exe -m lvs gui --no-open    # 不自动开浏览器
```

界面里能做的事：

- **新建任务**：上传拍摄稿文件，或直接粘文本 → 存成 `.work/<task>/manuscript.md`；
  顺手选一次**实拍镜的素材来源**（自动/Pexels/生图/素材库）
- **跑阶段**：流水线上一格一个「运行」，带进度（素材 12/64 这种）与实时日志
- **改来源策略**：流水线上方那个下拉随时能改（记进 `shots.json`，重跑素材时生效）；
  「一键到底」会带着它一起跑（同 `--source`）
- **镜级审片**：分镜栅格看缩略图、按类型/来源/失败筛选、搜索旁白与提示词；
  点开可改 **提示词 / seed / 来源 / 旁白**，或「换一张」（seed+1 重抽）
- **自己挑一张图**：分镜弹层里可以直接从文件夹选一张图替换本镜画面 ——
  走 D14 的 `library_asset` 钉死，**之后重跑素材也不会被覆盖**（票 40）
- **看成片**：直接播放 `final.mp4`，可下载、可看 `subtitle.srt`
- **素材库 / 设置与体检**：素材库只读；设置页可**改白名单配置**（素材库位置、LLM/生图参数等，注释保留、密钥打码）

几条刻意的边界：

| 边界 | 为什么 |
|---|---|
| **只监听 127.0.0.1** | 它是本机工具，不给局域网开门，也不做鉴权 |
| **只写 `.work/<task>/` 与配置白名单** | 只有少数键可在界面改（素材库目录、LLM/生图参数），tomlkit 回写、注释保留、密钥打码；素材库只读（D13） |
| **同一时刻只跑一个阶段** | 8GB 显存下生图与 TTS 不可并存；排队只会让两个都变慢 |
| **关掉浏览器不影响任务** | 阶段跑在独立子进程里，界面只是订阅者；重开界面从产物与日志恢复现场 |
| **进度数盘上产物** | `shots.json` 只在阶段**结束时**才写，读它会一路卡在 0%（实测踩过） |

### 进度与看板

长视频跑起来动辄几十分钟，所以每个长循环都有进度条，另有独立看板：

```powershell
# 进度条：素材 / 配音 / 合成 / 拆镜 阶段自动显示
#   TTY 上原地刷新：[████████░░░░░░░░] 45% 122/270 ETA 12:30
#   重定向到日志时：每个整数百分比打一行（不刷屏、不进 \r）

# 看板：把任务进度摊成一块看板（纯读取，不改产物）
.venv\Scripts\python.exe -m lvs board --task K005

.venv\Scripts\python.exe -m lvs board --task K005 --html   # 另存 self-contained board.html
```

终端看板长这样：

```
看板 · 任务 long30
  ✅ 解析 已完成   正文 18 段
  ✅ 拆镜 已完成   654 镜
  🟡 素材 有失败   ████████████░░░░░░░░  162/654  失败 3
  ✅ 配音 已完成   ████████████████████  654/654
  ⬜ 合成 未开始
  分镜 654：已配 162（24%）｜未配 489｜失败 3
  来源：local 394，pexels 260
```

---

## 环境与依赖

| 依赖 | 状态 | 说明 |
|---|---|---|
| `ffmpeg` | ✅ 已自动接入 | 由 `imageio-ffmpeg`（PyPI）提供 **ffmpeg 7.1**（含 libass/zoompan/x264/aac/mp3）。无需系统安装 |
| `ffprobe` | ⚠️ 缺失可用 | 没有 ffprobe 时，时长/尺寸由 `ffmpeg -i` 降级解析，实测精度足够 |
| `edge-tts` | ✅ 已装 | 一期配音 + 词边界时间戳（**需要联网**） |
| LLM key | ⬜ 待填 | `config.toml [app].openai_api_key`，拆镜质量的关键 |
| Pexels key | ⬜ 待填 | `config.toml [pexels].api_key`，仅 `source=pexels` 的分镜需要 |
| ComfyUI + 模型 | ✅ 已装并**真机验证出图** | 代码在 `D:\ComfyUI`（独立 venv），模型见下「本地生图」 |
| Stable Audio 3.0 Small-Music | ⬜ 待下载 | `lvs bgm download`（HF 仓库 **gated**：先申请访问 + 配 `HF_TOKEN`；走 `hf-mirror.com`）。推理**走 CPU**，不抢 8 GB 显存 |

### 网络与镜像（重要）

> 若你所在网络访问 **github.com / huggingface.co 不通**（国内常见），下图是实测可用的替代源。
> 能直连的话，下面这些镜像**都不需要**，按官方方式装即可。

| 用途 | 可用源 | 实测速度 |
|---|---|---|
| 下模型 | **`modelscope.cn`**（首选） | ~5 MB/s |
| 下模型（备用） | `hf-mirror.com` | ~0.6 MB/s |
| 下 ComfyUI 源码 | `codeload.github.com` | — |
| Python 包 | `pypi.tuna.tsinghua.edu.cn`（pip 已配镜像） | — |
| CUDA 版 torch | `download.pytorch.org/whl/cu126` | 可直连 |

> ⚠️ 注意：**PyPI 镜像上的 `torch` 是 CPU 版**（`+cpu`）。装 ComfyUI 的 torch 必须显式指定
> `--index-url https://download.pytorch.org/whl/cu126`，否则会装成 CPU 版、`torch.cuda.is_available()` 为 False。

---

## 本地生图（ComfyUI + Z-Image Turbo）

**选型**（本机 RTX 4060 Laptop **8 GB** 显存 / 16 GB 内存）：

| 优先级 | 模型 | 说明 |
|---|---|---|
| 主力 | **Z-Image Turbo**（6B，Apache 2.0） | 8 步出图、**中文理解强**、可商用；int8 量化版对 8G 友好 |
| 备用 | **SDXL**（3.5B） | LoRA/ControlNet 生态最强；单文件、最稳 |
| 暂缓 | Qwen-Image（20B） | 8G 卡上量化到 Q2 才勉强，质量明显下降 |

### 模型落在哪

Z-Image 是**分体式**（3 个文件），加上备用 SDXL 共 4 个，都在 `D:\ComfyUI\models\`：

```
D:\ComfyUI\models\
├─ diffusion_models\z_image_turbo_int8_convrot.safetensors   6.2 GB   UNETLoader
├─ text_encoders\qwen_3_4b_fp8_mixed.safetensors             5.63 GB  CLIPLoader(type=lumina2)
├─ vae\ae.safetensors                                         0.34 GB  VAELoader（Flux ae）
└─ checkpoints\sd_xl_base_1.0.safetensors                     6.94 GB  备用
```

### 工作流模板

工作流是 `workflows/` 下**带占位符的 JSON 模板**，`config.toml [comfyui]` 指哪个就用哪个：

| 模板 | 用哪个模型 | 关键参数 |
|---|---|---|
| `zimage_turbo.json`（默认） | Z-Image Turbo（UNET+CLIP+VAE 三个文件） | `res_multistep` + `simple` + 8 步 + `cfg=1` + shift 3 |
| `sdxl.json`（备用） | `sd_xl_base_1.0.safetensors`（单文件） | `dpmpp_2m` + `karras` + 25 步 + `cfg=7` |

换成 SDXL：把 `config.toml` 的 `[comfyui].workflow` 改为 `sdxl.json`、`model` 改为 `sd_xl_base_1.0.safetensors` 即可。

### 安装步骤

```powershell
# 1) 拉模型（按 models.lock.json，默认走 ModelScope，支持断点续传）
.venv\Scripts\python.exe scripts\fetch_models.py             # 全部
.venv\Scripts\python.exe scripts\fetch_models.py --only sdxl  # 只要 SDXL
.venv\Scripts\python.exe scripts\fetch_models.py --only zimage
.venv\Scripts\python.exe scripts\fetch_models.py --source hf-mirror  # 换备用源

# 2) 启动 ComfyUI（关闭窗口即停止；显存吃紧加 --lowvram）
scripts\start_comfyui.cmd

# 3) 验证
.venv\Scripts\python.exe -m lvs doctor      # ComfyUI 一项应显示「通过」

# 4) 真机出图冒烟测试（一条命令确认能出图，不用跑整条流水线）
.venv\Scripts\python.exe scripts\smoke_imagegen.py
#   默认出 768×768 / 4 步的小图到 .work\_smoke\out.png；更接近实际：
#   .venv\Scripts\python.exe scripts\smoke_imagegen.py --size 1024 --steps 8
#   测备用模型：--workflow sdxl.json
```

### 参考：作者机器上的真机验证记录

| 项目 | 结果 |
|---|---|
| ComfyUI 启动 | `main.py --port 8188`，约 **35s** 就绪 |
| 出图 768×768 / 4 步 | **15.2s**，0.80 MB |
| 出图 1344×768 / 8 步（生产分辨率） | 正常，1.40 MB |
| 2 镜真实流水线 | `lvs assets → voice → build` 产出 `final.mp4`：画面轨 9.0s｜旁白 9.1s｜**偏差 0.03s**，真实 AI 画面 + 烧录中文字幕，**0 个占位帧** |
| 幂等 / 断点续跑 | 重跑 `lvs voice` 显示「合成 1，**跳过 1**」——只补失败的那一镜 |

模型对**中文提示词理解良好**（"九鼎""夯土城墙""黄昏金色余晖""写实电影感"都能准确落画面）。

### 踩过的坑（已解决，重装时照此避开）

| 现象 | 原因 | 解法 |
|---|---|---|
| ComfyUI 装不上 | `comfyui-workflow-templates` 的传递依赖要求 Python <3.12，与 3.12.7 冲突 | 从 requirements 剔除该包（只用 API 驱动，不需要示例工作流） |
| `import comfy_kitchen` 崩 | `comfy_kitchen` 要求 **torch ≥ 2.7**，而系统 torch 是 2.5.1 | ComfyUI 独立 venv 升级 torch |
| `torch.cuda.is_available()` 为 False | PyPI 镜像的 torch 是 CPU 版 | `pip install torch==2.14.0+cu126 torchvision==0.29.0+cu126 --index-url https://download.pytorch.org/whl/cu126` |

> ComfyUI 有**自己的 venv**（`D:\ComfyUI\.venv`），与项目主环境（`D:\LongVideoStudio\.venv`）相互隔离，
> 两边的 torch 版本不同是**有意为之**，互不影响。

### 安全停止长跑任务（重要）

`.venv\Scripts\python.exe` 是**启动器 shim**，真正干活的是它拉起的 **base 解释器**（即创建这个 venv 时用的那个 Python）。
所以「只杀掉 shim」（Ctrl-C 停外层、`timeout` 到期、任务面板里停 wrapper）**不会停掉 worker** ——
它会以**孤儿**身份继续跑：`lvs assets` 会继续出图、`lvs build` 会继续写 `final.mp4`。

```bash
# 1) 找到所有 lvs worker（含 base 解释器）
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*lvs *' } | Select ProcessId,CommandLine"
# 2) 杀整棵进程树（不是只杀 shim）
taskkill /PID <pid> /T /F
# 3) 核验：base 解释器确实消失（只杀 shim 时它还在）
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Select ProcessId,CommandLine"
```

> 即便出现孤儿也**不会产出错数据**：`assets` 以磁盘产物为状态，重跑只会**跳过已完成项**；
> 代价仅是 GPU 空转 + 产物上限不确定。ComfyUI 自己（`D:\ComfyUI\.venv`）不要顺手杀掉。

### 自己手动生图（可选）

你也可以先手动把图生好，再让流水线复用——`lvs assets` 的**「翻库优先」**就是干这个的。

**方式 A：图形界面手搓（最自由）**

启动 ComfyUI 后浏览器打开 `http://127.0.0.1:8188`，把 `workflows\zimage_turbo.json` 的用户版
拖进去即可（模板里 `{{...}}` 占位符手动填；或直接用 ComfyUI 自带的 Z-Image 模板）。
出图落在 `D:\ComfyUI\output\`。

**方式 B：命令行单张出图（推荐）**

```powershell
# 直接给提示词
.venv\Scripts\python.exe -m lvs image --prompt "宋代汴京街市，黄昏，写实电影感"

# 连出 4 张（seed 递增），指定尺寸
.venv\Scripts\python.exe -m lvs image --prompt "..." --count 4 --width 1344 --height 768

# 用某个分镜已经写好的提示词（先跑过 lvs shots）
.venv\Scripts\python.exe -m lvs image --task K005 --from-shot 12

# 用备用模型 SDXL
.venv\Scripts\python.exe -m lvs image --prompt "..." --workflow sdxl.json --model sd_xl_base_1.0.safetensors
```

产物默认落 `.work\_imagegen\`（或 `--out` 指定路径）。`lvs image --help` 看全部开关。

**方式 C：手生图直接喂给某个分镜（推荐）**

1. 把图丢进任意本地文件夹（例如 `D:\素材库\`）
2. `config.toml` 里配 `[library].dirs = ["D:\\素材库"]`
3. 编辑 `.work/<task>/shots.json`，给该镜加一行 `"library_asset": "D:\\素材库\\我的图.png"`
   （钉死后 `lvs assets` 一定用它，不再检索）
4. `lvs assets` → 该镜被复制/硬链到 `assets/library/`，`resolved_by=library`

> 钉死优于检索：`library_asset` 一旦非空，**跳过一切启发式**，想用哪张用哪张。

---

## 背景音乐 BGM（可选：本地生成 + 压低闪避）

> **为什么本地生成**：成片过去只有旁白，静得像有声书；而**可商用**的曲子在 B 站 / 抖音 / YouTube
> 各要单独买，换一本书还得重来。于是把"配乐"也当成流水线的一步 —— 零成本、可复现（同 seed 同结果）、
> 版权干净，而且**旁白永远是主角**。

```powershell
# 0) 装依赖（故意不进 `all`：stable-audio-tools 会拖 torch/transformers，GB 级）
#    这条会把推理链一起装上（stable-audio-tools → k-diffusion → torchsde → trampoline、
#    kornia / clean-fid / clip-anytorch 等）。本机 venv 为复用已有 torch 用了 `--no-deps`，
#    所以那几包是手工补的 —— 新机器按下面这条装即可。
# ★ 但 PyPI 上的 stable-audio-tools 0.0.19 **不支持 SA3**（没有 SA3 的 `taae_v2` 解码器，
#   也没有 `T5GemmaConditioner`）：本机装的是 GitHub main（0.0.20），即
#   `pip install --no-deps https://codeload.github.com/Stability-AI/stable-audio-tools/tar.gz/refs/heads/main`
#   （它的 `models/lora/__init__.py` 会 import pytorch-lightning，用不到时手工包 try/except）。
.venv\Scripts\python.exe -m pip install -e ".[bgm]"

# 1) 下权重 → models/stable-audio-3-small-music/（约 3.3 GB）
#    走 **ModelScope 国内镜像**（`stabilityai/stable-audio-3-small-music`，同内容、
#    非门控、**不需要 HF_TOKEN**）；只有 ModelScope 挂了才退回 hf-mirror（那条才要令牌）。
.venv\Scripts\python.exe -m lvs bgm download --config config.toml

# 2) 只看会用什么提示词 / 风格（**不加载模型**，秒出；调风格用这个）
.venv\Scripts\python.exe -m lvs bgm prompt --task UGE01 --config config.toml

# 3) 生成 → .work/<任务>/bgm/bgm.wav（+ bgm.json 元信息）
.venv\Scripts\python.exe -m lvs bgm --task UGE01 --config config.toml

# 4) 混进成片 → .work/<任务>/bgm_final.mp4（**原片保留不动**）
.venv\Scripts\python.exe -m lvs bgm mix --task UGE01 --config config.toml
```

**① 自适应文案风格**（讲稿什么调子，配乐就什么调子）—— 两层策略：

| 路径 | 什么时候走 | 做什么 |
|---|---|---|
| LLM | `config.toml` 填了 `[app].openai_api_key` | 让 LLM 把讲稿主题/情绪翻成 `genre / instruments / mood / bpm` + 一句英文提示词 |
| 规则兜底 | 没 key（或 LLM 失败 / `--no-llm`） | 内置"关键词 → 风格"表：历史/庄重→古典弦乐、激昂/热血→管弦乐、悬疑/惊悚→低沉电子、治愈/温情→钢琴、轻松/日常→轻快木管、悲伤/离别→室内乐、神秘/奇幻→氛围；全不命中 → 中性柔和保底 |

LLM 只是"更好"，不是"必须有"：**任何失败都会退回规则**，并把原因写进 `bgm.json.warnings`。

**② 音量适中、不打扰人声** —— `volume` 压低 + `sidechaincompress` 闪避（旁白为 key）：

```toml
# config.toml 里都是可选的，不给就用这组保守默认
[bgm]
volume_db      = -16.0   # BGM 基础音量（负值 = 压低）
duck           = true    # 人声一出现就把 BGM 再压下去
duck_threshold = 0.03    # 触发阈值（线性幅度，≈ -30 dBFS）
duck_ratio     = 8.0     # 压缩比
duck_attack    = 20.0    # ms
duck_release   = 400.0   # ms
duration       = 60      # 生成时长秒（夹在 15–180；混音时循环铺满整片）

# —— 生成参数：默认值照**模型卡**（models/stable-audio-3-small-music/README.md）——
# SA3 是 rectified-flow，采样器/步数是模型卡给的一套；SA2 那套（100 步 / cfg 6.0 /
# dpmpp-3m-sde）是给 v-diffusion 的，会走错采样分支且 CPU 上慢十倍。
steps          = 8       # 采样步数（CPU 上步数≈耗时）
cfg_scale      = 1.0     # 提示词贴合度
sampler        = "pingpong"
```

CLI 同名开关可临时覆盖：`--volume-db` / `--duck-threshold` / `--duck-ratio` / `--no-duck`
（实测：人声段 BGM 比静音段低 **6.4 dB**，关掉闪避是 0.00 dB）。混音用 `amix=normalize=0`
（ffmpeg ≥4.4 默认会把每个输入各除一半 —— 那就是"加了 BGM 旁白变小"的经典事故）。

**许可（务必看清，照权重自带的 `LICENSE.md` 原文）**：模型 `stabilityai/stable-audio-3-small-music`，
**Stability AI Community License** —— 个人 / 组织**年收入 < 100 万美元可商用**（**商用前要在
`stability.ai/community-license` 注册**）；**生成音频（输出）归使用者所有，用输出不必署名**；
只有**分发模型 / 衍生权重（含内嵌它的产品）**时才需要随附协议 + "Powered by Stability AI" 标注。
权重自带 T5Gemma 文本编码器（附 **Gemma Terms of Use**）。
这些都写进每份 `bgm.json.license`（含 `authoritative` 指向 `LICENSE.md`）；
换模型 = 换许可证，README 与 `lvs/bgm.py` 一起改。

**约束与失败降级**：

- **推理固定走 CPU**（`device="cpu"`）：8 GB 单卡被 ComfyUI(:8188) 与 Qwen3-TTS(:8100) 串行占用，BGM 不许抢显存 —— 代价是"每 30 秒音频约 1–3 分钟"，所以默认只生成 60 秒的 bed，混音时循环。
- **离线可用（实测）**：权重齐了（`model.safetensors` 2.27 GB + `model_config.json` +
  `t5gemma-b-b-ul2/` 1.18 GB ≈ 3.3 GB）就**完全不查 HF、不要 HF_TOKEN** —— `load_model` 把
  conditioner 的 `repo_id/subfolder` 改写成本地目录，`HF_HUB_OFFLINE=1` 下也能出 wav
  （UGE03 实测：`--duration 15 --no-llm` 全程 89 s，其中 8 步采样 17 s，其余是加载 3.3 GB 权重）。

- **torch 2.5 上的 T5Gemma 掩码补丁**：transformers ≥4.53 的 T5Gemma 编码器只在 `torch>=2.6`
  下能造掩码，本机 torch 2.5.1 会抛 `require torch>=2.6`。`lvs/bgm.py` 的
  `_install_t5gemma_encoder_mask_shim()` **只在进程内**把 2D padding 掩码折成 T5GemmaEncoder
  官方就支持的 4D dict 掩码（语义一致：双向 + 挡 padding，sliding 层再叠 |q-kv| < window），
  **不改 transformers 文件、不为它升级 torch**；装了 torch ≥2.6 后这段自动跳过。
- **推理参数以模型卡为准**：权重下下来后，同目录的 `README.md` 就是 SA3 的用法原文
  （`get_pretrained_model` + `generate_diffusion_cond_inpaint`，`steps=8 / cfg_scale=1.0 / sampler_type="pingpong"`）——
  我们按它取默认值，也优先用模型卡那个入口（老版本 SAT 自动退回 `generate_diffusion_cond`）。
- 权重没下 / 生成失败 **不静默**：打印“缺什么 + 下一步敲什么”（`lvs bgm download` 走 ModelScope，**不要令牌**），退出码 1；前置缺成片 / 缺 BGM 是退出码 2。

---

### LLM provider 路由（省钱：低风险调用走本地 ollama）

拆镜的每条 LLM 调用都在花钱，但真正需要大模型的只有两类：**beat 归位 / 分类**与
**逐镜提示词**（错了整片画面跑偏）。其余（BGM 风格、投稿标题/简介/标签、素材打标）
都是"结构化搬运"，且都带规则兜底 → 默认交给**本地 ollama**（`qwen2.5:7b-instruct-q4_K_M`）。

```toml
[providers]
mode = "auto"                 # auto（默认，按路由表）/ remote（全部远程，一键回滚）/ local（全部本地）
                              # 环境变量 LVS_LLM_PROVIDER 优先于这里
[providers.local]
base_url = "http://localhost:11434/v1"
model = "qwen2.5:7b-instruct-q4_K_M"

[providers.remote]            # 留空 = 沿用 [app] 段（密钥不搬家）
base_url = ""
model = ""
```

| 接入点 | 默认 | 说明 |
|---|---|---|
| `bgm.style` | **local** | `lvs bgm` 的风格提示词（7 条规则兜底） |
| `publish.meta` | **local** | `lvs publish --llm` 的标题/简介/标签 |
| `assets.tagging` | **local** | 素材筛选/打标（预留；当前没有真实调用点） |
| `shots.beats` / `shots.prompts` | remote | 拆镜，**不动**（质量敏感） |
| 其它 / 未登记 | remote | 保守默认：没声明的一律不切 |

- "LLM 配没配"仍由 `[app].openai_api_key` 决定：没 key = 没配 LLM（一切照旧走规则）；
  只有 `mode = "local"` 才允许无 key 直接用 ollama（离线跑法）。
- 本地调用**费用记 0**：`lvs cost` 另起一行显示"本地 N 次（省估算 $X）/ 远程 M 次"。
- ★ **本地 ollama 必须跑 CPU**（8 GB 单卡被 ComfyUI(:8188) / Qwen3-TTS(:8100) 串行占用）：
  设 `OLLAMA_LLM_LIBRARY=cpu` + `OLLAMA_VULKAN=0` 后重启 `ollama serve`，用 `ollama ps`
  确认 `PROCESSOR` 是 `100% CPU`（实测一句 JSON 约 16 s；GPU 占用 0）。
- 缓存键含 base_url：本地与远程各存一份，不会互相串味。单点覆盖写 `[providers.routes]`
  （键名带点号要加引号），例如 `"bgm.style" = "remote"`。

## 画面质量（拆 beat / 图文卡片 / 抗抖）

拍过真实成片后按三个症状定点修的，都**没加新依赖**：

| 症状 | 根因 | 现在怎么办 |
|---|---|---|
| 运镜时抖动 | ffmpeg `zoompan` 取整 → 推近变阶梯 | 超采样 2× 算再 lanczos 降回（抖动 −41%） |
| 图上冒出"格式转换器" | `[画面位]` 整段被当提示词 | 拆 beat + `sanitize()` 剥引号字幕/编辑动词 |
| 图上文字错乱（"西夏文"） | 大字画面不该交给扩散模型画 | 时间轴/对照/数字走本地排版卡片 |

相关配置（`config.toml`）：

```toml
[shots]
visual_mode = "graphic"          # graphic=图文图表感（默认） / photo=实拍剧照感

[build]
kenburns_supersample = 2          # 2=默认（抗抖，build 慢 7×）；1=旧行为；4=更稳但更慢
```

- **拆 beat**：`[画面位]` 一行按 `→` 拆，显式 `[场景]`/`[图表]` 标记优先，无标记走关键词兜底。
  模板头（`时间轴：`）与 ≤4 字数据碎片不拆，保持成一个 beat。
- **图文卡片**：`kind=graphic` 的 beat 由 `lvs/cards.py`（决定显示什么）+ `lvs/cardhtml.py`
  （HTML/CSS 出图）渲染成 1920×1080，落 `assets/graphic/`，**不碰 ComfyUI**。
  四套版式：`statement`（数据行）/ `compare`（左右对照）/ `timeline`（时间轴）/ `list`（条目）。
- **渲染方式**：首选**本机无头浏览器截图**（Edge / Chrome，可用环境变量 `LVS_BROWSER` 指定路径），
  观感上限高（真字体、渐变、自适应字号）；找不到浏览器就**自动回退 Pillow**，纯 Python 环境也能出片。
- **卡上只有内容**：没有段落标题（「第一段 · 钩子落定」这类拍摄稿脚手架不上屏），
  也没有旁白页脚（成片底部已烧同一条字幕）。指令式 beat（「三段原文并排浮现」）
  会自动回退取**所辖分镜的旁白**，把真实数据取出来。
- **一条 beat 一张卡**：同一 `[画面位]` 下同一 beat 常铺十几个镜（small3：33 图文镜只有 5 条 beat，
  其中一条铺 17 镜）。内容按 beat 算一次、只栅格化一次，其余镜复用同一张。
- **两种观感**：`visual_mode=graphic` 全走排版卡片；`=photo` 把 graphic 也交给扩散模型出实拍剧照。

**真机小样验证**（`small` 任务，110 镜：64 实拍 + 46 卡片，DeepSeek 走 LLM 拆镜）：

```
素材 110/110（local 64 + graphic 46，0 失败）
配音 110/110（整轨 447.2s，字幕 126 行）
成片 447.2s｜旁白 447.3s｜偏差 0.04s｜1920×1080 30fps + aac｜字幕已烧录
占位 0 个｜零帧片段 0 个
```

卡片视觉与内容在 `small3`（36 镜：33 图文 + 3 实拍）上重做过一轮：
33 个图文镜只栅格化 **5 次**、产出 33 个产物文件和 **5 张不同内容**的卡；
数据行实测不折行、不溢出（内容 bbox 单行带，左右边距 150px）。详见票 34/35。

过程中修掉的三处缺陷（卡片正文夹带剪辑动词、角标溢出边框、静止段整段 0 帧）记在票 31；
其中「静止段 0 帧」曾让成片只剩 64.7s 而旁白 447.3s —— 教训是**返回码 0 ≠ 产物有效**（D23）。

---

## 流水线

```
拍摄稿.md
   │  lvs parse      解析（段落/时间码/[画面位]/画面位清单；排除区段零泄漏）
   ▼
parse.json
   │  lvs shots      逐句拆镜 + 拆 beat（场景/图表）+ 英文提示词（清洗）+ 检索词 + source 判定
   ▼
shots.json  ★ 人工可编辑的真相源（改 source / library_asset 后重跑即可）
   │  lvs assets     素材解析：library_asset → source=library → 翻库 → Pexels / ComfyUI / 图文卡片
   ▼
assets/{library,pexels,local,graphic}/*  +  shots.json(写回 resolved_by/asset_path)
   │  lvs voice      逐镜 TTS → 时长写回 → 拼整轨 → SRT（词边界）
   ▼
audio/narration.mp3  +  subtitle.srt
   │  lvs build      逐镜切片 → Ken Burns（超采样抗抖）→ 拼接 → 混音 → 烧字幕
   ▼
final.mp4
```

### 关键设计

- **文件即状态**：`.work/<task>/manifest.json` 记录各阶段产物，支持幂等与断点续跑。
  删掉某阶段产物再 `lvs run`，只重跑该阶段及其下游（见 spec §7.1）。
- **失败隔离**：单镜素材失败只标记该镜，其余继续；缺素材的分镜合成时用占位帧，**不整片报废**。
  占位镜号记在 `segments/.placeholders.json`，**素材补齐后重跑 `lvs build` 会把占位帧换回真画面**（D17）——
  即「先 build 出草稿、再补素材、再 build」不会把成片卡在占位黑帧上。
- **本地素材库优先**：`lvs assets` 先翻你已下好/生成好的素材，命中即复用，**绝不改原文件**（复制/硬链接）。
- **8 GB 显存串行**：本地生图与本地 TTS 不可同时驻留。冲突判定靠**探测对端服务**
  （TTS 的 `/health`、ComfyUI 的 `/system_stats`），**不靠空闲显存数字** —— ComfyUI 出过图后
  常驻约 6 GB、空闲只剩 ~2.6 GB，那**是正常状态而非冲突**（用显存当闸门会把第 2 个分镜起全拦下，见 spec §11）。
- **不用 moviepy**：合成走 ffmpeg 原生，长视频下更稳、更省内存。
- **音画不漂移**：非末镜的片段时长 = 音频时长 + 镜间静音，画面轨总长与旁白严格对齐。
- **画面位 ≠ 提示词**：`[画面位]` 是给人看的编辑设计（`A → B → C`），**先拆 beat、再清洗**才进生图，
  否则编辑动词与引号字幕会整段漏进提示词（D18/D19）。
- **大字画面走排版**：时间轴/分栏/对照/数字这类"要字准确"的画面由 `lvs/cardhtml.py`（HTML/CSS +
  无头浏览器，缺浏览器时回退 `lvs/graphic.py` 的 Pillow 版式）本地排版，**不进扩散模型** ——
  扩散画中文必然错乱（D20/D27）。
- **运镜不抖**：默认超采样 2× 算 Ken Burns，把 `zoompan` 的取整误差压到亚像素（抖动 −41%，锐度不降）；
  嫌慢设 `build.kenburns_supersample = 1` 回退（D21）。

---

## 任务目录

```
.work/<task>/
├─ source.md            输入副本
├─ parse.json           解析结果
├─ shots.json           ★ 分镜（人工可编辑）
├─ audio/
│   ├─ shot-001.mp3 …   逐镜音频
│   ├─ shot-001.json    词边界时间戳（字幕用）
│   └─ narration.mp3    拼接整轨
├─ subtitle.srt         全程字幕
├─ assets/{library,pexels,local,graphic}/
├─ segments/            逐镜视频片段（.placeholders.json 记占位镜号）
├─ video-track.mp4      无声画面轨
├─ final.mp4            成片
├─ logs/                LLM 原始响应等排查材料
└─ manifest.json        阶段状态
```

---

## 测试

```powershell
.venv\Scripts\python.exe -m pytest tests -q           # 推荐
.venv\Scripts\python.exe -m unittest discover -s tests -v   # 等价（纯 stdlib）
```

覆盖：解析、拆镜（断句/引号保护/残片合并）、时间轴与滤镜串、字幕 SRT、素材库索引、
workflow 模板渲染（占位符/节点引用/提示词转义）等。
