# LongVideoStudio

把一篇已成稿的**长视频拍摄稿**（Markdown），逐分镜地变成**成片**。

- 输入：`D:\fanshu\资治通鉴\10-语料\*\05-拍摄稿\*.md`（只读引用）
- 输出：一集 `final.mp4`（画面 + 配音 + 烧录字幕）
- 形态：CLI（命令名 `lvs`），每阶段产物落盘、可编辑、可重跑

规格与票据在 `.scratch/long-video-pipeline/`；领域词汇见 `CONTEXT.md`。

## 快速开始

```bash
# 1) 复制配置模板并填入密钥（config.toml 已被 .gitignore 忽略）
cp config.example.toml config.toml

# 2) 环境体检（会告诉你还缺什么，如 ffmpeg）
python -m lvs doctor

# 3) 看命令
python -m lvs --help
```

无需 `pip install` 即可用 `python -m lvs` 运行；也可 `pip install -e .` 后直接用 `lvs`。

## 命令

| 命令 | 阶段 | 状态 |
|---|---|---|
| `lvs doctor` | 环境体检 | ✅ 已实现（票据 01） |
| `lvs parse <script.md>` | 解析拍摄稿 → `parse.json` | ✅ 已实现（票据 04/05） |
| `lvs shots` | LLM 拆镜 + 提示词 → `shots.json` | 待做（票据 06/07/08） |
| `lvs assets` | 素材获取（本地素材库 → Pexels / 本地生图） | 待做（票据 09/18/19） |
| `lvs voice` | 逐镜配音 + 字幕 | 待做（票据 10/11/12） |
| `lvs build` | ffmpeg 合成 → `final.mp4` | 待做（票据 13/14/15） |
| `lvs run <script.md>` | 一键串起全部阶段 | 待做（票据 16） |
| `lvs library index` | 本地素材库扫描建索引 | ◐ 索引/检索已实现（票据 18）；`lvs assets` 内的命中接入待票据 09 |

每个子命令都支持 `--task <name>`，本次运行的产物落在 `.work/<name>/`。

## 关键设计

- **文件即状态**：`.work/<task>/manifest.json` 记录各阶段产物，支持幂等与断点续跑（见 spec §7.1）。
- **本地素材库优先**：`lvs assets` 先翻你已下好/生成好的素材，命中即复用（见 spec §8）。
- **8 GB 显存串行**：本地生图与本地 TTS 不可同时驻留，冲突时**显式报错**（见 spec §11）。
- **不用 moviepy**：合成走 ffmpeg 原生。

## 依赖

- **ffmpeg**（必需，当前未安装）：`winget install Gyan.FFmpeg`
- Python 3.10+（实测 3.13）
- 可选：`edge-tts`（一期配音）、`faster-whisper`（字幕回退）、ComfyUI（二期本地生图）

## 测试

```bash
python -m unittest discover -s tests -v
```
