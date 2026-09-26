# 07: 拆镜 —— prompt 与 keywords 生成

**What to build:** 为每个 shot 生成两个字段：`prompt`（`local` 分支用的英文生图提示词，含统一风格后缀）与 `keywords`（`pexels` 分支用的英文检索词，1–3 词）。批量调用 LLM 产出，写回 `shots.json`。

**Blocked by:** 06

**Status:** ready-for-agent

- [ ] 每个 shot 的 `prompt` 与 `keywords` 均非空，且为英文
- [ ] `prompt` 遵循红线：**不引入讲稿中没有的具体人名、数字、事件**，只做视觉转译
- [ ] 同段落内相邻 shot 的 `prompt` 不机械重复（避免整段同一张图）
- [ ] LLM 返回异常时落盘原始响应到任务目录，便于排查
