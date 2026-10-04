# 两轴代码评审（第 2 轮）

- **固定点**：`HEAD` = `263ac04`。工作区全部**未提交**：27 个已跟踪文件改动 + 42 个新文件。
- **范围提示**：因为项目至今**一次都没提交过**，这个 diff 实际横跨**票 04–31**（不止本轮），
  所以"不属于本轮票号"不构成越界。
- **规范来源**：`AGENTS.md`、`docs/agents/*.md`、`CONTEXT.md`（词汇表 + D1–D24 绑定决策）、`README.md`。
- **工具**：`pyproject.toml` 存在，但**未配置任何 linter / formatter / type checker** → 无"工具已强制"项可跳过。
- **方法**：标准轴与规格轴各起一个独立子代理（互不污染上下文），再由主代理**逐条回原始文件复核**。
  下文标了 ✅ 已复核 / ❌ 已推翻。

---

## 轴一：Standards（是否守住本仓既定规范）

### S1 ✅ 硬违规 —— build 有占位片段却记 `done`（D15 / D17 失效）

`lvs/build.py:434`
```python
ws.mark_stage("build", outputs=[final], duration=round(final_dur, 2), subtitle_burned=burned)
```
没传 `status`，而 `Workspace.mark_stage` 的 `status` 默认 `"done"`（`workspace.py:132`）。
占位镜集合在 `build.py:414` 算出来了（`segments, placeholders = build_segments(...)`），
只在 418 行打印，**从未传给 manifest**。

对照：`assets.py:500` 与 `tts.py:549` 都传了 `status=stage_status(len(failures))` —— **build 是唯一的例外**。
`is_stage_done` 明确把 `partial` 视为未完成（`workspace.py:118`）。

**后果**：`lvs run` 在 `run.py:185` 处 `is_stage_done("build")` 为真 → **整段跳过 build**。
于是"先 build 出草稿、后补素材、再 build"在 `run` 驱动下**永远补不上**（单跑 `lvs build` 才走
`_segment_reusable` 的重渲路径）。即 **D17 在 `run` 下是死的**。

> 与**票 26 不重叠**：票 26 讲的是 `assets`/`voice` 被打断时**阶段内进度**不留痕（可观测性）；
> 本条是 build **跑完后**把"有占位"记成 `done`（功能正确性）。

### S2 🔁 代码是对的，**文档是错的** —— 负向提示词那条路并没死（D19）

子代理报"`imagegen.py:154` 的 `NEGATIVE` 与 `{{NEGATIVE}}` 映射让 D19 死了的路复活"，**方向反了**。
回原始模板复核：

| 模板 | 负向节点 | cfg | 结论 |
|---|---|---|---|
| `workflows/zimage_turbo.json:34` | `ConditioningZeroOut`（**硬置零**，根本不读 `{{NEGATIVE}}`） | `1.0` | 负向**确实无效** |
| `workflows/sdxl.json:25` | `CLIPTextEncode`，`"text": "{{NEGATIVE}}"` | **`7.0`** | 负向**真实生效** |

所以 `imagegen.py` 保留 `NEGATIVE` 默认值 + 配置映射是**正确且必需**的（SDXL 备用模板要用）。
真正错的是**文字**：D19 与票 29 都写着"负向提示词那条路不走 …… **模板里也没有 `{{NEGATIVE}}` 占位符**"
—— `sdxl.json` 里**有**，而且 cfg=7 让它**真的起作用**。
该改的是 D19 / 票 29 的措辞（"Z-Image 模板把它置零；SDXL 备用模板启用它"），不是代码。

### S3 ✅ 判断项 —— `shots.json` 写回逻辑重复

同一形状 `json.dumps(data, ensure_ascii=False, indent=2) + "\n"` + `write_text` 出现在
`assets.py:480`、`tts.py:545`、`run.py:108`（另 `studio.py:275`、`studio.py:308`、`shots.py:692` 同形）。
→ 抽 `Workspace.write_shots(data)`，六处归一。属 Judgement call，不致命。

### S4 ⬜ 越界（不在本 diff）—— `parse_script` 用了词汇表禁用词

`lvs/parse.py:121` 的 `parse_script` 用 `script`，而词汇表禁用 `脚本/script`（须 `拍摄稿`/`manuscript`）。
但 `git show HEAD:lvs/parse.py` 里它**已存在**（HEAD 第 110 行），且本 diff **没碰这一行**
→ 属**既有**问题，不是本轮的账。建议单独记一笔，别混进本轮。

### S5 ✅ 软违规 —— `final.mp4` 没走产物校验（D23）

`assert_segment_rendered` 只在 `build.py:289`（逐镜循环内）被调用；
`finalize()` 从不复读 `final.mp4`（只 `ff_duration` 用于**打印** 433 行的偏差）。
D23 原文是"**所有渲染产物**一律过 `assert_segment_rendered`"。拼接是 `-c copy`，风险低，但契约上不成立。

### S6 ✅ 潜在 —— `short_mode` 非法值无人管

`build.py:204/208` 只分支 `loop` 与 `freeze`。任何其它值（含空串）对**短素材**既不循环也不冻结，
片段比目标短 → `_segment_reusable` 时长不匹配 → **每轮重渲、永不收敛**。
当前默认是 `loop`，所以是潜在问题；但没有任何地方校验这个枚举。

---

## 轴二：Spec（是否忠实实现了 spec / 票）

### SP1 ✅ 实测确认 —— `lvs board` **不是只读的**（违反票 23 验收项）

`cli.py:258` 对**所有**子命令无条件执行 `Workspace(task=...).ensure()`；
`Workspace.ensure()` 会落地整个 `.work/<task>/` 目录树 + 写一份 `manifest.json`。

实测：
```
$ ls -d .work/__probe_readonly        → before: absent
$ lvs board --task __probe_readonly   → 正常打印看板
$ ls .work/__probe_readonly           → assets audio logs manifest.json shots-motion   ← 被创建了
```
票 23 复选框 5 明写"**只读：不写 manifest、不动 shots、不触发任何生成**"，
spec §16 也写"**纯读取，不改任何产物**"—— **两处都不成立**。
修法很轻：`board` 路径不要 `.ensure()`；`board.collect()` 已容忍 manifest 缺席（`board.py:106` `ws.manifest or {}`）。

### SP2 ✅ —— 刻意挂起的票是 {25, 26, 32}，不是只有 32

票 25（ETA 窗口）、票 26（阶段 partial）的 `Status` 都是 `ready-for-agent`，Comments 写着"**刻意暂不改**"。
（本评审委托词里"22–31 已实现"的说法不准，特此更正。）

### SP3 ✅ 小 —— 票 22 的"未知显示 `--:--`"在起点不成立

`fmt_eta` 对未知返回 `--:--`（`progress.py:37`），但 ETA 字段只在 `0 < done` 时才追加（`progress.py:108`），
所以**开跑那一刻 ETA 字段整个不存在**，而不是显示 `--:--`。

### SP4 ❌ 推翻 —— "`画面` 在 `_GRAPHIC_MARKERS` 里导致场景被误判成字卡"

实测反驳：
```
'画面' in _GRAPHIC_MARKERS → False
'画面' in _SCENE_MARKERS   → False
_KIND_OF_TAG['画面']       → 'scene'          # 它是「场景类标注」，不是图表标记
classify('画面缓缓推近城墙') → 'scene'         # 判定正确
```
子代理把 `_KIND_OF_TAG`（标注表）看成了 `_GRAPHIC_MARKERS`（关键词表）。**不成立，撤回。**

### SP5 ✅ —— `redo_shots` 无视向导选的 `--action`

`studio.py:313`：`redo_shots` 调 assets 时写死 `only=None` → 六个分支全跑，而非本向导选中的子集。
因为幂等（已配的分镜会跳过）所以不出错，但与票 30"素材按 `--action` 收窄"的意图不符。

### SP6 ⬜ 误判 —— `lvs image` 不是越界

`lvs image` 是**票 19** 的产物（README 已登记为"票据 19"），不是本轮的 scope creep。

---

## 一句话结论

- **Standards 轴**：5 条成立（1 条硬违规 S1、1 条文档与代码冲突 S2、3 条判断项/潜在）+ 1 条越界 + 0 条推翻。
  **最严重 = S1：`lvs run` 下 D17 是死的** —— 补了素材也永远补不上画面。
- **Spec 轴**：3 条成立 + 1 条误判 + 1 条越界误判。
  **最严重 = SP1：`lvs board` 会写盘**，与票 23 验收项、spec §16 直接冲突（已实测）。
- 两轴都指同一处结构性软肋：**manifest 的 `status` 语义**（S1 记错、票 26 缺失、SP1 被 `ensure()` 顺带写坏）。

> 注：本轮评审**只读**，未改任何源码。仅做了一处清理：`31-trial-fixes.md` 与
> `31-graphic-card-text-and-frame.md` **重号**（同一轮建了两个 31，后者是子集），
> 已合并入前者并删除重号文件 —— issue-tracker 约定"一个票一个文件，从 01 连续编号"。
