# ---- S4 \u5b88\u536b\u6d4b\u8bd5\uff1a\u884c\u53f7\u951a\u5b9a\u8bfb\u53d6 ----------------------------------------------
#
# \u5b88\u4ec0\u4e48\uff1a`lvs map` \u7684\u5168\u90e8\u4ef7\u503c\u5c31\u662f\u201c\u53ea\u62ff\u8981\u7684\u90a3\u51e0\u884c\u3001\u4e0d\u7559\u4e2d\u95f4\u6587\u4ef6\u201d\u3002
# \u5b83\u4e00\u65e6\u591a\u6253\u884c / \u53c8\u5f00\u59cb\u8f6c\u50a8\uff0c\u7701\u4e0b\u6765\u7684 15 \u4e07 token \u5c31\u53c8\u56de\u53bb\u4e86\u3002
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from lvs import maptool

REPO = Path(__file__).resolve().parents[1]


class ShowTests(unittest.TestCase):
    def test_show_returns_exactly_the_requested_range(self) -> None:
        with TemporaryDirectory() as tmp:
            f = Path(tmp) / "a.py"
            f.write_text("\n".join(f"line{i}" for i in range(1, 101)), encoding="utf-8")
            text = maptool.show(f, 10, 12)
            self.assertEqual(text.splitlines(), ["   10| line10", "   11| line11", "   12| line12"])

    def test_show_clamps_instead_of_raising(self) -> None:
        with TemporaryDirectory() as tmp:
            f = Path(tmp) / "a.py"
            f.write_text("one\ntwo\n", encoding="utf-8")
            self.assertEqual(len(maptool.show(f, 1, 999).splitlines()), 2)  # \u8d8a\u754c\u6536\u7d27\u5230\u6587\u4ef6\u672b
            self.assertEqual(maptool.show(f, 5, 9), "")                     # \u8d77\u70b9\u8d8a\u754c = \u7a7a
            self.assertEqual(maptool.show(f, 2, 1), "")                     # \u8d77 > \u6b62 = \u7a7a
            self.assertEqual(maptool.show(f, 1, None), "    1| one\n    2| two")  # \u4e0d\u7ed9 end = \u5230\u6587\u4ef6\u672b

    def test_raw_has_no_line_numbers(self) -> None:
        with TemporaryDirectory() as tmp:
            f = Path(tmp) / "a.py"
            f.write_text("alpha\nbeta\n", encoding="utf-8")
            self.assertEqual(maptool.show(f, 2, 2, numbered=False), "beta")


class GrepTests(unittest.TestCase):
    def _tree(self, tmp: str) -> Path:
        root = Path(tmp)
        (root / "pkg").mkdir()
        (root / "pkg" / "a.py").write_text("import os\nTARGET = 1\nother\n", encoding="utf-8")
        (root / "pkg" / "b.md").write_text("TARGET in docs\n", encoding="utf-8")
        (root / "pkg" / "img.png").write_bytes(b"\x89PNG target")
        (root / "pkg" / "__pycache__").mkdir()
        (root / "pkg" / "__pycache__" / "c.py").write_text("TARGET cached\n", encoding="utf-8")
        return root

    def test_grep_finds_text_files_and_skips_cache_dirs(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            out = maptool.grep("TARGET", ["."], root=root)
            self.assertIn("pkg/a.py:2:", out)
            self.assertIn("pkg/b.md:1:", out)
            self.assertNotIn("__pycache__", out)   # \u7f13\u5b58\u76ee\u5f55\u4e0d\u626b
            self.assertNotIn("img.png", out)       # \u4e8c\u8fdb\u5236\u4e0d\u626b

    def test_grep_limit_reports_the_remainder(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "big.py").write_text("HIT\n" * 10, encoding="utf-8")
            out = maptool.grep("HIT", ["."], limit=3, root=root)
            self.assertEqual(len([l for l in out.splitlines() if l.startswith("big.py:")]), 3)
            self.assertIn("\u8fd8\u6709 7 \u5904", out)

    def test_grep_context_and_ignore_case_and_bad_regex(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("before\nneedle\nafter\n", encoding="utf-8")
            out = maptool.grep("NEEDLE", ["."], context=1, ignore_case=True, root=root)
            self.assertIn("a.py-1- before", out)
            self.assertIn("a.py:2: needle", out)
            self.assertIn("a.py-3- after", out)
            self.assertIn("\u6b63\u5219\u5199\u9519\u4e86", maptool.grep("([", ["."], root=root))

    def test_grep_no_hit_is_a_plain_message(self) -> None:
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "a.py").write_text("x\n", encoding="utf-8")
            self.assertIn("\u6ca1\u547d\u4e2d", maptool.grep("nope", ["."], root=Path(tmp)))


class NoIntermediateFilesTests(unittest.TestCase):
    def test_reading_leaves_no_new_files(self) -> None:
        """\u2605 S4 \u7684\u6838\u5fc3\u627f\u8bfa\uff1a\u8bfb\u5c31\u662f\u8bfb\uff0c\u4e0d\u4ea7\u751f `*_numbered.txt` \u4e4b\u7c7b\u7684\u4e2d\u95f4\u6587\u4ef6\u3002"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("hello\nworld\n", encoding="utf-8")
            before = sorted(p.name for p in root.rglob("*"))
            maptool.run_command(_ns(map_command="show", file="a.py", start=1, end=2, raw=False))
            maptool.grep("hello", ["."], root=root)
            self.assertEqual(before, sorted(p.name for p in root.rglob("*")))


def _ns(**kw):
    from types import SimpleNamespace

    return SimpleNamespace(**kw)


class MapCliTests(unittest.TestCase):
    def test_show_on_cli_prints_61_lines(self) -> None:
        """\u9a8c\u6536\u5224\u636e\uff1a`lvs map show lvs/cli.py 180 240` \u8f93\u51fa\u884c\u6570 = 61\u3002"""
        proc = subprocess.run(
            [sys.executable, "-m", "lvs", "map", "show", "lvs/cli.py", "180", "240"],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(proc.stdout.strip().splitlines()), 61)

    def test_show_default_span_is_60_lines(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "lvs", "map", "show", "lvs/cli.py", "1"],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        self.assertEqual(len(proc.stdout.strip().splitlines()), maptool.DEFAULT_SPAN)

    def test_missing_file_is_a_clean_error(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "lvs", "map", "show", "no/such/file.py", "1"],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("\u6ca1\u6709\u8fd9\u4e2a\u6587\u4ef6", proc.stdout)


if __name__ == "__main__":
    unittest.main()
