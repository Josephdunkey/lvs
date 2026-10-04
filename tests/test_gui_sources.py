"""来源策略在界面上的接口（票 41）：任务页选择器 + 一键到底要带上它。"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lvs.gui import jobs


def _task(root: Path, name: str, *, source_mode: str | None = "auto", shots: list | None = None) -> Path:
    d = root / ".work" / name
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"task": name, "stages": {}}), encoding="utf-8")
    data: dict = {"title": "演示", "shots": shots if shots is not None else [
        {"id": 1, "source": "pexels", "kind": "scene"},
    ]}
    if source_mode is not None:
        data["source_mode"] = source_mode
    (d / "shots.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (d / "manuscript.md").write_text("旁白一。\n", encoding="utf-8")   # `run` 需要它当位置参数
    return d


class _FakeJob:
    def __init__(self, task: str, stage: str) -> None:
        self.task, self.stage, self.id = task, stage, "fake"

    def as_dict(self) -> dict:
        return {"id": self.id, "task": self.task, "stage": self.stage, "running": True,
                "returncode": None, "ok": False, "elapsed": 0.0, "error": "", "lines": 0,
                "log_path": ""}


class SourceModeRouteTest(unittest.TestCase):
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

    def test_task_api_exposes_the_mode(self) -> None:
        _task(self.root, "a", source_mode="library")
        data = self.client.get("/api/task/a").get_json()
        self.assertEqual(data["source_mode"], "library")

    def test_task_api_defaults_to_auto(self) -> None:
        _task(self.root, "b", source_mode=None)
        self.assertEqual(self.client.get("/api/task/b").get_json()["source_mode"], "auto")

    def test_set_persists(self) -> None:
        d = _task(self.root, "c", source_mode="auto")
        r = self.client.post("/api/task/c/source-mode", json={"mode": "local"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["persisted"])
        saved = json.loads((d / "shots.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["source_mode"], "local")
        self.assertEqual(saved["shots"][0]["source"], "pexels",
                         "只记意向，逐镜 source 由 assets 阶段去落 —— 界面不替它做")

    def test_set_before_shots_says_not_persisted(self) -> None:
        d = self.root / ".work" / "draft"
        (d).mkdir(parents=True)
        (d / "manuscript.md").write_text("旁白。", encoding="utf-8")
        r = self.client.post("/api/task/draft/source-mode", json={"mode": "local"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["persisted"], "还没拆镜就如实说没处记")

    def test_set_rejects_garbage(self) -> None:
        _task(self.root, "e")
        r = self.client.post("/api/task/e/source-mode", json={"mode": "magic"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("来源策略", r.get_json()["error"])

    def test_set_unknown_task_is_404(self) -> None:
        self.assertEqual(self.client.post("/api/task/nope/source-mode", json={"mode": "local"}).status_code, 404)

    def test_stage_spec_exposes_source_everywhere_needed(self) -> None:
        spec = self.client.get("/api/stage-spec").get_json()
        for stage in ("shots", "assets", "run", "studio"):
            self.assertTrue(
                any(o["key"] == "source" for o in spec[stage]),
                f"{stage} 应该能选来源策略",
            )

    # ---- 一键到底：不能悄悄推翻用户选的来源 ----

    def test_run_with_explicit_source_is_not_auto_demoted(self) -> None:
        """选了 Pexels 但没配 key —— 界面不许背着用户把镜改成生图。"""
        d = _task(self.root, "f", source_mode="auto", shots=[{"id": 1, "source": "pexels"}])
        self.registry.start = lambda *a, **k: _FakeJob("f", "run")
        res = self.client.post("/api/task/f/run", json={"stage": "run", "options": {"source": "pexels"}})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["note"], "")
        saved = json.loads((d / "shots.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["shots"][0]["source"], "pexels", "source 不该被动过")

    def test_run_in_auto_mode_still_demotes(self) -> None:
        """自动策略下那条兜底仍然要生效，否则一键到底会卡在素材阶段。"""
        d = _task(self.root, "g", source_mode="auto", shots=[{"id": 1, "source": "pexels"}])
        self.registry.start = lambda *a, **k: _FakeJob("g", "run")
        res = self.client.post("/api/task/g/run", json={"stage": "run", "options": {}})
        self.assertIn("pexels", res.get_json()["note"])
        saved = json.loads((d / "shots.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["shots"][0]["source"], "local")

    def test_run_uses_the_recorded_mode_when_options_are_silent(self) -> None:
        d = _task(self.root, "h", source_mode="pexels", shots=[{"id": 1, "source": "pexels"}])
        self.registry.start = lambda *a, **k: _FakeJob("h", "run")
        res = self.client.post("/api/task/h/run", json={"stage": "run", "options": {}})
        self.assertEqual(res.get_json()["note"], "", "任务记着 pexels，就不该被自动改成生图")
        saved = json.loads((d / "shots.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["shots"][0]["source"], "pexels")


class BuildArgvSourceTest(unittest.TestCase):
    def test_source_flag_is_forwarded(self) -> None:
        argv = jobs.build_argv("assets", "t", {"source": "local"})
        self.assertIn("--source", argv)
        self.assertEqual(argv[argv.index("--source") + 1], "local")

    def test_source_rejects_unknown_value(self) -> None:
        with self.assertRaises(jobs.JobError):
            jobs.build_argv("assets", "t", {"source": "magic"})

    def test_stages_that_do_not_know_source_reject_it(self) -> None:
        with self.assertRaises(jobs.JobError):
            jobs.build_argv("voice", "t", {"source": "local"})


class PinnedShotSurvivesModesTest(unittest.TestCase):
    """钉死的镜必须打上 `source_pinned`（票 41 复审修的真 bug）。

    不打标记的后果：用户换成「本地生图」重跑时，`sources.apply_mode` 把这镜当成
    可重定向的镜，`library_asset`（他挑的那张图）会被 `_STALE_FIELDS` 一起清掉 ——
    界面上的承诺是"之后重跑素材也不会被覆盖"。
    """

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
        self.registry.start = lambda *a, **k: _FakeJob("t", "assets")
        self.task = _task(self.root, "t", source_mode="auto", shots=[
            {"id": 1, "source": "local", "kind": "scene"},
        ])

    @staticmethod
    def _png() -> bytes:
        from io import BytesIO

        from PIL import Image

        buf = BytesIO()
        Image.new("RGB", (320, 180), (200, 90, 65)).save(buf, "PNG")
        return buf.getvalue()

    def _pick(self) -> dict:
        from io import BytesIO

        r = self.client.post(
            "/api/task/t/shot/1/pick",
            data={"image": (BytesIO(self._png()), "mine.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(r.status_code, 200, r.get_json())
        return json.loads((self.task / "shots.json").read_text(encoding="utf-8"))["shots"][0]

    def test_pick_marks_the_shot_as_pinned(self) -> None:
        shot = self._pick()
        self.assertTrue(shot["source_pinned"], "钉图必须打标记，否则策略会把它当普通镜")

    def test_pinned_shot_survives_forcing_local(self) -> None:
        from lvs import sources

        shot = self._pick()
        pinned = shot["library_asset"]
        data = json.loads((self.task / "shots.json").read_text(encoding="utf-8"))
        self.assertEqual(sources.apply_mode(data, "local"), 0, "钉死的镜不该被算作「改了来源」")
        after = data["shots"][0]
        self.assertEqual(after["source"], "library")
        self.assertEqual(after["library_asset"], pinned, "他挑的图不许被清掉")

    def test_pinned_shot_is_still_reported_by_the_api(self) -> None:
        self._pick()
        shots = self.client.get("/api/task/t/shots").get_json()["shots"]
        self.assertEqual(shots[0]["source"], "library")
        self.assertTrue(shots[0]["pinned"])


if __name__ == "__main__":
    unittest.main()
