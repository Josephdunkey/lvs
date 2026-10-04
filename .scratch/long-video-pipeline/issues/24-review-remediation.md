# 24: 代码评审整改 —— 两轴评审的修正落地

**What to build:** 对未提交的 diff 做一次双轴评审（Standards / Spec），逐条核实并修复。

**Blocked by:** 21

**Status:** done

**评审轴**：Standards（已文档化的规范 + Fowler smell）、Spec（票据/规范符合度），两轴各跑一个子代理，分别汇总。

- [ ] 每条评审意见都**先核对源码**再修，不盲改
- [ ] 修复不引入回归（全量单测通过）
- [ ] 每处修复留下可回溯的记录（spec §17 表）

---

## Comments

**What was built:** 8 项整改，见 spec §17 表。

**交付记录**
1. 断点续跑契约：`outputs` 记**每镜产物**、有失败标 `partial`、`is_stage_done` 用 manifest 记录的 outputs 判定（D15）→ `tests/test_resume.py`
2. 素材幂等：`existing_asset` 只查与 `source`/`library_asset` 匹配的分支，不跨三分支 glob
3. 时间轴污染：`build.shots_with_audio()` 把无音频镜（TTS 失败 `start=None`）排除出画面轨并报告
4. D11：采样参数固化进 `workflows/*.json` 字面量，`imagegen.DEFAULT_VALUES` 只留通用旋钮（D16）；移除 `--steps/--cfg`
5. 词汇表漂移：CLI `SCRIPT.md`/`script` → `拍摄稿.md`/`manuscript`
6. 死代码：删 `assets.RESOLVERS`、`shots.script_start/end`、`build._segment_filter_video` 死参数
7. 重复：抽 `Workspace.load_shots()`；build 复用 `assets.existing_asset`
8. issue 文档：`## 交付记录` → `## Comments`，符合 `issue-tracker.md`
9. 占位片段卡死：片段缓存只看时长 → 补素材重渲不生效；用 `segments/.placeholders.json` + `_segment_reusable` 修复，占位镜素材补齐后重跑会重渲；登记表逐镜增量落盘，build 中断也不丢

**刻意保留**：`lvs run` 的 GPU 冲突仍是快速失败（`return 2`），`assets`/`voice` 是逐镜隔离 ——
语义不同已在 docstring 写明。
