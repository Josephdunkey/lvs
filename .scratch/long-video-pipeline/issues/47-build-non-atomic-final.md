# 47 · build 非原子写入：中断后留下 `moov atom not found` 的坏 `final.mp4`

Status: open
Type: bug
Severity: medium（会静默污染交付目录）

## 现象

`NW03` 的 `lvs build` 在**烧字幕**一步失败退出（见 issue 46 的环境背景），
但 `.work/NW03/final.mp4` 仍然存在（235 MB，06:50），且被复制到 `07-成片/`。

实测：

```
[mov,mp4,m4a,3gp,3g2,mj2 @ ...] moov atom not found
Error opening input: Invalid data found when processing input
```

文件头 `ftyp isom` 正常，但 **moov box 缺失** → 整个文件不可播放。

## 根因

`build` 直接把 ffmpeg 的输出**写到最终路径** `final.mp4`。
ffmpeg 被中断（崩溃 / 拒绝加载 DLL / 断点续跑的进程被 kill）时，
已写入的 `ftyp + mdat` 留在原地，`moov`（写在文件末尾）永远没写 →
留下一个**大小正常、但打不开**的文件。

配套的 `manifest.json` 因为进程非正常退出，`stages.build` 是空的 `{}`，
所以**状态文件也没记录"失败"**。下次跑 `lvs build` 若只判 `final.mp4` 是否存在，
就会把这具空壳当成有效产物跳过。

## 影响

- `NW03` 中招：坏 `final.mp4` 已被交付过一次（现已删除重跑）
- 任何在编码中途被杀的任务都会中招（含机器崩溃、会话中断、手动 kill）
- 更糟的是**它看起来"正常"**：文件非零、体积合理、扩展名正确

## 建议修法

1. **原子写**：ffmpeg 输出到 `final.mp4.tmp`，成功后 `os.replace()` 到 `final.mp4`。
   一步到位，成本最低。
2. **产物校验**：`build` 收尾时探测 `moov`（或直接 `ffmpeg -i` 试开），
   失败则删除半成品并报错退出。这样配合 `manifest` 就不会留下"假成功"。
3. **幂等判定别只看存在性**：`build` 判"是否已完成"时应读 `manifest.stages.build.status == done`，
   而不是 `Path("final.mp4").exists()`。
4. `07-成片/` 的复制动作也应加同一道校验（本项目是手动 `cp`，建议脚本化并带校验）。

## Comments

- 2026-09-30 · 发现于 `NW03` 重跑排版。建议 1 + 2 一起做：1 防产生，2 防漏网。
- 与 issue 46 同源场景（都在 `build` 收尾处暴露），但**根因独立**，可分别修。
