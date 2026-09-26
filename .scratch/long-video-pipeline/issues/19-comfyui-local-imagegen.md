# 19: 本地生图 —— ComfyUI + Z-Image Turbo 接入 `local` 分支

**What to build:** 让 `lvs assets` 对 `source=local`（或翻库未命中而落到 `local`）的 shot，调用本机 ComfyUI 生成图片到 `assets/local/<shot>.png`，再由 `lvs build` 做 Ken Burns。ComfyUI 未启动时给出可读中文错误，不阻塞其余 shot。

**Blocked by:** 06, 17

**Status:** ready-for-agent

**契约（见 spec §8.4、§9、§11）**：

- ComfyUI 监听 `127.0.0.1:8188`，用 `/prompt` 提交工作流、轮询 `/history`，下载产物到 `assets/local/`
- 工作流 JSON 与模型清单进版本管理：`workflows/zimage_turbo.json`、`models.lock.json`
- 模型权重不进 git，`scripts/fetch_models.py` 按清单拉取
- 主力 **Z-Image Turbo**（6B，8 步），备用 **SDXL**；模型可通过配置切换
- 出图尺寸按 16:9（如 1344×768 / 1216×684），统一到 spec 约定分辨率

- [ ] ComfyUI 未启动时，`lvs assets` 报中文错误（含启动提示），其余 shot 继续
- [ ] `source=local` 的 shot 全部产出 `assets/local/<shot>.png`，且为 16:9
- [ ] 出图提示词取自 `shots.json` 的 `prompt`；相同 prompt + seed 可复现（seed 写入 manifest）
- [ ] `scripts/fetch_models.py` 能在缺模型时按 `models.lock.json` 拉取（或打印可执行的手动步骤）
- [ ] 生成过程可中断续跑：已存在的 `assets/local/<shot>.png` 跳过
