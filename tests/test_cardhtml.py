"""HTML 卡片渲染测试（票据 34）。

真正的栅格化依赖本机浏览器，没装就跳过；模板与转义是纯逻辑，必须跑。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lvs import cardhtml, cards

CARD = cards.Card(kind=cards.KIND_STATEMENT, items=("三十六个邑", "三万口人", "六十万斤黄金"), source="beat")
LIST = cards.Card(
    kind=cards.KIND_LIST,
    items=("第一笔：秦将军伐韩，取阳城、负黍，斩首四万。", "第二笔：伐赵，取二十馀县，斩首虏九万。"),
    source="narration",
)


class TemplateTest(unittest.TestCase):
    def test_every_layout_has_a_template(self) -> None:
        for kind in (cards.KIND_STATEMENT, cards.KIND_COMPARE, cards.KIND_TIMELINE, cards.KIND_LIST):
            self.assertIn(kind, cardhtml.LAYOUTS)

    def test_layout_vocabulary_matches_cards(self) -> None:
        """版式词表只有一处定义（cards）；模板不许自己长出一套。"""
        self.assertEqual(set(cardhtml.LAYOUTS), set(cards.LAYOUTS))

    def test_html_contains_the_items(self) -> None:
        """内容不能丢：数据行版式会拆成「数字 + 单位」，拆了也算在。"""
        page = cardhtml.html_for(CARD)
        for item in CARD.items:
            parts = cards.split_num(item)
            if parts:
                self.assertIn(parts[0], page)
                self.assertIn(parts[1], page)
            else:
                self.assertIn(item, page)

    def test_list_items_appear_verbatim(self) -> None:
        page = cardhtml.html_for(LIST)
        for item in LIST.items:
            self.assertIn(item, page)

    def test_html_is_a_full_document(self) -> None:
        page = cardhtml.html_for(CARD)
        self.assertIn("<!doctype html>", page.lower())
        self.assertIn("charset", page.lower())

    def test_no_footer_slot(self) -> None:
        """页脚旁白与烧录字幕重复 —— 任何一个版式都不该再输出这个位置。"""
        for kind in cards.LAYOUTS:
            card = cards.Card(kind=kind, items=("甲", "乙"), source="beat")
            page = cardhtml.html_for(card).lower()
            self.assertNotIn("footer", page, kind)
            self.assertNotIn("narration", page, kind)
            self.assertNotIn("segment_heading", page, kind)

    def test_items_are_escaped(self) -> None:
        evil = cards.Card(kind=cards.KIND_STATEMENT, items=("<script>alert(1)</script>",), source="beat")
        html = cardhtml.html_for(evil)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_unknown_kind_falls_back(self) -> None:
        weird = cards.Card(kind="nope", items=("甲",), source="beat")
        self.assertIn("甲", cardhtml.html_for(weird))


class RenderTest(unittest.TestCase):
    @unittest.skipUnless(cardhtml.available(), "本机没有可用的无头浏览器")
    def test_renders_a_1920x1080_png(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "card.png"
            cardhtml.render(CARD, out)
            self.assertTrue(out.is_file() and out.stat().st_size > 0)
            with Image.open(out) as im:
                self.assertEqual(im.size, (cardhtml.W, cardhtml.H))

    @unittest.skipUnless(cardhtml.available(), "本机没有可用的无头浏览器")
    def test_two_layouts_render_differently(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td) / "a.png", Path(td) / "b.png"
            cardhtml.render(CARD, a)
            cardhtml.render(LIST, b)
            self.assertNotEqual(a.read_bytes(), b.read_bytes())


class BrowserTest(unittest.TestCase):
    def test_browser_returns_str_or_none(self) -> None:
        self.assertIsInstance(cardhtml.browser(), (str, type(None)))

    def test_available_is_bool(self) -> None:
        self.assertIsInstance(cardhtml.available(), bool)


if __name__ == "__main__":
    unittest.main()
