# 07: 拆镜 —— prompt 与 keywords 生成

**What to build:** 为每个 shot 生成两个字段：`prompt`（`local` 分支用的英文生图提示词，含统一风格后缀）与 `keywords`（`pexels` 分支用的英文检索词，1–3 词）。批量调用 LLM 产出，写回 `shots.json`。

**Blocked by:** 06

**Status:** done

- [ ] 每个 shot 的 `prompt` 与 `keywords` 均非空，且为英文
- [ ] `prompt` 遵循红线：**不引入讲稿中没有的具体人名、数字、事件**，只做视觉转译
- [ ] 同段落内相邻 shot 的 `prompt` 不机械重复（避免整段同一张图）
- [ ] LLM 返回异常时落盘原始响应到任务目录，便于排查

---

## Comments

**What was built:** `prompt`（英文生图提示词 + 统一风格后缀）与 `keywords`（英文检索词 1–3 个）。

**交付记录**
- LLM 路径：分批（20 镜/批）调用 OpenAI 兼容接口；坏 JSON 重试 1 次，两次都坏则把**原始响应落盘**到 `.work/<task>/logs/llm-shots-*.txt` 并在错误信息里给出路径
- 提示词红线写进 system prompt：只做视觉转译，**不引入讲稿外的具体人名/数字/事件**
- 无 key 时的启发式降级：中文画面 + 英文风格后缀（Z-Image 中文理解强）+ 小型中英词表
- 每镜标 `generated_by: "llm" | "heuristic"`，便于事后分辨质量来源
