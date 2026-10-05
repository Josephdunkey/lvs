# LongVideoStudio · 项目结构摘要

> **续跑第一读**：新会话先读本文件，快速恢复上下文后再开工。
> 权威与细节在 `AGENTS.md`、`.workbuddy/memory/MEMORY.md`、`docs/INDEX.md`（本文件只是浓缩导航）。

---

## 1. 这是什么

长视频生成流水线：**拍摄稿.md → final.mp4**（画面 + 配音 + 烧录字幕）。
形态 = CLI（命令 `lvs`，Python 包在 `lvs/`）；每阶段产物落盘 `.work/<task>/`，**幂等、可断点续跑**；全程 **6 道人审门禁**，未批准不放行。

**核心工作流**（`docs/全流程流水线-编排手册.md`）：

```
拍摄稿.md → parse → shots → cast(G2 定妆) → assets → voice → build → final.mp4
            解析     拆镜/提示词  定妆参考图   素材    配音+字幕  合成
```

---

## 2. 目录结构

```
D:\LongVideoStudio\
├─ lvs/               ★ 主包（CLI）。含 gui/（图形界面，已冻结，不新增功能）
├─ tests/             pytest 测试（约 1200 例，分组跑，见 §7）
├─ docs/              文档。先读 docs/INDEX.md 只读相关 1–2 篇，勿整读审查报告
├─ scripts/           工具脚本（fetch_models.py / smoke_imagegen.py 等）
├─ workflows/         ComfyUI 工作流 JSON 模板（zimage_turbo.json 默认 / sdxl.json 备用）
├─ config.toml        主配置（机器相关：key / ComfyUI / TTS / 人脸权重）
├─ config.雨月物语.toml  每本书一个项目配置（path / 风格 / 期号）
├─ config.ugetsu.toml    挪威的森林项目配置
├─ .work/<task>/      任务产物（parse/shots/assets/audio/segments/final.mp4/gates.json…）
├─ .work/tools/       探针/手术脚本（audit_*.py、fix_modern_prompts.py 等，复用不重写）
├─ .workbuddy/memory/ MEMORY.md 长期规程 + 每日日志 YYYY-MM-DD.md
├─ .agents/skills      agent 技能
├─ models/             本地模型权重（含 stable-audio BGM）
├─ covers/  outputs/  build/  wheelhouse/  其他产物/构建目录
```

---

## 3. 六道门禁 G0–G5（防"下一步白花钱"）

| 门禁 | 守什么 | 对应命令 |
|---|---|---|
| G0 | 拍摄稿格式 | `lvs check` |
| G1 | 分镜与提示词 | `lvs shots` |
| G2 | 定妆参考图 | `lvs cast`（★ 未批角色会被 assets 拦） |
| G3 | 分镜图 + 巡检 | `lvs qc` |
| G4 | 配音与字幕 | `lvs voice` |
| G5 | 成片 | `lvs build` |

- **放行 = 人已批准 AND 自动判据通过**（`lvs/criteria.py`）；产物带指纹，事后改动 → 门禁失效需重审。
- `lvs gate --config <cfg>` 查门禁状态（G2 必须带 config 才准）。

---

## 4. 当前项目与任务状态（2026-10-05）

**主项目：雨月物语**（config.雨月物语.toml，浮世绘风格 `ukiyo-e-woodblock`，`source_mode=local`，`image_granularity=beat`，`build.kenburns=none` 用户拍板不用运镜）。

| 任务 | 图 | 门禁 | next | 最近 |
|---|---|---|---|---|
| UGE01 | 435 | 5/6 | G2 cast | 10-04 |
| UGE02 | 399 | 5/6 | G2 cast | 10-04 |
| UGE03 | 363 | 4/6 | G1 shots | 10-05 |
| UGE04 | 594 | 5/6 | G2 cast | 10-05 |
| UGE05 | 471 | 3/6 | G1 shots | 10-05 |

**下一道通用门**多为 G2 cast（定妆库 9 角色需先 `lvs cast --approve` 才能过）。

---

## 5. 环境与硬件（用户真实工作台）

- **Windows / RTX 4060 Laptop 8GB 显存** → 生图与配音**不可共存**，串行：
  ComfyUI(`D:\ComfyUI`) 起 → `assets` → **关 ComfyUI** → `voice`。
- 服务：ComfyUI `:8188`｜TTS(Qwen3) `D:\qwentts\lvs_tts_server.py :8100`（Base 模型，clone 模式）。
- 书库：`D:\fanshu\<书名>\10-语料\知识视频素材库\`；拍摄稿在 `05-拍摄稿/`。
- 生图：主力 **Z-Image Turbo**（8 步、中文强、可商用），备用 SDXL；模板在 `workflows/`。
- 依赖：ffmpeg 7.1（imageio-ffmpeg 自带）、edge-tts（联网）、DeepSeek 拆镜 key 已配。
- 网络镜像：modelscope.cn 首选 / hf-mirror 备用；pip 已配清华镜像（torch 需显式 +cu126）。

---

## 6. 关键铁律（省 token · 续跑）

1. **第一步永远跑摘要**：`./.venv/Scripts/python.exe -m lvs status --brief --config config.雨月物语.toml`，**不要**一上来整读 shots.json（516KB）或审查报告。
2. **全 CLI 铁律**：`.venv/Scripts/python.exe -m lvs <cmd> --config config.雨月物语.toml`。**不用 anaconda python**（判活/测试假失败）。
3. 看分镜用 `lvs shots --task X --index` / `--peek <id>` / `--full`；读源码用 `lvs map show/grep`；只读相关文档。
4. 测试分组：日常 `pytest -m "not slow and not gpu"`（≈1200 例/≈120s）；只改公共底座（artifact/workspace/pipeline/build）才跑全量。
5. 长任务（voice/build/pytest 10–16 分钟级）一律 `run_in_background` 一次 + 等通知，不反复 block。
6. 阶段完成立即追加 `.workbuddy/memory/YYYY-MM-DD.md` 日志；会话崩溃后先读日志再动手。
7. **单会话 ≤2–3 小时**，禁止跨夜；自查 `audit_all_sessions.py`。
8. 原子写收口在 `lvs/artifact.py`；新增产物一律别用裸 `write_text`。
9. 成片后投稿物料一条命令：`lvs publish --task X --config <cfg>`（已叠字封面+标题+简介+标签），**别手工叠字**。

---

## 7. 权威来源导航（按需精读，勿整读）

| 文件 | 用途 |
|---|---|
| `AGENTS.md` | 项目规则 / 续跑引导 / 会话纪律（总纲） |
| `.workbuddy/memory/MEMORY.md` | 长期规程与踩坑（唯一权威） |
| `.workbuddy/memory/YYYY-MM-DD.md` | 当日进展日志 |
| `docs/INDEX.md` | 文档导航（先读它，再定位 1–2 篇） |
| `docs/全流程流水线-编排手册.md` | 六门禁完整编排 |
| `docs/拍摄稿格式契约.md` | 拍摄稿 markdown 格式（v2） |
| `docs/新项目接入指南.md` | `lvs init` 接入新书目 |
| `docs/Agent-接口契约.md` | 门禁/信封/退出码/续跑三件套 |
| `docs/严重等级登记册.md` | 99 条定级，先 grep 编号 |
| `docs/出图流程-v2-编排方案.md` | 出图链路 v2 与一致性两层 |

---

## 8. 关键架构概念（同一概念只许一处定义）

- 阶段顺序 `lvs/stage.py`｜错误/退出码 `lvs/errors.py`｜指纹 `lvs/artifact.py`｜契约 `lvs/handoff.py`｜熔断 `lvs/breaker.py`｜门禁判据 `lvs/criteria.py`｜结果信封 `lvs/result.py`｜轨迹 `lvs/runlog.py`。
- 退出码：`0` 成功｜`1` 有失败件（逐镜隔离，run 继续）｜`2` 输入/前置不对（可 `--keep-going` 跳）｜`3` 人审门禁（停，keep-going 也不跳）。
- **文件即状态**：`.work/<task>/manifest.json` 记录产物，删掉某阶段产物再 run 只重跑该阶段及下游。
- 多项目：`[paths].lib` 一配路径全派生；一本书 = 一个素材库 + 一份项目 config。
