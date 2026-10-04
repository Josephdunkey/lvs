# 41 · 实拍镜的素材来源策略（自动 / Pexels / 生图 / 素材库）

**Status:** done
**复审**：第四轮见 `review-two-axis-4.md`（9 条成立已修 / 3 条推翻，含"钉死的镜会被策略删掉"这个真 bug）；
第五轮见 `review-two-axis-5.md`（5 条成立已修 / 1 条推翻，含"库模式回退过的镜切回自动会被判死"这个真 bug）
**关联**：D14（钉死）、D20/D24（图文卡片不进扩散）、D30（GUI 边界）、票 30（studio）、票 38/S3（一键到底的 pexels 兜底）

## 需求

「项目开始在自动化项目开始时和手动生成素材的时候，可以选择是 pexel 下载还是生图还是
选择本地素材库内文件。」

## 原来的样子：来源在**四个地方各定一次**

| 谁定 | 怎么定 |
|---|---|
| `lvs shots` | LLM/启发式**逐镜**给 `source ∈ {pexels, local, graphic}` |
| `lvs assets --only` | 只筛**这次跑哪些分支**；source 不匹配的镜本轮不动、然后缺素材 |
| `lvs studio` | 勾「取哪些素材」，不勾 pexels 时靠 `route_pexels_to_local` 兜底 |
| GUI 一键到底 | `demote_pexels` —— 没配 key 就**偷偷**改成生图 |

后两条是补丁式的特例。这个功能就是把它们收成一个正经的开概念。

## 设计：意向与现实分家

- **`source_mode`** —— 任务级**意向**（`auto`/`pexels`/`local`/`library`），记在 `shots.json`。
- **`source`** —— 逐镜**现实**，被 `branches_for` / `existing_asset` / `--only` / GUI 筛选直接消费。

`source_mode` 是"这部片子实拍镜从哪儿来"，`source` 是"这一镜最后实际从哪儿来"。
落成动作只有一处：`sources.apply_mode(data, mode)`（票 40 那次三重复制的教训）。

三条边界（都在 `lvs/sources.py` 里写死）：

1. **只作用于实拍镜** —— 图文/图表 beat 留在本地排版渲染器上（D20/D24，进扩散必出伪汉字）。
2. **不碰手工钉死的镜** —— `source_pinned`（D14 的「自己选一张图」）优先级最高。
3. **强制就是强制** —— 选了 `pexels` 就连 §8.1 的「翻库优先」也关掉。否则用户选了下载
   却端出库里的文件，画面哪来的没法解释。`library` 是唯一带第二步的：未命中→回退生图。

### 库未命中 → 自动回退本地生图（用户选定）

`resolve_shot` 的 ② 分支里，`mode == library` 且库里没命中时直接走 `_gen_local()`
（这一段抽成了函数，`source=local` 与回退共用一个实现）。

关键取舍：**回退时 `source` 保持 `library` 不变**，只把 `resolved_by` 记成 `local`。
因为 `source` 是意向；而 `branches_for(mode=library)` 返回 `("library","local")` 两支，
所以重跑能命中回退产物、不会每次白生成一遍。真机验过。

回退必须**可见**：结束时报「库未命中、已回退本地生图：N 镜（2、3、4）」。静默补上
会让用户以为库里真有这些素材。

## 四个入口

```
lvs run      --source local     一键到底，从一开始就按这个来源拆镜
lvs shots    --source library   拆镜时就把意向落成逐镜 source
lvs assets   --source pexels    手动生成素材时改策略（并落盘）
lvs studio   （交互问「实拍镜的素材从哪儿来？」）+ --source 可脚本化
config.toml  [shots] source_mode = "auto"   全局默认
```

优先级：`--source` > `shots.json` 里记着的 > 配置默认。
中间那条是为了"重跑拆镜不会把上次选的来源忘掉"。

GUI：新建任务弹窗选一次（跟 URL 带到任务页），任务页流水线上方随时可改
（`POST /api/task/<name>/source-mode` 只记意向，逐镜 source 仍由 `lvs assets` 落）。

## 现场验证（真机，DeepSeek + 本地 ComfyUI）

拿 `small3` 的副本（3 实拍 + 33 图文），把 3 个实拍镜设成 `pexels`：

```
$ lvs assets --task probe --source library
素材来源策略：本地素材库，其中 3 个实拍镜改了来源
来源策略是「本地素材库」，但库里没有可用索引 —— 本次所有实拍镜都会回退本地生图。
素材获取完成：新取 3，跳过 33，失败 0（共 36 镜）
  来源：local 3
  库未命中、已回退本地生图：3 镜（2、3、4）
```
→ `shots.json`：`source=library / resolved_by=local / status=done`；`source_mode=library`

```
$ lvs assets --task probe                 # 不传 --source，用记着的
素材来源策略：本地素材库
素材获取完成：新取 0，跳过 36，失败 0      ← 幂等，回退产物被复用
```

```
$ lvs assets --task probe --source pexels  # 未配 key
素材来源策略：下载实拍素材（Pexels），其中 3 个实拍镜改了来源
有分镜需要 Pexels 素材，但配置里 `pexels.api_key` 为空。   ← 快速失败，不是 36 个失败
```
→ `source_mode=pexels` 照样落盘（策略是意图，失败也要留）

```
$ lvs assets --task probe --source local
素材来源策略：本地生图（ComfyUI），其中 3 个实拍镜改了来源
素材获取完成：新取 0，跳过 36，失败 0      ← 换来源后旧产物不会被误用，回退产物被复用
```

`lvs assets --source library --no-library` → 拒绝（"两者矛盾，无从取素材"）。

## 顺手修的

- **`branches_for` 曾经漏掉 `source=library`**（改成 mode 驱动时掉的）：auto 下
  `_shot(1,"library")` 从 `("library",)` 变成 `("library","pexels","local")`。
  被 `test_resume.TestBranchScoping` 抓住 —— 这就是回归测试的价值。
- **`lvs assets` 早退时策略没落盘**：`apply_mode` 自己会把 `source_mode` 写成目标值，
  于是"值变了才写"的判断永远为假 → `--source pexels` 失败后 `source_mode` 还是 `auto`。
  改成先记下原值再比。也是测试先发现的。
- `studio._assets_scope()` 里有个局部变量叫 `sources`，与本模块同名 —— 改名 `branches`。
- `route_pexels_to_local` 改为委托 `sources.retarget`（保守翻转，只动正好等于 pexels 的镜）；
  和 `apply_mode`（全量强制）分开，因为 auto 策略下用户的逐镜编辑要尊重。

## 验证清单

- 新增测试 **48** 项：`test_sources.py` 21 + `test_asset_sources.py` 14 + `test_gui_sources.py` 13。
- 全量 **363 → 411 passed**。
- GUI 现场：`/api/task/<n>` 带 `source_mode`；`/api/stage-spec` 四个阶段都有 `source`；
  `POST /source-mode` 落盘、非法值 400；任务页选择器在。

## 已知未做

- `library` 模式的**逐镜挑选**仍然只能靠 GUI 的「自己选一张图」（`source_pinned`）；
  库自动匹配只看关键词与 `min_score`，没做"在界面上从候选里挑一张"的选择器。
- 换来源后**旧分支的文件不会被删**（`apply_mode` 是纯函数，不碰磁盘）。它们已经
  不可达（`branches_for` 只认当前来源），但会留在 `assets/<旧分支>/` 里。想清就 `--force`。
