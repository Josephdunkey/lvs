# 38 · 第二轮审查（票 34–37）的问题修复

**Status:** done
**关联**：`review-two-axis-3.md`、票 34/35/36/37、D23、D27、D30

两轴独立 sub-agent + 主代理逐条回原文件复核。复核后 **5 条成立、4 条推翻**。
本票记录成立的那 5 条怎么修的（误报清单见审查文档）。

## 修了什么

| # | 修法 | 测试 |
|---|---|---|
| R1 条目上限两处定义且已漂（4 vs 5，回退版式 `[:7]` 永远截不到） | `graphic.MAX_ITEMS = cards.MAX_ITEMS`；删死钳位 | `test_max_items_has_one_owner` |
| R2 密钥遮蔽两套实现、后缀表已漂 | 判定与打码收进 `configio.is_secret_key()/mask()`，`app._config_rows` 改用 | `MaskSingleOwnerTest` |
| R3 进度假报 100%（`total==0` 时拿 `done` 凑） | `store` 不再凑数；界面显示 `—` | `ProgressUnknownTotalTest` |
| R4 浏览器失败时**静默**回退 Pillow（观感不同却无声） | 回退时喊一声（每进程一次） | 手工验证 |
| R5 卡片 PNG 从不校验（D23） | 新增 `_assert_rendered()`：两条路统一解码 + 核尺寸 | `ReviewFixesTest` 4 项 |
| R6 `card_for` 文档称"永不返回空卡"，实际可能空 | 改文档写明边界（夸大比 bug 更贵） | — |

## 复盘

四类误报全部同源：**只读被调函数、不看调用方**（graphic 幂等那条最典型 ——
`resolve_shot` 确实不查已存在产物，但 `run_command` 在调它之前就查了）。
所以"回原文件复核"不是礼节，是唯一能挡住误报的步骤。
