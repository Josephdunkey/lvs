# 43 · 第七轮双轴审查的修复

**Status:** done
**关联**：`review-two-axis-7.md`、票 25/26/32（上轮落地）、票 42（第五轮复查）、D25

第七轮复查针对票 25/26/32 落地后的状态。两轴共报 3 条成立（全部**结构/卫生**，
无功能缺陷、无规格违规），已全部修；另推翻若干"读代码读出来的错觉"。

## 修了什么

### T1 · 删掉同名死方法 `Workspace.stage_status`

`lvs/workspace.py` 有一个实例方法 `stage_status(self, stage)`，与**模块级**函数
`stage_status(failures: int) -> str` 同名，全仓库从未被调用（`grep -rn "\.stage_status(" .`
零命中；所有 `stage_status` 用法都指模块函数）。同名 + 死代码，是下次误改的陷阱。已删。

### T2 · "已完成数"统一成一个名字 `done`

票 26 引入增量记账时，素材阶段落 `done`、配音阶段落 `synthesized`，于是 `board.py` 得用
`entry.get("done", entry.get("synthesized", 0))` 兜两个名字 —— 而且这个兜底表达式**写了两遍**
（`_stage_detail` 与 `collect`）。同一概念两个 owner，改一处忘一处就会显示错。

修法：
- `lvs/tts.py`：`_progress()` 与收尾 `mark_stage` 都改写 `done=done`（原 `synthesized=done`）。
- `lvs/board.py`：读数收成唯一口径 `_done_count(entry)`，两处共用。
- GUI（`gui/store.py`）数的是**盘上产物**，不读这两个键，不受影响。

红→绿证据：驱动真实 `tts.run_command`（后端桩一合成即抛 `TTSError`），修复前盘上是
`{'status':'partial', …, 'synthesized':0, …}`，修复后是 `done`。

### T3 · `mark_stage_progress` 的计数改成具名参数

原来是 `**counts: Any` 自由字典，`entry.update(counts)` 无校验 —— 拼错键（`don=3`）
会被**静默**写进 manifest，而看板只认 `done`，错键永远不显示、也不报错（"静默降级"）。

修法：签名改成具名关键字参数 `done/skipped/failed/total: int | None = None`，
只在非 None 时写入（保留"不覆盖已有字段"的 merge 语义）。拼错键 → 直接 `TypeError`。

## 验证

- 新增 `tests/test_stage_progress.py::ProgressCountKeyTest`（2 项）：未知键被拒、
  配音落盘用 `done` 而非 `synthesized`。两条都先红后绿（红证据见上）。
- 全量 `pytest`：**459 → 461 passed**。
- `git` 一行没动（全项目仍未提交）。
