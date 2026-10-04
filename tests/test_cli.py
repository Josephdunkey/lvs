"""`lvs` 骨架与体检的最小回归测试。

运行：在仓库根执行  `python -m unittest discover -s tests -v`
"""

from __future__ import annotations

import shutil
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
        for cmd in ("doctor", "parse", "shots", "assets", "voice", "build", "run", "library", "image", "board"):
            self.assertIn(cmd, proc.stdout, f"`{cmd}` 未出现在 --help 中")

    def test_every_subcommand_has_help(self) -> None:
        for cmd in ("doctor", "parse", "shots", "assets", "voice", "build", "run", "library", "image", "board"):
            proc = _run_cli(cmd, "--help")
            self.assertEqual(proc.returncode, 0, f"`lvs {cmd} --help` 失败：{proc.stderr}")
            self.assertIn("--task", proc.stdout, f"`lvs {cmd}` 缺少 --task")

    def test_command_creates_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.toml"
            cfg.write_text("[app]\n", encoding="utf-8")
            proc = _run_cli("shots", "--task", "unit-test-task", "--config", str(cfg))
            # 缺少前置产物（parse.json）→ 退出码 2，但任务目录已就绪
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("parse.json", proc.stdout)
            self.assertTrue((ROOT / ".work" / "unit-test-task").is_dir())

    def test_missing_config_errors_with_path(self) -> None:
        proc = _run_cli("shots", "--config", str(Path("does-not-exist.toml")))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("does-not-exist.toml", proc.stdout + proc.stderr)


class TestBoardReadOnly(unittest.TestCase):
    """`lvs board` 是纯读取：不建任务目录、不写 manifest（票 23 验收项 / spec §16）。"""

    def test_board_does_not_create_task_dir(self) -> None:
        name = "board-readonly-probe"
        task_dir = ROOT / ".work" / name
        if task_dir.exists():
            shutil.rmtree(task_dir)
        self.addCleanup(lambda: shutil.rmtree(task_dir, ignore_errors=True))
        proc = _run_cli("board", "--task", name)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(
            task_dir.exists(),
            "`lvs board` 不该创建 .work/<task>/（只读契约）",
        )


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


class TestImageCommand(unittest.TestCase):
    """`lvs image` —— 手动生图的参数校验与降级（不依赖 ComfyUI 在线）。"""

    def _cfg(self, td: str) -> Path:
        cfg = Path(td) / "config.toml"
        # 指向一个不会有服务的端口；跳过 GPU 守卫以免 CI/无卡机器上不确定
        cfg.write_text('[comfyui]\nbase_url = "http://127.0.0.1:65533"\n[gpu]\nskip_check = true\n', encoding="utf-8")
        return cfg

    def test_no_prompt_is_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proc = _run_cli("image", "--config", str(self._cfg(td)))
            self.assertEqual(proc.returncode, 2)
            self.assertIn("--prompt", proc.stdout)

    def test_comfyui_down_is_graceful(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proc = _run_cli("image", "--prompt", "x", "--config", str(self._cfg(td)))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("ComfyUI 不可达", proc.stdout)
            self.assertIn("start_comfyui", proc.stdout)

    def test_from_shot_without_shots_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proc = _run_cli(
                "image", "--task", "no-such-task-xyz", "--from-shot", "1",
                "--config", str(self._cfg(td)),
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("shots.json", proc.stdout)

    def test_image_help_documents_key_flags(self) -> None:
        proc = _run_cli("image", "--help")
        for flag in ("--prompt", "--from-shot", "--count", "--workflow", "--model", "--seed"):
            self.assertIn(flag, proc.stdout)


class TestDoctor(unittest.TestCase):
    def test_doctor_runs_and_reports(self) -> None:
        proc = _run_cli("doctor")
        self.assertIn(proc.returncode, (0, 1))
        self.assertIn("环境体检", proc.stdout)
        for label in ("ffmpeg", "Python", "GPU"):
            self.assertIn(label, proc.stdout)


if __name__ == "__main__":
    unittest.main()
