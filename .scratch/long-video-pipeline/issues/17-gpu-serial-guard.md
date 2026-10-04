# 17: GPU 串行调度守卫

**What to build:** 阶段入口读取当前显存占用与已知常驻服务（本地生图、本地 TTS），当发现会争抢 8 GB 显存的组合时，**显式报错并提示先停掉哪个服务**；不做自动抢占、不静默降级。

依据：实测本地 TTS（Qwen3-TTS 1.7B）单模型占 4.45 GB，本地生图约 6–8 GB，两者无法在 8 GB 卡上同时驻留（见 spec §11）。

**Blocked by:** 10

**Status:** done

- [ ] 冲突场景下打印中文、可照做的提示（说明停哪个服务、怎么停），且不产生 OOM
- [ ] 无冲突时不增加明显开销（检查本身 < 1s）
- [ ] 可通过配置跳过检查（用于换大显存卡或服务器环境）
- [ ] 检测不到 `nvidia-smi` 时静默降级（不阻断流程）

---

## Comments

**What was built:** GPU 串行调度守卫（`lvs/guard.py`）。

**交付记录**
- 阶段入口检查：`assets` 的 local 分支 vs 本地 TTS；冲突时抛 `GPUConflict`，打印中文可照做提示 + **列出占用显存的进程**（pid/名称/显存），明确说「停哪个服务、怎么停」，**不自动抢占、不静默降级**
- 无 `nvidia-smi` → 静默降级（不阻断）
- `gpu.skip_check=true` 可跳过（换大显存卡/服务器）

**⚠️ 重大修正（真机跑通后，见票据 19 的「踩坑 4」）**

初版用「空闲显存 ≥ 阈值」当闸门，**实测会误伤生图**：ComfyUI 第一次出图后常驻 ~6 GB，
空闲只剩 ~2.6 GB，于是第 2 个分镜起全部被拦下 —— 270 镜的片子在实际跑批时必然全废。

**改用「探测对端服务」作为冲突判据**（精确、廉价、无副作用）：

| 阶段 | 判据 | 动作 |
|---|---|---|
| `imagegen` | `tts.backend=openai_speech` 且 `tts.base_url/health` 可达 | 抛 `GPUConflict`（提示停 TTS） |
| `tts` | `comfyui.base_url/system_stats` 可达 | 抛 `GPUConflict`（提示关 ComfyUI） |
| `tts` + `backend=edge` | 云端合成、不占本地显存 | 直接放行，不检查 |

显存数字降级为**报告 / 告警**：
- 生图阶段：低于 `need_imagegen_mb` 只打印提示（"仅提示，不阻断"）——因为 ComfyUI 常驻后空闲显存变低是**正常状态**；
- 本地 TTS 阶段：低于 `need_tts_mb` 仍是**硬报错**（此时确实没有余量）。

回归测试：`tests/test_guard.py` 共 9 例，其中 `test_low_free_vram_is_not_a_conflict` 专门锁死这个坑。
