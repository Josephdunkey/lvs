# 08: 拆镜 —— source 自动判定与落盘校验重试

**What to build:** 为每个 shot 判定 `source`（`pexels` / `local`）并写回 `shots.json`。判定规则：写实场景（自然、城市、人物、器物实拍）→ `pexels`；抽象概念、古籍、地图、图表、示意 → `local`。对 LLM 返回做 JSON 结构校验，失败重试一次，仍失败则落盘原文并报错退出。

**Blocked by:** 07

**Status:** done

- [ ] 每个 shot 的 `source` ∈ {`pexels`, `local`}，无缺失
- [ ] 人工修改 `source` 后再次运行，校验通过且不覆盖人工值（除非显式 `--force`）
- [ ] LLM 返回坏 JSON 时触发一次重试；两次都坏则报错，错误信息包含落盘文件路径
- [ ] `shots.json` 可被直接手工编辑并重跑，不因格式微调而崩

---

## Comments

**What was built:** `source` 判定 + JSON 结构校验 + 人工编辑保护。

**交付记录**
- `source ∈ {pexels, local}`，无缺失（`_validate()` 逐镜校验，失败列明 id 与原因）
- **人工修改 `source` 后重跑 `lvs shots` 不会覆盖**（保留并打印「保留了 N 处人工编辑」），`--force` 才覆盖
- 坏 JSON：重试 1 次 → 仍坏则报错退出，错误信息含落盘文件路径
- `shots.json` 可直接手工编辑（`lvs assets` 只读它，不重新生成）
