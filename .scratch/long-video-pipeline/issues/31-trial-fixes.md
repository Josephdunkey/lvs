# 31: 试跑暴露的三处缺陷（卡片正文 / 角标溢出 / 静止段 0 帧）

**What to build:** 修掉 `small` 真机试跑（110 镜）暴露的三个缺陷。

**Blocked by:** 27, 28, 29

**Status:** done

**背景**：票据 27–30 落地后跑真机小样，逐镜核对产物时发现三处问题。前两处是"字不对"，
第三处**直接让成片废掉**（64.7s 画面 vs 447.3s 旁白）。

## 缺陷 1：卡片正文夹带剪辑动词

11/46 张卡片把 beat **原文**当正文，于是屏幕上出现「三段原文并排浮现」这种剪辑指令。
生图提示词过了 `sanitize()`，卡片正文没过。

**坑**：`sanitize()` 对卡片是**错**的工具 —— 它把引号里的内容整段删掉，而对卡片来说
引号里**正是要显示的数据**（`"斩首四万"`）。两者方向相反。

- [x] 新增 `prompting.card_text()`：**摘引号留内容 + 去剪辑动词**（与 `sanitize` 相反）
- [x] 拉丁词两侧留白（`vs` 不糊在一起）
- [x] `assets` 的 graphic 分支改用它；整条都是动作时退回该镜旁白
- [x] 原始素材 `prompting.sanitize()` 一行未动（生图路径不受影响）

实际效果：

```
高亮"斩首四万/斩首虏九万"vs"邑三十六/口三万"  →  斩首四万/斩首虏九万 vs 邑三十六/口三万
三段原文并排浮现                              →  三段原文
"45万"砸屏                                    →  45万
```

## 缺陷 2：右下角旁白角标溢出边框

角标右对齐绘制、**不限宽**，长旁白直接跑出边框被切掉半句（`narration[:18]` 的硬截救不了，
18 个汉字照样超宽）。

- [x] 新增 `graphic.ellipsize(text, measure, max_w)`：二分截断 + 省略号；放不下就返回空串
- [x] `_frame` 先自动缩字号（28→18），再 `ellipsize`，永不溢出
- [x] 去掉 `narration[:18]` 硬截

## 缺陷 3：静止卡片段整段 0 帧（**最严重**）

`build` 报「片段：110 个（占位 0 个）／偏差 382.56s」一切"正常"，实际：

| | 值 |
|---|---|
| `final.mp4` 画面轨 | **64.7 s** |
| 旁白 | **447.3 s** |
| 46 个 graphic 段 | **261 字节空壳**，`duration()` 读不出 |

**根因**：票据 28 让 graphic 段走 `mode="none"` 求"静止"，而这个分支只挂 `fps=30`。
Ken Burns 分支能出片是因为 `zoompan` 的 `d=` **自己复制帧**；`fps` 对**单帧输入**一帧都产不出来 ——
ffmpeg 打印 `No filtered frames for output stream`，却**仍以 rc=0 退出**，于是产出空壳、
`ff_run` 的返回码检查完全失效。`mode="none"` 在此前是**死代码**，票据 28 第一次把它点亮，
等于第一次真正走这条路径。

- [x] 静图输入补 `-loop 1`（Ken Burns 路径不动，它本来就靠 zoompan 复制帧）
- [x] 新增 `build.assert_segment_rendered(path, expected)`：**零帧 / 不可读产物当场抛错**
      —— 教训是"返回码 0 ≠ 产物有效"
- [x] `build_segments` 单镜渲染失败**退回占位帧**并记入 `.placeholders.json`，
      一镜坏不毁整片；修好后重跑 `lvs build` 自动补渲（D17）
- [x] 时长偏差 > max(0.5s, 25%) 时打印提醒（不致命）

---

**验证**：

- `tests/test_build.py::SegmentOutputGuardTest`（2）：空文件 / 缺文件必须被拒
- `tests/test_build.py::StaticSegmentRenderTest`（3）：真跑 ffmpeg，静止段必须有时长、
  Ken Burns 段不受影响、正常产物能过校验
- `tests/test_prompting.py::CardTextTest`（6）、`tests/test_graphic.py::EllipsizeTest`（3）
- 全量 **234 passed**

**复盘**：三处里有**两处**（卡片正文、静止段）都是"新代码第一次真正被点亮"。这说明
**死代码 + 缺产物校验**的组合会让一条从未执行过的分支静默产出垃圾。产物校验（本节缺陷 3 的
`assert_segment_rendered`）比"看返回码"有价值得多。

---

## Comments

本票曾与 `31-graphic-card-text-and-frame.md` **重号**（同一轮建了两个 31，后者是前两处缺陷的子集）。
已合并入本票、删除重号文件 —— issue-tracker 约定「一个票一个文件，从 01 连续编号」。

试跑同时暴露两处**设计层**问题，未在本票处理，另开**票 32** 等对齐：
同一 beat 在不同镜被判成 local/graphic；一个 beat 铺满多镜产生重复卡片。

