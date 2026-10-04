"""长阶段的**增量**进度记账（票 26）。

`assets` / `voice` 都是一镜一份产物、动辄跑二十分钟。原来只在**收尾**写一次
`manifest.json`，于是跑到一半被中断（Ctrl-C、断电、崩）时盘上什么都不留 ——
重开界面、看 `lvs board` 都只能从零看起。
"""

from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import types
import unittest
from pathlib import Path

from lvs import assets as assets_mod
from lvs import board as board_mod
from lvs import tts as tts_mod
from lvs.config import Config
from lvs.workspace import Workspace
import pytest


def _cfg() -> Config:
    return Config({"library": {"dirs": [], "min_score": 1}, "pexels": {"api_key": ""}}, None)


class MarkStageProgressTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.ws = Workspace(task="t", root=Path(td)).ensure()

    def test_writes_partial_with_counts(self) -> None:
        self.ws.mark_stage_progress("assets", done=3, skipped=1, failed=0, total=10)
        entry = self.ws.manifest["stages"]["assets"]
        self.assertEqual(entry["status"], "partial", "跑到一半绝不能记成 done（D25）")
        self.assertEqual((entry["done"], entry["skipped"], entry["total"]), (3, 1, 10))

    def test_does_not_clobber_existing_fields(self) -> None:
        self.ws.mark_stage("assets", outputs=[self.ws.path("shots.json")], note="上次留下的")
        self.ws.mark_stage_progress("assets", done=1, total=10)
        entry = self.ws.manifest["stages"]["assets"]
        self.assertEqual(entry["note"], "上次留下的")
        self.assertTrue(entry["outputs"], "增量更新不该把已登记的产物抹掉")

    def test_partial_is_not_done(self) -> None:
        self.ws.mark_stage_progress("voice", done=1, total=10)
        self.assertFalse(self.ws.is_stage_done("voice"))

    def test_survives_a_reread(self) -> None:
        self.ws.mark_stage_progress("assets", done=7, skipped=0, failed=1, total=10)
        again = Workspace(task="t", root=self.ws.root).ensure()
        self.assertEqual(again.manifest["stages"]["assets"]["done"], 7,
                         "要真落盘，不能只在内存里")


@pytest.mark.slow   # ★ 慢组：InterruptedAssetsTest
class InterruptedAssetsTest(unittest.TestCase):
    """跑到一半炸掉 —— 盘上必须留着"跑到哪了"。"""

    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.ws = Workspace(task="t", root=Path(td)).ensure()
        self.ws.write_shots({
            "shots": [{"id": i, "source": "graphic", "kind": "graphic",
                       "visual": f"第{i}块数据", "narration": f"旁白{i}。"} for i in (1, 2, 3, 4)],
        })

    def test_partial_record_is_on_disk_when_the_loop_dies(self) -> None:
        calls = {"n": 0}
        real = assets_mod.graphic.render

        def explode(card, out):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("模拟：进程被杀了")
            return real(card, out)

        assets_mod.graphic.render = explode          # type: ignore[assignment]
        self.addCleanup(setattr, assets_mod.graphic, "render", real)

        args = types.SimpleNamespace(force=False, only=None, no_library=False, source=None)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                assets_mod.run_command(_cfg(), self.ws, args)

        entry = Workspace(task="t", root=self.ws.root).ensure().manifest["stages"]["assets"]
        self.assertEqual(entry["status"], "partial")
        self.assertEqual(entry["done"], 2, "前两镜的成果要记下来")
        self.assertEqual(entry["total"], 4)

    def test_rerun_finishes_and_flips_to_done(self) -> None:
        args = types.SimpleNamespace(force=False, only=None, no_library=False, source=None)
        with contextlib.redirect_stdout(io.StringIO()):
            code = assets_mod.run_command(_cfg(), self.ws, args)
        self.assertEqual(code, 0)
        entry = Workspace(task="t", root=self.ws.root).ensure().manifest["stages"]["assets"]
        self.assertEqual(entry["status"], "done", "补跑完就该是 done")


class BoardPartialRenderingTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.ws = Workspace(task="t", root=Path(td)).ensure()
        self.ws.write_shots({"shots": [{"id": 1, "source": "local"}]})

    def _card(self):  # noqa: ANN202
        return next(c for c in board_mod.collect(self.ws).stages if c.stage == "assets")

    def test_stopped_halfway_reads_as_unfinished_not_failed(self) -> None:
        """`partial` + done<total = 停在半路，不是"有失败" —— 两者给人看到的下一步完全不同。"""
        self.ws.mark_stage_progress("assets", done=2, skipped=0, failed=0, total=5)
        card = self._card()
        self.assertEqual(card.status_text, "部分完成")
        self.assertEqual((card.done, card.total), (2, 5))

    def test_finished_with_failures_still_reads_as_failed(self) -> None:
        self.ws.mark_stage_progress("assets", done=5, skipped=0, failed=2, total=5)
        self.assertEqual(self._card().status_text, "有失败")

    def test_text_board_shows_the_progress(self) -> None:
        self.ws.mark_stage_progress("assets", done=2, skipped=1, failed=0, total=5)
        out = board_mod.render_text(board_mod.collect(self.ws))
        self.assertIn("3/5", out, "看板要显示 已产出/总数")


class ProgressCountKeyTest(unittest.TestCase):
    """票 26 遗留的"一个概念两个名字"。

    素材阶段往 manifest 写 `done`，配音阶段却写 `synthesized` —— 于是 `board.py`
    得用 `entry.get("done", entry.get("synthesized", 0))` 去兜两个名字，而且这个
    兜底表达式在**两处**各写了一遍。统一成一个名字，读者也就只剩一个口径。
    """

    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.ws = Workspace(task="t", root=Path(td)).ensure()
        self.ws.write_shots({"shots": [{"id": 1, "narration": "第一句。"}]})

    def test_unknown_count_key_is_rejected(self) -> None:
        """拼错键名（`don=`）不能被静默并进 manifest —— 那正是"静默降级"。

        自由 `**counts` 会把 `don=3` 老老实实写进 JSON，而看板只认 `done`，
        于是错键永远不显示、也不报错。
        """
        with self.assertRaises(TypeError):
            self.ws.mark_stage_progress("assets", don=3)  # type: ignore[call-arg]

    def test_voice_writes_the_same_done_key(self) -> None:
        """配音阶段落盘的"已完成数"必须叫 `done`，与素材阶段同名。

        驱动真实的 `tts.run_command`：让后端一合成即失败，从而走到 `_progress()`
        那一次增量记账（此刻最该留下"跑到哪了"）—— 键名就在这里定下来。
        """
        class _FailingBackend:
            name = "edge"

            def synthesize(self, text: str, out_path, voice: str) -> object:  # noqa: ANN001
                raise tts_mod.TTSError("模拟：合成失败")

        real_make = tts_mod.make_backend
        tts_mod.make_backend = lambda cfg: _FailingBackend()          # type: ignore[assignment]
        self.addCleanup(setattr, tts_mod, "make_backend", real_make)

        args = types.SimpleNamespace(force=False)
        with contextlib.redirect_stdout(io.StringIO()):
            code = tts_mod.run_command(_cfg(), self.ws, args)
        self.assertEqual(code, 2, "一镜都没合成成功，应当以失败码退出")

        entry = Workspace(task="t", root=self.ws.root).ensure().manifest["stages"]["voice"]
        self.assertIn("done", entry, "配音进度也要用 `done` 记账")
        self.assertNotIn("synthesized", entry, "`synthesized` 是同一个概念的第二个名字，不该再写")


if __name__ == "__main__":
    unittest.main()
