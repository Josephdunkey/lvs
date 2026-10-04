"""`lvs studio` 手动向导（票据 30）。不真正生图、不联网。"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path

from lvs import studio
from lvs.workspace import Workspace


def _args(**kw):
    base = {"manuscript": None, "action": None, "redo": None, "prompt": None,
            "seed": None, "visual": None, "redo_shots": False, "yes": False, "stop": False}
    base.update(kw)
    return argparse.Namespace(**base)


class ParseShotIdsTest(unittest.TestCase):
    def test_single_and_range(self):
        self.assertEqual(studio.parse_shot_ids("1-3,7"), [1, 2, 3, 7])

    def test_dedup_and_sort(self):
        self.assertEqual(studio.parse_shot_ids("5,1,5,2-3"), [1, 2, 3, 5])

    def test_reversed_range(self):
        self.assertEqual(studio.parse_shot_ids("9-7"), [7, 8, 9])

    def test_full_width_comma(self):
        self.assertEqual(studio.parse_shot_ids("1，2"), [1, 2])

    def test_bad_input_raises(self):
        for spec in ("", "abc", "1-x", "  "):
            with self.assertRaises(studio.StudioError, msg=spec):
                studio.parse_shot_ids(spec)


class ParseActionsTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(studio.parse_actions("local,graphic"), ["local", "graphic"])

    def test_all_expands_to_every_branch(self):
        self.assertEqual(studio.parse_actions("all"), list(studio.assets_mod.ASSET_BRANCHES))

    def test_unknown_raises(self):
        with self.assertRaises(studio.StudioError):
            studio.parse_actions("pexels,bogus")

    def test_empty_raises(self):
        with self.assertRaises(studio.StudioError):
            studio.parse_actions("")


class AskActionsTest(unittest.TestCase):
    def test_flag_wins(self):
        self.assertEqual(studio.ask_actions(_args(action="pexels"), auto=True), ["pexels"])

    def test_yes_takes_everything(self):
        self.assertEqual(
            studio.ask_actions(_args(), auto=True), list(studio.assets_mod.ASSET_BRANCHES)
        )

    def test_non_interactive_without_flags_raises(self):
        # 关键：非交互终端下绝不能挂在那里等输入，必须报错让人改用参数
        with self.assertRaises(studio.StudioError):
            studio.ask_actions(_args(), auto=False)


class AskNextTest(unittest.TestCase):
    def test_stop_flag(self):
        self.assertEqual(studio.ask_next(_args(stop=True), auto=True), "stop")

    def test_yes_continues(self):
        self.assertEqual(studio.ask_next(_args(), auto=True), "continue")

    def test_non_interactive_without_flags_raises(self):
        with self.assertRaises(studio.StudioError):
            studio.ask_next(_args(), auto=False)


class ClearShotAssetsTest(unittest.TestCase):
    def test_removes_assets_and_segment_and_marks_pending(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(task="t", root=Path(td)).ensure()
            shot = {"id": 7, "status": "done", "asset_path": "x", "resolved_by": "local",
                    "library_asset": "/lib/a.png"}
            for branch, name in (("local", "shot-007.png"), ("pexels", "shot-007.mp4"),
                                 ("graphic", "shot-007.png")):
                p = ws.path("assets", branch, name)
                p.write_bytes(b"data")
            seg = ws.path("segments", "shot-007.mp4")
            seg.parent.mkdir(parents=True, exist_ok=True)
            seg.write_bytes(b"seg")
            # 别的镜不能被牵连
            other = ws.path("assets", "local", "shot-008.png")
            other.write_bytes(b"data")

            studio.clear_shot_assets(ws, shot)

            self.assertFalse(ws.path("assets", "local", "shot-007.png").exists())
            self.assertFalse(ws.path("assets", "pexels", "shot-007.mp4").exists())
            self.assertFalse(ws.path("assets", "graphic", "shot-007.png").exists())
            self.assertFalse(seg.exists())
            self.assertTrue(other.exists(), "不该动到别的镜")
            self.assertEqual(shot["status"], "pending")
            for key in ("asset_path", "resolved_by", "library_asset"):
                self.assertNotIn(key, shot)


class RedoShotsTest(unittest.TestCase):
    def _ws(self, td: str) -> Workspace:
        ws = Workspace(task="t", root=Path(td)).ensure()
        ws.path("shots.json").write_text(
            json.dumps({"shots": [
                {"id": 1, "status": "done", "prompt": "old", "source": "local", "asset_path": "a"},
                {"id": 2, "status": "done", "prompt": "old", "source": "graphic", "asset_path": "b"},
            ]}, ensure_ascii=False), encoding="utf-8")
        return ws

    def test_seed_bumps_and_assets_cleared(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            ws.path("assets", "local", "shot-001.png").write_bytes(b"x")
            # 不真正跑 assets：把 run_command 换掉
            calls = []
            original = studio.assets_mod.run_command
            studio.assets_mod.run_command = lambda *a, **k: (calls.append(a[2]), 0)[1]
            try:
                code = studio.redo_shots(self._config(), ws, "1", _args())
            finally:
                studio.assets_mod.run_command = original
            self.assertEqual(code, 0)
            self.assertEqual(len(calls), 1)
            data = json.loads(ws.path("shots.json").read_text(encoding="utf-8"))
            shot = data["shots"][0]
            # 默认 seed = 全局 42 + 镜号 1 = 43；「换一张」在它基础上 +1
            self.assertEqual(shot["seed"], 44)
            self.assertNotIn("asset_path", shot)

    def test_prompt_replaces_and_no_seed_bump(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            original = studio.assets_mod.run_command
            studio.assets_mod.run_command = lambda *a, **k: 0
            try:
                studio.redo_shots(self._config(), ws, "2", _args(prompt="新提示词"))
            finally:
                studio.assets_mod.run_command = original
            data = json.loads(ws.path("shots.json").read_text(encoding="utf-8"))
            self.assertEqual(data["shots"][1]["prompt"], "新提示词")
            self.assertNotIn("seed", data["shots"][1], "改了提示词就不该再动 seed")

    def test_missing_ids_reported_not_fatal(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            original = studio.assets_mod.run_command
            studio.assets_mod.run_command = lambda *a, **k: 0
            try:
                code = studio.redo_shots(self._config(), ws, "999", _args())
            finally:
                studio.assets_mod.run_command = original
            self.assertEqual(code, 2)

    def _config(self):
        from lvs.config import Config
        return Config({"comfyui": {"seed": 42}}, None)


class RedoShotsScopeTest(unittest.TestCase):
    """改图重跑要尊重向导选的 `--action` 范围（票 30），别把六个分支全跑一遍。"""

    def _ws(self, td: str) -> Workspace:
        ws = Workspace(task="t", root=Path(td)).ensure()
        ws.path("shots.json").write_text(
            json.dumps({"shots": [
                {"id": 1, "status": "done", "prompt": "o", "source": "local"},
                {"id": 2, "status": "done", "prompt": "o", "source": "graphic"},
            ]}, ensure_ascii=False), encoding="utf-8")
        return ws

    def _capture(self):
        seen: dict = {}
        original = studio.assets_mod.run_command
        studio.assets_mod.run_command = lambda config, ws, ns: (seen.update(vars(ns)), 0)[1]
        return seen, original

    def test_scope_forwarded_to_assets(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            seen, original = self._capture()
            try:
                studio.redo_shots(self._config(), ws, "1", _args(), only="local")
            finally:
                studio.assets_mod.run_command = original
            self.assertEqual(seen["only"], "local")

    def test_no_scope_means_all_branches(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            seen, original = self._capture()
            try:
                studio.redo_shots(self._config(), ws, "1", _args())
            finally:
                studio.assets_mod.run_command = original
            self.assertIsNone(seen["only"])

    def _config(self):
        from lvs.config import Config
        return Config({"comfyui": {"seed": 42}}, None)


class RoutePexelsToLocalTest(unittest.TestCase):
    def test_flips_pexels_shots_only(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(task="t", root=Path(td)).ensure()
            ws.path("shots.json").write_text(
                json.dumps({"shots": [
                    {"id": 1, "source": "pexels"},
                    {"id": 2, "source": "graphic"},
                    {"id": 3, "source": "pexels", "library_asset": "/lib/a.png"},
                ]}, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(studio.route_pexels_to_local(ws), 1)
            data = json.loads(ws.path("shots.json").read_text(encoding="utf-8"))
            self.assertEqual([s["source"] for s in data["shots"]], ["local", "graphic", "pexels"])

    def test_missing_shots_json_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(task="t", root=Path(td)).ensure()
            self.assertEqual(studio.route_pexels_to_local(ws), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
