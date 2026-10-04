"""`shots.json` 的读写契约（票 42）。

它同时是**给人手改的真相源**（D14）和界面读的数据源，所以"读成什么、写成什么格式"
必须只有一份定义 —— 这些测试就是钉住那一份，免得哪天又冒出一个 `json.dumps`。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from lvs.workspace import Workspace, write_shots_json

SAMPLE = {"title": "名分账", "source_mode": "library",
          "shots": [{"id": 1, "source": "local", "narration": "第一句旁白。", "seed": 45}]}


class WriteFormatTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.root = Path(td)

    def test_workspace_write_matches_the_module_owner(self) -> None:
        ws = Workspace(task="t", root=self.root).ensure()
        via_ws = ws.write_shots(dict(SAMPLE))
        reference = self.root / "ref.json"
        write_shots_json(reference, dict(SAMPLE))
        self.assertEqual(via_ws.read_bytes(), reference.read_bytes())

    def test_gui_write_matches_the_cli_write(self) -> None:
        """界面按任务目录写、阶段按 Workspace 写 —— 落到盘上必须逐字节一样。"""
        from lvs.gui.app import write_shots_file

        ws = Workspace(task="t", root=self.root).ensure()
        cli = ws.write_shots(dict(SAMPLE))

        other = Workspace(task="u", root=self.root).ensure()
        write_shots_file(other.dir, dict(SAMPLE))
        self.assertEqual(cli.read_bytes(), other.path("shots.json").read_bytes())

    def test_format_is_unescaped_utf8_with_trailing_newline(self) -> None:
        out = self.root / "s.json"
        write_shots_json(out, dict(SAMPLE))
        text = out.read_text(encoding="utf-8")
        self.assertIn("名分账", text, "中文不该被转义成 \\uXXXX（人要看）")
        self.assertTrue(text.endswith("}\n"))
        self.assertIn('\n  "title"', text, "两空格缩进")


class TolerantReadTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.ws = Workspace(task="t", root=Path(td)).ensure()

    def test_missing_is_empty(self) -> None:
        self.assertEqual(self.ws.try_load_shots(), {})

    def test_corrupt_is_empty(self) -> None:
        self.ws.path("shots.json").write_text("{ not json", encoding="utf-8")
        self.assertEqual(self.ws.try_load_shots(), {})

    def test_non_dict_is_empty(self) -> None:
        self.ws.path("shots.json").write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(self.ws.try_load_shots(), {})

    def test_round_trip(self) -> None:
        self.ws.write_shots(dict(SAMPLE))
        self.assertEqual(self.ws.try_load_shots(), SAMPLE)

    def test_load_shots_still_raises_when_missing(self) -> None:
        """宽容版不能把"必须有前置产物"那版也变宽容 —— 两者语义不同。"""
        with self.assertRaises(RuntimeError):
            self.ws.load_shots(error=RuntimeError)


class GuiStoreReadTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.root = Path(td)
        self.ws = Workspace(task="t", root=self.root).ensure()
        self.ws.write_shots(dict(SAMPLE))

    def test_store_read_matches_workspace_read(self) -> None:
        from lvs.gui import store

        self.assertEqual(store.read_shots("t", self.root), self.ws.try_load_shots())

    def test_unknown_task_is_empty(self) -> None:
        from lvs.gui import store

        self.assertEqual(store.read_shots("nope", self.root), {})


if __name__ == "__main__":
    unittest.main()
