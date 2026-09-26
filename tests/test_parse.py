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

    def test_reengagement_block_excluded(self) -> None:
        exc = self.d["segments"][1]["excluded"]
        self.assertTrue(exc)
        joined = " ".join(e["text"] for e in exc)
        self.assertIn("这是个问题吗", joined)
        self.assertIn("答案在这里", joined)

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


if __name__ == "__main__":
    unittest.main()
