# 18: 本地素材库 —— 索引、检索与"翻库优先"命中

**What to build:** 让 `lvs assets` 在下载/生图之前，先翻用户本机的**素材库文件夹**（配置在 `config.toml` 的 `[library].dirs`，可多个，默认空 = 功能关闭）。提供 `lvs library index` 扫描素材库生成 `library-index.json`（图片 + 视频，带 `tags`），并在素材解析时按关键词命中：命中即复制/软链到 `assets/library/`，写 `library_asset` 与 `resolved_by=library`，不再走下载/生图。

**Blocked by:** 09

**Status:** ready-for-agent

**契约（见 spec §8.1、§8.3）**：

- 素材解析顺序：`钉死 library_asset` → `source=library（必须命中）` → `翻库命中` → `source 分支`
- 索引条目字段：`path / type / width / height / duration / tags / mtime / size`
- `tags` 来源：文件名分词 + 所在文件夹名 + 同名 sidecar（`*.json` 的 `tags` 字段或 `*.txt` 逐行）；缺失 ffprobe/PyAV 时降级为"仅文件名标签"
- 命中：按 shot 的 `keywords` 与 entry `tags` 加权重合度打分，≥ `min_score` 取最高
- 索引缓存：`.work/_library/library-index.json`（跨任务复用，按 `dirs` mtime 增量），`--reindex` 强制重建

- [ ] 素材库未配置（`dirs` 为空或目录不存在）时，`lvs assets` **静默跳过**该步，流程与未加此功能时**完全一致**
- [ ] 配置素材库后，命中分镜落到 `assets/library/<shot>.<ext>`，`shots.json` 写入 `library_asset` 与 `resolved_by=library`
- [ ] **不改动**素材库里的原文件（命中是复制或软链；用 mtime/size 断言原文件未被修改）
- [ ] `source=library` 的 shot 未命中时，**只标记该 shot 失败**并给出可读原因，其余 shot 继续
- [ ] `min_score` 可调；分数不达标不命中（构造一个"擦边"用例断言不误命中）
- [ ] 缩略图/尺寸读取失败不崩溃，该 entry 降级但仍可用
- [ ] 连续两次 `lvs assets`，第二次不重复索引、不重复复制（幂等）
- [ ] `lvs library index` 可单独运行；`--reindex` 强制重建；打印条目数与耗时
