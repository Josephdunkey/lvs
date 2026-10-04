"""看板（`lvs board`）单元测试。纯读取，不碰产物。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lvs import board as board_mod
from lvs.workspace import Workspace


def _shot(sid: int, *, source="local", resolved_by=None, asset_path=None, status="pending"):
    return {
        "id": sid, "source": source, "resolved_by": resolved_by,
        "asset_path": asset_path, "status": status,
    }


class CollectTest(unittest.TestCase):
    def test_empty_workspace_is_all_todo(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(task="t", root=Path(td)).ensure()
            b = board_mod.collect(ws)
            self.assertEqual(b.shots_total, 0)
            self.assertEqual([c.status for c in b.stages], ["todo"] * 5)
            self.assertIn("还没有 shots.json", board_mod.render_text(b))

    def test_reads_manifest_and_shots(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(task="t", root=Path(td)).ensure()
            a1 = ws.path("assets", "local", "shot-001.png")
            a1.write_bytes(b"x")
            ws.mark_stage("parse", outputs=[ws.path("parse.json")], segments=6)
            ws.mark_stage("shots", outputs=[ws.path("shots.json")], count=3)
            ws.mark_stage("assets", outputs=[a1], status="partial",
                          total=3, done=1, skipped=1, failed=1)
            ws.path("shots.json").write_text(json.dumps({"title": "试片", "shots": [
                _shot(1, resolved_by="local", asset_path=str(a1), status="done"),
                _shot(2, source="pexels", status="failed"),
                _shot(3, source="pexels"),
            ]}), encoding="utf-8")

            b = board_mod.collect(ws)
            by = {c.stage: c for c in b.stages}
            self.assertEqual(by["shots"].status, "done")
            self.assertEqual(by["assets"].status, "partial")
            self.assertIn("失败 1", by["assets"].detail)
            self.assertEqual(by["voice"].status, "todo")
            self.assertEqual(b.shots_total, 3)
            self.assertEqual(b.shots_ready, 1)
            self.assertEqual(b.shots_failed, 1)
            self.assertEqual(b.shots_pending, 1)
            self.assertEqual(b.by_source, {"local": 1, "pexels": 2})

    def test_corrupt_shots_json_degrades(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(task="t", root=Path(td)).ensure()
            ws.path("shots.json").write_text("{not json", encoding="utf-8")
            b = board_mod.collect(ws)  # 不该抛
            self.assertEqual(b.shots_total, 0)


class RenderTest(unittest.TestCase):
    def _board(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(task="t", root=Path(td)).ensure()
            ws.mark_stage("parse", outputs=[ws.path("parse.json")])
            ws.mark_stage("assets", outputs=[], status="partial", total=10, done=6, failed=1)
            return board_mod.collect(ws)

    def test_text_has_all_stages(self) -> None:
        txt = board_mod.render_text(self._board())
        for label in ("解析", "拆镜", "素材", "配音", "合成"):
            self.assertIn(label, txt)

    def test_html_is_self_contained_and_lists_stages(self) -> None:
        html = board_mod.render_html(self._board())
        self.assertTrue(html.lstrip().startswith("<!DOCTYPE html>"))
        self.assertIn("<style>", html)
        self.assertNotIn("http://", html)  # 无外链
        for label in ("解析", "素材"):
            self.assertIn(label, html)

    def test_html_escapes_title(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(task="t", root=Path(td)).ensure()
            ws.path("shots.json").write_text(
                json.dumps({"title": "<script>alert(1)</script>", "shots": []}), encoding="utf-8")
            html = board_mod.render_html(board_mod.collect(ws))
            self.assertNotIn("<script>alert(1)</script>", html)
            self.assertIn("&lt;script&gt;", html)


if __name__ == "__main__":
    unittest.main()
