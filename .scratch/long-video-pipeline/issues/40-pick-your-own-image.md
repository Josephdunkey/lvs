# 40 · 自己挑一张图替换某一镜

**Status:** done
**关联**：D14（手工挑图是一等公民）、票 30（片段缓存只看时长）、票 36/37（GUI）

## 需求

「进任务之后那个换一张图片的功能，可以让我自己在文件夹里选一张图片。」

## 设计：复用已有的「钉死」机制，不造第二套

项目本来就有 D14 的 `library_asset` —— `assets.py` 的解析顺序第 ① 条就是
「`library_asset` 已钉死？→ 直接用该文件」，`branches_for()` 也以它为键。
所以这个功能**不需要新 schema、不需要新 source 值**：

```
存原图 → workspace.clear_shot_artifacts(sid)
       → shots.json: library_asset=<原图>, source=library
       → lvs assets --only library   （① 分支把原图材化进 assets/library/）
```

## 踩到的两个坑（都是真跑才发现的）

### 坑一：`studio --redo` 会把刚钉上的清掉
第一版用 `lvs studio --redo <sid> --action library`（复用"重做这一镜"）。
**结果卡片换了但画面没换**，日志里是：

```
shot 001：source=library 但素材库未命中（关键词 三十六个邑…）
seed: 44            ← redo 顺手改了 seed
library_asset: None  ← 钉死被清了
```

根因：`clear_shot_assets()` 里有 `shot.pop("library_asset", None)`，
注释写着「要的是"新一张"，不是复用上次的库命中」—— 对"重做"是对的，
但正好把"钉一张新的"也清掉了。改用 `lvs assets --only library`（它只材化，不清钉死）后正常。

**教训**：单元测试里把 `registry.start` 打桩成假 job，就**看不见**真实 CLI 的行为差异 ——
这个 bug 是现场跑出来的，不是测出来的。

### 坑二：不清旧产物，新图永远材化不出来
`existing_asset` 会在 `assets/library/` 里找到**上一次材化的那张** → 判定"已有" → 直接跳过，
于是第二次换图不生效。而且片段缓存**只看时长**（票 30），不删片段的话 `build` 会继续用旧帧。
所以换图时必须先把这一镜的旧素材与片段清掉。

## 顺手消掉的一处三重复制

「逐镜素材分支表」原本在 `assets.py`、`studio.py`、`gui/store.py` **各写了一份**，
`gui/store.py` 那份连顺序都不一样。现在归到 `workspace.ASSET_BRANCHES`（布局的唯一 owner），
并把 `shot_name()` 与 `clear_shot_artifacts(sid)`（只删文件、不碰 shots.json）也放那儿 ——
于是 studio 的「重做这一镜」与 GUI 的「换一张我选的图」**共用同一份清理逻辑**。

## 验证

- 新增 9 项测试：格式/空文件/坏图/镜号不存在/忙时拒绝（且**什么都不改**）、
  钉死写入、清理旧产物、必须走 assets 而非 studio、`pinned` 出现在 API。
  另加 2 项锁住 `library_asset` 的**自杀式硬链接**陷阱
  （`_materialize` 先 `dst.unlink()` 再 `os.link(src,dst)`；若钉死路径等于材化目标就会被删掉，
  所以原图另存到 `picked/` 而不是 `assets/library/`）。
- **现场端到端**：选一张纯青图 → `library_asset` 保住 → 材化产物字节**与该图完全一致**
  → 缩略图均值 RGB **(1, 200, 181)**（就是那张青图）→ 日志 `0/1 新取 1，跳过 0`。
- 测试 353 → **363**。
