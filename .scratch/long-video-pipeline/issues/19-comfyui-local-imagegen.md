# 19: 本地生图 —— ComfyUI + Z-Image Turbo 接入 `local` 分支

**What to build:** 让 `lvs assets` 对 `source=local`（或翻库未命中而落到 `local`）的 shot，调用本机 ComfyUI 生成图片到 `assets/local/<shot>.png`，再由 `lvs build` 做 Ken Burns。ComfyUI 未启动时给出可读中文错误，不阻塞其余 shot。

**Blocked by:** 06, 17

**Status:** done

**契约（见 spec §8.4、§9、§11）**：

- ComfyUI 监听 `127.0.0.1:8188`，用 `/prompt` 提交工作流、轮询 `/history`，下载产物到 `assets/local/`
- 工作流 JSON 与模型清单进版本管理：`workflows/zimage_turbo.json`、`models.lock.json`
- 模型权重不进 git，`scripts/fetch_models.py` 按清单拉取
- 主力 **Z-Image Turbo**（6B，8 步），备用 **SDXL**；模型可通过配置切换
- 出图尺寸按 16:9（如 1344×768 / 1216×684），统一到 spec 约定分辨率

- [x] ComfyUI 未启动时，`lvs assets` 报中文错误（含启动提示），其余 shot 继续
- [x] `source=local` 的 shot 产出 `assets/local/<shot>.png`（**真机已跑通**，见下）
- [x] 出图提示词取自 `shots.json` 的 `prompt`；相同 prompt + seed 可复现（seed 写入 manifest）
- [x] `scripts/fetch_models.py` 能按 `models.lock.json` 拉取（支持断点续传 / 换源 / 手动清单）
- [x] 生成过程可中断续跑：已存在的 `assets/local/<shot>.png` 跳过

---

## Comments

**What was built:** ComfyUI HTTP 客户端 + 工作流模板机制 + 模型拉取脚本 + 手动出图命令。

**核心代码**
- `lvs/imagegen.py`：`/system_stats` 探活 → `/prompt` 提交 → 轮询 `/history` → `/view` 下载
- 工作流是**带占位符的 JSON 模板**（`workflows/*.json`），换模型 = 换模板 + 改 `[comfyui]` 配置，**不用改代码**
- ComfyUI 未启动 → 报中文错误（含启动指引），该镜标记失败、其余继续
- 产出 `assets/local/shot-NNN.png`，已存在则跳过（幂等）
- `scripts/fetch_models.py` + `models.lock.json`：按清单拉模型，**实测 ModelScope ~5MB/s 远快于 hf-mirror ~0.6MB/s**，支持断点续传与换源

**工作流模板（对着 ComfyUI 官方模板逐节点核对）**

从 PyPI 的 `comfyui-workflow-templates-json` 取到官方 `image_z_image_turbo_int8.json`，据此写出 API 版模板：

| 模板 | 节点图要点 |
|---|---|
| `zimage_turbo.json`（主力，10 节点） | `UNETLoader`（int8 UNET）→ `ModelSamplingAuraFlow(shift=3)`；`CLIPLoader(type=lumina2)` 载 Qwen3-4B → `CLIPTextEncode` → **`ConditioningZeroOut` 当负向**（turbo cfg=1 无意义）；`EmptySD3LatentImage`（**Flux 制式 16 通道 /8**）→ `KSampler(res_multistep/simple/8步/cfg1)` → `VAEDecode` → `SaveImage` |
| `sdxl.json`（备用，7 节点） | `CheckpointLoaderSimple` → 双 `CLIPTextEncode` → `EmptyLatentImage` → `KSampler(dpmpp_2m/karras/25步/cfg7)` → `VAEDecode` → `SaveImage` |

> 关键结论：Z-Image 继承 Lumina2，`latent_format = Flux`（16 通道、/8），且 0.34G 的 `ae.safetensors` 就是 **Flux 的 VAE** —— 这两点决定了必须用 `EmptySD3LatentImage` 而不是 `EmptyLatentImage`。
> 另：`CLIPLoader` 的类型列表里**没有 `z_image`**；Z-Image 走 `type=lumina2`（文本编码器按权重自动识别为 Qwen3-4B → z_image 分支）。

**手动出图（D14）**
- 新增 `lvs image`：`--prompt` / `--from-shot N`（从 shots.json 取词）/ `--count` / `--width/--height/--steps` / `--workflow` / `--model`，产物落 `.work/_imagegen/`
- 新增 `scripts/smoke_imagegen.py`：一条命令做「探活 → 核对模型文件 → 出一张小图」的真机冒烟
- `lvs doctor` 的 ComfyUI 项增强：除了探活，还**核对 `[comfyui]` 配置的 UNET/CLIP/VAE 文件是否都在**（<1MB 视为没下完），避免"服务在线但模型没下完"的假绿灯

**测试**：新增 `tests/test_workflows.py`（13 例：占位符无遗漏、节点引用有效、提示词转义、官方参数断言）+ `tests/test_doctor.py`（模型文件核对 / 降级）+ `lvs image` 的 CLI 用例。

**环境（本机实测）**
- ComfyUI 源码取自 `codeload.github.com`（github.com 直连不通），装到 `D:\ComfyUI`，**独立 venv**（与项目主环境隔离）
- `comfyui-workflow-templates` 的传递依赖要求 Python <3.12，与 3.12.7 冲突 → 从 requirements 剔除（API 驱动用不到它的示例工作流）
- `comfy_kitchen` 0.2.35 要求 **torch ≥ 2.7**（torch 2.5.1 缺 `torch.library.custom_op`）→ 升级 ComfyUI venv 的 torch
- **踩坑 3（重要）**：PyPI/清华镜像上的 `torch` 是 **CPU 版**（`+cpu`），装完 `torch.cuda.is_available()` 为 False。必须显式走 CUDA 源：
  `pip install torch==2.14.0+cu126 torchvision==0.29.0+cu126 --index-url https://download.pytorch.org/whl/cu126`

**待验证（晚上开始、未跑完的两件事）**
1. 模型下载：SDXL（6.94G）✅ 已完成；Z-Image 三件套（6.2G + 5.63G + 0.34G）下载中
2. 真机出图：等模型齐 + ComfyUI 启动后，跑 `scripts/smoke_imagegen.py` 即可收官

---

## ✅ 真机验证记录（当晚跑通）

**环境**
- ComfyUI `D:\ComfyUI`，独立 venv，torch `2.14.0+cu126` / torchvision `0.29.0+cu126`，`torch.cuda.is_available()=True`
- 全部 4 个模型到位：SDXL 6.94G / Z-Image UNET 6.20G / Qwen3-4B 5.63G / ae VAE 0.34G
- ComfyUI 启动耗时 **~35s**（`main.py --port 8188`）

**测试 1：`scripts/smoke_imagegen.py`（768×768 / 4 步）**
```
[✓] ComfyUI 在线
[✓] 在线：http://127.0.0.1:8188，模型文件齐（3/3）
[✓] 出图成功：.work\_smoke\out.png（0.80 MB，耗时 15.2s）
```
产出一张「中国古代城郭 + 九鼎 + 黄昏金色余晖」，画面为写实电影感 —— **提示词中文理解完全正常**。
GPU 计算同时验证了 fp32 与 **bf16** matmul（Z-Image 用 bf16）。
> 注：`torch.cuda.get_arch_list()` 无 `sm_89`，但含 `sm_86`；CUDA 的次版本前向兼容（8.6 cubin 可跑在 8.9）成立，4060 上实测正常。

**测试 2：生产分辨率 1344×768 / 8 步**
```
出图成功 1.40 MB  prompt_id=4e7bf7a8-…
```
16:9 画面正常（城墙 + 角楼 + 黄昏 + 人流 + 现代建筑背景）。

**测试 3：真实流水线（2 镜，`lvs assets → voice → build`）**
```
lvs assets --task smoketest --no-library
  素材获取完成：新取 2，跳过 0，失败 0（共 2 镜）
  → assets/local/shot-001.png（1.2M）、shot-002.png（1.4M）

lvs voice --task smoketest
  配音完成：合成 1，跳过 0，失败 1     ← shot 002 遇到 edge-tts 瞬时网络错误
  整轨 narration.mp3 3.5s
lvs voice --task smoketest（重跑）
  配音完成：合成 1，跳过 1，失败 0     ← **幂等 + 断点续跑生效**：只补失败的 1 镜

lvs build --task smoketest --force
  片段：2 个（占位 0 个）             ← 2 个都是真实生图，无占位帧
  画面轨 9.0s｜旁白 9.1s｜成片 9.0s（偏差 0.03s）
  字幕：已烧录
```
抽帧目视确认：**真实 AI 画面 + 中文烧录字幕**（`三十六座城邑，三万口人，天子最后一次结账。`）同框，1920×1080。

**踩坑 4（本次最大，已修）—— GPU 守卫误伤生图**
- 原实现「空闲显存 ≥ 6000 MB」当闸门。第一次出图后 ComfyUI 常驻 ~6 GB，空闲只剩 2.6 GB，
  于是**第 2 个分镜起全部被守卫拦下** —— 270 镜的片子在真实跑批时必然全废。
- **根因**：ComfyUI 常驻本身就是生图阶段的正常状态，用"空闲显存"判断"冲突"是错的判据。
- **修法**：改为**探测对端服务**（精确、无副作用）——
  - `imagegen` 阶段：若 `tts.backend=openai_speech` 且其 `/health` 可达 → 才是真冲突；
  - `tts` 阶段：若 `comfyui.base_url` 的 `/system_stats` 可达 → 才是真冲突；`backend=edge` 直接放行（云端合成不占本地显存）。
- 显存数字降级为**报告 / 告警**（生图阶段只提示不阻断；本地 TTS 阶段仍是硬阈值）。
- 回归测试见 `tests/test_guard.py`（9 例），其中 `test_low_free_vram_is_not_a_conflict` 专门盯这个坑。

**踩坑 5 —— `assets` 回写不一致**
- `asset_path` 只在「跳过已存在产物」的分支写回，**新取的素材反而没写**。已补，并顺手清掉上次残留的 `error` 字段。

**已知小问题（不影响交付）**
- `comfyui-workflow-templates` 未安装 → ComfyUI **GUI 的模板浏览器**不可用（HTTP API 完全不受影响）。
  因该包的传递依赖与 Python 3.12 冲突，故不装；要手搓图可用 `lvs image`，或把自带的
  ComfyUI 官方模板拖进 `http://127.0.0.1:8188`。
- edge-tts 偶发「No audio was received」瞬时网络错误 → 单镜标记失败、其余继续；**重跑 `lvs voice` 即可补齐**。
- CUDA 版本告警：ComfyUI 提示「需要 pytorch cu130 或更高才能用优化 CUDA 算子」。cu126 实测可正常出图，
  只是走不到 `comfy_kitchen` 的优化 kernel（速度略慢）。后续可平滑升级到 cu130。
