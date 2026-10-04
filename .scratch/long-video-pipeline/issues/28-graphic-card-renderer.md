# 28: 图文 / 图表 beat 的 PNG 渲染器

**What to build:** `graphic` beat 不进生图，改由本地排版成 1920×1080 卡片进视频；
四套版式（时间轴 / 数值 / 分栏 / 流向），兜底 `card`。

**Blocked by:** 27

**Status:** done

**背景**：「画面位」里很多 beat 本来就不是照片 —— 时间轴、分栏呈现、流向图、账面对比图。
把它们交给图像模型，既画不对（伪汉字），又浪费 GPU。

- [x] 新模块 `lvs/graphic.py`，**只用 Pillow**（不引 matplotlib），结果确定
- [x] `layout_of()` 按关键词选版式；`extract_items()` 引号原文优先、其次箭头串
- [x] `available()` 缺 Pillow / 缺中文字体时为假 → 调用方降级，不硬崩
- [x] `assets` 增 `graphic` 分支（§8.1 ③，在翻库之前）：渲染卡片、**不调 ComfyUI**
- [x] `workspace.SUBDIRS` 增 `assets/graphic`
- [x] `build` 对这些镜**静止不运镜**（卡片是"要读的信息"，运镜只会更难读）
- [x] `assets --only` 按来源筛选，供 studio 分步取材

---

## Comments

**What was built:** `lvs/graphic.py`（5 套版式）+ `assets` 的 graphic 分支 + `build` 静止处理。

**交付记录**
1. `graphic.py`：`render(text, out_path, narration=, title=)`，1920×1080，暗底暖调，
   配色与占位帧/字幕一致；版式 `timeline` / `numbers` / `columns` / `flow` / `card`
2. `extract_items`：引号原文优先（`高亮"a"vs"b"` → 两条），其次箭头串（去掉 `时间轴：` 模板头）
3. 字体按 `msyh.ttc → simhei.ttf → simsun.ttc → PingFang → Noto CJK` 找，找不到则 `available()=False`
4. `assets.resolve_shot` 在"钉死 → source=library"之后插入 graphic 分支，翻库之前
5. `assets.run_command`：`--only` 按 `source` 筛选；缺渲染条件时明确提示并只影响该类镜
6. `build.build_segments`：`kind == graphic` 的镜用 `mode="none"`（静止）
7. 测试：`tests/test_graphic.py`（12，含四版式各出一张 1920×1080、
   确定性渲染、空 beat 不崩）

**实测**：真实合稿的 beat 渲染出可读中文卡片，**无伪汉字**；
`时间轴：前403 → …` 正确走 timeline 版式。
