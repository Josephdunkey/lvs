# 06: 拆镜 —— narration/visual 逐句切分

**What to build:** `lvs shots` 把段落切成逐句（短句优先）的 shot，每个 shot 带 `narration`（该句旁白）与 `visual`（画面描述，优先取时间上最近的画面位，其次用该句语义概括），产出 `shots.json` 骨架（时间字段留空，由后续阶段计算）。

**Blocked by:** 05

**Status:** ready-for-agent

- [ ] K005 产出 60–200 镜
- [ ] `narration` 不丢字、不重复、不包含任何画面位或标记文本
- [ ] `visual` 非空；有画面位的段落，视觉描述与画面位一致
- [ ] 中文断句正确（不把引号内的原文引用拆坏）
