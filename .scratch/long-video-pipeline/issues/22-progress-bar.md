# 22: 进度条 —— 长循环的可视化

**What to build:** `素材`/`配音`/`合成`/`拆镜` 四个长循环在终端上给出行进可视化，让用户知道"到哪了、还有多久"，而不是盯着一堆滚动日志。

**Blocked by:** 16

**Status:** done

- [ ] TTY 单行原地刷新：`[████░░░░] 45% 122/270 ETA 12:30`
- [ ] 非 TTY（日志/后台/重定向）退化为每整数百分比一行，**不刷屏、不混入 `\r`**
- [ ] 零第三方依赖
- [ ] `track(items, prefix)` 生成器：调用方 `continue`/提前 `break` 都不用额外照顾
- [ ] ETA 基于已用时间与已完成比例的均值，未知时显示 `--:--`

---

## Comments

**What was built:** 进度条模块（`lvs/progress.py`）。

**交付记录**
- `render_bar(done, total, width)` 纯函数，产出 `[████░░░░] 45%` 文本
- `fmt_eta(seconds)` 把剩余秒格式化成 `MM:SS` / `H:MM:SS`，未知返回 `--:--`
- `Progress` 类：TTY 走 `\r` 原地刷新；**非 TTY 只在整数百分比变化时打一行换行**，日志友好
- `track(items, prefix)` 生成器：包裹任意可迭代，逐项推进；`for shot in track(shots, "配音")` 写法，
  `continue` 天然安全（推进在 `next` 时发生）
- 挂载点：`assets`（素材）、`tts`（配音）、`build`（合成）、`shots._llm_enrich`（拆镜）
- 单测 `tests/test_progress.py`（13 项）：覆盖渲染、ETA 格式化、TTY/非 TTY 分流、`track` 的
  `continue`/提前退出行为
