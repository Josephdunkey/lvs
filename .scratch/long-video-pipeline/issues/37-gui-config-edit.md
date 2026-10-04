# 37 · GUI 二期：配置可编辑 + 一键到底 + 审查修复

**Status:** done
**关联**：票 36、D30（修订）、D23、review-gui.md

## 起因

1. 用户要**在界面里改配置**（尤其素材库位置）—— 推翻票 36 定的"配置只读"。
2. 补票 36 记的"一键到底"缺口。
3. 按用户要求做一轮代码审查（结果见 `review-gui.md`）。

## 做了什么

### 37.1 配置可编辑（`lvs/gui/configio.py`）
- **用 `tomlkit` 回写**：`config.toml` 每行都有注释，`tomllib`+重序列化会抹掉；tomlkit
  保留注释 / 顺序 / 格式（实测替换值时同行注释也在）。
- **白名单**：只有 `library.dirs / min_score`、`app.openai_*`、`pexels.api_key`、
  `shots.visual_mode`、`build.kenburns / kenburns_supersample`、`comfyui.base_url` 可改；
  其余（如 `build.subtitle_style`）一律拒。
- **密钥打码**：读时只露头尾（短密钥显示"已设置"）；改时填新值、留空 = 不改。
- **校验 + 原子写**：类型/取值校验，先写 `.tmp` 再 `replace`，失败抛 `ConfigIOError(ValueError)`。
- 改完**下一次跑阶段即生效**（子进程每次重新读 config.toml），不用重启服务。
- 接口：`GET /api/config`（白名单字段+当前值+打码）、`POST /api/config`（校验+回写）。
- 设置页新增可编辑表单（目录=多行文本框、选择项=下拉、密钥=password 输入框）。

### 37.2 一键到底
- `jobs.STAGE_SPEC` 加 `run`（空选项）—— 界面顶栏「一键到底」按钮，等价 `lvs run`，
  一个子进程跑完 parse→shots→assets→voice→build。
- 已知限制：有 pexels 镜且没配 key 时 `lvs run` 会在 assets 停下（诚实报错），可先在分镜栅格里改来源。

### 37.3 审查抓出、已修的两个 bug（见 review-gui.md）
- **R1 草稿任务不进列表**：`list_tasks` 只认 manifest.json，新建未跑 parse 的任务切回首页就消失。
  改为「有 manifest **或** manuscript」即列出。
- **R2 子进程 stdout 块缓冲**：日志一段段蹦；给子进程加 `PYTHONUNBUFFERED=1` 逐行 flush。

## 验证

- 测试 **318 → 337**（`tests/test_gui_config.py` 11 项 + 配置路由 5 项 + 草稿/无缓冲 2 项，全先红后绿）。
- 配置回写实测保留注释（`review-gui.md` 里贴了 tomlkit 的 dump 输出）。
- 审查覆盖 `lvs/gui/*` + 两个核心模块小改动，每条结论都读了真文件。

## 决策修订

`D30` 第②条的「config.toml 只读展示」改为「可编辑**白名单子集**，注释保留、密钥打码」；
其余四条边界不变。
