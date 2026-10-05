# LongVideoStudio · 项目规程（长期）

> 细节看 `docs/`。只留「改错了也不报错」的。

## 流程与门禁
- `lvs run` = 执行到下一道门为止；逃生门 `--skip-gates`。
- ★ migrate 顺序：`migrate` → 改否定式 → 以 `_v2/` 为准，**不再重跑 migrate**（会覆盖手工改写）。
- ★ 改定妆卡必重跑 `lvs cast --extract`；改锚定后旧候选移 `<角色>/_superseded-*/`，否则审阅表新旧混排。
- 退出码：`0` 成功｜`1` 逐镜失败（run 继续并汇总）｜`2` 输入前置不对（停，`--keep-going` 可跳）｜`3` 人审门禁（停，不跳）。**2/3 必须分开**。
- 门禁 = 人批准 AND 自动判据；判据自身出错按通过；`run` 与单命令共用 `stage.note_stage_end`。

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
