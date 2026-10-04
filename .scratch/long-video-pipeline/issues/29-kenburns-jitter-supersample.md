# 29: 图片运镜抖动 —— zoompan 超采样

**What to build:** 消掉 Ken Burns 运镜的抖动。不引新依赖，靠"高分辨率算、lanczos 降回"。

**Blocked by:** 24

**Status:** done

**背景（根因）**：抖动不是编码问题，是 ffmpeg `zoompan` 把裁切窗的 `x/y` 与尺寸**取整** ——
连续的推近于是变成「卡住几帧、跳 1 像素」的阶梯。实测帧间差异呈强交替（3.3 / 1.85 / 2.5 / 1.85…）。

- [x] `kenburns_filter(..., supersample=N)`：预放大 max(2,N)×、zoompan 在 2× 尺寸上算、
      `scale=…:flags=lanczos` 降回成片尺寸
- [x] `supersample=1` **精确回到旧行为**（便于对照与回退）
- [x] 上限 4×：预放大的瓶颈是**内存**（4× 时单帧约 100 MB），不是显存
- [x] 配置项 `build.kenburns_supersample`（默认 2）
- [x] 抑制语/负向提示词那条路**不走**：`zimage_turbo.json` 用 `ConditioningZeroOut` 且 `cfg=1`，
      负向提示词数学上不起作用 —— **但 `sdxl.json` 备用模板的负向是真实生效的**（`cfg=7`、读
      `{{NEGATIVE}}`），故 `lvs/imagegen.py` 保留 NEGATIVE 默认值与映射是**对的**（评审 S2 更正）

---

## Comments

**What was built:** `build.kenburns_filter` 增超采样参数 + 配置项 + 6 个单测。

**实测**（真实素材 `shot-001.png`，139 帧；抖动指标 = 帧间差异的变异系数，越小越匀速）：

| 变体 | 抖动 CoV | 锐度 | 单段编码 | 折算 43 min 成片 |
|---|---|---|---|---|
| `supersample=1`（旧） | 0.2034 | 6.74 | 1.3 s | 12 min |
| **`supersample=2`（新默认）** | **0.1191（−41%）** | **6.76** | 9.1 s | **85 min** |
| `supersample=4` | 0.0905（−55%） | 6.76 | 31.6 s | 297 min |

**关键**：锐度**没有下降**（6.74 → 6.76），所以不是"糊了看着不抖"，是真把量化误差压到了亚像素。

**代价（必须知道）**：默认值会让 `build` 慢 **7 倍**（12 min → 85 min / 43 分钟成片）。
`supersample=4` 是 5 小时，不实用。回退只需 `build.kenburns_supersample = 1`。

**测试**：`tests/test_build.py::KenBurnsSupersampleTest`（6）。
