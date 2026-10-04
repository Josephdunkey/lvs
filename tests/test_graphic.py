"""graphic 渲染入口测试（票据 28/34）。

只测"入口 + 回退"这一层：内容逻辑在 test_cards，HTML 模板在 test_cardhtml。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from lvs import cardhtml, cards, graphic


def _card(kind: str, items: tuple[str, ...]) -> cards.Card:
    return cards.Card(kind=kind, items=items, source="beat")


SAMPLE = {
    cards.KIND_STATEMENT: _card(cards.KIND_STATEMENT, ("三十六个邑", "三万口人")),
    cards.KIND_COMPARE: _card(cards.KIND_COMPARE, ("斩首四万/斩首虏九万", "邑三十六/口三万")),
    cards.KIND_TIMELINE: _card(cards.KIND_TIMELINE, ("前403", "前313", "前284", "前256")),
    cards.KIND_LIST: _card(cards.KIND_LIST, ("第一笔：斩首四万。", "第二笔：斩首虏九万。")),
}


@unittest.skipUnless(graphic.available(), "需要浏览器或 Pillow + 中文字体")
class RenderTest(unittest.TestCase):
    def test_every_layout_writes_1920x1080(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as td:
            for kind, card in SAMPLE.items():
                out = Path(td) / f"{kind}.png"
                graphic.render(card, out)
                self.assertTrue(out.is_file(), kind)
                with Image.open(out) as im:
                    self.assertEqual(im.size, (graphic.W, graphic.H), kind)
                self.assertGreater(out.stat().st_size, 5000, kind)

    def test_empty_card_does_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "empty.png"
            graphic.render(cards.Card(kind=cards.KIND_STATEMENT, items=(), source="beat"), out)
            self.assertTrue(out.is_file())

    def test_accepts_relative_path(self) -> None:
        """浏览器解析 `--screenshot=` 用自己的 CWD，相对路径必须被解析成绝对路径。"""
        with tempfile.TemporaryDirectory() as td:
            cwd = Path.cwd()
            try:
                import os

                os.chdir(td)
                graphic.render(SAMPLE[cards.KIND_STATEMENT], Path("rel.png"))
                self.assertTrue((Path(td) / "rel.png").is_file())
            finally:
                os.chdir(cwd)


class PillowFallbackTest(unittest.TestCase):
    @unittest.skipUnless(graphic._pillow_available(), "需要 Pillow + 中文字体")
    def test_falls_back_when_no_browser(self) -> None:
        """没有浏览器也必须出片 —— 这条保证纯 Python 环境不会整链失败。"""
        from PIL import Image

        real = cardhtml.available
        cardhtml.available = lambda: False       # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as td:
                out = Path(td) / "fallback.png"
                graphic.render(SAMPLE[cards.KIND_TIMELINE], out)
                self.assertTrue(out.is_file())
                with Image.open(out) as im:
                    self.assertEqual(im.size, (graphic.W, graphic.H))
        finally:
            cardhtml.available = real            # type: ignore[assignment]

    @unittest.skipUnless(graphic._pillow_available(), "需要 Pillow + 中文字体")
    def test_available_is_true_without_browser(self) -> None:
        real = cardhtml.available
        cardhtml.available = lambda: False       # type: ignore[assignment]
        try:
            self.assertTrue(graphic.available())
        finally:
            cardhtml.available = real            # type: ignore[assignment]

    def test_graphic_error_covers_both_paths(self) -> None:
        """调用方只 need 一个异常类型。"""
        self.assertIs(graphic.GraphicError, cardhtml.CardError)


class EllipsizeTest(unittest.TestCase):
    def test_short_text_untouched(self) -> None:
        self.assertEqual(graphic.ellipsize("短", lambda t: len(t) * 10.0, 100), "短")

    def test_long_text_fits_with_ellipsis(self) -> None:
        got = graphic.ellipsize("一二三四五六七八九十", lambda t: len(t) * 10.0, 50)
        self.assertLessEqual(len(got) * 10, 50)
        self.assertTrue(got.endswith("…"))

    def test_no_room_returns_empty(self) -> None:
        self.assertEqual(graphic.ellipsize("abc", lambda t: 5.0, 3), "")


class HtmlFallbackTest(unittest.TestCase):
    """HTML 渲染出**坏产物**时要和"截不出来"一样退回 Pillow（票 42）。"""

    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.out = Path(td) / "card.png"

    def _fake_cardhtml(self, behaviour) -> None:  # noqa: ANN001
        from lvs import cardhtml

        real_avail, real_render = cardhtml.available, cardhtml.render
        cardhtml.available = lambda: True            # type: ignore[assignment]
        cardhtml.render = behaviour                  # type: ignore[assignment]
        self.addCleanup(setattr, cardhtml, "available", real_avail)
        self.addCleanup(setattr, cardhtml, "render", real_render)

    @unittest.skipUnless(graphic._pillow_available(), "Pillow 不在就没法验回退")
    def test_unreadable_html_output_falls_back(self) -> None:
        """截图命令 rc=0 但文件是垃圾 —— 这条以前会直接把整镜判失败。

        `_assert_rendered` 原先在 try 之外，于是它一抛，Pillow 回退根本没机会跑。
        """
        def broken(card, out):  # noqa: ANN001
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"not a png at all")
            return Path(out)

        self._fake_cardhtml(broken)
        got = graphic.render(SAMPLE[cards.KIND_STATEMENT], self.out)
        self.assertEqual(got, self.out)
        graphic._assert_rendered(got)                # 回退产物必须是张能用的图

    @unittest.skipUnless(graphic._pillow_available(), "Pillow 不在就没法验回退")
    def test_missing_output_falls_back(self) -> None:
        """截图没写文件也算失败，同样要退回去。"""
        def silent(card, out):  # noqa: ANN001
            return Path(out)

        self._fake_cardhtml(silent)
        got = graphic.render(SAMPLE[cards.KIND_STATEMENT], self.out)
        graphic._assert_rendered(got)

    @unittest.skipUnless(graphic._pillow_available(), "Pillow 不在就没法验回退")
    def test_raising_browser_still_falls_back(self) -> None:
        """原来的路径不能坏（回归保护）。"""
        def boom(card, out):  # noqa: ANN001
            raise graphic.GraphicError("stub：浏览器挂了")

        self._fake_cardhtml(boom)
        graphic.render(SAMPLE[cards.KIND_STATEMENT], self.out)
        graphic._assert_rendered(self.out)


class WrapTest(unittest.TestCase):
    """`_wrap` 的断行规则（Pillow 回退版式用）—— 锁住，免得被"看起来反了"误改。"""

    @unittest.skipUnless(graphic._pillow_available(), "需要 Pillow")
    def test_latin_breaks_at_spaces(self) -> None:
        from PIL import Image, ImageDraw

        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        lines = graphic._wrap(draw, "Alpha beta gamma delta", graphic._font(48), 420)
        self.assertEqual(lines, ["Alpha beta", "gamma delta"], "正常英文该在空格处断")

    @unittest.skipUnless(graphic._pillow_available(), "需要 Pillow")
    def test_overlong_word_is_split(self) -> None:
        """单个词比整行还长时只能切开 —— 这正是那个 12 字阈值存在的理由。"""
        from PIL import Image, ImageDraw

        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        lines = graphic._wrap(draw, "averyveryverylongordindeed yes", graphic._font(48), 500)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(len(line) <= 22 for line in lines), lines)

    @unittest.skipUnless(graphic._pillow_available(), "需要 Pillow")
    def test_cjk_breaks_per_char(self) -> None:
        from PIL import Image, ImageDraw

        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        lines = graphic._wrap(draw, "中文没有空格所以逐字断行才对", graphic._font(48), 300)
        self.assertTrue(all(len(line) <= 6 for line in lines), lines)
        self.assertEqual("".join(lines), "中文没有空格所以逐字断行才对")


if __name__ == "__main__":
    unittest.main()


class ReviewFixesTest(unittest.TestCase):
    """票 38：第二轮审查的修复。"""

    def test_max_items_has_one_owner(self) -> None:
        """条目上限只在 cards 定义一次；回退版式另写一份会漂（曾经 5 vs 4）。"""
        self.assertEqual(graphic.MAX_ITEMS, cards.MAX_ITEMS)

    @unittest.skipUnless(graphic._pillow_available(), "需要 Pillow")
    def test_assert_rendered_rejects_corrupt_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.png"
            bad.write_bytes(b"not a png at all")
            with self.assertRaises(graphic.GraphicError):
                graphic._assert_rendered(bad)

    @unittest.skipUnless(graphic._pillow_available(), "需要 Pillow")
    def test_assert_rendered_rejects_wrong_size(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as td:
            small = Path(td) / "small.png"
            Image.new("RGB", (64, 64), (0, 0, 0)).save(small, "PNG")
            with self.assertRaises(graphic.GraphicError):
                graphic._assert_rendered(small)

    @unittest.skipUnless(graphic._pillow_available(), "需要 Pillow")
    def test_assert_rendered_accepts_good_file(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as td:
            ok = Path(td) / "ok.png"
            Image.new("RGB", (graphic.W, graphic.H), (0, 0, 0)).save(ok, "PNG")
            graphic._assert_rendered(ok)      # 不抛即通过

    @unittest.skipUnless(graphic.available(), "需要可用的渲染后端")
    def test_render_output_passes_validation(self) -> None:
        """正常出图要能过 D23 校验（否则每次跑都白干）。"""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "card.png"
            graphic.render(SAMPLE[cards.KIND_STATEMENT], out)
            graphic._assert_rendered(out)
