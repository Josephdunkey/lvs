# 双轴审查 · 第七轮（票 25/26/32 落地后的复查）

范围同第六轮：**全项目**。两轴各起独立 sub-agent，主代理逐条回原文件复核，能跑的都跑了。
共 3 条成立（均为结构/卫生），全部已修；另有大量"读起来像问题、跑起来不是"的条目被推翻。

## 一句话结论

**这轮改动本身是干净的。** 上一轮（票 25 ETA 滑窗、票 26 增量进度、票 32 beat 记忆、
死代码删除）八个目标不变量逐一核查后**零功能缺陷、零规格违规**。剩下的是我自己在票 26
里留下的三处**结构性卫生问题**：一个同名死方法、一个"一个概念两个名字"、一个静默吸收非法键。

## 成立并已修（3 条）

| # | 问题 | 位置 | 类型 |
|---|---|---|---|
| T1 | `Workspace.stage_status(self, stage)` 是**死方法**，且与模块级 `stage_status(failures)` **同名** | `lvs/workspace.py:193` | 结构 |
| T2 | "已完成数"在素材阶段叫 `done`、配音阶段叫 `synthesized`；`board.py` 用 `entry.get("done", entry.get("synthesized", 0))` 兜两个名字，**同一表达式出现两处** | `lvs/assets.py:570`、`lvs/tts.py:457,565`、`lvs/board.py:92,142` | 结构 |
| T3 | `mark_stage_progress(stage, **counts)` 是无约束自由字典：拼错键（`don=`）**静默并进 manifest**，看板只认 `done` → 错键永不显示、也不报错 | `lvs/workspace.py:225` | 结构（潜在） |

### T1 证据
`grep -rn "\.stage_status(" .` → **零命中**；`lvs/ tests/` 里所有 `stage_status` 都指模块级函数
（`assets.py:637`、`build.py:462`、`tts.py:564` 的 `stage_status(len(failures))`）。
方法体从未被调用。已删。

### T2 证据（红测试）
驱动真实 `tts.run_command`（后端桩一合成即抛 `TTSError`），落盘得到：

```
{'status': 'partial', 'at': '…', 'synthesized': 0, 'skipped': 0, 'failed': 1, 'total': 1}
```

`board.py` 若只读 `done` 就永远读不到配音进度。修法：**一个概念一个名字** ——
配音改写 `done`（`mark_stage_progress` 与收尾 `mark_stage` 两处），`board` 收成
唯一读数口径 `_done_count(entry)`。GUI 侧（`gui/store.py`）数的是**盘上产物**、不读这两个键，
不受影响。

### T3 证据（红测试）
`mark_stage_progress("assets", don=3)` 修复前**不报错**并写入 `don: 3`。修法：计数改成
**具名关键字参数**（`done/skipped/failed/total`，None 表示"这次不改这个键"），
拼错键直接 `TypeError`。

## 推翻的（读起来像问题，跑起来不是）

- **"ETA 滑窗在跳变时仍污染样本"** → 不成立。仿真"18 个瞬时跳过 + 20×30s"：
  `set(18)` 后 `_samples` 为空、`step>1` 只移基准不记样本；最终 `len==15`、`eta` 收敛正确。
- **"`_apply_llm_item` 的 beat 记忆跨批次不生效"** → 不成立。`BeatKindConsistencyTest` +
  `LlmEnrichConsistencyTest` 真实运行（含自相矛盾的 LLM 桩）全过。
- **"`partial` 记录会被 `mark_stage` 覆盖成 done"** → 不成立。`mark_stage_progress` 是 MERGE，
  收尾 `mark_stage` 是 REPLACE 且两阶段都回传计数，计数不丢（`test_stage_progress.py` 真跑循环炸点验证）。
- **"死函数删除留下悬空引用"** → 不成立，全仓 grep 零命中。
- **"`skipUnless` 在静默跳过"** → 不成立，18 处全挂在本机渲染器/浏览器能力门禁上。
- **Spec 轴**：D15/D25、D23、D26、D17、D31/D32、D9/D13、D2、D11/D16 八组不变量**全部合规**，
  含对 D32 `resolved_by=="local"` 回退与 `sources.resolve()` 优先级的一次性探针实测。
  其中"翻库命中会不会把镜钉死"经查由 `sources.apply_mode` 的 `STALE_WITH_LIBRARY` 化解，
  与 §8.1① 一致。

## 教训

第六轮"能跑就跑"的纪律继续有效：这轮 3 条成立项全部由**跑出来的证据**定案（一个 grep 零命中、
两个红→绿的行为测试）；被推翻的最大一类仍是"只读代码读出的错觉"。

## 项目健康度快照

```
代码    lvs/ 约 7400 行（纯模块）＋ gui/
测试    26 个文件，461 项通过
票据    43 张：40 done / 2 ready-for-agent（25、26 已在上轮落地，票待关）/ 1 ready-for-human（32）
审查    7 轮双轴，本轮成立项 → 票 43
```

**最弱结构点**（与第六轮同）：`lvs/shots.py` 把纯文本逻辑与 `Workspace/Config/LLM` 焊在一起，
仍无法脱离整套栈单测。属大重构，风险>收益，未动。
