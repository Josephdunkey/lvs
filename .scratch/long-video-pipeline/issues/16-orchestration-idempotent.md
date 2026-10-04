# 16: 编排 —— 幂等与断点续跑

**What to build:** `lvs run` 串起全部阶段，每个阶段入口先检测自己的产物是否已存在并完整，已完成的直接跳过；任一阶段失败后重跑，不重复已完成的工作。

**Blocked by:** 03

**Status:** done

- [ ] 连续两次 `lvs run`，第二次显著更快，且产物与第一次一致（不重复生成）
- [ ] 删除某一阶段的产物后重跑，只重跑该阶段及其下游，上游不重跑
- [ ] 某个 shot 的产物缺失时只补该 shot，不整批重做
- [ ] `--force` 可强制全量重跑
- [ ] 每阶段结束打印耗时与产物路径

---

## Comments

**What was built:** `lvs run` 编排（`lvs/run.py`）。

**交付记录**
- 按 `parse → shots → assets → voice → build` 顺序执行，**每阶段入口先看产物是否已存在且完整**，已完成则跳过
- `--force` 全量重跑；`--no-library` / `--no-llm` 透传下游
- 断点续跑：删除某阶段产物再 `lvs run`，只重跑该阶段及其下游（`manifest.json` + 实际文件双重判断）
- 单 shot 粒度：`assets`/`voice` 以 shot 为单位记录产物，缺哪个补哪个
- 每阶段打印耗时与产物路径；`--demo` 走走路骨架（票据 03）
