# 49 · 新增 `lvs publish`：投稿物料（封面叠字 + 标题 + 简介 + 标签）

Status: done
Type: feature
Severity: low（入口增量，不接入 `lvs run`）

## 背景

成片完成后，投 B 站 / 抖音需要四样东西：封面（带文字）、标题候选、简介、标签/话题。
之前靠手写，失败模式已在 UGE03 实测中暴露（内部拍摄稿文件名当标题、半截书名号等，见下）。

## 交付

- `lvs/publish.py`（新）：封面叠字 + 标题候选 + 简介 + 标签；`publish` 注册为
  **driver**（`stage.py:DRIVERS`，不进 `STAGES`/`ORDER`，不参与 `lvs run`）
- `lvs/graphic.py`：对外薄封装 `font_path()/font()/wrap()/fit()`
- `lvs/cli.py`：`publish` 子命令（`--out/--base/--lines/--llm/--json`）
- `tests/test_publish.py`（20 例）、`tests/test_architecture.py` 断言同步

## 设计约束（不可丢）

1. 封面三行字**一律取拍摄稿 `【封面文案】`**（`parse.json` → `meta.cover`），
   工具**不编封面文字**（守“文字锚定”原则）
2. 只降级不阻断：缺成片 / 缺封面字 / 无底图（走靛蓝渐变）均返回码 0
3. 幂等，故**无 `--force`**（不留“定义了却无人接线”的开关）

## 本次一并修掉的物料质量问题（均有回归测试）

- 标签带出内部标题 `003-…-拍摄稿`
- `"《雨月物语》夜宿荒宅".strip("《》")` 只去左端 → 半截书名号
- 标题候选把**拍摄稿文件名**投到公众视频
- 抖音出现“只有 #标签”的空候选
- `--out` 拷出文件名恒为 `publish-*.md` → 改用 `copy_to(..., prefix=ws.task)`（现为 `UGE03-*.md`）

## 验证

- `pytest tests/test_publish.py tests/test_architecture.py -q` → **50 passed**
- 全量 `pytest` → 1155 passed
- UGE03 实物：`cover-bilibili.png` 1344x768、`cover-douyin.png` 1080x1920、`bilibili.md`/`douyin.md`/`meta.json`，封面人眼核验通过

## Comments

- 2026-10-04 · 由全项目审计顺手交付；详见 `docs/项目全面分析与改进方案-2026-10-04.md` 附录 A。
- 未接入 `lvs run`（刻意）：投稿物料是人工发布前的一步，不应阻断成片流水线。