# LongVideoStudio

长视频生成流水线：上传讲稿 → 分镜拆解与逐镜提示词 → 素材获取（Pexels 下载 / 本地生图）→ 配音与字幕 → ffmpeg 合成成片。

## 续跑引导（省 token，先读这里）

1. **第一步永远跑摘要，不要重读大文件重建状态**：
   `./.venv/Scripts/python.exe -m lvs status --brief --config config.雨月物语.toml`
   一行一任务：门禁开闭 / 图片数 / 产物就绪 / 下一道门；**≤ 8 行 ≈ 200 token**。`--task X` 单任务、`--all` 全部、`--json` 给机器、`--legend` 看列说明。
   **禁止**一上来就 Read `shots.json`（516KB / 8 千行）或全部手册来“搞清楚在哪”。
   （旧工具 `.work/tools/continuation_summary.py` 已被 `lvs status` 取代，保留仅为考古。）
2. 约定与踩坑唯一权威 = `.workbuddy/memory/MEMORY.md`；当日进展追加 `.workbuddy/memory/YYYY-MM-DD.md`。续跑先读 MEMORY，再读当日日志。
3. 全 CLI 铁律：`./.venv/Scripts/python.exe -m lvs <cmd> --config config.雨月物语.toml`。不用 anaconda 的 python（判活/测试会假失败烧 token）。工具已固化于 `.work/tools/`（`audit_shots.py`、`audit_all_sessions.py`、`fix_modern_prompts.py`、`continuation_summary.py`）。
4. GPU 串行：ComfyUI(:8188) 与 Qwen3-TTS(:8100) 同机 8GB 不可共存；服务用完停回原状。TTS 起 **Base 模型**（clone 模式，config 已配 ref 克隆），勿起 CustomVoice 模型（会 500）。
5. 门禁 `lvs gate` 查 **G2(定妆)** 必须带 config 才准（否则 cast lock 路径错、误判 stale）。gate 带不带 config 也要保持一致。
6. 成片后投稿物料一条命令：`lvs publish --task X --config <cfg>` → `.work/<task>/publish/`（**已叠字**的横/竖封面 + 标题/简介/标签）。**不要**再手写投稿 md、也不要再手工叠字。
7. **读文档的铁律**：先读 `docs/INDEX.md`（60 行，逐篇说明“什么时候才该读它”），**只读当前任务相关的 1–2 篇**，不要把审查报告整篇读进来（`docs/*.md` 合计 ≈ 33 万 token）。
   要某一条先定位再读那一节：`Select-String -Path docs\<篇名>.md -Pattern '^### B0[0-9]'`（编号见《严重等级登记册》）。
8. **看分镜不要整份读**：`lvs shots --task X --index`（一镜一行，516 KB → 约 18 KB）、`lvs shots --task X --peek 193`（单镜摘要，< 400 字符）；看提示词全文才加 `--full`。这两个开关是只读的，不会建目录、不会改状态。
9. **覆盖率门禁（防倒退，不是逼补覆盖）**：`.coveragerc` 的 `fail_under = 71`（2026-10-05 实测 74.0%）。要验一条主路径没退步：`./.venv/Scripts/python.exe -m coverage run -m pytest -m "not slow and not gpu"` → `coverage report`（退出码即门禁）→ `coverage json -o .work/tmp/cov.json` → `pytest tests/test_coverage_ratchet.py`（关键模块水位，无 json 时自动 skip）。抬水位规矩见 `.coveragerc` 头注释。
10. **测试只跑该跑的那一组**：日常 `./.venv/Scripts/python.exe -m pytest -m "not slow and not gpu"`（**1281 例 / ≈120 s**，2026-10-05 实测；含 BGM 用例）；只有改了 `artifact` / `workspace` / `pipeline` / `build` 这类**公共底座**才跑全量 `pytest`（1207 例 / 255–276 s）。`-m slow` = 真 ffmpeg / 真子进程 / 真建 wheel / e2e。
11. **读源码不要整份读**：`./.venv/Scripts/python.exe -m lvs map show <文件> <起行> <止行>`（带行号）、`lvs map grep <正则> --glob "lvs/*.py" -C 2`、`lvs map ls`（列已有编号 dump）。
12. **BGM（配乐，可选）**：`lvs bgm --task X --config <cfg>` 本地生成可商用配乐（**固定 CPU**，不抢 ComfyUI/TTS 的显存）；只看风格 `lvs bgm prompt`（秒出、不加载模型），混进成片 `lvs bgm mix`（→ `.work/<task>/bgm_final.mp4`，**原片保留**；音量/闪避在 `[bgm]` 段：`volume_db` / `duck_*`）。推理参数照**模型卡**（steps 8 / cfg 1.0 / pingpong，别用 SA2 的 100 步）。权重**已落在** `models/stable-audio-3-small-music/`（≈3.3 GB = `model.safetensors` 2.27 GB + `model_config.json` + `t5gemma-b-b-ul2/` 1.18 GB），**下权重走 ModelScope 国内镜像**（`lvs bgm download` 首选 `stabilityai/stable-audio-3-small-music`，**非门控、不要 HF_TOKEN**；hf-mirror 只是备选、那条才要令牌），加载**完全离线**（`HF_HUB_OFFLINE=1` 实测出得了 wav）。★ torch 2.5 + transformers 4.57 靠 `lvs/bgm.py` 的 T5Gemma 掩码补丁才跑得起来（`torch>=2.6` 时自动跳过）；stable-audio-tools 要 **0.0.20（GitHub main）**，PyPI 的 0.0.19 不支持 SA3。用法与许可见 README「背景音乐 BGM」。

## 会话纪律（省 token，2026-10-05 增补；源自一个跨夜 9h37m / 1.09 亿 input 的会话复盘）

> 成本 = 调用次数 × 常驻上下文。上下文越大，每条小命令越贵（该会话 97% 是被重读的缓存）。以下四条与上面的续跑引导同等效力。

1. **禁止跨夜长会话**：单会话目标时长 ≤ 2–3 小时；任务没完就"写当日日志 + 交接"后**开新会话**，不要让上下文滚到 9 万+。
   自查：`./.venv/Scripts/python.exe .work/tools/audit_all_sessions.py`（一行一会话：input / cached% / medIn/call / 最贵时段 / 调用次数；另有 `--all` / `--top N` / `--session <子串>` / `--min-mb N` / `--json` / `--details`）。
2. **探针一律固化到 `.work/tools/`**：不再每轮 `python -c` 现写（PowerShell 下还会卡死）；复用 `audit_all_sessions.py`、`audit_session_*.py`、`audit_shots.py`，只改路径/参数。
3. **pytest 一次跑完该跑的那组**：日常一条 `./.venv/Scripts/python.exe -m pytest -m "not slow and not gpu"`（≈96 s），**不逐条/不反复重跑**；只有动公共底座才跑全量（见第 9 条）。
4. **读大文件走摘要命令**：任务状态 `lvs status --brief`、分镜 `lvs shots --index` / `--peek`、源码 `lvs map show`；禁止整读 `docs/*.md`、`shots.json`、会话 jsonl（58MB jsonl 只准流式逐行解析）。

## Agent skills

### Issue tracker

Issues live as local markdown files under `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical roles, label strings equal to their names. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context. See `docs/agents/domain.md`.
