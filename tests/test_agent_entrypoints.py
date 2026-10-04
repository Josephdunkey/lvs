# ---- S1/S2 入口守卫测试 -------------------------------------------------
#
# 为什么单独守：这两条命令是 agent 续跑的**入口**（省 token 全靠它们）。
# 入口一旦变贵（输出变长、又去写状态），成本会**静默地**加回去。
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from lvs import cli, shots, summary
from lvs.workspace import Workspace

REPO = Path(__file__).resolve().parents[1]


def _shot(sid: int, *, prompt: str = "prompt", scene: str = "场景", narration: str = "旁白") -> dict:
    return {
        "id": sid, "segment": 0, "narration": narration, "visual": scene, "kind": "scene",
        "scene": scene, "source": "local", "prompt": prompt, "keywords": ["k1", "k2"],
        "status": "done", "start": 0.0, "end": 2.0, "audio_duration": 2.0,
    }


def _slice_display(line: str, start: int, end: int) -> str:
    """按**显示宽度**切一段（中日韩字符占 2 列）—— 用来钉「中文也挤不歪列」。"""
    acc, idx = 0, 0
    while idx < len(line) and acc < start:
        acc += summary._width(line[idx])
        idx += 1
    out: list[str] = []
    while idx < len(line) and acc < end:
        out.append(line[idx])
        acc += summary._width(line[idx])
        idx += 1
    return "".join(out)


class TaskFixture:
    """建一个临时 root，里面放 N 任务（每个都有 manifest.json）。"""

    def __init__(self, tmp: str) -> None:
        self.root = Path(tmp)

    def make(self, name: str, *, shot_count: int = 0, images: int = 0, prompt: str = "p") -> Workspace:
        ws = Workspace(task=name, root=self.root).ensure()
        if shot_count:
            ws.write_shots({"count": shot_count, "shots": [_shot(i + 1, prompt=prompt) for i in range(shot_count)]})
            ws.path("parse.json").write_text("{}", encoding="utf-8")
        for i in range(images):
            ws.path("assets", "local", f"shot-{i:03d}.png").write_bytes(b"x")
        return ws


class StatusTableTests(unittest.TestCase):
    def test_brief_never_exceeds_eight_lines(self) -> None:
        rows = [summary.TaskRow(f"T{i}", "cfg.toml", i, 3, 6, "G2 cast", "YYYY--", "10-05 00:00")
                for i in range(9)]
        lines = summary.format_table(rows, brief=True).splitlines()
        self.assertLessEqual(len(lines), 8, lines)
        self.assertIn("另有", lines[-1])  # 截断时必须告诉读者还有多少个

    def test_brief_lists_all_when_few(self) -> None:
        rows = [summary.TaskRow("UGE03", "cfg.toml", 363, 4, 6, "G1 shots", "YYYYYY", "10-05 01:09")]
        lines = summary.format_table(rows, brief=True).splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("UGE03", lines[2])

    def test_legend_only_in_long_form(self) -> None:
        rows = [summary.TaskRow("UGE03", "cfg.toml", 1, 0, 6, "G0 script", "------", "")]
        self.assertNotIn("产物列顺序", summary.format_table(rows, brief=True))
        self.assertIn("产物列顺序", summary.format_table(rows, brief=False))

    def test_columns_are_aligned_by_display_width(self) -> None:
        """中文任务名 / 中文 config 名也不能把列挤歪（每列按显示宽度填充）。"""
        rows = [summary.TaskRow("UGE03", "config.雨月物语.toml", 363, 4, 6, "G1 shots", "YYYYYY", "10-05 01:09"),
                summary.TaskRow("MM01", "≈", 0, 0, 6, "G0 script", "------", "")]
        lines = summary.format_table(rows, brief=True).splitlines()
        pos, gate_col = 0, (0, 0)
        for name, width in summary._COLUMNS:
            if name == "门禁":
                gate_col = (pos, pos + width)
                break
            pos += width + 1
        else:
            self.fail("「门禁」列不见了")
        self.assertEqual(_slice_display(lines[0], *gate_col).strip(), "门禁")
        self.assertEqual(_slice_display(lines[2], *gate_col).strip(), "4/6")
        self.assertEqual(_slice_display(lines[3], *gate_col).strip(), "0/6")

    def test_error_row_is_surfaced_in_one_line(self) -> None:
        rows = [summary.TaskRow("T1", "cfg", 0, 0, 0, "-", "------", "", error="配置错误：boom")]
        lines = summary.format_table(rows, brief=True).splitlines()
        self.assertLessEqual(len(lines), 8)
        self.assertIn("boom", lines[-1])


class StatusCollectTests(unittest.TestCase):
    def test_counts_images_and_artifact_marks(self) -> None:
        with TemporaryDirectory() as tmp:
            fx = TaskFixture(tmp)
            fx.make("UGE99", shot_count=2, images=3)
            rows = summary.collect(task="UGE99", root=fx.root)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].images, 3)
            self.assertTrue(rows[0].artifacts.startswith("YY"))  # parse.json + shots.json
            self.assertEqual(rows[0].total_gates, 6)  # 门禁总数固定六道

    def test_prefix_and_skip_filters(self) -> None:
        with TemporaryDirectory() as tmp:
            fx = TaskFixture(tmp)
            for name in ("UGE01", "NW01", "default", "agentprobe"):
                fx.make(name)
            self.assertEqual([p.name for p in summary.select_tasks(summary.task_dirs(fx.root))], ["UGE01"])
            self.assertEqual([p.name for p in summary.select_tasks(summary.task_dirs(fx.root), prefix="NW")], ["NW01"])
            self.assertEqual(len(summary.select_tasks(summary.task_dirs(fx.root), show_all=True)), 2)  # 测试夹具不列

    def test_broken_manifest_never_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            broken = root / ".work" / "UGE98"
            broken.mkdir(parents=True)
            (broken / "manifest.json").write_text("{not json", encoding="utf-8")
            opened, total, nxt, err = summary.gate_state("UGE98", "", root=root)
            # 坏 manifest 不该让摘要炸掉：门禁总数仍是六道，只是没一道放行
            self.assertEqual((opened, total), (0, 6))
            self.assertIsInstance(err, str)


class ShotsReadonlyEntryTests(unittest.TestCase):
    def test_readonly_router_detects_peek_and_index(self) -> None:
        parser = cli.build_parser()
        peek = parser.parse_args(["shots", "--peek", "3"])
        index = parser.parse_args(["shots", "--index"])
        normal = parser.parse_args(["shots", "--force"])
        self.assertTrue(cli._shots_readonly(peek))
        self.assertTrue(cli._shots_readonly(index))
        self.assertFalse(cli._shots_readonly(normal))

    def test_peek_and_index_do_not_touch_task_state(self) -> None:
        """★ 回归守卫：`--peek` / `--index` 是只读命令。

        2026-10-05 实测它们曾经走 `_run_and_report` → `stage.note_stage_end`，
        于是"看一眼"就往 logs/run-*.jsonl 落两条 stage_end、
        并把 manifest.json 的 mtime 顶到当前时间（假的"最近活动"）。
        """
        with TemporaryDirectory() as tmp:
            fx = TaskFixture(tmp)
            ws = fx.make("UGE97", shot_count=4)
            before = sorted((p.relative_to(ws.dir).as_posix(), p.stat().st_mtime_ns)
                            for p in ws.dir.rglob("*"))
            rcs: list[int] = []
            texts: list[str] = []
            for ns in (
                SimpleNamespace(peek=2, index=False, index_width=40, index_limit=None, full=False),
                SimpleNamespace(peek=None, index=True, index_width=40, index_limit=None, full=False),
            ):
                cap = io.StringIO()
                with contextlib.redirect_stdout(cap):
                    rcs.append(shots.run_command(None, ws, ns))  # type: ignore[arg-type]
                texts.append(cap.getvalue())
            after = sorted((p.relative_to(ws.dir).as_posix(), p.stat().st_mtime_ns)
                           for p in ws.dir.rglob("*"))
            self.assertEqual(rcs, [0, 0])
            self.assertEqual(before, after, "只读命令不得改动任务目录")
            self.assertIn("#2", texts[0])


class PeekIndexTextTests(unittest.TestCase):
    def _ws(self, fx: TaskFixture, prompt: str = "p" * 600) -> Workspace:
        return fx.make("UGE96", shot_count=5, prompt=prompt)

    def test_peek_is_short_by_default(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = self._ws(TaskFixture(tmp))
            text = shots.peek_text(ws, 3)
            self.assertLess(len(text), 400, len(text))  # 验收：默认输出 < 400 字符
            self.assertIn("看全文", text)

    def test_peek_full_keeps_everything(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = self._ws(TaskFixture(tmp))
            text = shots.peek_text(ws, 3, full=True)
            self.assertIn("p" * 600, text)
            self.assertNotIn("看全文", text)

    def test_peek_missing_shot_is_graceful(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = self._ws(TaskFixture(tmp))
            self.assertIn("没有镜 #999", shots.peek_text(ws, 999))

    def test_index_is_one_line_per_shot(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = self._ws(TaskFixture(tmp))
            lines = shots.index_text(ws).splitlines()
            self.assertEqual(len(lines), 5)
            self.assertTrue(lines[0].startswith("   1"))
            self.assertEqual(len(shots.index_text(ws, limit=2).splitlines()), 2)

    def test_index_truncates_long_scene(self) -> None:
        with TemporaryDirectory() as tmp:
            fx = TaskFixture(tmp)
            ws = fx.make("UGE95", shot_count=1)
            ws.write_shots({"count": 1, "shots": [_shot(1, scene="场" * 100)]})
            line = shots.index_text(ws, width=40).splitlines()[0]
            self.assertIn("…", line)
            self.assertLessEqual(len(line), 4 + 1 + 7 + 1 + 7 + 1 + 8 + 1 + 40)


class StatusCliSmokeTests(unittest.TestCase):
    def test_brief_on_unknown_task_is_ok_and_short(self) -> None:
        """真实入口烟测（子进程）：不论有没有任务，
        都得 rc=0 且 ≤8 行——续跑第一步不能因为"没任务"就挂。"""
        proc = subprocess.run(
            [sys.executable, "-m", "lvs", "status", "--brief", "--task", "__no_such_task__"],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLessEqual(len(proc.stdout.strip().splitlines()), 8, proc.stdout)


if __name__ == "__main__":
    unittest.main()
