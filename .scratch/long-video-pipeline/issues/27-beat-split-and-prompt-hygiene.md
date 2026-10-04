# 27: 画面位拆 beat + 分类 + 提示词卫生

**What to build:** 让"剪辑设计"与"画面描述"分开 —— 一行 `[画面位]` 拆成多个 beat，每个 beat 判定
`scene` / `graphic`；`scene` beat 的提示词要先剥掉引号里的字幕原文、箭头与剪辑动词。

**Blocked by:** 24

**Status:** done

**背景（根因）**：`shots` 把 `[画面位]` 整行当提示词喂给图像模型，而那一行是**剪辑设计**：

    三段原文并排浮现 → 高亮"斩首四万/斩首虏九万"vs"邑三十六/口三万" → 中间一道"科目转换器"

于是引号里的字被**画出来**（观感上是伪汉字/乱码），`科目转换器` 被读成"格式转换器"；
且整段几十镜共用这一条，654 镜只有 19 个不同 prompt。

- [x] `prompting.split_beats()` 按 `→` 拆 beat，读掉显式 `[场景]`/`[图表]` 标注
- [x] 箭头**二义性**处理：`时间轴：前403 → 前313 → …` 里的箭头是数据不是分隔符
- [x] `prompting.sanitize()` 剥引号原文 / 箭头 / 剪辑动词，只留可拍语言
- [x] `prompting.classify()` 显式标注优先、关键词规则兜底；`has_markers()` 报出判不准的
- [x] `parse.py` 把 beats 挂到 flow 的 visual 项上（additive，`visual_marks` 保持原样）
- [x] `shots` 段内句子**按序平均摊到 beat 上**（无 LLM 时的确定行为）
- [x] LLM 路径：一次调用同时返回 beat 归位 / kind / scene 翻译 / prompt / keywords / source
- [x] 无 LLM 时**响亮警告**："按序平均分配，不是语义切分"，并列出判不准的 beat 供人复核

---

## Comments

**What was built:** 新模块 `lvs/prompting.py`（纯函数）+ `parse.py` 挂 beats + `shots.py` 按 beat 分配与分类。

**交付记录**
1. `lvs/prompting.py`：`split_beats` / `sanitize` / `classify` / `has_markers` / `resolve_kind`
   —— 三条拆分规则：模板头（`X：`）整行不拆；多数分片是短数据则整行不拆；带标注的 beat 吸收其后数据片段
2. `parse.py`：`beats_of()` 把 beat 挂到 `flow[].beats`（`visual_marks` 原样保留，老字段不破）
3. `shots.py`：`_sentences_with_visual` 改成"先归最近画面位、再按序平均摊到 beat"，
   返回 `(旁白, 画面, kind)`；`_new_shot` 增 `kind`/`scene` 字段
4. `shots._llm_enrich` 按**段**分批（一段的 beat 表只传一次），
   `_apply_llm_item` 对越界 beat 下标/缺字段一律回退
5. `_heuristic_fill(shots, mode)`：graphic → `source=graphic` 且 prompt 留空；
   scene → `sanitize()` 后拼提示词，净化到空则退段标题再退旁白
6. `_warn_no_llm()`：列出"没有任何分类标记词"的 beat（默认按场景处理，需人工过一眼）
7. 测试：`tests/test_prompting.py`（17）+ `tests/test_parse.py::TestVisualBeats`（4）
   + `tests/test_shots.py::BeatAssignmentTest/HeuristicFillKindTest`（10）

**实测（真机 DeepSeek + 30 分钟合稿）**：15 行 `[画面位]` → 拆出 60 个 beat；
654 镜不同 prompt 数从 **19 → 见 ticket 30 的实测记录**；`scene` 提示词里**不再含任何引号内容**。
