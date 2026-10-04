"""卡片内容模型测试（票据 34/35）。

用 small3 实跑里的真实 beat 作样例 —— 这五条覆盖了全部 33 个图文镜。
"""

from __future__ import annotations

import unittest

from lvs import cards

# ---- 真实样例（来自 .work/small3/shots.json） ------------------------------

BEAT_NUMBERS = "三十六个邑，三万口人，六十万斤黄金"
BEAT_INSTRUCTION = "三段原文并排浮现"
BEAT_COMPARE = '高亮"斩首四万/斩首虏九万"vs"邑三十六/口三万"'
BEAT_ONE = '中间一道"科目转换器"'
BEAT_TIMELINE = "时间轴：前403 → 前313 → 前284 → 前256 → 名分价格曲线缓缓下滑"

# 该指令 beat 所辖 4 镜的旁白（首个是"拍摄提示"，不该上卡）
NARR = [
    "先把这一年的三笔记载，并排摆出来。",
    "第一笔：秦将军伐韩，取阳城、负黍，斩首四万。",
    "第二笔：伐赵，取二十馀县，斩首虏九万。",
    "第三笔，就是我们开头说的那件：赧王入秦，顿首受罪。",
]


class SplitItemsTest(unittest.TestCase):
    def test_quotes_win(self) -> None:
        self.assertEqual(cards.split_items(BEAT_COMPARE), ["斩首四万/斩首虏九万", "邑三十六/口三万"])

    def test_single_quote_is_one_item(self) -> None:
        self.assertEqual(cards.split_items(BEAT_ONE), ["科目转换器"])

    def test_comma_list_splits(self) -> None:
        self.assertEqual(cards.split_items(BEAT_NUMBERS), ["三十六个邑", "三万口人", "六十万斤黄金"])

    def test_arrow_chain_drops_trailing_caption(self) -> None:
        """`名分价格曲线缓缓下滑` 是图表走势描述，不是节点。"""
        self.assertEqual(cards.split_items(BEAT_TIMELINE), ["前403", "前313", "前284", "前256"])

    def test_empty(self) -> None:
        self.assertEqual(cards.split_items(""), [])

    def test_long_prose_is_not_comma_split(self) -> None:
        """一整句旁白不能被顿号切碎成假数据。"""
        line = "第一笔：秦将军伐韩，取阳城、负黍，斩首四万。"
        items = cards.split_items(line)
        self.assertEqual(len(items), 1)
        self.assertIn("斩首四万", items[0])


class IsInstructionTest(unittest.TestCase):
    def test_edit_verb_beats_are_instructions(self) -> None:
        self.assertTrue(cards.is_instruction(BEAT_INSTRUCTION))

    def test_quoted_data_is_not_instruction(self) -> None:
        self.assertFalse(cards.is_instruction(BEAT_COMPARE))
        self.assertFalse(cards.is_instruction(BEAT_ONE))

    def test_plain_data_is_not_instruction(self) -> None:
        self.assertFalse(cards.is_instruction(BEAT_NUMBERS))
        self.assertFalse(cards.is_instruction(BEAT_TIMELINE))

    def test_empty_is_instruction(self) -> None:
        self.assertTrue(cards.is_instruction(""))


class CardForTest(unittest.TestCase):
    def test_data_beat_uses_its_own_data(self) -> None:
        card = cards.card_for(BEAT_NUMBERS, NARR)
        self.assertEqual(card.source, "beat")
        self.assertEqual(card.items, ("三十六个邑", "三万口人", "六十万斤黄金"))

    def test_instruction_beat_falls_back_to_narration(self) -> None:
        """核心修复：卡上要出那三笔真实记载，而不是"三段原文"。"""
        card = cards.card_for(BEAT_INSTRUCTION, NARR)
        self.assertEqual(card.source, "narration")
        self.assertEqual(card.items, tuple(NARR[1:]))
        self.assertNotIn("三段原文", card.items)

    def test_narration_lines_with_edit_verbs_are_dropped(self) -> None:
        card = cards.card_for(BEAT_INSTRUCTION, NARR)
        for item in card.items:
            self.assertNotIn("并排", item)

    def test_instruction_without_usable_narration_keeps_something(self) -> None:
        """不能返回空卡。"""
        card = cards.card_for(BEAT_INSTRUCTION, [])
        self.assertTrue(card.items)

    def test_empty_beat_uses_narration(self) -> None:
        card = cards.card_for("", ["周天子最后一次结账。"])
        self.assertEqual(card.items, ("周天子最后一次结账。",))

    def test_items_are_capped(self) -> None:
        card = cards.card_for("", [f"第{i}笔：某事。" for i in range(1, 9)])
        self.assertLessEqual(len(card.items), cards.MAX_ITEMS)


class CardShapeTest(unittest.TestCase):
    def test_card_carries_no_title_or_footer(self) -> None:
        """用户明确要求：卡上不准出现「第一段」这类脚本提示词，也不要重复的旁白页脚。"""
        fields = set(cards.Card.__dataclass_fields__)
        self.assertEqual(fields, {"kind", "items", "source"})
        self.assertNotIn("title", fields)
        self.assertNotIn("narration", fields)
        self.assertNotIn("footer", fields)


class LayoutTest(unittest.TestCase):
    def test_timeline(self) -> None:
        items = cards.split_items(BEAT_TIMELINE)
        self.assertEqual(cards.layout_of(BEAT_TIMELINE, items), cards.KIND_TIMELINE)

    def test_compare_needs_exactly_two(self) -> None:
        items = cards.split_items(BEAT_COMPARE)
        self.assertEqual(cards.layout_of(BEAT_COMPARE, items), cards.KIND_COMPARE)

    def test_short_data_row_is_statement(self) -> None:
        items = cards.split_items(BEAT_NUMBERS)
        self.assertEqual(cards.layout_of(BEAT_NUMBERS, items), cards.KIND_STATEMENT)

    def test_single_item_is_statement(self) -> None:
        self.assertEqual(cards.layout_of(BEAT_ONE, ["科目转换器"]), cards.KIND_STATEMENT)

    def test_long_items_become_list(self) -> None:
        self.assertEqual(cards.layout_of("", list(NARR[1:])), cards.KIND_LIST)

    def test_every_layout_has_a_template(self) -> None:
        for kind in (cards.KIND_STATEMENT, cards.KIND_COMPARE, cards.KIND_TIMELINE, cards.KIND_LIST):
            self.assertIn(kind, cards.LAYOUTS)


class SplitNumTest(unittest.TestCase):
    def test_splits_value_and_unit(self) -> None:
        self.assertEqual(cards.split_num("三十六个邑"), ("三十六", "个邑"))
        self.assertEqual(cards.split_num("六十万斤黄金"), ("六十万", "斤黄金"))

    def test_plain_text_is_not_a_value(self) -> None:
        self.assertIsNone(cards.split_num("科目转换器"))
        self.assertIsNone(cards.split_num("斩首四万/斩首虏九万"))


class BeatGroupsTest(unittest.TestCase):
    def test_groups_shots_by_beat_text(self) -> None:
        shots = [
            {"id": 1, "kind": "graphic", "visual": BEAT_TIMELINE},
            {"id": 2, "kind": "graphic", "visual": BEAT_TIMELINE},
            {"id": 3, "kind": "graphic", "visual": BEAT_ONE},
            {"id": 4, "kind": "scene", "visual": "城墙"},
        ]
        groups = cards.beat_groups(shots)
        self.assertEqual(sorted(groups), sorted([BEAT_TIMELINE, BEAT_ONE]))
        self.assertEqual([s["id"] for s in groups[BEAT_TIMELINE]], [1, 2])
        self.assertEqual([s["id"] for s in groups[BEAT_ONE]], [3])


if __name__ == "__main__":
    unittest.main()
