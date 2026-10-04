# LongVideoStudio

长视频生成流水线：上传讲稿 → 分镜拆解与逐镜提示词 → 素材获取（Pexels 下载 / 本地生图）→ 配音与字幕 → ffmpeg 合成成片。

## 续跑引导（省 token，先读这里）

1. **第一步永远跑摘要，不要重读大文件重建状态**：
   `./.venv/Scripts/python.exe -m lvs status --brief --config config.雨月物语.toml`
   一行一任务：门禁开闭 / 图片数 / 产物就绪 / 下一道门；**≤ 8 行 ≈ 200 token**。`--task X` 单任务、`--all` 全部、`--json` 给机器、`--legend` 看列说明。
   **禁止**一上来就 Read `shots.json`（516KB / 8 千行）或全部手册来“搞清楚在哪”。
   （旧工具 `.work/tools/continuation_summary.py` 已被 `lvs status` 取代，保留仅为考古。）
2. 约定与踩坑唯一权威 = `.workbuddy/memory/MEMORY.md`；当日进展追加 `.workbuddy/memory/YYYY-MM-DD.md`。续跑先读 MEMORY，再读当日日志。
3. 全 CLI 铁律：`./.venv/Scripts/python.exe -m lvs <cmd> --config config.雨月物语.toml`。不用 anaconda 的 python（判活/测试会假失败烧 token）。工具已固化于 `.work/tools/`（`audit_shots.py`、`fix_modern_prompts.py`、`continuation_summary.py`）。
4. GPU 串行：ComfyUI(:8188) 与 Qwen3-TTS(:8100) 同机 8GB 不可共存；服务用完停回原状。TTS 起 **Base 模型**（clone 模式，config 已配 ref 克隆），勿起 CustomVoice 模型（会 500）。
5. 门禁 `lvs gate` 查 **G2(定妆)** 必须带 config 才准（否则 cast lock 路径错、误判 stale）。gate 带不带 config 也要保持一致。
6. 成片后投稿物料一条命令：`lvs publish --task X --config <cfg>` → `.work/<task>/publish/`（**已叠字**的横/竖封面 + 标题/简介/标签）。**不要**再手写投稿 md、也不要再手工叠字。
7. **读文档的铁律**：先读 `docs/INDEX.md`（60 行，逐篇说明“什么时候才该读它”），**只读当前任务相关的 1–2 篇**，不要把审查报告整篇读进来（`docs/*.md` 合计 ≈ 33 万 token）。
   要某一条先定位再读那一节：`Select-String -Path docs\<篇名>.md -Pattern '^### B0[0-9]'`（编号见《严重等级登记册》）。
8. **看分镜不要整份读**：`lvs shots --task X --index`（一镜一行，516 KB → 约 18 KB）、`lvs shots --task X --peek 193`（单镜摘要，< 400 字符）；看提示词全文才加 `--full`。这两个开关是只读的，不会建目录、不会改状态。

## Agent skills

### Issue tracker

Issues live as local markdown files under `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical roles, label strings equal to their names. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context. See `docs/agents/domain.md`.
