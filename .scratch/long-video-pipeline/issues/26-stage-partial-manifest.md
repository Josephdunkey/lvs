# 26: 阶段被中断时 manifest 不留「部分完成」记录，看板误显「未开始」

**What to build:** 让 `assets`（以及 `voice`）在**执行过程中**就把「已产出 / 已跳过」记进
`manifest.json`，使被打断的阶段在看板上显示为**部分完成**，而不是「未开始」。

**Status:** done

**现象**：`mark_stage(stage, ...)` 只在阶段**跑完**时调用一次。
30 分钟压测中 `lvs assets` 被限时信号截断（rc=124），磁盘上已生成 **95 张真图**，
但 `manifest.json` 里**根本没有 `assets` 这一条**，`lvs board` 于是显示 `⬜ 素材 未开始`
——与实际进度不符（真实应是「部分完成 / 95 张」）。

**对照**：`build` 已有**增量**先例 —— 占位标记 `segments/.placeholders.json`
在循环内**逐镜追加**（见 D17），所以被截断的 build 仍能如实反映占位集合。
`assets` / `voice` 尚未对齐这一做法。

**影响面**：可观测性 —— **不影响功能**：`assets`/`voice` 都以磁盘产物为状态，重跑会
**跳过已完成项**并补齐余下项（幂等）。问题只在「阶段被打断后阶段内进度看不见」。

- [x] `assets`：每完成/跳过一个分镜就**增量落盘**阶段进度（至少 `done`/`skipped`/`failed` 计数与 `status="partial"`）
- [x] `voice`：同上（当前同样是收尾才写 manifest）
- [x] `lvs board` 能把 `status="partial"` 渲染成「部分完成」，并显示 已产出/总数
- [x] 覆盖用例：模拟「跑到一半抛异常/被中断」→ manifest 留有 partial 记录，`is_stage_done` 仍为 False（续跑会继续）
- [x] 不破坏幂等：partial 记录 + 重跑 = 跳过已完成项、补齐余下项，最终置 `done`

---

## Comments

**来源**：30 分钟长视频压测（`long30`）现场发现。`assets` 限时截断后看板显示「素材 未开始」，
而 `assets/local/` 实有 95 张真图。**刻意暂不改**：属可观测性打磨项，
且 `assets` 的幂等语义（磁盘产物即状态）已保证重跑正确，不影响压测结论。

## 落地（2026-09-27）

`workspace.mark_stage_progress(stage, **counts)`：**并进**（不是替换）manifest 里的阶段条目 ——
保留既有 `outputs`/`note`，只更新 `status="partial"` + 计数 + 时间戳。
之所以必须"并进"：`mark_stage` 是整体替换语义，增量写会把已登记的产物抹掉。

调用点：`assets` 逐镜循环（含"跳过"分支）、`voice` 逐镜循环（含失败分支）各一次。

`board` 侧：`partial` 现在分两种含义渲染 —— `done < total` → **"部分完成"**（接着跑），
否则 → **"有失败"**（去修那几镜）。原来两者都显示"有失败"，会把人引向错误的下一步。

新增 `tests/test_stage_progress.py`（9 项），其中最关键的一条是**真的从循环里抛异常**
（把 `graphic.render` 换成第 3 次调用时抛 `RuntimeError`），然后断言盘上留着
`status=partial, done=2, total=4`；再断言补跑完会翻回 `done`。
