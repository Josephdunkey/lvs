# 交接 · 006《吉备津之釜》开跑前（写给新会话）

> 本文件是**新对话的第一读**。读完它，再读当日日志（`.workbuddy/memory/2026-10-05.md` 的「005 收官」节）即可，**不要**一上来重读 shots.json / docs/*.md。

---

## 一、现状（2026-10-05 深夜交接）

- **005《佛法僧》已全链收官**：G0–G5 六门全开；成片 `07-成片/005-佛法僧-成片.mp4`（144.9MB／1221.8s／md5 42f437a2…）；投稿 `08-投稿/UGE05-*`（B站横版+抖音竖版封面**已叠字**、标题、简介、标签）；471 镜 0 占位、字幕已烧录；总表 001–005 五期已成片归档。
- **没有正在跑的后台任务**；ComfyUI/TTS 均已停，GPU 空闲、温度 43°C 健康。
- **config 已改两处**（005 期）：`[voice] align = "estimate"`（按字数估算字幕）、`[publish] episode = 5`。**006 必须把 `episode` 改 6**。

## 二、续跑第一步（省 token 铁律）

```powershell
cd D:\LongVideoStudio
./.venv/Scripts/python.exe -m lvs status --brief --config config.雨月物语.toml
```
≤8 行看全部门禁/产物/下一道门。禁止整读 `shots.json`、`docs/*.md`、会话 jsonl。

## 三、006《吉备津之釜》全链步骤（A 档）

原书 `10-语料/原书分章/雨月物语-06-吉备津之釜.md`（5129 字）。全链：**讲稿 4000–5000 字 → 拍摄稿 → shots 拆镜 → 定妆 → 生图 → 配音 → 成片 → publish 投稿 → 归档**。参考 005 的脚本（`.work/tools/make_shot_005.py` / `replace_slots_005.py` / `make_srt_005.py`）。

1. **讲稿**：写 `01-素材卡/006-吉备津之釜-讲稿.md`（六段 `## ①…⑥`，汉字 4000–5000，语体四红线：诚恳朴实深情忧伤／短句断行／零商业零戏谑／深度层）。
2. **拍摄稿**：`.work/tools/make_shot_005.py` 改参数生成（注意段标题格式 `### 【① …】时间码`、清单表、自检史实）。
3. **migrate→parse→shots**：`lvs migrate/parse/shots`。新角色登记定妆卡 + `lvs cast --extract` 锁定（**只加定妆卡不够，必须 extract 锁了才有**）。
4. **定妆（G2）**：起 ComfyUI(:8188) 出候选→审→approve。GPU 串行。
5. **生图（G3）**：`lvs assets`（停 ComfyUI 续跑自动补缺；看"停=利用率0且图数不涨"，忙时不响应 8188 探测是正常的）。
6. **配音（G4）**：停 ComfyUI → 起 TTS **Base 模型**（clone，勿 CustomVoice）`:8100` → `lvs voice`。**config 已 align=estimate，别再触发 whisper 全量对齐**（见踩坑①）。
7. **成片（G5）**：`lvs build`。**先清 ffmpeg 僵尸进程**（见踩坑②）。
8. **投稿**：`lvs publish --task UGE06 --out "D:\fanshu\雨月物语春雨物语\10-语料\知识视频素材库\08-投稿"`。
9. **归档**：成片→`07-成片/006-吉备津之釜-成片.mp4`；总表 `000-系列拆期总表.md` 加 006 行；当日日志追加。

## 四、踩坑速查（005 亲历，006 必避）

1. **voice 卡 whisper 对齐**：本地 TTS(Qwen3-TTS) 不返回词边界 → voice 会对全部镜跑 whisper 强制对齐，8GB 显存 + TTS 驻留下极慢/崩、srt 迟迟不落盘。**config 已 `align=estimate`**，勿改回 auto；若仍卡在整轨重拼(concat_track)，用手动脚本 `.work/tools/make_srt_005.py` 改 task 路径生成 srt（调 `lvs.tts.build_srt` 从 shots.json 时间轴算）。
2. **build 卡住**：先 `Get-Process ffmpeg*`，若见残留 BGM 测试僵尸（`.work/_bgmtest/final.mp4`，跑几小时、CPU 上万秒）→ `taskkill /F /PID <id>` 清掉，否则 build 逐镜被拖到停滞。
3. **GPU 串行**：ComfyUI(:8188) 与 TTS(:8100) 不可共存；服务用完停回原状。TTS 就绪看日志 `Uvicorn running`，GET / 404 属正常别误判。
4. **门禁**：`lvs gate` 查 G2 定妆**必须带 config**（否则 cast lock 路径错判 stale）。gate 带不带 config 保持一致。
5. **episode 每期必改**：006 改 config `[publish] episode = 6`，否则简介"本期第 N 期"错号。

## 五、命令铁律

- 全 CLI：`./.venv/Scripts/python.exe -m lvs <cmd> --task UGE06 --config config.雨月物语.toml`（不用 anaconda python；PowerShell 无 `&&/||/head`）。
- ffmpeg = `D:\LongVideoStudio\.venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe`。
- 看分镜：`lvs shots --task X --index` / `--peek <id>` / `--full`；读文档先 `docs/INDEX.md` 只读相关 1–2 篇；读源码 `lvs map show <文件> <起> <止>`。
- 测试：日常 `pytest -m "not slow and not gpu"`；只动公共底座才跑全量。

## 六、用户偏好（有 trace 依据）

- 一集一集往下做；B站+抖音双平台投稿、封面**带文字**；讲稿 A 档 4000–5000 字；在意 token 成本与散热（生图/配音长时间满载时 >75°C 就停歇冷却再续，005 实测 43–65°C 健康）。
- 交付要落盘素材库并 present_files；语体四红线见上。

## 七、本会话纪律提醒

005 这个会话跨 G3 生图→G5 收官，已严重超长（远超 AGENTS 建议的 2–3h）。**006 务必从新会话开跑**，本交接文件就是给它的入口。
