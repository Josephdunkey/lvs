# 双轴审查 · 第四轮（票 41「实拍镜来源策略」）

两轴各起一个独立 sub-agent（Standards / Spec），主代理再**逐条回原文件复核**。
本轮 12 条里 **9 条成立、3 条不成立**。误报与真问题都留着，因为"为什么误"比"是什么"更值钱。

## 成立并已修

### T1 · 钉死的镜会被策略删掉（真 bug，最严重）

`sources.apply_mode` / `retargetable` 靠 `shot["source_pinned"]` 认出"这是人挑的，别动"，
但**没有任何生产代码写过这个字段**（`grep source_pinned lvs/` 只有 `sources.py` 自己与测试）。
界面「自己选一张图」写的是 `library_asset` + `source="library"`，不打标记。

后果（已复现）：用户钉死镜 7，然后选「本地生图」重跑 ——

```
钉死前: D:/proj/.work/t/picked/shot-007.png
apply_mode(local) → 改了 1 镜
钉死后: None        ← 他挑的图连同引用一起被清掉
```

而 README 与 D31 都写着"手工钉死的镜不受影响"。修法：**钉图的那一方必须打标记**
（`lvs/gui/app.py` 的 `/pick` 端点写 `target[sources.PIN_FIELD] = True`）。

**为什么没被测试抓住**：`test_sources.py` 手工构造带 `source_pinned` 的数据去测
`apply_mode`，于是"标记有没有人打"这件事一直没被覆盖 —— 典型的
"测了自己设的前置条件"。补了三条回归（端点是否打标记 / 打了之后是否免疫 / 是否仍被 API 报出）。

### T2 · 优先级三处各写一套，且行为不一致

D31 写的是 `--source > shots.json > config`。实现里：
`shots.py` 三层都看；`assets.py` 只看两层；`app._effective_source_mode` 又各写一遍。
其中 `shots.py` 那份还有个真缺陷：显式的 `--source auto` 会被"记着的 local"盖掉 ——
**界面把选择器切回「自动」时会失效**。

修法：抽出 `sources.resolve(cli, recorded, default)`，三处都调它。
语义定死：命令行（含显式 `auto`）> 记下的（`auto` 视为"没记过"，因为 `shots` 每次都会写这个字段）
> 默认。

### T3 · GUI 里的 `demote_pexels` 是 `sources.retarget` 的第二个副本

```python
flipped = [s for s in (shots_data.get("shots") or []) if s.get("source") == "pexels"]
for shot in flipped: shot["source"] = "local"
```
与 `sources.retarget(data,"pexels","local")` 逐字同义，只少了旧字段清理（D2：界面不复制阶段逻辑）。
改为委托，顺带把那几个镜属于旧来源的 `asset_path/status/resolved_by` 也清掉。

### T4 · 文档与代码脱节（spec §8.1 / `assets.py` 模块头 / D31）

票 41 改掉了 §8.1 的两条行为（`library` 未命中要回退、强制模式关掉"翻库优先"），
但 spec 还写着旧规则，`lvs/assets.py` 的模块 docstring 也自相矛盾
（上面写"未命中→失败"，下面四行就是回退实现）。已同步；D31 里补明
"配置那一层只对 `shots` 阶段有意义，`assets` 是执行层只认 `--source` 与已记下的"。

### T5 · 小瑕疵

`shots.py` 里 `previous_mode` 被 `_load_existing` 的返回值二次绑定、之后再没人读。
改名 `_recorded_mode`（这段代码本来就对优先级敏感，重名比平时更贵）。

## 不成立（误报，附证据）

- **"`assets` 不看 `config [shots].source_mode`，是 D31 的实现偏差"** → 部分成立但**结论反了**。
  让 `assets` 也看配置，等于"改了配置就悄悄推翻某个任务已定好的选择"，
  而 shots 阶段已经会把配置播种进 `source_mode`，那一层在 assets 里既做不到好事也无必要。
  **该改的是文档措辞（已改），不是代码。**
- **"auto 模式下 §8.1 被破坏"** → 错。`branches_for` 在 `mode=auto` 时仍返回
  `("library", src)`，`resolve_shot` 的第 ④ 步也仍在，已有测试锁住。
- **"`mode` 参数四处穿越是数据泥团"** → 不成立。单一显式参数、一致地传，
  没有"总是同时出现的几个字段"。Spec 轴也独立确认没有实现层面的错误。

## 两轴一致确认没问题的

- D32 的库未命中回退（`branches_for(mode=library)→("library","local")`、回退后复用、逐镜报出）
  与票 41 的 `## 现场验证` 记录逐条对得上（Spec 轴把那段 transcript 一行行追过代码）。
- 四条边界都成立：只改实拍镜（图文 beat 归排版 D20/D24）、
  策略落成动作只有 `sources.apply_mode` 一处、强制模式确实关掉了翻库前置。
- 四个入口俱在：`lvs run/shots/assets/studio --source`、GUI 新建弹窗、任务页选择器、`config` 默认。

## 教训

`source_pinned` 这场事故的根因不是"忘了写一行"，而是**给一个机制配了新概念（标记）、
却没检查标记的生产者是否存在**。测试构造数据时把缺失的前置条件补上了，于是测试全绿。
以后凡是新增"由某一方打标记、另一方读标记"的协议，测试必须**从生产者那一侧跑**
（走端点的 HTTP 层），不能只从消费者那一侧喂数据。
