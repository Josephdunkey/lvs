# 09: Pexels 素材获取 —— 检索、下载、缓存幂等与手动覆盖

**What to build:** `lvs assets` 只处理 `source=pexels` 的 shot：按 `keywords` 检索横屏 16:9 实拍视频，下载到 `assets/pexels/`。同一素材 URL 本地缓存，重跑不重复下载。人工把某个 shot 的 `source` 改成 `pexels` 后重跑，该镜也会被补齐。

> 注：本票先不含"本地素材库优先命中"（票 18 会把它作为解析的第 ③ 步插入，见 spec §8.1）。本票的检索/下载逻辑要**可被单独调用**，便于 18 在其前插入。

**Blocked by:** 08

**Status:** done

- [ ] 所有 `source=pexels` 的 shot 都拿到本地文件
- [ ] 重跑时已存在的素材被跳过（打印跳过计数）
- [ ] 检索无结果时，该 shot 标记为失败并给出可读原因，其余 shot 继续
- [ ] Pexels key 未配置时给出中文错误，指出配置项名称
- [ ] 下载失败（超时/网络）有重试，且不留下 0 字节的坏文件

---

## Comments

**What was built:** `lvs assets` 的 Pexels 分支（`lvs/assets.py`）。

**交付记录**
- 按 `keywords` 检索横屏（`orientation=landscape`），挑「≥1920 且最小的 mp4」，否则取最大
- 下载到 `.work/_cache/pexels/<sha1>.mp4` 后再复制到 `assets/pexels/shot-NNN.mp4` → **同一 URL 不重复下载**
- 失败隔离：无结果/下载失败只标记该镜 `status=failed` 并给出可读原因，其余继续；结束时打印失败明细
- Pexels key 缺失 → 中文错误指出 `pexels.api_key`，并提示可把该镜改为 local/library
- 下载重试 3 次（递增退避），写 `.part` 再改名 → **不留 0 字节坏文件**

**未验证**：`pexels.api_key` 仍为空，未做真实联网下载验证。填 key 后即可用。
