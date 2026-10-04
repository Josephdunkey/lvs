# 44 · 画面风格可配置（日系青年黑白漫画风）

**Status:** done
**关联**：`lvs/shots.py`、D19（提示词清洗）、票 27、票 43

原来"整片统一画面风格"是 `lvs/shots.py` 里写死的两个常量：

```python
STYLE_SUFFIX = "cinematic historical documentary still, ancient China, …"
STYLE_NAME   = "historical-documentary"
```

要换风格只能改代码。本票把它变成**具名预设**，可经配置或命令行切换。

## 改了什么

`lvs/shots.py`：

- 新增 `Style(name, suffix)`（frozen dataclass）与 `STYLES` 预设表：
  - `historical-documentary`（默认，历史纪录片感，= 原来那串）
  - `jp-youth-manga-bw`（日系青年黑白漫画风：黑白墨线、网点、强对比）
- `style_for(name)`：取预设；**未知名字抛 `KeyError`**，不静默退回默认
  （整片风格悄悄跑偏，要等全出完图才发现）。
- `STYLE_SUFFIX` / `STYLE_NAME` 保留为**默认预设的别名**（老代码/老测试引用它们）。
- 把 `style` 作为**可选参数**贯穿：`heuristic_prompt` / `_heuristic_fill` /
  `_apply_llm_item` / `_llm_enrich` / `build_skeleton`（`style_name`）/ `_new_shot`。
  省略 = 默认风格，全部调用点向后兼容。
- LLM 提示词规则里的风格后缀：`_RULES` 保持按默认风格拼；新增 `_rules(style)`
  在换风格时做**后缀替换**（而非重排模板），少一处"模板与代码漂移"。
- `run_command` 解析风格：`--style` > `config [shots].style`；未知名列出可选项并返回 2。

`lvs/cli.py`：`shots` 与 `run` 增加 `--style NAME`（选项名单来自 `shots.STYLES`，单一来源）。
`lvs/run.py`：把 `--style` 透传给 shots 阶段。
`config.toml` / `config.example.toml`：`[shots] style = "historical-documentary"`。
`README.md`：常用开关表补 `--style`。

## 验证

- `tests/test_shots.py::StyleTest`（6 项）：默认即历史纪录片感、具名预设改后缀、
  未知名抛错、`heuristic_prompt`/`_heuristic_fill`/`_apply_llm_item` 三处都吃传进来的风格、
  `build_skeleton` 把风格名写进 `shot["style"]`。
- `config` 解析实测：`shots.style = historical-documentary`（两个 config 文件的 `[shots]` 都有该键）。
- 全量 `pytest`：**461 → 467 passed**。

## 用法

```powershell
lvs run 拍摄稿.md --style jp-youth-manga-bw --visual photo --source local
```

`--visual photo` 让**所有 beat**（含图表）都翻译成可拍场景 → 全部走生图；
`--source local` 让所有实拍镜都用本地 ComfyUI 出图（不联网下载）。

## 复盘：黑白约束要"前置 + 重复"（票 45 实测）

K005 首轮 270 张里 **2 张出成了彩色**（shot-106 饱和度 0.32、shot-198 0.44）——
根因是 LLM 写的场景文本里带了颜色词（`warm dim interior`、`gold ingots`），
把后缀里的 `black and white` 压过去了。**`zimage_turbo.json` 用 `ConditioningZeroOut`
且 `cfg=1` → 负向提示词无效**（D19），所以只能靠正向提示词压回去。

改法（一行）：把 `jp-youth-manga-bw` 的后缀改成**前置 + 收尾**双重声明：

```
strictly monochrome black and white, Japanese youth manga illustration,
black and white ink line art, screentone shading, bold expressive strokes,
dramatic chiaroscuro, dynamic panel composition, grayscale, 16:9
```

同样两张重出后饱和度 **0.005 / 0.031** —— 成立。
