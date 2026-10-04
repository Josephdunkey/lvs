# CONTEXT

本项目的领域词汇表。写 issue 标题、测试名、假设时，**必须使用这里定义的词**，不要退化成同义词。

## 这是什么

`LongVideoStudio`（CLI 名 `lvs`）：把一篇已成稿的**长视频拍摄稿**，逐分镜地变成成片。

上游内容源在 `D:\fanshu\资治通鉴\10-语料\*\05-拍摄稿\*.md`（**只读引用，不修改**）。

## 词汇表

| 术语 | 定义 | 避免使用 |
|---|---|---|
| **拍摄稿** | 输入资产：一篇结构化讲稿 Markdown（含时间码、`[画面位]` 行、画面位清单表） | "脚本"（歧义：也指代码）、"文案" |
| **分镜 / shot** | 流水线最小单位：一个时间区间 + 一段旁白 + 一个画面意图 | "镜头"、"画面"（单独用时歧义） |
| **画面位** | 拍摄稿里 `[画面位] …` 那一行，是分镜画面的原始依据 | — |
| **画面位清单** | 拍摄稿文末的表格，给出每处画面位的时间/画面/素材建议 | — |
| **旁白 / narration** | 该分镜要被 TTS 念出来的文本 | "台词"、"解说词" |
| **素材来源 / source** | 单个分镜画面的取得方式，取值 `pexels`、`local` 或 `library` | "素材类型" |
| **素材库 / library** | 用户本机已下好/已生成的素材文件夹（图片+视频），在 `config.toml` 的 `[library].dirs` 指定；`lvs assets` 时优先翻找 | "素材目录"、"资源库" |
| **素材命中 / library hit** | 某分镜的关键词在素材库索引中匹配到可用文件，该镜不再下载/生图 | — |
| **excluded 区段** | 不进 TTS、不进字幕的讲稿部分（画面位、重参与点、转场/停顿提示、传达层、留存自检、史实核验） | "噪声" |
| **重参与点** | 讲稿里的互动设计（"如果你是…"），属 excluded 区段 | — |
| **成片** | 最终输出 `final.mp4`：画面 + 音频 + 烧录字幕 | "视频"（泛称） |
| **任务目录** | `.work/<task>/`，某一次运行的全部中间产物；不进 git | — |
| **生图** | 用本地 ComfyUI 出图，作为 `source=local` 分镜的画面（阶段 3 的 local 分支） | "AI 作图"、"画图" |
| **画面风格 / style** | 整片统一的生图风格**预设**（`shots.STYLES`）：`historical-documentary`（默认）/ `jp-youth-manga-bw`（日系青年黑白漫画风）。`--style` 或 `config [shots].style` 选定，名字记进 `shots.json` | "滤镜"、"画风"（笼统） |
| **workflow 模板** | `workflows/*.json`：ComfyUI 的 API 图 + `{{PROMPT}}`/`{{SEED}}`/`{{CKPT}}` 等占位符。**换模型 = 换模板 + 改 `[comfyui]` 配置**，不改代码 | "工作流文件"、"comfy 配置" |
| **手动出图** | 用户自己先出好图（`lvs image` 或 ComfyUI 界面），再让流水线复用：放进素材库或给分镜钉 `library_asset` | "预生成" |
| **钉死 / pin** | 分镜的 `library_asset` 字段非空时，`lvs assets` 跳过一切启发式，直接用该文件 | — |
| **看板 / board** | `lvs board` 把任务进度摊成一块看板（阶段状态 + 分镜统计）；`--html` 另存单文件 HTML。**纯读取** | "仪表盘"、"面板" |
| **阶段状态** | `manifest.json` 里每个阶段的 `status`：`done`（无失败）/ `partial`（有失败镜）/ `skipped`。`partial` 一律视作未完成、可重跑 | "进度" |
| **画面位** | 拍摄稿里 `[画面位]` 标出的编辑设计（`A → B → C`）；**不是提示词**，须拆 beat + 清洗后才能用 | "镜头描述" |
| **beat（拍点）** | `[画面位]` 按 `→` 拆出的最小画面单元，带类型 `scene`/`graphic`；一个分镜可含多个 beat | "子镜头" |
| **图文卡片** | `kind=graphic` 的 beat 由 `lvs/graphic.py` 用 Pillow 排版本地渲染的 1920×1080 卡片（timeline/numbers/columns/flow/card）。字一定对 | "图表"、"字卡" |
| **超采样** | build 时为消运镜抖动，预放大 `max(2,N)×` 再 zoompan、lanczos 降回成片尺寸；`build.kenburns_supersample` 控制 | "抗抖动" |
| **向导 / studio** | `lvs studio`：素材段的手动检查点流程（取素材 → 出完再问继续/改图）；与全自动 `lvs run` 并存 | "手动模式" |

## 关键决策（ADR 前身，尚未落到 docs/adr/）

- **D1 代码自持**：不 fork `D:\MoneyPrinterTurbo`，只参考其服务分层。
- **D2 CLI 优先，GUI 是薄壳**：CLI 永远是一等公民 —— 每阶段产物落盘、可编辑、可重跑。图形界面（`lvs gui`）**只做 CLI 的薄壳**：一律经由 `python -m lvs <阶段>` 子进程驱动，**不 import、不复制任何阶段内部逻辑**，状态仍以 `.work/<task>/` 为唯一真相源。（原先写的"本期不做 GUI"已被票 36 取代 —— 放开的是"可以有界面"，不是"可以有两套逻辑"。）
- **D3 生成粒度 = 逐分镜**：音频逐镜合成，每镜音频时长即该镜画面时长。
- **D4 素材来源自动判定 + 手动覆盖**：默认 LLM 判定，改 `shots.json` 的 `source` 字段后重跑 `lvs assets`。
- **D5 本地生图栈**：ComfyUI + Z-Image Turbo（主力）/ SDXL（备用）；Qwen-Image 暂缓（8G 显存不够）。
- **D6 8 GB 显存串行**：生图与 TTS 不得同时驻留，阶段间必须显式释放；冲突时报错而非自动抢占。
- **D7 不用 moviepy**：合成改用 ffmpeg 原生，长视频下更稳定。
- **D8 字幕**：edge-tts 词边界为主，faster-whisper 回退。
- **D9 本地素材库优先**：`lvs assets` 解析素材时按 `钉死 > source=library > 翻库命中 > source 分支` 顺序；素材库文件夹后续指定（默认空 = 功能关闭），命中为复制/软链，**绝不改用户原文件**。
- **D10 编排以文件为状态**：不照抄 MoneyPrinterTurbo 的内存状态 + 单函数线性执行；改用 `.work/<task>/manifest.json` + 阶段化 + 幂等 + 失败隔离（§7.1）。
- **D11 工作流即模板**：ComfyUI 的图以「带占位符的 JSON」存于 `workflows/`，随仓库版本管理；`lvs/imagegen.py` 只负责替换占位符与走 HTTP，**不含任何模型专用节点知识**。这样换模型不动代码。
- **D12 模型不入库**：权重数十 GB，不进 git；用 `models.lock.json` 声明清单 + `scripts/fetch_models.py` 拉取（首选 ModelScope，备用 hf-mirror）。
- **D13 上游素材只读**：素材库命中一律复制/硬链到 `.work/<task>/assets/`，**绝不修改用户原文件**（与 D9 同一原则）。
- **D14 手动优先**：用户手工出图/挑图是一等公民。`lvs image` 提供命令行出图；`library_asset` 提供"用哪张我说了算"的钉死能力；两者都**不依赖 LLM**。
- **D15 阶段产物即校验**：`manifest.json` 的 `outputs[]` 记**每镜产物**（不是笼统的 `shots.json`），阶段可跳过的充要条件是「`status==done` 且 outputs 全在」；有失败镜的阶段标 `partial`，一律重跑补缺（§7.1）。删掉任一产物即触发该阶段重跑。
- **D16 模型参数住在模板里**：采样器/步数/cfg/shift 等**模型专用参数写在 `workflows/*.json` 的字面量里**，`lvs/imagegen.py` 只留通用旋钮与文件回退值，不含模型专用知识（D11 的具体落实）。
- **D17 占位不粘人**：缺素材的分镜在 `build` 时生成占位黑帧以保音画同步，占位镜号记于 `segments/.placeholders.json`；**占位片段一旦本镜补上真素材就重渲**（时长校验挡不住它，必须靠这份登记表判定）。即「先 build 后补素材」能自动补上真画面。
- **D18 画面位是编辑设计，不是提示词**：`[画面位]` 原文（`A → B → C`）是给人看的编辑意图，**不能整段当提示词**喂扩散模型（正是"格式转换器"上图的根因）。必须**按 `→` 拆 beat**，再逐 beat 判类型、清洗后才进提示词（票 27）。
- **D19 提示词清洗**：进生图前剥掉引号内字幕文本、箭头、编辑动词（高亮/砸屏/分栏/浮现/逐行）；只留可拍语言。负向提示词**分模板而定**：`zimage_turbo.json` 用 `ConditioningZeroOut` 且 `cfg=1` → 负向**无效**；`sdxl.json` 备用模板是真 `CLIPTextEncode`、`cfg=7` 读 `{{NEGATIVE}}` → 负向**真实生效**（`lvs/imagegen.py` 的 `NEGATIVE` 默认值与配置映射正是为它保留）（票 27/29）。
- **D20 大字画面归排版，不归扩散**：时间轴/分栏/对照/数字这类"要字准确"的画面，走**本地排版卡片**（Pillow，1920×1080），不进生图 —— 扩散模型画中文必然错乱（"西夏文"根因）。`shots.visual_mode` 可选 `graphic`（图文感）/ `photo`（实拍感）（票 28）。
- **D21 抖动是量化误差，用超采样压**：Ken Burns 抖动源于 `zoompan` 取整，非编码问题。默认 `build.kenburns_supersample=2`（预放大算、lanczos 降回），CoV −41%、锐度不降；代价 build 慢 7×，回退设 `=1`（票 29）。
- **D22 手动向导与全自动并存**：`lvs run` 一键到底、不问；`lvs studio` 每步停下确认（取哪些素材 → 出完再问继续/改图）。二者**共用同一批阶段模块**，只是编排不同 —— 手动流程不复制任何生成逻辑（票 30）。
- **D23 返回码 0 ≠ 产物有效**：阶段产物必须**真的读一遍**（时长/可解码）才算数，不能只看子进程 rc。教训来自票 31：ffmpeg 在"一帧都没编"时**仍以 rc=0 退出**，46 个 261 字节空壳被当成成功，成片画面 64.7s 而旁白 447.3s。所有渲染产物一律过 `assert_segment_rendered()`；单镜失败退回占位帧，一镜坏不毁整片。
- **D24 两个方向相反的文本清洗**：进**生图**的文本要**删**引号内容（否则被画成伪汉字，用 `sanitize()`）；上**图文卡片**的文本要**留**引号内容（那正是要展示的数据，用 `card_text()`）。同名同形的输入，处理恰好相反 —— **不可复用同一个函数**。
- **D25 阶段状态要如实记账**：`mark_stage` 的 `status` 必须反映**实际结果** —— 有失败镜/有占位片段一律 `partial`（`stage_status(n)`），绝不能默认 `done`。`is_stage_done` 只在 `status=="done"` 且登记产物齐全时为真。漏记会让 `lvs run` 整段跳过、缺口永补不上（票 33.1）。同一条账也约束 `build`。
- **D26 只读命令不落盘**：`lvs board` 这类「看一眼状态」的命令用 `Workspace.read()` —— 有 manifest 才加载，**绝不建目录/写 manifest**。只有真正会产出新状态的阶段才 `ensure()`（票 33.2）。
- **D27 卡片渲染优先 HTML/CSS**：图文卡片用**本机无头浏览器**截图（`lvs/cardhtml.py`，Edge/Chrome，`LVS_BROWSER` 可指定），Pillow 版式降为**回退**。两条路吃的是**同一个 `cards.Card`**，所以内容逻辑只有一份，换渲染器不会让卡上出现的东西变样。字号不靠 Python 估宽（必然估不准），由**页内脚本实测收敛**。浏览器解析 `--screenshot=` 用自己的 CWD，**路径必须绝对化**（票 34）。
- **D28 卡上只有内容，没有脚手架**：段落标题（「第一段 · 钩子落定」）是拍摄稿的编辑脚手架，旁白页脚与成片烧录字幕重复且会撞边框 —— **两者都不上卡**。`cards.Card` 只有 `kind/items/source` 三个字段，物理上没地方放它们。指令式 beat（「三段原文并排浮现」）回退取其**所辖分镜的旁白**，把真实数据取出来（票 34/35）。
- **D29 一条 beat 出一张卡**：同一 `[画面位]` 下的同一 beat 常铺十几个镜（small3：33 个图文镜只有 5 条 beat，其中一条铺 17 镜）。**内容按 beat 算一次、PNG 只栅格化一次**，其余镜复制复用；但产物仍按镜落盘（`shot-NNN.png`），以保住「删产物即重跑」的语义（票 35）。
- **D30 GUI 的五条边界**（票 36/37）：①**只监听 127.0.0.1**，不做鉴权、不开局域网；②**只写 `.work/<task>/` 与 `config.toml` 的可编辑白名单子集**（`library.dirs`/LLM/生图等少数键，tomlkit 回写、**注释保留**、密钥打码；其余配置与素材库只读）；③**全局串行**，同一时刻只允许一个阶段在跑（8GB 显存下生图与 TTS 不可并存，spec §11），不做排队；④**进度数盘上已落盘的逐镜产物**，不读 `shots.json` —— 它只在阶段**结束时**才写，读它会一路卡在 0%；⑤阶段跑在独立子进程，**关掉浏览器或服务都不影响它**，重开界面从产物与日志恢复现场。
- **D31 意向与现实分家——`source_mode` vs `source`**：`source_mode`（`auto`/`pexels`/`local`/`library`）是**任务级意向**，记在 `shots.json`；`source` 是**逐镜现实**，被 `branches_for`/`existing_asset`/`--only`/界面筛选直接消费。落成动作只有一处 `sources.apply_mode()`（票 41）。优先级只有一处 `sources.resolve()`：**命令行 > 任务里记着的 > 配置默认**（记着的若为 `auto` 视为"没记过"，因为 `shots` 每次都会写这个字段；显式的 `--source auto` 一定生效，界面靠它切回自动）。配置那一层只对 `shots` 阶段有意义 —— `assets` 是执行层，只认 `--source` 与已记下的，免得改了配置就悄悄推翻某个任务已定好的选择。三条边界：只作用于**实拍镜**（图文 beat 归排版，D20）、不碰**手工钉死**的镜（`source_pinned`，D14：翻库命中**不算**钉死，否则库模式跑一次就再也切不走）、**强制就是强制**（选 `pexels` 连 §8.1 的「翻库优先」也关掉，否则画面哪来的没法解释）。
- **D32 库未命中回退生图要留痕**：`source_mode=library` 时库里没命中 → 自动回退本地生图，但**`source` 保持 `library`**（意向不变），只把 `resolved_by` 记成 `local`；`branches_for(mode=library)` 返回 `("library","local")` 两支，回退产物因此**可复用、重跑幂等**。回退**必须在结束报告里逐镜列出** —— 静默补上会让人以为库里真有这些素材（票 41）。**切回 `auto` 也要认这份留痕**：`apply_mode(auto)` 不重写逐镜 `source`，所以回退过的镜在 `auto` 下 `source` 仍是 `library`；若只按 `source` 判定，切回自动会把它们全判死 —— 而图就躺在 `assets/local/` 里。故 `branches_for` 与 `resolve_shot` 都要对 `resolved_by == "local"` 放行（`library` 支 + `local` 支，产物没了就重新回退）。**这不改**人手把 `source` 改成 `library` 的镜在 `auto` 下"未命中即报错"的语义 —— 那种镜没有这份留痕（票 41 第五轮审查，`review-two-axis-5.md` T1）。
- **D33 画面风格是具名预设，不是常量**：整片生图风格收在 `lvs/shots.py` 的 `STYLES` 预设表（`Style(name, suffix)`）里，取用优先级 `--style` > `config [shots].style` > 默认 `historical-documentary`；**未知名字报错**（`KeyError`，`run_command` 列出可选项并返回 2），不静默退回默认 —— 整片风格悄悄跑偏要等全出完图才发现。预设须在**三处**一致生效：无 LLM 的 `heuristic_prompt()`、LLM 的提示词规则 `_rules()`、LLM 漏字段时的兜底；风格名写进 `shots.json` 与 manifest `shots` 阶段留痕（票 44）。
