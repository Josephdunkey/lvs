"""GUI 后端测试（票据 36）：只读数据层 + 任务运行器。

两者职责分开：`store` 是**磁盘真相**（D15 产物即状态），`jobs` 是**活进程状态**。
界面把两者合起来显示，谁也不替谁猜。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

from lvs.gui import jobs, store


def _task_fixture(root: Path, name: str = "demo", shots: int = 3) -> Path:
    """造一个最小的任务目录：像真跑过一半的样子。"""
    d = root / ".work" / name
    for sub in ("assets/graphic", "assets/local", "audio", "segments", "logs"):
        (d / sub).mkdir(parents=True, exist_ok=True)

    (d / "manifest.json").write_text(json.dumps({
        "task": name,
        "created_at": "2026-09-27T10:00:00",
        "stages": {
            "parse": {"status": "done", "at": "2026-09-27T10:00:00", "outputs": [str(d / "parse.json")]},
            "shots": {"status": "done", "at": "2026-09-27T10:00:05", "outputs": [str(d / "shots.json")], "count": shots},
            "assets": {"status": "partial", "at": "2026-09-27T10:01:00", "outputs": []},
            "build": {"status": "done", "at": "2026-09-27T10:05:00", "outputs": [],
                      "duration": 147.5, "placeholders": 0},
        },
    }, ensure_ascii=False), encoding="utf-8")

    (d / "parse.json").write_text(json.dumps({"title": "演示", "segments": []}, ensure_ascii=False), encoding="utf-8")
    (d / "shots.json").write_text(json.dumps({
        "title": "演示", "count": shots,
        "shots": [{"id": i, "kind": "graphic", "source": "graphic",
                   "narration": f"第{i}句旁白。", "prompt": f"prompt {i}",
                   "visual": f"画面位{i}", "asset_path": str(d / "assets" / "graphic" / f"shot-{i:03d}.png")}
                  for i in range(1, shots + 1)],
    }, ensure_ascii=False), encoding="utf-8")

    # 产物只落了一半：2 张图、1 段音、1 个片段 —— 用来验证"按盘上件数算进度"
    for i in (1, 2):
        (d / "assets" / "graphic" / f"shot-{i:03d}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (d / "audio" / "shot-001.mp3").write_bytes(b"ID3")
    (d / "audio" / "narration.mp3").write_bytes(b"ID3")     # 整轨不算进逐镜进度
    (d / "segments" / "shot-001.mp4").write_bytes(b"\x00\x00\x00\x18ftyp")
    (d / "segments" / "concat.txt").write_text("x", encoding="utf-8")   # 辅助文件也不算
    (d / "final.mp4").write_bytes(b"\x00\x00\x00\x18ftyp")
    return d


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.root = Path(td)

    def test_ignores_dirs_without_manifest(self) -> None:
        _task_fixture(self.root, "demo")
        (self.root / ".work" / "_fixtures").mkdir(parents=True)
        (self.root / ".work" / "_imagegen").mkdir(parents=True)
        names = [t.name for t in store.list_tasks(self.root)]
        self.assertEqual(names, ["demo"])

    def test_reads_stage_status_from_manifest(self) -> None:
        _task_fixture(self.root)
        task = store.read_task("demo", self.root)
        by = {s.name: s for s in task.stages}
        self.assertEqual(by["parse"].status, "done")
        self.assertEqual(by["assets"].status, "partial")
        self.assertEqual(by["voice"].status, "pending")     # manifest 里没有 → 未开始

    def test_progress_counts_files_on_disk_not_shots_json(self) -> None:
        """关键：assets 阶段只在**结束时**才写 shots.json。

        所以运行中必须数盘上产物，否则进度会在 0% 卡到结束（票据 36）。
        """
        _task_fixture(self.root)
        task = store.read_task("demo", self.root)
        by = {s.name: s for s in task.stages}
        self.assertEqual((by["assets"].done, by["assets"].total), (2, 3))
        self.assertEqual((by["voice"].done, by["voice"].total), (1, 3))
        self.assertEqual((by["build"].done, by["build"].total), (1, 3))

    def test_ignores_aggregate_and_aux_files(self) -> None:
        """`audio/narration.mp3`、`segments/concat.txt` 不是逐镜产物，不能算进进度。"""
        _task_fixture(self.root)
        task = store.read_task("demo", self.root)
        by = {s.name: s for s in task.stages}
        self.assertEqual(by["voice"].done, 1)
        self.assertEqual(by["build"].done, 1)

    def test_reads_final_and_duration(self) -> None:
        _task_fixture(self.root)
        task = store.read_task("demo", self.root)
        self.assertTrue(task.has_final)
        self.assertEqual(task.final_duration, 147.5)
        self.assertEqual(task.shots_total, 3)

    def test_corrupt_manifest_does_not_crash(self) -> None:
        d = _task_fixture(self.root)
        (d / "manifest.json").write_text("{ 这不是 json", encoding="utf-8")
        task = store.read_task("demo", self.root)     # 不能抛
        self.assertEqual(task.shots_total, 3)          # 还能从 shots.json 拿到

    def test_missing_task_returns_none(self) -> None:
        _task_fixture(self.root)
        self.assertIsNone(store.read_task("nope", self.root))

    def test_read_shots_and_editable_fields(self) -> None:
        _task_fixture(self.root)
        data = store.read_shots("demo", self.root)
        self.assertEqual(len(data["shots"]), 3)
        first = data["shots"][0]
        for key in ("prompt", "seed", "source", "narration"):
            self.assertIn(key, store.EDITABLE_SHOT_FIELDS)

    def test_task_dir_is_inside_work_root(self) -> None:
        """路径必须被钉死在 .work 里 —— 界面上的任何名字都不能跳出这个盒子。"""
        _task_fixture(self.root)
        self.assertIsNotNone(store.task_dir("demo", self.root))
        for evil in ("../..", "..", "demo/../..", "C:/Windows"):
            self.assertIsNone(store.task_dir(evil, self.root), evil)


class StageSpecTest(unittest.TestCase):
    def test_every_stage_has_spec(self) -> None:
        for stage in store.STAGES:
            self.assertIn(stage, jobs.STAGE_SPEC, stage)

    def test_argv_maps_options_to_flags(self) -> None:
        argv = jobs.build_argv("shots", "demo", {"visual": "photo", "no_llm": True, "force": False})
        self.assertEqual(argv[:4], [sys.executable, "-m", "lvs", "shots"])
        self.assertIn("--task", argv)
        self.assertIn("demo", argv)
        self.assertIn("--visual", argv)
        self.assertIn("photo", argv)
        self.assertIn("--no-llm", argv)
        self.assertNotIn("--force", argv)          # 未勾选的不传

    def test_argv_rejects_unknown_option(self) -> None:
        with self.assertRaises(jobs.JobError):
            jobs.build_argv("shots", "demo", {"rm_rf": True})

    def test_argv_rejects_bad_choice(self) -> None:
        with self.assertRaises(jobs.JobError):
            jobs.build_argv("shots", "demo", {"visual": "; rm -rf /"})

    def test_argv_never_shell_interpolates_task_name(self) -> None:
        """任务名进 argv 时不能被当命令执行（列表传参，不用 shell）。"""
        argv = jobs.build_argv("assets", "a; echo pwned", {})
        self.assertIn("a; echo pwned", argv)
        self.assertNotIn("shell", " ".join(argv))


class JobRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.root = Path(td)
        self.reg = jobs.JobRegistry(root=self.root)

    def _echo(self, text: str = "hello") -> list[str]:
        return [sys.executable, "-c", f"print({text!r})"]

    def test_runs_and_captures_lines(self) -> None:
        job = self.reg.start("demo", "assets", command=self._echo("ok-line"))
        self.assertTrue(job.wait(timeout=30))
        self.assertEqual(job.returncode, 0)
        self.assertIn("ok-line", "\n".join(job.lines()))

    def test_records_failure_returncode(self) -> None:
        job = self.reg.start("demo", "assets", command=[sys.executable, "-c", "raise SystemExit(3)"])
        job.wait(timeout=30)
        self.assertEqual(job.returncode, 3)
        self.assertFalse(job.ok)

    def test_only_one_job_at_a_time(self) -> None:
        """8GB 显存下生图与 TTS 不可并存（§11）→ 全局串行。"""
        first = self.reg.start("demo", "assets", command=[sys.executable, "-c", "import time; time.sleep(3)"])
        with self.assertRaises(jobs.JobBusy):
            self.reg.start("other", "voice", command=self._echo())
        self.reg.stop()
        first.wait(timeout=30)

    def test_stop_kills_and_frees_the_slot(self) -> None:
        job = self.reg.start("demo", "assets", command=[sys.executable, "-c", "import time; time.sleep(30)"])
        time.sleep(0.4)
        self.assertTrue(self.reg.stop())
        job.wait(timeout=20)
        self.assertNotEqual(job.returncode, 0)
        self.assertIsNone(self.reg.current())

    def test_current_is_none_when_idle(self) -> None:
        self.assertIsNone(self.reg.current())

    def test_subscribe_replays_history_then_live(self) -> None:
        job = self.reg.start("demo", "assets", command=[
            sys.executable, "-c", "import time; print('one'); time.sleep(0.3); print('two')",
        ])
        seen: list[str] = []
        for line in self.reg.stream(job.id, timeout=30):
            seen.append(line)
            if "two" in line:
                break
        self.assertIn("one", "\n".join(seen))
        self.assertIn("two", "\n".join(seen))

    def test_lines_are_bounded(self) -> None:
        """长任务的日志不能无限吃内存。"""
        job = self.reg.start("demo", "assets", command=[
            sys.executable, "-c", "for i in range(5000): print('line', i)",
        ])
        job.wait(timeout=60)
        self.assertLessEqual(len(job.lines()), jobs.MAX_LINES)


if __name__ == "__main__":
    unittest.main()


class RoutesTest(unittest.TestCase):
    """HTTP 层：页面能开、产物能取、越界取不到、编辑能落盘。"""

    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.root = Path(td)
        self.task = _task_fixture(self.root, "demo")
        from lvs.gui.app import create_app

        self.app = create_app(self.root)
        self.app.testing = True
        self.client = self.app.test_client()

    def test_pages_render(self) -> None:
        for url in ("/", "/task/demo", "/library", "/settings"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_unknown_task_is_404(self) -> None:
        self.assertEqual(self.client.get("/task/nope").status_code, 404)

    def test_task_api_reports_progress(self) -> None:
        data = self.client.get("/api/task/demo").get_json()
        by = {s["name"]: s for s in data["stages"]}
        self.assertEqual((by["assets"]["done"], by["assets"]["total"]), (2, 3))

    def test_shots_api(self) -> None:
        data = self.client.get("/api/task/demo/shots").get_json()
        self.assertEqual(data["count"], 3)
        self.assertTrue(data["shots"][0]["has_asset"])

    def test_stage_spec_api_exposes_options(self) -> None:
        data = self.client.get("/api/stage-spec").get_json()
        self.assertIn("shots", data)
        self.assertTrue(any(o["key"] == "visual" for o in data["shots"]))

    def test_edit_shot_persists(self) -> None:
        r = self.client.post("/api/task/demo/shots/1",
                             json={"prompt": "新的提示词", "narration": "新的旁白。"})
        self.assertEqual(r.status_code, 200)
        data = json.loads((self.task / "shots.json").read_text(encoding="utf-8"))
        self.assertEqual(data["shots"][0]["prompt"], "新的提示词")
        self.assertEqual(data["shots"][0]["narration"], "新的旁白。")

    def test_edit_shot_rejects_unknown_field(self) -> None:
        r = self.client.post("/api/task/demo/shots/1", json={"asset_path": "/etc/passwd"})
        self.assertEqual(r.status_code, 400)

    def test_edit_shot_rejects_unknown_shot(self) -> None:
        self.assertEqual(self.client.post("/api/task/demo/shots/999", json={"prompt": "x"}).status_code, 404)

    def test_media_serves_inside_task(self) -> None:
        r = self.client.get("/media/demo/segments/shot-001.mp4")
        self.assertEqual(r.status_code, 200)

    def test_media_blocks_traversal(self) -> None:
        """产物服务绝不能跳出任务目录（重定向也算没给 —— 关键是永不返回 200）。"""
        for bad in ("../../config.toml", "..%2F..%2Fconfig.toml", "/etc/passwd", "....//config.toml",
                    "../manifest.json", "..%5C..%5Cconfig.toml"):
            res = self.client.get(f"/media/demo/{bad}", follow_redirects=True)
            self.assertNotEqual(res.status_code, 200, bad)

    def test_create_task_saves_manuscript(self) -> None:
        r = self.client.post("/api/task", data={"task": "新任务 A", "text": "旁白一。\n旁白二。"})
        self.assertEqual(r.status_code, 200)
        name = r.get_json()["task"]
        saved = self.root / ".work" / name / "manuscript.md"
        self.assertTrue(saved.is_file())
        self.assertIn("旁白一", saved.read_text(encoding="utf-8"))

    def test_create_task_rejects_empty(self) -> None:
        r = self.client.post("/api/task", data={"task": "空的", "text": "   "})
        self.assertEqual(r.status_code, 400)

    def test_run_rejects_unknown_stage(self) -> None:
        r = self.client.post("/api/task/demo/run", json={"stage": "rm-rf", "options": {}})
        self.assertEqual(r.status_code, 400)

    def test_run_rejects_bad_option(self) -> None:
        r = self.client.post("/api/task/demo/run", json={"stage": "shots", "options": {"nope": 1}})
        self.assertEqual(r.status_code, 400)

    def test_run_without_manuscript_is_rejected(self) -> None:
        """parse 需要拍摄稿；没上传就点运行，要给一句人话而不是崩。"""
        r = self.client.post("/api/task/demo/run", json={"stage": "parse", "options": {}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("拍摄稿", r.get_json()["error"])

    def test_new_task_dir_name_is_slugified(self) -> None:
        """任务名不能靠用户自觉 —— 存盘前必须规整，且结果只能是 .work 下的一个目录名。"""
        r = self.client.post("/api/task", data={"task": "../逃逸", "text": "有内容。"})
        name = r.get_json()["task"]
        self.assertNotIn("..", name)
        self.assertNotIn("/", name)
        self.assertTrue((self.root / ".work" / name).is_dir())


class ConfigRoutesTest(unittest.TestCase):
    """配置编辑接口：能读、能写、能拒非法。"""

    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.root = Path(td)
        self.cfg = self.root / "config.toml"
        self.cfg.write_text(
            '[library]\ndirs = []\nmin_score = 1\n[shots]\nvisual_mode = "graphic"\n',
            encoding="utf-8")
        from lvs.gui.app import create_app

        self.app = create_app(self.root, config_path=self.cfg)
        self.app.testing = True
        self.client = self.app.test_client()

    def test_get_returns_editable_fields(self) -> None:
        data = self.client.get("/api/config").get_json()
        self.assertIn("library.dirs", data["fields"])
        self.assertEqual(data["fields"]["shots.visual_mode"]["value"], "graphic")

    def test_set_persists(self) -> None:
        r = self.client.post("/api/config", json={"library.dirs": ["D:\素材库"]})
        self.assertEqual(r.status_code, 200)
        self.assertIn("素材库", self.cfg.read_text(encoding="utf-8"))

    def test_set_rejects_bad_value(self) -> None:
        r = self.client.post("/api/config", json={"shots.visual_mode": "wat"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("只接受", r.get_json()["error"])

    def test_set_rejects_unknown_key(self) -> None:
        r = self.client.post("/api/config", json={"rm_rf": "x"})
        self.assertEqual(r.status_code, 400)

    def test_run_stage_available_for_one_click(self) -> None:
        spec = self.client.get("/api/stage-spec").get_json()
        self.assertIn("run", spec)


class DraftTaskTest(unittest.TestCase):
    """刚建的任务只有拍摄稿、还没 manifest，也必须出现在列表里（票 37 修复）。"""

    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.root = Path(td)

    def test_draft_task_is_listed(self) -> None:
        (self.root / ".work" / "_draft").mkdir(parents=True)
        (self.root / ".work" / "_draft" / "manuscript.md").write_text("x", encoding="utf-8")
        names = [t.name for t in store.list_tasks(self.root)]
        self.assertIn("_draft", names)

    def test_scratch_dirs_still_excluded(self) -> None:
        (self.root / ".work" / "_fixtures").mkdir(parents=True)
        (self.root / ".work" / "_fixtures" / "foo.md").write_text("x", encoding="utf-8")
        (self.root / ".work" / "_imagegen").mkdir(parents=True)
        self.assertEqual(store.list_tasks(self.root), [])


class UnbufferedSubprocessTest(unittest.TestCase):
    """子进程要逐行 flush，界面日志才真实时（票 37 修复）。"""

    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.reg = jobs.JobRegistry(root=Path(td))

    def test_child_gets_pythonunbuffered(self) -> None:
        job = self.reg.start("t", "assets", command=[
            sys.executable, "-c", "import os; print('PYTHONUNBUFFERED=' + os.environ.get('PYTHONUNBUFFERED',''))",
        ])
        job.wait(timeout=30)
        self.assertIn("PYTHONUNBUFFERED=1", "\n".join(job.lines()))


class ProgressUnknownTotalTest(unittest.TestCase):
    """总数未知时不许拿 done 凑成 100%（票 38 修复）。"""

    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.root = Path(td)

    def test_missing_shots_json_keeps_total_zero(self) -> None:
        d = self.root / ".work" / "t"
        (d / "assets" / "graphic").mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({"task": "t", "stages": {}}), encoding="utf-8")
        for i in (1, 2, 3):
            (d / "assets" / "graphic" / f"shot-{i:03d}.png").write_bytes(b"x")
        task = store.read_task("t", self.root)
        assets = next(s for s in task.stages if s.name == "assets")
        self.assertEqual(assets.done, 3)
        self.assertEqual(assets.total, 0, "总数未知就该是 0，不能等于 done")


class ReviewRound2FixesTest(unittest.TestCase):
    """票 38：第二轮审查遗留项的修复（S1/S2/S3/S5）。"""

    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.root = Path(td)
        self.cfg = self.root / "config.toml"
        self.cfg.write_text('[pexels]\napi_key = ""\n[library]\ndirs = []\n', encoding="utf-8")
        from lvs.gui.app import create_app

        self.app = create_app(self.root, config_path=self.cfg)
        self.app.testing = True
        self.client = self.app.test_client()
        self.registry = self.app.extensions["lvs_registry"]

    # ---- S1：缩略图新鲜度 ----

    def test_thumb_freshness_requires_strictly_newer(self) -> None:
        from lvs.gui import app as gui_app

        with tempfile.TemporaryDirectory() as td:
            cache, src = Path(td) / "c.jpg", Path(td) / "s.png"
            src.write_bytes(b"x")
            self.assertFalse(gui_app.thumb_is_fresh(cache, src), "缓存不存在 → 不新鲜")
            cache.write_bytes(b"y")
            os.utime(cache, (src.stat().st_atime, src.stat().st_mtime))     # 同秒 → 视为过期
            self.assertFalse(gui_app.thumb_is_fresh(cache, src), "同 mtime 必须当作过期")
            os.utime(cache, (src.stat().st_atime + 10, src.stat().st_mtime + 10))
            self.assertTrue(gui_app.thumb_is_fresh(cache, src), "缓存更新 → 新鲜")

    # ---- S2：不静默覆盖拍摄稿 ----

    def test_create_then_recreate_needs_overwrite(self) -> None:
        first = self.client.post("/api/task", data={"task": "t1", "text": "老稿。"})
        self.assertEqual(first.status_code, 200)
        again = self.client.post("/api/task", data={"task": "t1", "text": "新稿。"})
        self.assertEqual(again.status_code, 409)
        self.assertTrue(again.get_json()["exists"])
        self.assertIn("老稿", (self.root / ".work" / "t1" / "manuscript.md").read_text(encoding="utf-8"))

        forced = self.client.post("/api/task", data={"task": "t1", "text": "新稿。", "overwrite": "1"})
        self.assertEqual(forced.status_code, 200)
        self.assertIn("新稿", (self.root / ".work" / "t1" / "manuscript.md").read_text(encoding="utf-8"))

    def test_invalid_submission_leaves_no_stray_dir(self) -> None:
        r = self.client.post("/api/task", data={"task": "空稿", "text": "   "})
        self.assertEqual(r.status_code, 400)
        self.assertFalse((self.root / ".work" / "空稿").exists(), "无效提交不该建目录")

    # ---- S3：没 key 时一键到底得能跑 ----

    def test_demote_pexels_pure(self) -> None:
        from lvs.gui.app import demote_pexels

        data = {"shots": [{"id": 1, "source": "pexels"}, {"id": 2, "source": "local"}]}
        self.assertEqual(demote_pexels(data, has_pexels_key=True), 0)
        self.assertEqual(data["shots"][0]["source"], "pexels", "有 key 就不该动它")
        self.assertEqual(demote_pexels(data, has_pexels_key=False), 1)
        self.assertEqual(data["shots"][0]["source"], "local")
        self.assertEqual(data["shots"][1]["source"], "local", "本来不是 pexels 的不受影响")

    def test_run_returns_note_and_flips(self) -> None:
        task = self.root / ".work" / "t2"
        task.mkdir(parents=True)
        (task / "manuscript.md").write_text("旁白。", encoding="utf-8")
        (task / "shots.json").write_text(json.dumps({
            "shots": [{"id": 1, "source": "pexels"}, {"id": 2, "source": "graphic"}],
        }, ensure_ascii=False), encoding="utf-8")

        started: dict = {}

        def fake_start(name, stage, options=None, **kw):
            started.update(name=name, stage=stage, options=options, kw=kw)
            return _FakeJob(name, stage)

        self.registry.start = fake_start            # 不真的起子进程
        res = self.client.post("/api/task/t2/run", json={"stage": "run", "options": {}})
        self.assertEqual(res.status_code, 200)
        self.assertIn("pexels", res.get_json()["note"])
        self.assertEqual(started["stage"], "run")
        shots = json.loads((task / "shots.json").read_text(encoding="utf-8"))["shots"]
        self.assertEqual(shots[0]["source"], "local", "pexels 镜应已改为本地生图")

    def test_single_stage_run_does_not_flip(self) -> None:
        """只有「一键到底」才需要这个预处理；单跑某阶段别偷偷改数据。"""
        task = self.root / ".work" / "t3"
        task.mkdir(parents=True)
        (task / "shots.json").write_text(json.dumps({"shots": [{"id": 1, "source": "pexels"}]}), encoding="utf-8")
        self.registry.start = lambda *a, **k: _FakeJob("t3", "assets")
        self.client.post("/api/task/t3/run", json={"stage": "assets", "options": {}})
        shots = json.loads((task / "shots.json").read_text(encoding="utf-8"))["shots"]
        self.assertEqual(shots[0]["source"], "pexels")


class _FakeJob:
    """替身：只提供路由用得到的两个方法。"""

    def __init__(self, task: str, stage: str) -> None:
        self.task, self.stage, self.id = task, stage, "fake"

    def as_dict(self) -> dict:
        return {"id": self.id, "task": self.task, "stage": self.stage, "running": True,
                "returncode": None, "ok": False, "elapsed": 0.0, "error": "", "lines": 0,
                "log_path": ""}


class JobLogPersistenceTest(unittest.TestCase):
    """S5：日志要落盘，服务重启也不丢。"""

    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.root = Path(td)
        self.reg = jobs.JobRegistry(root=self.root)

    def test_log_written_under_task_logs(self) -> None:
        job = self.reg.start("mytask", "assets", command=[
            sys.executable, "-c", "print('落盘测试')",
        ])
        job.wait(timeout=30)
        logs = list((self.root / ".work" / "mytask" / "logs").glob("*.log"))
        self.assertEqual(len(logs), 1)
        text = logs[0].read_text(encoding="utf-8")
        self.assertIn("落盘测试", text)
        self.assertIn("assets", logs[0].name)
        self.assertTrue(job.as_dict()["log_path"])

    def test_unsafe_task_name_skips_logging(self) -> None:
        """任务名带路径分隔符就不落盘 —— 日志文件不能写到任务目录外。"""
        job = self.reg.start("../evil", "assets", command=[sys.executable, "-c", "print('x')"])
        job.wait(timeout=30)
        self.assertEqual(job.as_dict()["log_path"], "")
        self.assertFalse((self.root / "evil").exists())


class PickImageTest(unittest.TestCase):
    """票 40：自己挑一张图替换某一镜。"""

    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.root = Path(td)
        self.cfg = self.root / "config.toml"
        self.cfg.write_text('[pexels]\napi_key = ""\n', encoding="utf-8")
        from lvs.gui.app import create_app

        self.app = create_app(self.root, config_path=self.cfg)
        self.app.testing = True
        self.client = self.app.test_client()
        self.registry = self.app.extensions["lvs_registry"]

        self.task = self.root / ".work" / "t"
        self.task.mkdir(parents=True)
        (self.task / "shots.json").write_text(json.dumps({
            "shots": [{"id": 1, "source": "local", "status": "done"}],
        }, ensure_ascii=False), encoding="utf-8")
        self.started: dict = {}

        def fake_start(name, stage, options=None, **kw):
            self.started.update(name=name, stage=stage, options=options or {})
            return _FakeJob(name, stage)

        self.registry.start = fake_start

    @staticmethod
    def _png() -> bytes:
        from io import BytesIO

        from PIL import Image

        buf = BytesIO()
        Image.new("RGB", (320, 180), (200, 90, 65)).save(buf, "PNG")
        return buf.getvalue()

    def _shots(self) -> dict:
        return json.loads((self.task / "shots.json").read_text(encoding="utf-8"))["shots"][0]

    def test_pick_saves_copy_and_pins(self) -> None:
        from io import BytesIO

        res = self.client.post(
            "/api/task/t/shot/1/pick",
            data={"image": (BytesIO(self._png()), "my-photo.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 200)
        picked = Path(res.get_json()["picked"])
        self.assertTrue(picked.is_file(), "原图副本要落盘")
        self.assertEqual(picked.parent.name, "picked")
        self.assertEqual(picked.read_bytes(), self._png())

        shot = self._shots()
        self.assertEqual(shot["library_asset"], str(picked), "library_asset 要指向副本（钉死）")
        self.assertEqual(shot["source"], "library", "钉死的镜按 library 分支走")

        # 必须走 assets 材化 —— 走 studio --redo 会被它的 clear_shot_assets
        # pop 掉 library_asset，等于刚钉上就被清掉（票 40 踩过）
        self.assertEqual(self.started["stage"], "assets")
        self.assertEqual(self.started["options"].get("only"), "library")

    def test_pick_rejects_non_image(self) -> None:
        from io import BytesIO

        res = self.client.post(
            "/api/task/t/shot/1/pick",
            data={"image": (BytesIO(b"this is not an image"), "fake.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("读不出来", res.get_json()["error"])

    def test_pick_rejects_bad_suffix(self) -> None:
        from io import BytesIO

        res = self.client.post(
            "/api/task/t/shot/1/pick",
            data={"image": (BytesIO(self._png()), "note.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("只收这些格式", res.get_json()["error"])

    def test_pick_rejects_empty_file(self) -> None:
        from io import BytesIO

        res = self.client.post(
            "/api/task/t/shot/1/pick",
            data={"image": (BytesIO(b""), "empty.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 400)

    def test_pick_rejects_unknown_shot(self) -> None:
        from io import BytesIO

        res = self.client.post(
            "/api/task/t/shot/99/pick",
            data={"image": (BytesIO(self._png()), "a.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 404)

    def test_pick_clears_stale_artifacts(self) -> None:
        """不清旧产物 = existing_asset 认为"已有"，新钉的图永远材化不出来。"""
        from io import BytesIO

        seg = self.task / "segments"
        seg.mkdir(parents=True, exist_ok=True)
        (seg / "shot-001.mp4").write_bytes(b"old segment")
        old = self.task / "assets" / "graphic"
        old.mkdir(parents=True, exist_ok=True)
        (old / "shot-001.png").write_bytes(b"old card")

        self.client.post("/api/task/t/shot/1/pick",
                         data={"image": (BytesIO(self._png()), "a.png")},
                         content_type="multipart/form-data")
        self.assertFalse((seg / "shot-001.mp4").exists(), "片段要删，否则 build 复用旧帧")
        self.assertFalse((old / "shot-001.png").exists(), "旧素材要删")

    def test_pick_rejected_while_busy_leaves_data_untouched(self) -> None:
        """忙的时候必须**什么都不改** —— 不能改了 shots.json 却起不了 job。"""
        from io import BytesIO

        before = (self.task / "shots.json").read_text(encoding="utf-8")
        self.registry.current = lambda: _FakeJob("t", "assets")
        res = self.client.post(
            "/api/task/t/shot/1/pick",
            data={"image": (BytesIO(self._png()), "a.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual((self.task / "shots.json").read_text(encoding="utf-8"), before)
        self.assertFalse((self.task / "picked").exists(), "被拒时不该留下文件")

    def test_shots_api_exposes_pin(self) -> None:
        data = self.client.get("/api/task/t/shots").get_json()
        self.assertIn("pinned", data["shots"][0])
