# 18: 本地素材库 —— 索引、检索与"翻库优先"命中

**What to build:** 让 `lvs assets` 在下载/生图之前，先翻用户本机的**素材库文件夹**（配置在 `config.toml` 的 `[library].dirs`，可多个，默认空 = 功能关闭）。提供 `lvs library index` 扫描素材库生成 `library-index.json`（图片 + 视频，带 `tags`），并在素材解析时按关键词命中：命中即复制/软链到 `assets/library/`，写 `library_asset` 与 `resolved_by=library`，不再走下载/生图。

**Blocked by:** 09

**Status:** in-progress —— 索引与检索已完成并测试；`lvs assets` 内的"翻库优先"接入待票据 09

已完成（`lvs/library.py` + `tests/test_library.py`）：
- [x] 素材库未配置（`dirs` 为空或目录不存在）时**静默跳过**（`lvs library index` 打印提示、退出 0；不产生索引）
- [x] 扫描：图片/视频识别、忽略非媒体与 sidecar、递归开关
- [x] tags：文件名分词 + 文件夹名 + 同名 sidecar（`*.json` 的 `tags` / `*.txt` 逐行）
- [x] 检索：`score_entry` 加权重合度（词元交集 ×2 + 路径子串命中 ×1），`min_score` 可调
- [x] **不改动原文件**（测试断言扫描前后 mtime/size 不变）
- [x] 索引进 `.work/_library/library-index.json`，签名一致则复用；`--reindex` 强制重建
- [x] 不相关关键词零误命中（测试用 `zzz-nonexistent` 断言）
- [x] 尺寸/时长探测失败降级（无 Pillow / 无 ffprobe 时为 `None`，不报错）

待完成（依赖票据 09）：
- [ ] 在 `lvs assets` 中按 §8.1 顺序插入"翻库优先"，命中落到 `assets/library/` 并写 `library_asset`/`resolved_by`
- [ ] `source=library` 未命中时只标记该 shot 失败，其余继续
- [ ] 缩略图/尺寸读取失败的 entry 降级但仍可用（当前为 None，需在 assets 侧确认可消费）
