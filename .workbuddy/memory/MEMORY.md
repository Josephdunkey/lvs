# LongVideoStudio · 项目规程（长期）

> 细节看 `docs/`。只留「改错了也不报错」的。

## 流程与门禁
- `lvs run` = 执行到下一道门为止；逃生门 `--skip-gates`。
- ★ migrate 顺序：`migrate` → 改否定式 → 以 `_v2/` 为准，**不再重跑 migrate**（会覆盖手工改写）。
- ★ 改定妆卡必重跑 `lvs cast --extract`；改锚定后旧候选移 `<角色>/_superseded-*/`，否则审阅表新旧混排。
- 退出码：`0` 成功｜`1` 逐镜失败（run 继续并汇总）｜`2` 输入前置不对（停，`--keep-going` 可跳）｜`3` 人审门禁（停，不跳）。**2/3 必须分开**。
- 门禁 = 人批准 AND 自动判据；判据自身出错按通过；`run` 与单命令共用 `stage.note_stage_end`。
- ★ **G2（定妆）的指纹 = 本集槽位级**（`cast.slots_fingerprint()`：本集槽位名+state+锚定+参考图 stat+禁项）。**别改回**「指纹全库 `_cast/lock.json`」——那会让任意一集增删人物把其余各集全判失效（2026-10-06 实测：008 加 4 人、009 加 2 人各触发一轮 UGE01-UGE09 重批）。判活/批准的唯一入口是 `pipeline.subject_state()`。
- ★ 拆镜分批 `[shots].llm_batch = 8`：一批 20 镜会撞 deepseek-chat 的 4096 输出上限、响应截断、`lvs shots` 退 2（整段拆镜白跑）。**别无脑调大**；要省钱试 12，失败也只是退 2 不出坏产物。

## 架构（`tests/test_architecture.py` 守）
真源：`stage`/`errors`/`artifact`/`styles`/`handoff`/`breaker`/`criteria`/`result`/`runlog`/`workspace`；血缘 `mark_stage`。
- ★ **目录型产物必须递归指纹**（`refs()` 记 `is_dir`）——踩过：改了图 `voice` 仍判完成、拿旧图配音全程不报错。
- ★ **缓存复用必须带参数指纹**——`_audio_reusable` 漏了 `[tts] rate/pitch/backend` 会静默沿用旧音频（票据 46 open）。凡写 `if 产物存在: 跳过`，先问"决定内容的全参数都在指纹里吗"。
- ★ **原子写收口 `artifact.py`**（`atomic_write_text/bytes`、`commit_file`）；新产物禁裸 `write_text`。ffmpeg 写 `.part` 必须显式 `-f mp4`；ffmpeg 返 0 也可能零产物（`_run_to_part` 抛 `FFmpegError`）；`gates.json` 坏不静默重建为空。
- ★ `[paths].lib` 一配全派生；多项目化后必做路径迁移遗漏审计。风格 `jp-youth-manga-bw`，`monochrome` 是风格属性。

## 环境
- ComfyUI `D://ComfyUI//start_comfyui.cmd`:8188｜TTS `D://qwentts//lvs_tts_server.py`:8100（Base 模型 clone，勿起 CustomVoice 会 500）。8GB 显存 → **生图与配音不可共存**（起 ComfyUI → assets → 关 → voice）。
- 书在 `D://fanshu//<书名>//10-语料//知识视频素材库//`；`config.toml` 的 lib 固定指挪威的森林，换书须 `lvs init … --name`；`source_mode` 建议 `"local"`（auto+pexels 无 key 必失败）。
- **解释器只用 `.venv/Scripts/python.exe`**（anaconda python 全假败）。CLI：`./.venv/Scripts/python.exe -m lvs <cmd> --config <cfg>`。

## 一致性
L1 文字锚定 ✅｜L2 参考图 img2img ✅｜L3 LoRA ❌（8G 训不了）。`qc` 只查人脸**数量**、不查同一性；未批准角色无参考图（9 角色 state=IMG → 先 `lvs cast --approve`）。

## 工程纪律（最狠几条）
- ★★ **判据自己会说谎**：宣布"干净/零命中"前，先用已知会命中的样本验证扫描器本身（grep 转义错、glob 漏目录都假绿过）。
- ★★ **「定义了却从不接线」是最大坑型**：给上层决策用的公开函数，要么有调用点，要么在测试里。
- ★★ **pytest 计数看 `grep -cE "^FAILED|^ERROR"`**，失败数 0 才过；不看汇总行最后一个数字。
- ★★ **打包必须真建 wheel 验内容**（`packages=["lvs"]` 曾静默漏整个 `lvs.gui`）。
- ★ **桩必须「成功即产出」**（ffmpeg 桩只记 args 不落文件 → 3 例假绿）。
- ★ `urllib` 不绕 localhost 代理 → `lvs/net.py`。★ 写进文档的规矩要同时写成测试。
- ⚠ 不用正则批量改源码（下次导入才炸）；`Out-File -Encoding ascii` 吞中文、PS5.1 的 utf8 带 BOM → 用 `[IO.File]::WriteAllText($p,$s,UTF8Encoding($false))`。
- ⚠ `-q` 叠 `addopts` 变 `-qq` 汇总行消失；只判绿看退出码。
- ⚠ `subprocess.run(timeout=)` 形同虚设（子孙继承管道）→ 输出落文件 + `taskkill /F /T /PID`。

## 省 token / 会话纪律
- 日常测试一条：`pytest -m "not slow and not gpu"`（1281 例/≈120 s）；只有改 `artifact`/`workspace`/`pipeline`/`build` 公共底座才全量（255–276 s）。长任务 `run_in_background` 一次等通知，不轮询不重跑。
- 读大文件走摘要：`lvs status --brief`、`shots --index/--peek`、`lvs map show/grep`；**禁**整读 `shots.json`(475KB)、`docs/*.md`(33万 token)、会话 jsonl。
- 阶段完成立即追加当日日志；核验脚本固化 `.work/tools/`；批量取数一次打印。
- **单会话 ≤2–3 h，禁跨夜**；自查 `.work/tools/audit_all_sessions.py`。

## 密钥与只读
- `LVS_OPENAI_API_KEY` / `LVS_PEXELS_API_KEY` 环境变量优先于文件；`lvs doctor` 只说键名绝不打印值。
- 设置页密钥框永远空：回显是掩码，见掩码即拒写；**改密钥必须填完整新值**（原值无副本，填错=不可逆覆盖）。
- GUI 写请求跨站防护：Origin/Referer 非本机 → 403（curl 无头照常放行，不是鉴权）。
- `shots --peek/--index` 走 `cli._shots_readonly`（否则绕过 `_ENVELOPE_COMMANDS` 会写阶段状态）。

## 讲书稿（newmuyu × MrBeast）
- 野兽层只作用于帧边界（标题/封面/冷开场/章节缝/收尾），句子层归木鱼；只改 3 处，其余一字不动。
- **成片不用运镜**：`build.kenburns = "none"` 写项目 config，所有期数纯静态拼图（主 config 的 `zoom-in` 是历史默认）。
- 拍摄稿：章名不能含「传达层」；章内标题用 `>` 行（非 `###`/加粗）；时间码 = 正文非空白字符 ÷ 4.5（`scripts/regen_timecodes.py` 重算）。`retention_curve_checker.py` 先剥传达层元数据。

## BGM
- 权重走 **ModelScope**（`lvs bgm download` 首选，非门控、不要 HF_TOKEN；hf-mirror 只是备选），落 `models/stable-audio-3-small-music/` ≈3.3 GB；加载完全离线。
- torch 2.5 + transformers 4.57 靠 `lvs/bgm.py` 的 T5Gemma 掩码补丁（torch≥2.6 自动跳过）；stable-audio-tools 要 **0.0.20 GitHub main**（PyPI 0.0.19 无 SA3）。
- 本 venv 是 `--system-site-packages`：**勿装 pytorch-lightning/torchmetrics**（拖 matplotlib → numpy ABI 炸）。
- 实测（CPU）：15 s 音频生成 89 s；`bgm mix` 对 1519 s 成片 189 s（原片保留）。

## 投稿与产物契约
- 成片后一条命令 `lvs publish --task X --config <cfg>` → `.work/<task>/publish/`（已叠字封面+标题/简介/标签）；**不再手写投稿 md、不再手工叠字**。封面三行取拍摄稿【封面文案】，工具不编字。`[publish].episode` 跟本期不跟书。
- `schemas/*.json` 四份契约与 `handoff.validate_shots` **互为镜像**，改任一边两边同改（`tests/test_schemas.py` 有双向红判据）；改 `schemas/` 顺手跑 `tests/test_packaging.py`。
- `lvs config check`：必填+类型错=error(1)，未知键只 warn；`lvs qc --final` 只报警不改判、只读不进 G5。

## 2026-10-06 新增（第 24 段会话）

- **assets 的 GPU 成本 = 去重后的画面位数，不是镜数**。006 实测：375 镜 → 368 个文件，其中只有 **58 张不同**；007 583 镜 / 81 画面位。进度条按「镜」报，ETA 会严重高估（007 先报 6:20，后降到 1:13）。判进度看 `assets/local` 的文件数增长，别信 ETA。
- **`lvs check <拍摄稿.md>` 的三条硬口径**（009 起沿用）：① 单段 > 200 汉字必须配画面位；② 画面位密度 ≈ **每 100–115 汉字一条**（低于此会 warn）——**⑤ 评述段也要配画面位**（器物／空镜／复用人物镜）；③ 画面位禁写否定式（「没有人」「不见」会被按字面画出来）。
- **`lvs check` 的槽位库＝`lvs cast --extract` 锁定的库**，不是定妆卡 md。新槽位顺序：写定妆卡 → `lvs check`（此时会报 C026，正常）→ `lvs parse <拍摄稿> --task X` → `lvs cast --extract --task X` → 再 `lvs check` 应全绿。
- **`make_shot_0NN.py` 的时间码 bug**：第一循环把 `[画面位]` 行也算进 `used`，导致子节时间码漂移（006–009 的旧稿有此现象）。已在 `make_shot_010.py` 修（加 `elif line.strip().startswith(WZ): continue`）。**旧稿不要回头改**（会动到已批指纹）。
- **011《天津处女》立论与分章正文不符**（详见总表下注）：正文没有「不肯改姓」情节，实写三朝唐化与良岑宗贞／遍昭。写 011 之前必须先重定立论。【信息不足】
- 010 新增定妆 4 人：`{JOKO}` 平城上皇 / `{YAKO}` 药子 / `{NAKANARI}` 藤原仲成 / `{SAGA}` 嵯峨天皇（已 extract 入册，待 GPU 出候选）。

## 2026-10-06 第 25 段会话（定妆卡 ID 契约坑 + 两处回归）

- ★★ **定妆卡的槽位名由 `{SLOT}` 决定，不由罗马字推断**。`cast._id_from_display` 早期**只认括号里的拉丁别名**，于是 `良岑宗贞（… / Munesada / … / {SORIN}）` 登记成 `MUNESADA`、`小野小町（… / Ono no Komachi / {KOMACHI}）` 登记成 `ONO`、`纪贯之（… / Ki no Tsurayuki / … / {KIYOYUKI}）` 登记成 `KI`；而 `### 屋秋津 / 海盗（{PIRATE}）` 因为标题里多了 `/ 海盗`，整节**根本没被解析**（`_CARD_CHAR_HEAD` 要求名字后**直接**跟括号）。后果全是静默的：拍摄稿里的 `{SORIN}` 等变成"库里没有"的 PLAN 幽灵槽位（无锚定）→ `apply_slots` 绑定不上（prompt 里留字面量 `{SORIN}`）、`gate_missing` 报缺人、定妆候选一张也渲染不出来，而界面上只看得到一句"锚定描述为空"。**自检**：`lvs cast --extract` 表里出现「PLAN／锚定描述为空」而定妆卡明明白白写了描述 = 就是这条坑。已修（brace-first + 标题容斜杠）并加两条用例。
- ★ `_PERSON_WORDS` 原先不含身份名词（nobleman / courtier / monk …）→ `{KIYOYUKI}`「a Japanese nobleman in his sixties with …」被误报 `A-nosubject`。**假阳性比漏报更贵**：报警多了操作者学会无视它，真丢主语那版（MIDORI）也一起被无视。已补身份词表，**只收只能当名词的词**（official / noble 兼作形容词的一律不收，收了会放过真问题）。
- ★ `schemas/shots.schema.json` 的 `resolved_by` 只许 `string`，而 `shots.py` 写的是 `None` → **同一份产物在流水线中途过不了自己的契约**（UGE07 在 assets 跑到一半时被判违约 21 处，跑完就绿 = 判据红绿取决于进度）。已改成 `["string","null"]`（与 `asset_path`／`audio_path` 同档）。
- ★ `douyin_captions` 第 2 条候选与第 1 条**同形**（两行封面字时 `"｜".join(lines)` ≡ `首｜尾`），去重后只剩 2 条 →「≥3 条候选」静默失守。已改成第 2 条走顿读口径（`首，尾`），并补三行／两行两条用例。
- **拍摄稿里的 `{…}` = "本集要用的人"**：连"本集不复用旧槽位"这类**说明句**里写 `{SORIN}` 都会把它算进 G2 指纹、逼你为不出场的人出定妆照。说明文字里**不要写花括号**。
- 全量 `pytest -m "not slow and not gpu"` 绿；`lvs cast --lint` 对 SORIN/KOMACHI/KIYOYUKI/PIRATE 四项全 ✅。