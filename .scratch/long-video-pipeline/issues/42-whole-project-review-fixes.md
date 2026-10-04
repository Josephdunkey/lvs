# 42 · 全项目复查（第五轮）的修复

**Status:** done
**关联**：`review-two-axis-6.md`、票 34（卡片改版）、票 38（R4/R5 回退校验）、票 23（只读）、D2、D23、D27

第六轮换**范围**（不再看单个 diff，而是全项目健康度）+ 换**问法**（"项目现在怎么样了"）。
两轴共 9 条，复核后 **5 条成立、4 条推翻**。

## 修了什么

### R1 · HTML 产物坏了就不回退（真缺陷，票 38 的验收被自己挡住）

`graphic.render` 的 `_assert_rendered(path)` 写在 `try` **之外**：

```python
if cardhtml.available():
    try:
        path = cardhtml.render(card, out_path)
    except GraphicError:          # ← 只接住"渲染器抛错"
        ...回退...
if path is None:
    path = _render_pillow(...)
_assert_rendered(path)            # ← 校验抛错时，Pillow 回退根本没机会跑
```

于是"截图命令成功、产物是张坏图"这种**最该回退**的情况，反而把整镜判失败 ——
票 38 加 D23 校验（R5）是为了挡坏产物，却顺手掐掉了 R4 的回退。
修法：校验挪进 `try`，坏产物与"截不出来"走同一条回退路径（仍然喊一声 + 留痕）。

现场证据：把 `cardhtml.render` 换成"写一堆垃圾字节然后返回路径"，修复前
`lvs.cardhtml.CardError: 卡片产物无法解码`；修复后正常回退 Pillow 并产出可解码的 1920×1080。
三条回归测试（坏产物 / 没写文件 / 渲染器抛错）。

### R2 · `shots.json` 的写入格式有两个 owner

`lvs/workspace.Workspace.write_shots` 与 `lvs/gui/app.write_shots_file` 各拼一遍
`json.dumps(..., ensure_ascii=False, indent=2) + "\n"`。这个文件同时是**给人手改的真相源**（D14）
和界面读的数据源，格式漂移了会很难查。

修法：格式收进模块级 `workspace.write_shots_json(path, data)`，`Workspace.write_shots`
与界面都调它。读取一侧顺带归一：`run.py` 的内联读取改成 `ws.try_load_shots()`，
`gui/store.read_shots` 也委托给它（"宽容地读"这套语义原本也是三份）。

新增 `tests/test_workspace.py`（10 项）钉住契约：两条写路径逐字节一致、
中文不转义、`try_load_shots` 对缺失/坏 JSON/非 dict 都返回 `{}`、
以及**宽容版不能把"必须有前置产物"那版也变宽容**。

### R3 · spec §18.3 描述的是一个已经不存在的渲染器

spec 写着"`lvs/graphic.py`：**只用 Pillow**…支持 timeline / numbers / columns / flow / card
五套版式"。实际：HTML/CSS 优先、Pillow 回退（票 34），版式是四套且名字不同
（`statement`/`compare`/`timeline`/`list`）。CONTEXT 的 D27 早就写对了，只有 spec 没跟。
已改写。

### R4 · spec §13 还写着"GUI（在 CLI 稳定后补）"

GUI 已交付（票 36/37/40/41），spec 里却仍是个占位符。已补成实况描述
（薄壳、只绑本机、写权限边界、页面清单），并指向 D2/D30/D31。

### R5 · 跟踪器的状态词表对不上

`docs/agents/triage-labels.md` 只定义了五个**分诊**角色，而票用的是生命周期词
（`done` / `implemented` / `open` / `ready-for-agent`）。两个票写 `implemented`，
一个票写 `open`，其余 38 个写 `done` —— 词表里一个都没有。

修法：词表补一张**生命周期状态**表（`claimed`/`resolved`/`ready-for-agent`/`ready-for-human`/
`done`/`open`/`wontfix`），并写明一条纪律：**`done` 必须能被代码与前一轮审查对上**。
`implemented` → `done`；票 32 从 `open` 改为 `ready-for-human`。

## 推翻的（附证据）

- **"`_wrap` 的空格断行条件是反的"** → 错，**实测**（`WrapTest`）：正常英文
  `"Alpha beta gamma delta"` 在空格处断成 `Alpha beta` / `gamma delta`；只有单个词
  **比整行还长**时才切开（那是必须的）；中文逐字断。那个 12 字阈值就是"这个词是不是
  长到放不下"的代理判据，意图是对的 —— 读起来像 bug 是因为没人写注释。已补注释 + 三条测试锁住，
  免得下次有人"修"它。
- **"`shots.json` 读取散在 5 处"** → 夸大。`shots.py` 与 `studio.py` 早在用
  `Workspace.try_load_shots`；真正剩下的只有 `run.py` 一处内联（已改）。写入侧确实重复（R2）。
- **"review-gui.md 的 R1/R2 修复从未进跟踪器（唯一的静默丢失）"** → 错。票 37 第 32、34 行
  逐条记了（草稿任务不进列表 / 子进程块缓冲），并写明 318 → 337 的测试数。
- **"`gui/app.py` 已不是薄壳"** → 不成立。它只经 `registry.start` 起子进程，
  import 的只有 `sources/configio/doctor/workspace/store/config`，没有阶段内部逻辑。

## 留下没做的（等定）

- 票 **25**（ETA 用近期速率而非全程平均）—— 仍在，`ready-for-agent`。
- 票 **26**（assets/voice 增量写 manifest，跑到一半被中断也要留 partial 记录）—— 仍在，`ready-for-agent`。
- 票 **32 现象 A**（同一 beat 判定漂移）—— 仍在，`ready-for-human`。
- ~~死代码~~ **已删**（用户 2026-09-27 确认）：`lvs/ffmpeg.py` 的
  `has_ffprobe`/`has_audio`/`has_video`/`video_size` 与 `gui/store.read_parse` ——
  删前逐条 grep 复核（各只有定义、全仓库含测试零调用）。**用脚本删的时候踩了个坑**：
  我的删除脚本把函数前的空行当成了函数体的一部分并只删了空行，函数本体还在 ——
  已改为用精确的整块替换重做，并复核了空行格式（PEP8 两个空行）。
- `shots.py`（735 行）把断句/beat 归位/LLM 提示词/来源策略/校验/报告揉在一个模块里 ——
  最大的结构性改进点是把纯文本逻辑拆成不碰 IO/LLM 的核心，但那是一次大重构，
  风险与本轮收益不成比例，未动。

## 验证

- 测试 **422 → 440**（新增 18：`test_workspace.py` 10 + `test_graphic` 的回退 3 与断行 3 + 2 处既有回归）。
- `git` 一行没动（全项目仍未提交）。
