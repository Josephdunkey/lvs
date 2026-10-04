# 双轴审查 · 第五轮（票 41「实拍镜来源策略」）

两轴各起一个独立 sub-agent（Standards / Spec），主代理再**逐条回原文件复核**。
本轮 6 条里 **5 条成立、1 条推翻**；另有 1 条是两轴都没点名、复核过程中撞出来的真 bug。
误报留着，因为"为什么误"比"是什么"更值钱。

范围：票 41 的实现（`lvs/sources.py`、`shots.py`、`assets.py`、`gui/app.py`、`cli.py`、
`studio.py`、`gui/store.py` + 三份来源测试）。
**注意**：仓库只有 3 个提交，这些文件全部未跟踪，`git diff` 取不到东西 —— 两轴都是直接读工作区文件。

---

## 成立并已修

### T1 · 库模式回退过的镜，切回「自动」会被判死，旧图还不可达（真 bug，最严重）

**这条两个轴都没点名**，是主代理复核上面那条误报时撞出来的，并且**已复现**：

```
① 库模式首跑      → ok | resolved_by = local   （产物 assets/local/shot-001.png）
② 切回 auto 认领  → 找不到 ❌   branches = ('library',)
③ 切回 auto 取素材 → FAIL ❌ → source=library 但素材库未命中（关键词 城墙）
```

根因：`apply_mode(MODE_AUTO)` **不重写逐镜 `source`**（`sources.py:113` 直接 `return 0`），
所以 `--source library` 跑过的任务里，每镜的 `source` 还是 `library`；
而 `branches_for` 只看 `source` 就返回 `("library",)`
（`assets.py:142`），`resolve_shot` 也在 `mode != LIBRARY` 时直接判失败（`assets.py:353`）。
于是图明明躺在 `assets/local/` 里，这一镜却报"素材库未命中"。

**为什么算问题**：切回「自动」正是票 41 明确支持的界面操作（票 41 的复审 T2 专门修过
"选择器切回自动会失效"）。而这个用户的 `config.toml` 里 `[library].dirs = []`，
库模式**必然**未命中、必然全量回退 —— 也就是说这条路径对他是必经之路。

**修法**（最小补丁）：判据用 D32 自己留下的 `resolved_by == "local"`。
`branches_for` 与 `resolve_shot` 各放行一处，人手把 `source` 改成 `library` 的镜没有这个
留痕，语义不变。

**补了 2 条回归**：`test_switching_back_to_auto_keeps_the_fallback_reachable`
（认领得到）、`test_auto_regenerates_a_lost_fallback`（产物没了要重生成，而不是判死）。
`test_auto_mode_does_not_fall_back` 那条既定意图的测试**仍然绿**。

**边界（如实记）**：这条补丁盖的是「切选择器 → 跑 assets」。若改完选择器又跑
`lvs shots` 重拆镜，`_preserve_manual`（`shots.py:566`）只保 `source`/`library_asset`、
不保 `resolved_by`，留痕会丢、问题复现。那一路径语义上更接近"重来一遍"，留待后续票据决定。

### T2 · 一句与事实不符的注释

`lvs/gui/app.py:247` 原文：「把它落成逐镜来源是 `lvs assets` 的事
（`sources.apply_mode` 的**唯一调用点**在那儿）」。
实际 `lvs/shots.py:690` 也调 `apply_mode`。已订正为"两边都调、实现只有那一份"。
（本轮被推翻的那条误报正是踩在这句话上 —— 见下。）

### T3 · `shots.json` 读取器两份逐字重复

`lvs/shots.py:537 _read_shots_file` 与 `lvs/studio.py:383 _read_shots` **除 docstring 外完全相同**
（`diff` 过）。而 `lvs/workspace.py:79` 的 `load_shots` 注释早就写明它存在的理由就是
"消除 assets / tts 里两份逐字相同的读取逻辑" —— 同一个坑又踩了一次。

修法：新增 `Workspace.try_load_shots()`（与 `load_shots` 只差缺失时返回空而非抛），
删掉那两份，调用点改为 `ws.try_load_shots()`（`shots.py:539/628`、`studio.py:286`）。
**没有留一层一行转发** —— 那正是基线里的 Middle Man 坏味道。

### T4 · 失效字段集四处各写一份，且 `apply_mode` 漏清 `error`

同一个概念（"上一次解析的结果"）被四处手写成不同子集：

| 位置 | 清掉的字段 |
|---|---|
| `sources.py:121`（`apply_mode`） | library_asset, asset_path, resolved_by, status ← **无 error** |
| `sources.py:139`（`retarget`） | asset_path, resolved_by, status ← **无 error** |
| `gui/app.py:457`（钉图） | error, asset_path, resolved_by |
| `studio.py:270-273` | asset_path, resolved_by, library_asset, error |

漏 `error` 是真问题：`assets.py:590` 会把失败原因写进 `shot["error"]`，`app.py:289` 会读出来
给界面看。所以"换了策略之后，这镜还挂着上个来源的失败原因"是用户可见的。

修法：在 `sources` 里定两个常量，四处都引用它 ——
`STALE_RESULT_FIELDS`（含 `error`）与 `STALE_WITH_LIBRARY`（多一个 `library_asset`，
只有强制换来源才该连库命中成果一起作废）。

### T5 · `retarget` 的"钉死"判据说明是错的

`sources.py:137` 用 `not shot.get("library_asset")` 当"不碰钉死的镜"，
可同一模块 `retargetable()`（`sources.py:94`）明文写「**不能**见 `library_asset` 就放过」。
两处对同一个词用了两套判据。

**但代码是对的，错的是说明**：`retarget` 只想知道"这镜是不是已经有素材了"，库命中的镜
`branches_for` 第一行就认领，改它的 `source` 反而白丢一次 `library_asset`。所以只订正 docstring，
讲明这里的判据是"已有素材"而**不是** `PIN_FIELD` 那种人工钉死，别混用。

---

## 不成立（误报，附证据）

### F1（Standards 轴）·「D31 说落成动作只有一处，但实现有两处调用 `apply_mode`」

**结论反了。** D31 与票 41 写的是「落成动作只有一处 `sources.apply_mode()`」，指的是
**落成的实现只有那一份** —— 票 41 原文点明了动机是「票 40 那次三重复制的教训」，
说的正是"别写三份"，不是"只许调一次"。

而且票 41 明确要求 `lvs shots --source library` **在拆镜时就把意向落成逐镜 `source`**。
所以 `shots.py:690` 调用它不但不违规，还是规格要求。
**该改的不是代码，而是 `app.py:247` 那句错注释** —— 已作为 T2 修掉。

### F2（Spec 轴）·「`branches_for` 在 auto 下对 `source=library` 应返回 `("library","pexels","local")`」

**因果方向读反了。** 票 41 原文是：

> `branches_for` 曾经漏掉 `source=library`（改成 mode 驱动时掉的）：auto 下
> `_shot(1,"library")` **从 `("library",)` 变成 `("library","pexels","local")`**

这是在描述**引入 bug 时的症状**（`source=library` 那个分支被漏掉，于是掉到最后一行兜底），
修复是把它**还原成** `("library",)`。而票 41 自己点名的那个回归测试正是权威：

```python
# tests/test_resume.py · TestBranchScoping
self.assertEqual(assets_mod.branches_for(_shot(1, "library")), ("library",))
```

当前代码就是 `("library",)`，测试绿着。**若照建议改成三支，反而会弄坏这条回归。**

---

## 两轴一致确认没问题的（主代理逐条复核过）

- **只作用实拍镜**：`sources.is_scene` 排除 `source=graphic`；图文 beat 仍归本地排版（D20/D24）。
- **不碰人工钉死**：`retargetable` 只认 `PIN_FIELD`；`resolve_shot` 写 `library_asset` 时不置
  `source_pinned`（`assets.py:582`）—— 翻库命中确实**不算**钉死，D31 那条边界成立。
- **强制就是强制**：`branches_for` 对 pexels/local 返回单支，`resolve_shot` 的翻库步骤
  只在 `mode == AUTO` 触发（`assets.py:384`）。
- **回退可见**：`assets.py:605-612` 逐镜列出"库未命中、已回退本地生图：N 镜（2、3、4）"。
- **早退也落盘**：先记下 `recorded` 再比（`assets.py:451-458`），`--source pexels` 失败后
  `source_mode` 仍是 pexels。
- **优先级只有一处**：`sources.resolve`，且显式 `--source auto` 一定生效；配置那层只喂
  `shots` 阶段（D31 那条边界成立）。
- **四个入口俱在**：`run` / `shots` / `assets` / `studio` 的 `--source` 共用 `sources.MODES`，
  `config.toml [shots] source_mode` 作为默认。
- **GUI 只记意向**：`POST /source-mode` 只写 `source_mode`、非法值 400；
  `_effective_source_mode` 与 `demote_pexels` 都委托 `sources.resolve` / `sources.retarget`，
  未复制阶段逻辑（D2）。
- **顺手修 #3/#4**：`studio._assets_scope` 的局部变量已是 `branches`；
  `route_pexels_to_local` 已委托 `sources.retarget`。
- **已知未做两项确未实现**：`apply_mode` 是纯函数不碰磁盘；逐镜挑选仍只靠 GUI 钉图。

---

## 顺带记下（不在本轮范围，未动）

- **本项目不受 mypy 门禁约束**：`mypy lvs` 报 15 处错误，其中 10 处落在本轮**没碰过**的文件上
  （`build.py` / `tts.py` / `parse.py` / `graphic.py` / `progress.py` …）。所以那 15 条不是本轮回归。
  顺带一提 `workspace.py:113` 那条 `write_shots_json is not defined` 是 mypy 误报 ——
  该名字确实定义在 `workspace.py:58`，424 个测试全过。
- ruff 未安装，而代码里写着 `# noqa: ANN001`。也就是说本仓库的规范**靠测试与双轴审查执行，
  不靠工具** —— 这解释了为什么"文档与代码脱节"这类问题能活到审查轮才被抓到。

---

## 教训

**一、看到"文档与代码不符"，先去查点名的那条回归测试。**
本轮两个轴报出来的两条"主要发现"，都是把「从 X 变成 Y」读成了"要求改成 Y"。
而票 41 自己就写了是哪个测试抓住的 —— 那个测试是语义的唯一权威来源。
**测试绿着的时候，"实现错了"这个结论要先被怀疑。**

**二、复核不只是过滤误报，它本身就是发现手段。**
本轮最重的那条（T1，回退产物不可达）没有任何一个轴提到，是主代理在
"证明 F2 是误报"的过程中、顺着同一条分支读下去撞出来的。
把复核当成"给 sub-agent 挑错"会漏掉这类问题；它真正的价值是**逼你把那段代码自己读一遍**。

**三、一个概念配两套判据，迟早出事。**
T5 的 `library_asset` vs `PIN_FIELD`、T4 的四处字段子集，都是"同一个词、两套实现"。
上一轮 T1 的教训是"新标记要检查生产者"；这一轮补上后半句：
**已有标记也要检查有没有第二套判据在偷偷替代它。**
