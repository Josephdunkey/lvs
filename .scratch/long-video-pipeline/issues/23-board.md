# 23: 看板 —— 五阶段状态一屏总览

**What to build:** `lvs board [--html]`。读 `manifest.json` + `shots.json`，把 `parse/shots/assets/voice/build`
五个阶段渲染成五列卡片（状态 + 明细 + 进度条），末尾给分镜统计。**纯读取，不改任何产物。**

**Blocked by:** 16, 22

**Status:** done

- [ ] 终端看板：五列卡片，状态 `done/partial/todo` 用不同标记区分
- [ ] 分镜统计：已配/未配/失败，按来源（local/pexels/library）分组
- [ ] `--html` 另存**自包含单文件**：内联 CSS、无外链、支持深色模式
- [ ] HTML 标题转义，防注入
- [ ] 只读：不写 manifest、不动 shots、不触发任何生成
- [ ] 复用 manifest 里已记录的状态，不重新推断

---

## Comments

**What was built:** 看板模块（`lvs/board.py`）。

**交付记录**
- `collect(ws)`：从 manifest + shots.json 组装 `Board`（`StageCard` 列表 + 分镜统计）
- `render_text(board)`：终端看板，五列卡片 + 末尾统计
- `render_html(board)`：`--html` 自包含单文件（内联 `_CSS`、深色模式 `@media (prefers-color-scheme: dark)`、
  标题 HTML 转义、**无任何外链**）
- `STAGE_ORDER` 常量固定阶段顺序；状态沿用 manifest 记录的 `done/partial/todo`，不重新推断
- CLI：`lvs board [--task] [--html]`
- 单测 `tests/test_board.py`（6 项）：`collect` 组装、文本渲染、HTML 转义、只读保证
