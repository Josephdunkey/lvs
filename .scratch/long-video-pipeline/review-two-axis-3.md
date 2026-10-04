# 第二轮双轴审查（票 34–37 的新代码）

**范围**：`lvs/cards.py`、`lvs/cardhtml.py`、`lvs/graphic.py`（重写）、`lvs/assets.py`（CardBench）、
`lvs/prompting.py`、`lvs/gui/*`（store/jobs/app/configio/模板/静态）、`lvs/config.py`、`lvs/doctor.py`、`lvs/cli.py`。
**固定点**：`HEAD` = `263ac04`（一切未提交）。**方法**：两轴各派一个独立 sub-agent 读码，
主代理**逐条回原文件复核**（上一轮的教训：不做这一步就会把误报当结论）。

## 复核后成立、已修（5 条）

| # | 问题 | 证据 | 修法 |
|---|---|---|---|
| **R1** | 条目上限**两处定义且已漂**：`cards.MAX_ITEMS=4`、`graphic.MAX_ITEMS=5`；回退版式的 `[:MAX_ITEMS+2]`（=7）永远截不到 | 实测两值 | `graphic.MAX_ITEMS = cards.MAX_ITEMS`，去掉死钳位；加测试锁住"只有一处定义" |
| **R2** | **密钥遮蔽两套实现**，后缀表已经漂（`configio` 有 `api_key`，`app` 靠 `key` 子串兜） | 两处 grep 对比 | 判定+打码收进 `configio.is_secret_key()/mask()`，`app._config_rows` 改用 |
| **R3** | 进度**会假报 100%**：`store` 在 `total==0` 时拿 `done` 凑总数 → `5/5` | `store.py:164` | 去掉凑数；总数未知就报 0，界面显示 `—`（`task.js` + `task.html`） |
| **R4** | **静默降级**：浏览器截图失败时 `graphic.render` 无声回退 Pillow —— 两套版式观感不同，混在同一条片子里查不出原因 | `graphic.py:render` | 回退时喊一声（每进程一次，避免刷屏） |
| **R5** | **卡片 PNG 从不校验**（D23）：`cardhtml` 只看 `size > 0`，Pillow 路径写成功即返回 | `cardhtml.py:281` | 新增 `_assert_rendered()`：两条路出口统一解码 + 核对 1920×1080，坏产物抛错 |
| **R6** | `card_for` 文档说"**永不返回空卡**"，但 beat 与旁白都空时会返回 `Card(items=())` | 实测 | 不夸大：改文档写明这个边界（实际跑不到，每个分镜都有旁白） |

## 复核后**推翻**的误报（4 条）

| 误报 | 谁报的 | 为什么错 |
|---|---|---|
| `api_create_task` 里 `slugify()` 可能返回 `""`，于是写进 `.work` 根目录 | Standards | **错**。`slugify` 的兜底就是 `cleaned or "default"`，实测 `''`/`'...'`/`'!!!'` 全返回 `'default'`，永远不为空 |
| graphic 分支**不幂等**，每次 `lvs assets` 都会重渲每个 beat 的第一张卡 | Spec（它列为**最严重**那条） | **错**。它只读了 `resolve_shot` 的 graphic 分支，没看调用方：`run_command` 在进 `resolve_shot` **之前**先查 `existing_asset`。实测重跑 `lvs assets --only graphic` → **「新取 0，跳过 33」**，文件 mtime 未变 |
| 数据行里的单位（`.u`）不参与收缩，可能溢出 | Spec | **夸大**。自适脚本量的是 `.row` 的 `scrollWidth`，单位撑宽会触发数字继续缩，直到放得下；只有数字触到 22px 下限才会溢出 |
| `graphic.py` 文档说"降级为占位帧"与行为不符 | Spec | **基本不成立**。该镜被标 `failed`，而 `build` 对失败镜本就生成占位帧（D17）—— 端到端就是"降级为占位帧" |
| `card_for` 的机制是返回 `[""]` | Spec | 机制错（实测返回 `[]` → `items=()`），但**结论对**（确实可能空卡），已按 R6 处理 |

**两轴独立收敛到同一条**：R1（MAX_ITEMS 漂移）两轴都报了 —— 值得优先修的那种信号。

## 标准符合度复查（确认无违规）

- **D2**（GUI 是薄壳）✓：`gui/jobs.py` 只 spawn `python -m lvs`，不 import 阶段模块。
- **D13**（上游只读）✓：素材库页只扫描。
- **D24**（两种清洗方向相反）✓：`sanitize()` 与 `card_text()` 没被混用。
- **D27/D28/D29** ✓：`Card` 仍只有 `kind/items/source`；一条 beat 一张卡、按镜落盘。
- **D30** ✓：只绑 127.0.0.1、全局串行、进度数产物（R3 修完更准）。

## 结论

新代码**没有**"`Status: done` 但有验收项没做"的情况；两轴各报 6 条，复核后 **5 条成立并已修 + 补测试**，
**4 条推翻**。误报全部集中在"只读一个函数、不看调用方"这一类推理上 —— 这正是必须回原文件复核的原因。
测试 **337 → 345**。
