# LongVideoStudio · 项目规程（长期）

> 细节看 `docs/`（全流程手册／格式契约／出图流程 v2／跨技能接线）。只留「改错了也不报错」的。

## 全流程（序列见 `docs/全流程流水线-编排手册.md`）
`lvs run` 的语义是**执行到下一道门为止**；逃生门 `--skip-gates`。

## 三条顺序（都真踩过）
1. `migrate` 从原稿重生成 `_v2/`，覆盖手工改写 → 顺序：`migrate` → 改否定式 →
   以 `_v2/` 为准，**不再重跑 migrate**。
2. 改定妆卡 → 必须重跑 `lvs cast --extract`，否则库里还是旧锚定（照用旧脸）。
3. 改锚定后旧候选要移进 `<角色>/_superseded-*/`，否则审阅表新旧混排。

## 退出码四态
`0` 成功｜`1` 有失败件（逐镜，`run` 继续并汇总返回 1）｜`2` 输入前置不对（停，`--keep-going` 可跳）｜
`3` 人审门禁（停，keep-going 也不跳）。`2`/`3` 必须分开（该等人 vs 该报警）。

## 架构：同一概念只许一处定义（`tests/test_architecture.py` 守着）
真源：阶段`stage.py`｜错误与退出码`errors.py`｜指纹`artifact.py`｜风格`styles.py`｜
阶段契约`handoff.py`｜熔断`breaker.py`｜门禁判据`criteria.py`｜结果信封`result.py`｜
轨迹`runlog.py`｜manifest 版本`workspace.py`。血缘 `mark_stage(inputs=…)`。

- ★ **目录型产物必须递归指纹**（`refs()` 记 `is_dir`）——目录 mtime 不反映子文件。
  踩过：改了图 `voice` 仍判"已完成"，拿旧图配音全程不报错。
- ★ `[paths].lib` 一配路径全派生；多项目化后必做"路径迁移遗漏"审计（抓过 3 处静默失效）。
- ★ 门禁 = **人批准 AND 自动判据**；判据是必要条件，判据自身出错按**通过**处理。
- ★ `run` 与直接敲单命令**共用** `stage.note_stage_end`（agent 是逐条敲命令的）。

## 环境（用户真实工作台）
- ComfyUI `D://ComfyUI//start_comfyui.cmd`:8188｜TTS `D://qwentts//lvs_tts_server.py`:8100
  （`openai_speech`/`voice-F-clean-youth`/clone）｜DeepSeek 拆镜已配 key。
- ★ 8GB 显存 → **生图与配音不可共存**：起 ComfyUI → `assets` → **关 ComfyUI** → `voice`。
- 书在 `D://fanshu//<书名>//10-语料//知识视频素材库//`，拍摄稿在 `05-拍摄稿/`。
- ★ 只有一份 `config.toml`（lib 固定指挪威的森林）→ 跑别的书会串库，须 `lvs init … --name`。
- ★ `source_mode="auto"` + pexels 无 key → 部分镜必失败（建议改 `"local"`）；
  风格 `jp-youth-manga-bw`，`monochrome` 是**风格属性**（决定 qc 查不查彩度）。

## 一致性三档（细节见 `docs/出图流程-v2-编排方案.md`）
L1 文字锚定 ✅｜**L2 参考图 img2img ✅ 已真机验证**｜L3 LoRA ❌（8G 训不了）。
★ `qc` 只查人脸**数量**（Q006/Q007），**没有**"两张脸是不是同一人"的校验。
★ 未批准的角色不给参考图（`bindable=False`）；定妆库 9 角色全部 `state=IMG` → 会被 G2 拦
（先 `lvs cast --approve`）。人脸巡检曾"静默死"（onnx 权重退化）→ 已改大声报。

## 工程纪律（踩出来的）
- ★★ **判据自己会说谎，先问「扫描范围覆盖到了吗」**：`grep` 正则转义写错 → 返回空 →
  据此宣布"零个人路径"，实际 5 个文件有；`test_architecture._modules()` 只 glob `lvs/*.py`
  → 整个 `gui/` 是盲区。**宣布"干净"前先用已知会命中的样本验证判据本身。**
- ★★ **「定义了却从不接线」是最大坑型**（`EXIT_FAILED` 从不返回、`cli.main` 从不调
  `exit_code_for`）。判据：**给上层决策用的公开函数，要么有调用点，要么在测试里。**
- ★★ **统计测试不能看 pytest 汇总行最后一个数字**（`3 failed, 80 passed` 会取到 `80 passed`）。
  正确：`grep -cE "^FAILED|^ERROR"`，**失败数为 0 才算通过**。
- ★★ **打包必须真建 wheel 验内容**：`packages=["lvs"]` 静默漏掉整个 `lvs.gui`，`pip wheel` 不报错。
- ★★ **`urllib` 不绕 localhost 代理，`requests` 绕** → 已建 `lvs/net.py`（本机 `ProxyHandler({})`）。
- ★ 校验器要**按消费者声明必需字段**；fail-closed 留出口（判据管 `approved` 不管 `--skip`）；
  写在文档里的规矩要同时写成测试，否则写的人自己不看。
- ★★ **缓存/复用判据必须带「参数指纹」**：`_audio_reusable` 只比 `voice` + 文本 hash →
  改 `[tts] rate/pitch/backend` 会**静默沿用旧音频**（票据 46 仍 open）。凡写
  `if 产物存在: 跳过`，先问"决定它内容的**全部**参数都在指纹里吗？"
- ★★ **原子写已收口到 `artifact.py`**（2026-10-05）：`atomic_write_text` / `atomic_write_bytes` /
  `commit_file`（外部程序写完再原子归位）。manifest / `shots.json` / `gates.json` / `final.mp4`
  全走它 —— **新增产物一律别再用裸 `write_text`**（半截文件 = 记录无声作废）。
  - ⚠ `.part` 交给外部程序（ffmpeg）写盘时**必须显式 `-f mp4`**：`.part` 扩展名推不出封装格式
    （实测 `Unable to choose an output format for 'final.mp4.part'`）。
  - ⚠ ffmpeg 返回 0 **也可能零产物**（磁盘满/权限/被拦）→ `_run_to_part` 已改抛 `FFmpegError`；
    否则裸 `FileNotFoundError` 会炸穿「BGM 失败退回无 BGM」外壳。
  - ⚠ **门禁账本坏了不静默重建为空**：现在会大声报 + 另存 `gates.json.corrupt` + fail-closed。
- ⚠ **不要用正则批量改源码**（改完不报错，下次导入才炸）；Markdown/TOML 用脚本改才安全。
- ⚠ 本机 RTX 4060 Laptop 8GB（可用常只 ~4GB）→ **生图并行非选项**；全量 pytest 慢，分组跑。

- ★ **测试分层**（2026-10-05）：日常 `pytest -m "not slow and not gpu"` = 1150 例 / **96 s**；
  全量 1207 例 / 255–276 s（只在改 `artifact`/`workspace`/`pipeline`/`build` 等公共底座时跑）。
  `-m slow` = 真 ffmpeg / 真子进程 / 真建 wheel / e2e；marker 定义在 `pyproject.toml`。
- ⚠ **桩必须「成功即产出」**：`tests/test_bgm.py` 的 ffmpeg 桩原先只记 args 不落文件，
  掩盖了「ffmpeg 返回 0 却零产物」这条真故障（#2 原子化时被它绊到，3 例假绿）。
- ⚠ **`Out-File -Encoding ascii` 会把 here-string 里的中文变成问号**（写补丁脚本中招一次）：
  改用 `-Encoding utf8`，并在脚本里自检「有没有中文被写成问号」；补丁脚本要幂等或先 `git checkout --`。
- ⚠ **`Out-File -Encoding utf8` 在 PS 5.1 会写 BOM**：往已有文件**中间**插内容时，
  BOM 就变成正文里的 `U+FEFF`（`Select-String -Pattern '^## '` 会直接漏掉那一行）。
  写“要给 git / 别的程序读”的文件用
  `[System.IO.File]::WriteAllText($p,$s,(New-Object System.Text.UTF8Encoding($false)))`，
  或落地后 `replace(chr(0xFEFF), '')` 清一遍。
- ⚠ **`-q` 叠在 `addopts="-q --tb=line"` 上 = `-qq`，末尾「N passed in Xs」汇总行会消失**：
  看到 `[100%]` 就没了，别以为跑挂了。要计数/计时用 `-o "addopts=--tb=line"` 覆盖；只判绿看退出码。

## 省 token 纪律（2026-10-04 复盘：一次返工烧掉大半天 token，7 条防复发）
1. ★★ **全量 pytest 只发一轮、用对解释器**：`.venv/Scripts/python.exe`（带 ffmpeg 等依赖，
   1123 全过）；**anaconda python 必假败**（e2e 12 个全挂，还要再花 token 定性复跑）。
   实测教训：两轮全量**并行**各跑 2.5h = 双倍浪费 + 互相干扰出 20 个假 F。
2. ★★ **长任务一律 run_in_background 一次 + 等通知**：voice/build/pytest 10–16 分钟级，
   不要前台 timeout 被杀重跑（本轮 voice 前台被杀一次，白等 10 分钟）；不反复 block 轮询。
3. ★★ **阶段完成立即追加当日日志**（做了什么/实测值/下一步）：session 崩溃后**先读日志再动手**，
   不重新探索现场（本轮崩溃恢复重读代码 = 最大单笔开销）。
4. ★ **核验/手术脚本固化进 `.work/tools/`**（如 `tag_quote_shots.py` 带双断言）：
   下次同类活直接复用，不重写；脚本内自带断言，失败即崩，不产误导清单。
5. ★ **批量取数**：实测值（时长/计数/md5/指纹）一次脚本全打印，别一条条 grep 问；
   读文件先 `grep -n` 定位再 Read 局部，不整读大文件。
6. ★ **改动只做最小必要面**：修 parse 只动块语义一处 + 1 个回归测试；shots 手术只删不重排；
   每步断言过了才走下一步，避免回滚重做。
7. ★ **门禁 note 一次写全实测值**（删几镜/时长/残留计数），别事后发现写错再重批（多一轮调用）。
8. ★ **单会话目标 ≤ 2–3 小时，禁止跨夜**（成本 = 调用次数 × 常驻上下文，跨夜滚到 9 万+ 后每条小命令都背着它走）；
   自查一条命令：`./.venv/Scripts/python.exe .work/tools/audit_all_sessions.py`（input / cached% / 每模型调用 input 中位数 / 最贵时段 / 调用数）。详见 AGENTS.md「会话纪律」。

## 密钥与环境（2026-10-05 起）

- **密钥可以只活在环境变量里**：`LVS_OPENAI_API_KEY` → `app.openai_api_key`、`LVS_PEXELS_API_KEY` → `pexels.api_key`。**环境变量优先于文件**（`config._ENV_KEYS`），空串不生效。`lvs doctor` 会说"来自环境变量"（只说键名，**绝不打印值**）。
- **设置页的密钥框永远是空的**：回显值是掩码（`sk-1…cdef`），前端不把它填进 `value=`，后端 `configio.looks_masked()` 见到掩码就**拒绝写入**。看到掩码被当成新密钥提交 = 真 key 被不可逆覆盖（原值无副本）。改密钥必须填**完整新值**；留空 = 不改。
- **GUI 写请求有跨站防护**：`Origin`（无则 `Referer`）不是本机 → 403。curl / 脚本（没有这两个头）照常放行 —— 这是"防浏览器跨站"，**不是**鉴权。`Origin: null` 也拦。
- **只读标志必须真只读**：`shots --peek/--index` 走 `cli._shots_readonly`（因为 `shots` 在 `_ENVELOPE_COMMANDS` 里，不绕过就会 `note_stage_end` 写阶段状态）。

## 续跑只看这些（省 token，2026-10-05 起）

- `lvs status --brief`（6 行 / 4 任务）→ 再决定要不要看别的。
- 看分镜：`lvs shots --index`（516 KB → 18.6 KB）/ `--peek <id>`（约 300 token）。
- 读源码/文档：`lvs map show <file> <起> <止>` / `lvs map grep <正则>`。
- **别**：整读 `shots.json`（475 KB）、整读 `docs/` 审查报告、造 `*_numbered.txt`。

## 子进程纪律（2026-10-05 实测，`lvs/cardhtml.py`）

- `subprocess.run(capture_output=True, timeout=…)` **超时可能形同虚设**：子进程的**子孙**会继承 stdout 管道，`communicate()` 要等它们全退出（实测 1.5 s 的 timeout 等了 **29.4 s**）。
- 正解：输出落**文件**（不用管道）+ 超时后 `taskkill /F /T /PID`（Windows）杀整棵树，再 `proc.wait(timeout=10)` 兜底。判据：`tests/test_cardhtml_shoot.py`。
- 通用推论：**任何"起子进程 + 超时"的地方都要问一句"它会不会生孩子"**。

## 投稿物料（2026-10-04 起，`lvs publish`）
- ★ 成片后**一条命令**出投稿物料：`lvs publish --task X --config <cfg>` →
  `.work/<task>/publish/`（横/竖**带字封面** + 标题候选 + 简介 + 标签/话题）。
  **不要再手写 `08-投稿/*.md`，也不要再手工叠封面字**（002 期交付时"三行标题字尚未叠加"就是这个缺口）。
- ★ 封面三行字**一律取拍摄稿 `【封面文案】` 的 行1/行2/行3**；工具**不编封面字**
  （编了 = 绕开"文字锚定"的校验）。稿子没写才降级到【标题】卡，并给 warning。
- ★ 它是 **driver 不是 stage**：不进 `stage.ORDER`，不参与 `lvs run`，没有门禁（错了重跑 5 秒）。
- ★ `[publish].episode` 跟**本期**走，不跟书走 —— 换期忘了改，简介里会带上一期的期号。
- `--out <素材库>/08-投稿` 可顺带拷一份留档（文件名带任务前缀，多期共存不撞）。

## 讲书稿（newmuyu × MrBeast）
- ★ **野兽层只作用于「帧边界」**（标题/封面/冷开场/章节缝/收尾），**句子层归木鱼**。
- ★ **成片不用运镜**（用户 2026-10-04 拍板"运镜效果不好"，09:20 再确认"就和第二集一样"）：
  `build.kenburns = "none"` 写在**项目 config**（`config.雨月物语.toml` /
  `config.ugetsu.toml` 的 `[base]` 深合并只覆盖这一个键）。**所有期数（含 001 重编）一律
  纯静态拼图**；主 config.toml 的 `zoom-in` 是历史默认，别让项目配置漏继承。
- 只改 3 处：冷开场、章节缝、标题+封面；其余一字不动。节奏转译为**认知密度递增**，借结构不借语气。
- 跑 `retention_curve_checker.py` **必须先剥掉传达层元数据**（否则 0–30 秒判据看的是包装）；
  它的时间轴比真实偏长约 1.3×，且词表会漏判（赌注/预告写了也测不出）。
- 拍摄稿格式：章名**不能含「传达层」**（会顶掉 `一、传达层` 章）；章内标题用 `>` 行，
  不要 `###`（C031）或 `**加粗**`（C040）。时间码口径 = **正文非空白字符 ÷ 4.5**，
  `scripts/regen_timecodes.py` 可一次重算场景时间码 + 画面位清单 + 头部元信息。
