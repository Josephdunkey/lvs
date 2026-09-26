"""`lvs` 骨架与体检的最小回归测试。

运行：在仓库根执行  `python -m unittest discover -s tests -v`
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lvs.config import Config, ConfigError
from lvs.workspace import SUBDIRS, Workspace, slugify

ROOT = Path(__file__).resolve().parent.parent


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "lvs", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


class TestCliShell(unittest.TestCase):
    def test_help_lists_all_subcommands(self) -> None:
        proc = _run_cli("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for cmd in ("doctor", "parse", "shots", "assets", "voice", "build", "run", "library"):
            self.assertIn(cmd, proc.stdout, f"`{cmd}` 未出现在 --help 中")

    def test_every_subcommand_has_help(self) -> None:
        for cmd in ("doctor", "parse", "shots", "assets", "voice", "build", "run", "library"):
            proc = _run_cli(cmd, "--help")
            self.assertEqual(proc.returncode, 0, f"`lvs {cmd} --help` 失败：{proc.stderr}")
            self.assertIn("--task", proc.stdout, f"`lvs {cmd}` 缺少 --task")

    def test_stub_command_creates_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.toml"
            cfg.write_text("[app]\n", encoding="utf-8")
            proc = _run_cli("shots", "--task", "unit-test-task", "--config", str(cfg))
            # 未实现 → 退出码 3
            self.assertEqual(proc.returncode, 3, proc.stderr)
            self.assertTrue((ROOT / ".work" / "unit-test-task").is_dir())

    def test_missing_config_errors_with_path(self) -> None:
        proc = _run_cli("shots", "--config", str(Path("does-not-exist.toml")))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("does-not-exist.toml", proc.stdout + proc.stderr)


class TestConfig(unittest.TestCase):
    def test_require_names_missing_field(self) -> None:
        cfg = Config({"app": {}}, Path("config.toml"))
        with self.assertRaises(ConfigError) as ctx:
            cfg.require("app.openai_api_key", hint="拆镜阶段必需")
        msg = str(ctx.exception)
        self.assertIn("app.openai_api_key", msg)
        self.assertIn("config.toml", msg)

    def test_empty_string_counts_as_unfilled(self) -> None:
        cfg = Config({"app": {"openai_api_key": ""}}, Path("config.toml"))
        self.assertFalse(cfg.has("app.openai_api_key"))

    def test_get_returns_default_for_nested_missing(self) -> None:
        cfg = Config({}, None)
        self.assertEqual(cfg.get("a.b.c", "fallback"), "fallback")

    def test_load_missing_file_raises(self) -> None:
        with self.assertRaises(ConfigError):
            Config.load(Path("definitely-not-here.toml"))


class TestWorkspace(unittest.TestCase):
    def test_slugify_keeps_cjk(self) -> None:
        self.assertEqual(slugify("K005-36个邑3万口"), "K005-36个邑3万口")
        self.assertEqual(slugify("  a/b:c  "), "a-b-c")
        self.assertEqual(slugify(""), "default")

    def test_ensure_creates_spec_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(task="t1", root=Path(td)).ensure()
            self.assertTrue(ws.manifest_path.is_file())
            for sub in SUBDIRS:
                self.assertTrue((ws.dir / sub).is_dir(), f"缺少目录 {sub}")

    def test_stage_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(task="t2", root=Path(td)).ensure()
            self.assertFalse(ws.is_stage_done("parse"))
            out = ws.path("parse.json")
            out.write_text("{}", encoding="utf-8")
            ws.mark_stage("parse", outputs=[out])
            self.assertTrue(ws.is_stage_done("parse", outputs=[out]))
            # 产物被删 → 视为未完成（断点续跑要重跑）
            out.unlink()
            self.assertFalse(ws.is_stage_done("parse", outputs=[out]))


class TestDoctor(unittest.TestCase):
    def test_doctor_runs_and_reports(self) -> None:
        proc = _run_cli("doctor")
        self.assertIn(proc.returncode, (0, 1))
        self.assertIn("环境体检", proc.stdout)
        for label in ("ffmpeg", "Python", "GPU"):
            self.assertIn(label, proc.stdout)


if __name__ == "__main__":
    unittest.main()
