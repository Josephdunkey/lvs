"""画面位 beat 拆分 / 提示词卫生 / beat 分类（票据 27）。

纯函数，不依赖网络与 LLM。样本取自真实合稿里的 `[画面位]` 行。
"""

from __future__ import annotations

import unittest

from lvs import prompting


class SplitBeatsTest(unittest.TestCase):
    def test_splits_on_arrow(self):
        raw = '三段原文并排浮现 → 高亮"斩首四万/斩首虏九万"vs"邑三十六/口三万" → 中间一道"科目转换器"'
        beats = prompting.split_beats(raw)
        self.assertEqual(len(beats), 3)
        self.assertIn("三段原文并排浮现", beats[0].text)
        self.assertIn("科目转换器", beats[2].text)

    def test_no_arrow_is_single_beat(self):
        beats = prompting.split_beats("大军漫山遍野长镜")
        self.assertEqual([b.text for b in beats], ["大军漫山遍野长镜"])
        self.assertIsNone(beats[0].kind)

    def test_timeline_is_not_split(self):
        # 「时间轴：前403 → 前313 → …」里的箭头是**数据**，不是 beat 分隔符
        raw = "时间轴：前403 → 前313 → 前284 → 前256 → 名分价格曲线缓缓下滑"
        beats = prompting.split_beats(raw)
        self.assertEqual(len(beats), 1, [b.text for b in beats])

    def test_merge_not_split_short_beats(self):
        # 全是短场景词时不能因为“短”就被吞掉
        raw = "大军漫山遍野长镜 → 猛兽特写 → 镜头拉到昆阳小城城墙"
        self.assertEqual(len(prompting.split_beats(raw)), 3)

    def test_explicit_tag_sets_kind_and_strips(self):
        raw = "[场景] 大军漫山遍野长镜 → [图表] 时间轴：前403 → 前313"
        beats = prompting.split_beats(raw)
        self.assertEqual(beats[0].kind, "scene")
        self.assertEqual(beats[0].text, "大军漫山遍野长镜")
        self.assertEqual(beats[1].kind, "graphic")

    def test_cjk_bracket_tag(self):
        beats = prompting.split_beats("【图表】分栏呈现三种反抗形态")
        self.assertEqual(beats[0].kind, "graphic")
        self.assertNotIn("【图表】", beats[0].text)

    def test_empty(self):
        self.assertEqual(prompting.split_beats(""), [])
        self.assertEqual(prompting.split_beats("   "), [])


class SanitizeTest(unittest.TestCase):
    def test_quoted_text_is_removed(self):
        got = prompting.sanitize('高亮"斩首四万/斩首虏九万"')
        self.assertNotIn("斩首四万", got)
        self.assertNotIn('"', got)

    def test_edit_verbs_removed(self):
        got = prompting.sanitize("户籍册被清点 → 玉玺被推回桌面（核心画面）")
        for word in ("→", "核心画面"):
            self.assertNotIn(word, got)
        self.assertIn("玉玺被推回桌面", got)

    def test_keeps_shot_size_language(self):
        got = prompting.sanitize("大军漫山遍野长镜 → 猛兽特写")
        self.assertIn("长镜", got)
        self.assertIn("特写", got)

    def test_stray_brackets_cleaned(self):
        got = prompting.sanitize('长平（远景，"46日"字幕）')
        self.assertIn("长平", got)
        self.assertIn("远景", got)
        self.assertNotIn("46日", got)
        self.assertNotIn("字幕", got)
        self.assertNotIn("，）", got)

    def test_pure_editorial_collapses_to_empty(self):
        self.assertEqual(prompting.sanitize('"45万"砸屏'), "")

    def test_sanitize_result_has_no_quotes_ever(self):
        for raw in (
            '三段原文并排浮现 → 高亮"斩首四万"vs"邑三十六"',
            '苏代"所得民无几何人"砸屏',
            '民谣三句逐行砸屏',
            "王莽像 → 竹简改令飞出",
        ):
            for beat in prompting.split_beats(raw):
                cleaned = prompting.sanitize(beat.text)
                for ch in '"“”「」『』':
                    self.assertNotIn(ch, cleaned)


class CardTextTest(unittest.TestCase):
    """图文卡片的正文：与 `sanitize()` **相反** —— 引号里的数据要留下，只去掉剪辑动词。"""

    def test_quotes_are_unwrapped_not_dropped(self):
        # sanitize 会把「斩首四万」整段删掉（对提示词正确），但卡片上这**正是要显示的数据**
        self.assertEqual(prompting.card_text('高亮"斩首四万"砸屏'), "斩首四万")

    def test_edit_verbs_removed(self):
        self.assertEqual(prompting.card_text("三段原文并排浮现"), "三段原文")

    def test_keeps_label_and_data_with_arrow(self):
        self.assertEqual(prompting.card_text("时间轴：前403 → 前313"), "时间轴：前403 前313")

    def test_real_editorial_line(self):
        got = prompting.card_text('高亮"斩首四万/斩首虏九万"vs"邑三十六/口三万"')
        self.assertEqual(got, "斩首四万/斩首虏九万 vs 邑三十六/口三万")

    def test_never_leaves_edit_verbs(self):
        for raw in (
            '三段原文并排浮现', '高亮"斩首四万"', '"45万"砸屏',
            "七种反抗形态分栏呈现", "民谣三句逐行砸屏",
        ):
            got = prompting.card_text(raw)
            for verb in ("浮现", "并排", "高亮", "砸屏", "分栏", "逐行", "呈现"):
                self.assertNotIn(verb, got, raw)

    def test_empty(self):
        self.assertEqual(prompting.card_text(""), "")


class ClassifyTest(unittest.TestCase):
    def test_explicit_wins(self):
        self.assertEqual(prompting.classify("大军长镜", explicit="graphic"), "graphic")
        self.assertEqual(prompting.classify("时间轴：前403", explicit="scene"), "scene")

    def test_graphic_by_rules(self):
        self.assertEqual(prompting.classify("时间轴：前403 → 前313"), "graphic")
        self.assertEqual(prompting.classify("七种反抗形态分栏呈现"), "graphic")
        self.assertEqual(prompting.classify('民谣三句逐行砸屏'), "graphic")

    def test_scene_by_rules(self):
        self.assertEqual(prompting.classify("大军漫山遍野长镜"), "scene")
        self.assertEqual(prompting.classify("未央宫火光"), "scene")

    def test_has_markers(self):
        self.assertTrue(prompting.has_markers("分栏呈现"))
        self.assertFalse(prompting.has_markers("你看着远方"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
