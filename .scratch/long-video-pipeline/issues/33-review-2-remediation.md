# 33 · 第二轮代码评审的问题修复

**Status:** done
**关联**：`review-two-axis-2.md`（固定点 `263ac04`）、票 26、票 23、票 22、票 30

上一轮评审（两轴 + 主代理逐条回原始文件复核）查出 6 条成立的问题。本轮逐条修掉，
全部 TDD（先写红测再实现），测试从 234 → 244。

## 修了什么

### 票 33.1 —— build 有占位却记 `done`（S1，硬违规，D17 失效）
`lvs/build.py:run_command` 调 `mark_stage("build", …)` 时**漏传 `status`**（默认 `done`），
占位集合算出来只打印、从不入 manifest。于是 `lvs run` 里 `is_stage_done("build")` 为真 →
**整段跳过 build** → 「先出草稿、后补素材」永远补不上画面（单跑 `lvs build` 才走重渲路径）。
- 修：`status=stage_status(len(placeholders))` + 记 `placeholders` 数。
- 测：`tests/test_resume.py::TestBuildStageContract`（占位→partial 且 blocked；无占位→done）。

### 票 33.2 —— `lvs board` 会写盘（SP1，违反票 23 验收项 / spec §16）
`cli.py` 对所有子命令无条件 `Workspace(...).ensure()`，而 `ensure()` 落地整棵目录树 + 写
manifest。实测 `lvs board --task X` 会凭空造出 `.work/X/`。
- 修：新增 `Workspace.read()`（**只读**：有 manifest 才加载，绝不建目录/落盘）；`board` 走它。
  `--html` 是显式落盘，故写前 `mkdir`。
- 测：`tests/test_cli.py::TestBoardReadOnly`（跑完断言任务目录不存在）。

### 票 33.3 —— `short_mode` 非法值无人管（S6，潜在死循环）
`build.short_asset` 只认 `loop`/`freeze`，别的值（含空串）对短素材既不循环也不冻结 →
片段永远比目标短 → 每轮重渲、永不收敛。
- 修：`normalize_short_mode()` 在**渲染前**校验，非法即 `BuildError`。
- 测：`tests/test_build.py::ShortAssetModeTest`。

### 票 33.4 —— `final.mp4` 没走产物校验（S5，D23）
D23 说「**所有**渲染产物一律复读」，但 `assert_segment_rendered` 只在逐镜循环里调，
`finalize()` 产出的成片从没复读。拼接虽是 `-c copy`、风险低，但契约不成立。
- 修：`run_command` 里对 `final` 也 `assert_segment_rendered`，空成片当场返回 2。
- 测：`TestBuildStageContract::test_unreadable_final_is_rejected`。

### 票 33.5 —— ETA 起点整段消失（SP3，票 22）
`fmt_eta` 未知返回 `--:--`，但 ETA 字段只在 `0 < done` 时才追加 → 开跑那一刻字段根本不存在。
- 修：`done < total` 就显示 ETA 字段，`done==0` 时给 `--:--` 占位，位置固定。
- 测：`tests/test_progress.py::ProgressTest::test_eta_field_present_at_start`。

### 票 33.6 —— `redo_shots` 无视向导选的 `--action`（SP5，票 30）
`redo_shots` 调 assets 时写死 `only=None` → 六个分支全跑，与本向导选中的子集不符。
- 修：`redo_shots(..., only=…)` 收 `--action` 范围；抽 `_assets_scope()` 复用 `run_command`
  里那份收窄逻辑。**关键护栏**：把 `only` 放宽到覆盖「本次要重做的镜」自身的来源 ——
  否则目标镜会被 `assets.only_matches` 按 `source` 筛掉、根本不会被重做。
- 测：`tests/test_studio.py::RedoShotsScopeTest`。

### 票 33.7 —— `shots.json` 写回逻辑重复（S3，判断项）
同一串 `json.dumps(..., ensure_ascii=False, indent=2) + "\n"` + `write_text` 散在
`assets/tts/studio×2/shots/run` 六处。
- 修：抽 `Workspace.write_shots(data)`，六处归一；`assets.py` 的 `import json` 随之删掉。
- 测：既有 shots/voice/assets/build 契约测试已覆盖写回格式。

### 票 33.8 —— 文档与代码冲突（S2）
D19 与票 29 都写「模板里没有 `{{NEGATIVE}}` 占位符」—— **错的**：`zimage_turbo.json` 用
`ConditioningZeroOut`+`cfg=1`（负向无效），但 `sdxl.json` 是真 `CLIPTextEncode`+`cfg=7`
读 `{{NEGATIVE}}`（负向**真实生效**）。代码是对的，改的是措辞（D19 + 票 29）。

## 没动（说明）

- **S4 `parse_script` 命名**：`git show HEAD:lvs/parse.py` 里已存在、本 diff 没碰 → 既有问题，
  不并进本轮（词汇表禁 `脚本`/`script`，建议单独记一笔）。
- **票 25 / 26 / 32**：刻意挂起，维持原状。

## 验收

- `pytest -q` → **244 passed**（+10）。
- `lvs board --task <新任务名>` 不再创建 `.work/<task>/`。
