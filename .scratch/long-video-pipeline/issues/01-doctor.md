# 01: 环境体检与 ffmpeg 引导

**What to build:** 一条 `lvs doctor` 命令，在新机器上跑一下就知道还缺什么：ffmpeg、Python 版本、NVIDIA 驱动与可用显存、LLM API key、可选组件（ComfyUI、本地 TTS）。缺 ffmpeg 时给出 Windows 上可执行的安装指引（winget / 官方包 / 解压后加 PATH），检测通过后打印实际版本号。

**Blocked by:** None (can start immediately)

**Status:** done (2026-02-14)

- [x] `lvs doctor` 逐项打印检查结果（通过 / 缺失 / 警告）
- [ ] ffmpeg 缺失时给出可直接照做的安装步骤，装好后 `ffmpeg -version` 可用且 doctor 显示通过
      —— 安装步骤已给出并实测打印；"装好后转通过"待在真机装 ffmpeg 后回归（当前机器未装）
- [x] 打印 NVIDIA 驱动版本与当前可用显存
- [x] 检查过程静默降级：某项无法检测时不崩溃、不误报通过
