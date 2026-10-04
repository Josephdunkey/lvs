# GUI 代码审查（票 36/37 之后）

**范围**：`lvs/gui/*`（store / jobs / app / configio / 模板 / 静态）、`cli.py` 的 `gui` 子命令、
为 GUI 开的两个口（`Config.as_dict()`、`doctor.all_checks()`）。
**固定点**：本轮改动之前。
**基准**：`CONTEXT.md` 的 D 决议、`AGENTS.md`、spec、票 36 的 grill 结论。

## 结论速览

两条**真 bug**（审查时靠"读真文件 + 跑真码"抓出来，不是靠猜），已修 + 回归测试：
一条实现细节确认**正确**（差点误报），若干低危/判断项留着没动。

## 抓出来并已修

### R1 · 草稿任务不进列表（用户能复现）
`store.list_tasks()` 只认 `manifest.json`。但 `POST /api/task` 只写 `manuscript.md`，
`parse` 之后才有 manifest。于是**用户建完任务、切回首页，任务消失了**，直到先跑一次 parse。
修：判据改成「有 manifest.json **或** manuscript.md」；`.work/_fixtures` 这类临时目录两者都没有，仍被排除。
测试：`DraftTaskTest`。

### R2 · 子进程 stdout 块缓冲 → 日志不是实时的
`jobs._run` 里子进程 stdout 是管道（非 tty），Python 默认**块缓冲**；`_configure_stdio()` 只设了
UTF-8 编码、没设行缓冲。所以界面"实时日志"其实是一段一段蹦出来的。
修：子进程 env 加 `PYTHONUNBUFFERED=1`（逐行 flush）。测试：`UnbufferedSubprocessTest`。

## 验证过、确认"对"的（差点误报）

- **tomlkit 替换值时保留同行注释**：`min_score = 1  # 关键词命中阈值` 改值后注释还在（实测）。
- **串行锁真的串行**：`JobRegistry.start` 的 busy 检查 + `_current` 赋值都在同一把锁里，
  `_retire` 清位也上锁；双线程同时 `start` 不会漏出两个并发 job。
- **路径越界挡得住**：`../..`、`..%2F..%2F`、`..%5C..%5C`、`/etc/passwd`、`....//` 全都取不到
  （测试断言"永不 200"，重定向也算）。
- **密钥不泄漏**：`configio._mask`（短密钥显示"已设置"，长密钥只露头尾）与 `_config_rows`
  的遮蔽一致；`/media` 只能到 `.work/<task>/` 内，够不着根目录的 `config.toml`。

## 留着没动的（低危 / 判断项）

> **2026-09-27 更新**：S1/S2/S3/S5 已修（票 39），S4 已修（票 38 / R2）。
> 只剩 S6，且它是设计选择（唯一安全边界是只绑 127.0.0.1）。下表保留原始记录。

| 编号 | 位置 | 问题 | 处置 |
|---|---|---|---|
| S1 | `app.media_thumb` | 缩略图新鲜度用 `mtime >=`，同一秒内重渲可能吃到旧图 | 重渲 ≥1s，几乎不可能同秒；留 |
| S2 | `app.api_create_task` | 任务名 slugify 后撞已有目录会静默覆盖拍摄稿 | 低危；留 |
| S3 | `run` 一键到底 | 有 pexels 镜且没配 key 时 `lvs run` 会在 assets 停下 | 已知限制；界面的"改来源"可绕 |
| S4 | 遮蔽逻辑两处 | `_config_rows` 靠后缀、`configio` 靠显式 `secret` 标志 | 可接受；两处语义不同（全表 vs 可编辑白名单） |
| S5 | `jobs.JobRegistry` | 日志只在内存，服务重启即丢 | 票 36 已记，非本轮 |
| S6 | 无鉴权 | 唯一安全边界是「只绑 127.0.0.1」 | 保持；**不要加 `--host 0.0.0.0`** |

## 标准符合度

- **D2（GUI 是薄壳）** ✓：`jobs` 只 spawn `python -m lvs`，不 import 阶段模块；状态读 `.work/`。
- **D13（上游资产只读）** ✓：素材库页只扫描；唯一的新写是 `config.toml`（用户本轮明确要求，见 D30 修订）。
- **D26（只读命令不落盘）** ✓：`GET /`、`GET /task`、`/api/library`、`/api/doctor` 都不写。
- **D30 五条边界** ✓，其中「配置只读」被本轮推翻 → 见 D30 修订。
- **零构建前端** ✓：Jinja + 原生 JS + SSE，无 node_modules。

## 一次自纠

本轮的**验收脚本**自己先犯过错（`wait_idle` 只看"空闲"、不看退出码，把 rc=1 的失败当成功），
已修 —— 这跟 D23「返回码 0 ≠ 产物有效」是同一课的两面：**进度条证明不了成功，退出码才能**。
