"""拍摄稿解析器回归测试（票据 04 + 05）。

自包含样本，不依赖 D: 盘素材；`python -m unittest discover -s tests -v`
"""

from __future__ import annotations

import unittest

from lvs.parse import parse_range, parse_script, parse_timecode

SAMPLE = """# T001 拍摄稿 · 测试样本

> 源卡：x｜风格：y

---

## 一、传达层（先定三件套）

**【标题】**（12–22 字，设问式）
> 测试标题一行

备选：`备选标题`

**【封面文案】**（大字 ≤10 字）
> 大字：**「测试」**
> 画面建议：一卷竹简。

**【冷开场】**（0:00–0:30，直接从最戏剧的一刻切入）
> 第一句。
> （转场）这是旁白，不是提示。

---

## 二、正文讲稿

### 【第一段 · 开场】0:30–1:00

这里是第一段旁白。
**加粗的旁白。**

[画面位] 一段画面描述 → 高亮

### 【第二段 · 中段】1:00–2:00

第二段旁白。
`引用的原文。`

[重参与点①｜1:30]
这是个问题吗？
（停顿）答案在这里。

[冷断句]
这是候选收尾句。

### 【第三段 · 收尾】2:00–结尾

第三段旁白。
第一个画面：户籍册被登记。

---

## 三、画面位清单

| 时间 | 画面 | 素材建议 |
|---|---|---|
| 0:00 | 竹简 | 字幕动画 |
| 1:00 | 曲线 | 动画 |
| 结尾 | 预告 | 复用 |

## 四、留存曲线自检（对照 MrBeast）

| 时间段 | 目标留存 |
|---|---|
| 0–1min | >90% |

## 五、史实核验与红线

- **锚点**：xxx
- 这是不该进旁白的章
"""


class TestTimecode(unittest.TestCase):
    def test_mm_ss(self) -> None:
        self.assertEqual(parse_timecode("0:30"), 30.0)
        self.assertEqual(parse_timecode("16:00"), 960.0)

    def test_hh_mm_ss(self) -> None:
        self.assertEqual(parse_timecode("1:02:03"), 3723.0)

    def test_end_words_and_garbage(self) -> None:
        self.assertIsNone(parse_timecode("结尾"))
        self.assertIsNone(parse_timecode(""))
        self.assertIsNone(parse_timecode("abc"))

    def test_range_with_en_dash_and_hyphen(self) -> None:
        self.assertEqual(parse_range("0:30–1:00")[:2], (30.0, 60.0))
        self.assertEqual(parse_range("1:00-2:00")[:2], (60.0, 120.0))
        self.assertEqual(parse_range("16:00–结尾")[:2], (960.0, None))


class TestParseScript(unittest.TestCase):
    def setUp(self) -> None:
        self.d = parse_script(SAMPLE)

    def test_title_and_meta(self) -> None:
        self.assertEqual(self.d["title"], "T001 拍摄稿 · 测试样本")
        self.assertEqual(self.d["meta"]["title_card"], "测试标题一行")
        self.assertIn("大字：「测试」", self.d["meta"]["cover"])

    def test_cold_open_is_narration(self) -> None:
        co = self.d["cold_open"]
        self.assertIsNotNone(co)
        self.assertEqual((co["start"], co["end"]), (0.0, 30.0))
        self.assertIn("第一句。", co["text"])
        # 行内提示只剥离标记，保留文字
        self.assertIn("这是旁白，不是提示。", co["text"])
        self.assertNotIn("（转场）", " ".join(co["text"]))

    def test_segments_and_timecodes(self) -> None:
        segs = self.d["segments"]
        self.assertEqual(len(segs), 3)
        self.assertEqual((segs[0]["start"], segs[0]["end"]), (30.0, 60.0))
        self.assertEqual((segs[1]["start"], segs[1]["end"]), (60.0, 120.0))
        self.assertEqual((segs[2]["start"], segs[2]["end"]), (120.0, None))

    def test_narration_cleaned_and_ordered(self) -> None:
        self.assertEqual(self.d["segments"][0]["narration"], ["这里是第一段旁白。", "加粗的旁白。"])
        # 引用原文的内层文字要保留、反引号要去掉
        self.assertIn("引用的原文。", self.d["segments"][1]["narration"])

    def test_visual_marks(self) -> None:
        self.assertEqual(self.d["segments"][0]["visual_marks"], ["一段画面描述 → 高亮"])

    def test_stage_markers_stripped_from_narration(self) -> None:
        """〔原文〕〔注〕〔附〕及半角形是讲稿脚手架，不进旁白 —— 不念、不上字幕。

        单一剥离点在 parse（用户拍板 2026-10-04）：配音/字幕/时间轴都只读 narration，
        源头剥掉即天然同步。
        """
        text = """# T900 拍摄稿 · 标记剥离样本

## 一、传达层

**【标题】**（12–22 字，设问式）
> 标题一行

---

## 二、正文讲稿

### 【第一段 · 开场】0:30–1:00

〔原文〕人不能行千里，而魂日行千里。

他笑道，〔原文〕死生有命。

[原文] 半角形也不念。

〔注〕出处说明也要剥。

结尾句。
"""
        d = parse_script(text)
        narr = [n for seg in d["segments"] for n in seg["narration"]]
        joined = "|".join(narr)
        for marker in ("〔原文〕", "[原文]", "〔注〕"):
            self.assertNotIn(marker, joined)
        # 标记剥掉、正文一字不少
        self.assertIn("人不能行千里，而魂日行千里。", joined)
        self.assertIn("他笑道，死生有命。", joined)
        self.assertIn("半角形也不念。", joined)
        self.assertIn("出处说明也要剥。", joined)
        self.assertIn("结尾句。", joined)

    def test_reengagement_block_excluded(self) -> None:
        exc = self.d["segments"][1]["excluded"]
        self.assertTrue(exc)
        joined = " ".join(e["text"] for e in exc)
        self.assertIn("这是个问题吗", joined)
        self.assertIn("答案在这里", joined)

    def test_quote_block_body_excluded(self) -> None:
        """`> 〔引原文〕` 是**块语义**：标记行 + 其后正文（无前缀）整块 excluded。

        回归 001/002 实锤 bug（2026-10-04）：块正文按单行处理会落进 narration，
        与行内〔原文〕副本各念一遍 —— 两期共 36 处重复念。
        """
        text = """# T910 拍摄稿 · 引文块样本

## 一、传达层

**【标题】**（12–22 字，设问式）
> 标题一行

---

## 二、正文讲稿

### 【第一段 · 开场】0:30–1:00

他笑道，〔原文〕死生有命，何惧之有。

[画面位] 樟子门拉开一道缝

> 〔引原文〕
死生有命，何惧之有。

旁白继续。

### 【第二段 · 中段】1:00–2:00

第二段旁白。
> 〔引原文〕
此句之后紧跟段标题也整块排除。
### 【第三段 · 收尾】2:00–结尾

第三段旁白。
"""
        d = parse_script(text)
        narr = "|".join(n for seg in d["segments"] for n in seg["narration"])
        exc = "|".join(e["text"] for seg in d["segments"] for e in seg["excluded"])
        # 行内〔原文〕副本照常念（剥标记）
        self.assertIn("他笑道，死生有命，何惧之有。", narr)
        # 块正文绝不能进旁白（只允许行内那一份）
        self.assertEqual(narr.count("死生有命，何惧之有。"), 1, narr)
        self.assertIn("此句之后紧跟段标题也整块排除。", exc)
        # 块正文进 excluded
        self.assertIn("死生有命，何惧之有。", exc)
        # 段标题后的旁白不受影响
        self.assertIn("第三段旁白。", narr)
        self.assertIn("旁白继续。", narr)

    def test_uncertain_holds_ambiguous_blocks(self) -> None:
        kinds = {(u["kind"], u.get("marker", "")) for u in self.d["uncertain"]}
        self.assertIn(("cold_close_lines", "冷断句"), kinds)
        all_lines = " ".join(l for u in self.d["uncertain"] for l in u["lines"])
        self.assertIn("这是候选收尾句。", all_lines)
        self.assertIn("第一个画面：", all_lines)

    def test_excluded_zero_leak(self) -> None:
        narration: list[str] = []
        if self.d["cold_open"]:
            narration += self.d["cold_open"]["text"]
        for s in self.d["segments"]:
            narration += s["narration"]
        joined = "\n".join(narration)
        for marker in ("[画面位]", "[重参与点", "（转场）", "（停顿）", "[冷断句]", "第一个画面："):
            self.assertNotIn(marker, joined, f"excluded 泄漏：{marker}")
        # §四/§五 整章不得进旁白
        self.assertNotIn(">90%", joined)
        self.assertNotIn("这是不该进旁白的章", joined)

    def test_visual_mark_table(self) -> None:
        rows = self.d["visual_mark_table"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["time_seconds"], 0.0)
        self.assertEqual(rows[1]["suggestion"], "动画")
        self.assertIsNone(rows[2]["time_seconds"])  # 「结尾」→ None


class TestGracefulDegradation(unittest.TestCase):
    def test_document_without_expected_sections(self) -> None:
        d = parse_script("# 只有标题\n\n随便一段文字。\n又一段。")
        self.assertEqual(d["title"], "只有标题")
        self.assertIsNone(d["cold_open"])
        self.assertEqual(d["visual_mark_table"], [])
        self.assertTrue(d["notes"], "降级必须有 notes 说明原因")
        # 至少不抛异常，且旁白被收集
        narr = [ln for s in d["segments"] for ln in s["narration"]]
        self.assertIn("随便一段文字。", narr)


class TestVisualBeats(unittest.TestCase):
    """票据 27：`[画面位]` 行按 `→` 拆成 beat，随 flow 的 visual 项落库。"""

    def _visual_items(self, d):
        return [
            item
            for seg in d["segments"]
            for item in (seg.get("flow") or [])
            if item.get("kind") == "visual"
        ]

    def test_beats_recorded_on_flow_visual_items(self) -> None:
        items = self._visual_items(parse_script(SAMPLE))
        self.assertEqual(len(items), 1)
        self.assertEqual([b["text"] for b in items[0]["beats"]], ["一段画面描述", "高亮"])
        self.assertEqual([b["kind"] for b in items[0]["beats"]], [None, None])

    def test_explicit_tags_recorded(self) -> None:
        text = SAMPLE.replace(
            "[画面位] 一段画面描述 → 高亮",
            "[画面位] [场景] 玉玺被推回桌面 → [图表] 分栏呈现",
        )
        item = self._visual_items(parse_script(text))[0]
        self.assertEqual([b["kind"] for b in item["beats"]], ["scene", "graphic"])
        self.assertEqual(item["beats"][0]["text"], "玉玺被推回桌面")

    def test_timeline_line_stays_one_beat(self) -> None:
        text = SAMPLE.replace(
            "[画面位] 一段画面描述 → 高亮",
            "[画面位] 时间轴：前403 → 前313 → 前284 → 名分价格曲线缓缓下滑",
        )
        item = self._visual_items(parse_script(text))[0]
        self.assertEqual(len(item["beats"]), 1)

    def test_visual_marks_kept_verbatim(self) -> None:
        # 旧字段保持原样：下游与人工排查仍能拿到原始整行
        self.assertIn("一段画面描述 → 高亮", parse_script(SAMPLE)["segments"][0]["visual_marks"])


if __name__ == "__main__":
    unittest.main()


class TestColdOpenVisualMarks(unittest.TestCase):
    """冷开场块里的 `[画面位]` 必须被收进 `cold_open["visual_marks"]`。

    背景（2026-10-03 实测）：片头 0–30 秒的旁白**只住在** `一、传达层/【冷开场】` 里，
    `shots.build_skeleton` 先把它铺成片头的镜，再遍历正文段。收不到画面位时，
    这些镜的提示词只能退化成 `meta.title_card` —— 也就是把**标题文案**当画面画出来
    （真机上 10 镜的提示词全变成「主用（金句钩子）：他念了一整夜的经…」）。

    第二个坑：裸 `[画面位]` 行原先会触发 `flush()`，把冷开场**拦腰截断** ——
    前半段进 cold_open、后半段掉进 uncertain，且不报任何错。
    """

    SCRIPT = """# T · 冷开场画面位

## 一、传达层

**【标题】**
> 标题一行

**【冷开场】**（0:00–0:30）
> 第一句。第二句。
> 第三句。第四句。

[画面位] [场景] 山头一个低矮土墩 → [场景] 石头近景
> [画面位] [场景] 一排宫檐剪影
[画面位] [场景] 灯笼照亮石阶

**【封面文案】**
> 行1：甲

## 二、正文讲稿

### 【① 报幕】0:00–0:30

旁白一句。

[画面位] [场景] 书册与月光

## 三、画面位清单

| 时间 | 画面 | 素材建议 |
|---|---|---|
| 0:00 | 书册与月光 | 生图 |
"""

    def setUp(self) -> None:
        self.d = parse_script(self.SCRIPT)

    def test_marks_captured_and_block_not_truncated(self) -> None:
        co = self.d["cold_open"]
        self.assertIsNotNone(co)
        # 三条画面位（裸行 + `> ` 前缀行）一条不落
        self.assertEqual(len(co["visual_marks"]), 3, co["visual_marks"])
        self.assertIn("石头近景", co["visual_marks"][0])
        self.assertIn("宫檐剪影", co["visual_marks"][1])
        self.assertIn("灯笼照亮石阶", co["visual_marks"][2])

    def test_narration_survives_bare_mark_line(self) -> None:
        """裸画面位行**不许**当块边界：四句旁白要全在。"""
        co = self.d["cold_open"]
        self.assertEqual(len(co["text"]), 2, co["text"])
        joined = "".join(co["text"])
        for frag in ("第一句", "第二句", "第三句", "第四句"):
            self.assertIn(frag, joined)

    def test_marks_are_cleaned(self) -> None:
        """清洗只在 parse 一处 —— 带前缀的原文不能流到提示词里（`[画面位] [场景]` 会被画出来）。"""
        for mark in self.d["cold_open"]["visual_marks"]:
            self.assertNotIn("[画面位]", mark)
            self.assertNotIn("[场景]", mark)
            self.assertNotIn("[图表]", mark)

    def test_marks_not_leaked_into_narration(self) -> None:
        joined = "".join(self.d["cold_open"]["text"])
        self.assertNotIn("画面位", joined)
        self.assertNotIn("土墩", joined)

    def test_following_meta_block_still_parsed(self) -> None:
        """冷开场之后的【封面文案】不能因为多了画面位行就丢。"""
        self.assertIn("行1：甲", self.d["meta"]["cover"])
