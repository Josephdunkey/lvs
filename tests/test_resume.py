"""断点续跑 / 阶段状态契约（spec §7.1，票据 16）—— 回归测试。

踩过的坑（code-review 查出）：
1. `assets` 阶段把 `outputs` 记成 `[shots.json]`（恒存在），于是删掉 `assets/*`
   也不会触发重跑；run 的阶段关卡同样只看 shots.json。
2. 有失败镜时阶段仍标 `done` → `lvs run` 整段跳过 → 失败镜**永不补**。

契约：阶段 `done` 的充要条件是「状态为 done（不是 partial）」**且**「manifest 里
记录的所有 outputs 都还在」。`partial`（存在失败镜）一律视作未完成，可被重跑补缺。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from lvs import assets as assets_mod
from lvs import build as build_mod
from lvs import tts as tts_mod
from lvs.config import Config
from lvs.workspace import Workspace, stage_status


def _cfg(**kw) -> Config:
    data: dict = {
        "library": {"dirs": [], "min_score": 1},
        "pexels": {"api_key": ""},
        "comfyui": {"base_url": "http://127.0.0.1:8188"},
        "tts": {"backend": "edge", "voice": "zh-CN-YunxiNeural"},
        "voice": {"gap": 0.3, "subtitle_max_chars": 18},
    }
    for section, values in kw.items():
        data.setdefault(section, {}).update(values)
    return Config(data, None)


def _write_shots(ws: Workspace, shots: list[dict]) -> None:
    ws.path("shots.json").write_text(
        json.dumps({"title": "t", "count": len(shots), "shots": shots}, ensure_ascii=False),
        encoding="utf-8",
    )


def _shot(sid: int, source: str = "library", **extra) -> dict:
    base = {
        "id": sid, "segment": 1, "segment_heading": "h",
        "narration": f"第{sid}句旁白。", "visual": "画面", "source": source,
        "prompt": "p", "keywords": ["k"], "library_asset": None,
        "resolved_by": None, "status": "pending", "start": None, "end": None,
    }
    base.update(extra)
    return base


class TestStageStatusHelper(unittest.TestCase):
    def test_no_failures_is_done(self) -> None:
        self.assertEqual(stage_status(0), "done")

    def test_any_failure_is_partial(self) -> None:
        self.assertEqual(stage_status(1), "partial")
        self.assertEqual(stage_status(37), "partial")


class TestWorkspaceStageContract(unittest.TestCase):
    def test_is_stage_done_uses_recorded_outputs_when_none_passed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(task="t", root=Path(td)).ensure()
            p = ws.path("assets", "local", "shot-001.png")
            p.write_bytes(b"img")
            ws.mark_stage("assets", outputs=[p])
            self.assertTrue(ws.is_stage_done("assets"))
            p.unlink()  # 删掉产物 → 应当重跑
            self.assertFalse(ws.is_stage_done("assets"))

    def test_partial_status_is_never_done(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(task="t", root=Path(td)).ensure()
            p = ws.path("assets", "local", "shot-001.png")
            p.write_bytes(b"img")
            ws.mark_stage("assets", outputs=[p], status="partial", failed=1)
            self.assertFalse(ws.is_stage_done("assets"))

    def test_missing_stage_is_not_done(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(task="t", root=Path(td)).ensure()
            self.assertFalse(ws.is_stage_done("assets"))

    def test_explicit_outputs_still_honoured(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(task="t", root=Path(td)).ensure()
            p = ws.path("final.mp4")
            p.write_bytes(b"v")
            ws.mark_stage("build", outputs=[p])
            self.assertTrue(ws.is_stage_done("build", [p]))
            self.assertFalse(ws.is_stage_done("build", [ws.path("nope.mp4")]))


class TestAssetsStageContract(unittest.TestCase):
    """assets 必须记录**每镜产物**，并在有失败镜时标 partial。"""

    def _run(self, td: str, shots: list[dict], outcomes: dict[int, assets_mod.Resolution]):
        ws = Workspace(task="t", root=Path(td)).ensure()
        _write_shots(ws, shots)

        real_resolve, real_existing = assets_mod.resolve_shot, assets_mod.existing_asset
        assets_mod.resolve_shot = lambda shot, **kw: outcomes[int(shot["id"])]  # type: ignore[assignment]
        assets_mod.existing_asset = lambda ws_, sid: None  # type: ignore[assignment]
        try:
            rc = assets_mod.run_command(
                _cfg(), ws, Namespace(force=True, no_library=True, task="t"))
        finally:
            assets_mod.resolve_shot, assets_mod.existing_asset = real_resolve, real_existing
        return ws, rc

    def test_all_success_records_every_asset_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p1 = Path(td) / ".work/t/assets/local/shot-001.png"
            p2 = Path(td) / ".work/t/assets/local/shot-002.png"
            p1.parent.mkdir(parents=True, exist_ok=True)
            p1.write_bytes(b"a")
            p2.write_bytes(b"b")
            outcomes = {
                1: assets_mod.Resolution(True, "local", p1),
                2: assets_mod.Resolution(True, "local", p2),
            }
            ws, rc = self._run(td, [_shot(1), _shot(2)], outcomes)
            self.assertEqual(rc, 0)
            entry = ws.manifest["stages"]["assets"]
            self.assertEqual(entry["status"], "done")
            self.assertIn(str(p1), entry["outputs"])
            self.assertIn(str(p2), entry["outputs"])
            # 删掉一个产物 → 该阶段应判定为未完成
            p2.unlink()
            self.assertFalse(ws.is_stage_done("assets"))

    def test_one_failure_marks_partial_and_failed_shot_missing_from_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p1 = Path(td) / ".work/t/assets/local/shot-001.png"
            p1.parent.mkdir(parents=True, exist_ok=True)
            p1.write_bytes(b"a")
            outcomes = {
                1: assets_mod.Resolution(True, "local", p1),
                2: assets_mod.Resolution(False, reason="ComfyUI 不可达"),
            }
            ws, rc = self._run(td, [_shot(1), _shot(2)], outcomes)
            # ★ 退出码 1 = **有失败件**（逐镜隔离）。「不中断」改由 `run.py` 保证：
            #   它收到 1 会**继续**跑后面的阶段，最后汇总返回 1。
            #   所以「隔离」的含义从「返回 0」变成了「run 会继续」，语义没丢。
            self.assertEqual(rc, 1)
            entry = ws.manifest["stages"]["assets"]
            self.assertEqual(entry["status"], "partial")
            self.assertEqual(entry["failed"], 1)
            # partial → run 会重跑该阶段，补上失败的那一镜
            self.assertFalse(ws.is_stage_done("assets"))


class TestBranchScoping(unittest.TestCase):
    """改 source 后重跑，不得误用上一次留在别的分支里的旧文件（§8.1）。"""

    def test_branches_track_source(self) -> None:
        self.assertEqual(assets_mod.branches_for(_shot(1, "library")), ("library",))
        self.assertEqual(assets_mod.branches_for(_shot(1, "pexels")), ("library", "pexels"))
        self.assertEqual(assets_mod.branches_for(_shot(1, "local")), ("library", "local"))
        self.assertEqual(assets_mod.branches_for(_shot(1, "local"), allow_library=False), ("local",))
        pinned = _shot(1, "local", library_asset="/some/where/x.png")
        self.assertEqual(assets_mod.branches_for(pinned), ("library",))

    def test_stale_other_branch_file_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(task="t", root=Path(td)).ensure()
            # 上一次是 pexels，留下一个 mp4；这次 source 改成 local
            stale = ws.path("assets", "pexels", "shot-001.mp4")
            stale.write_bytes(b"old pexels video")
            self.assertIsNone(assets_mod.existing_asset(ws, _shot(1, "local")))
            # 但该镜 source 仍是 pexels 时，它是有效产物
            self.assertEqual(assets_mod.existing_asset(ws, _shot(1, "pexels")), stale)

    def test_matching_branch_file_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(task="t", root=Path(td)).ensure()
            p = ws.path("assets", "local", "shot-001.png")
            p.write_bytes(b"img")
            self.assertEqual(assets_mod.existing_asset(ws, _shot(1, "local")), p)


class TestBuildTimeline(unittest.TestCase):
    """配音失败的分镜（start 为 None）不得进画面轨。"""

    def test_shots_with_audio_filters_silent(self) -> None:
        shots = [
            {"id": 1, "start": 0.0, "end": 3.0},
            {"id": 2, "start": None, "end": None},
            {"id": 3, "start": 3.3, "end": 6.0},
        ]
        keep, silent = build_mod.shots_with_audio(shots)
        self.assertEqual([s["id"] for s in keep], [1, 3])
        self.assertEqual(silent, [2])


class _StubBackend:
    name = "edge"

    class _Res:
        def __init__(self, path: Path):
            self.path = path
            self.duration = 1.5
            self.boundaries = []

    def synthesize(self, text: str, out: Path, voice: str):  # noqa: ANN001
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"audio")
        return self._Res(out)


class TestVoiceStageContract(unittest.TestCase):
    """voice 同样按镜记录产物，并在有失败镜时标 partial。"""

    def test_voice_partial_and_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(task="t", root=Path(td)).ensure()
            _write_shots(ws, [_shot(1), _shot(2)])

            real_make, real_concat = tts_mod.make_backend, tts_mod.concat_track
            real_srt, real_avail = tts_mod.build_srt, tts_mod.whisper_available

            def fake_make(config):  # noqa: ANN001
                return _StubBackend()

            class _Flaky(_StubBackend):
                def synthesize(self, text, out, voice):  # noqa: ANN001
                    if "2" in text:
                        raise tts_mod.TTSError("No audio was received")
                    return super().synthesize(text, out, voice)

            tts_mod.make_backend = lambda config: _Flaky()  # type: ignore[assignment]
            tts_mod.concat_track = lambda ws_, shots, gap: _touch(ws_.path("audio", "narration.mp3"))  # type: ignore[assignment]
            tts_mod.build_srt = lambda *a, **k: "1\n00:00:00,000 --> 00:00:01,500\n第1句\n"  # type: ignore[assignment]
            tts_mod.whisper_available = lambda: False  # type: ignore[assignment]
            try:
                rc = tts_mod.run_command(_cfg(), ws, Namespace(force=True, task="t"))
            finally:
                tts_mod.make_backend, tts_mod.concat_track = real_make, real_concat
                tts_mod.build_srt, tts_mod.whisper_available = real_srt, real_avail

            # ★ 有失败件 → 退出码 1（`run.py` 收到 1 仍会继续跑后面的阶段）
            self.assertEqual(rc, 1)
            entry = ws.manifest["stages"]["voice"]
            self.assertEqual(entry["status"], "partial")
            self.assertEqual(entry["failed"], 1)
            self.assertIn(str(ws.path("audio", "shot-001.mp3")), entry["outputs"])
            self.assertFalse(ws.is_stage_done("voice"))


class TestBuildStageContract(unittest.TestCase):
    """build 有占位片段时必须标 partial —— 否则 `lvs run` 会整段跳过，D17 补渲失效。

    回归：`build` 曾把「有占位」记成 `done`（唯一不传 status 的阶段），而 `is_stage_done`
    只看 `status`，于是 `run` 再也不重跑 build、「先出草稿、后补素材」永远补不上画面。
    """

    def _run(self, td: str, placeholders: list[int], final_ok: bool = True):
        ws = Workspace(task="t", root=Path(td)).ensure()
        _write_shots(ws, [_shot(1, start=0.0, end=2.0, asset_path=None)])
        _touch(ws.path("audio", "narration.mp3"))
        seg = _touch(ws.path("segments", "shot-001.mp4"))
        final = ws.path("final.mp4")

        names = ("tools", "build_segments", "concat_segments", "finalize", "ff_duration")
        real = {n: getattr(build_mod, n) for n in names}
        build_mod.tools = lambda: ("ffmpeg", "ffprobe")  # type: ignore[assignment]
        build_mod.build_segments = lambda ws_, shots, config, force, gap=0.0: ([seg], placeholders)  # type: ignore[assignment]
        build_mod.concat_segments = lambda ws_, segs: _touch(ws_.path("video-track.mp4"))  # type: ignore[assignment]
        build_mod.finalize = lambda ws_, track, narr, srt, config: (_touch(final), False)  # type: ignore[assignment]
        if final_ok:
            build_mod.ff_duration = lambda p: 2.0  # type: ignore[assignment]
        else:
            # 空成片：track/narration 正常，但 final.mp4 读不出时长（0 帧空壳）
            build_mod.ff_duration = lambda p: None if p.name == "final.mp4" else 2.0  # type: ignore[assignment]
        try:
            rc = build_mod.run_command(_cfg(), ws, Namespace(force=True, task="t"))
        finally:
            for n, v in real.items():
                setattr(build_mod, n, v)
        return ws, rc

    def test_placeholders_mark_partial_and_block_skip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, rc = self._run(td, [7])
            # ★ 有占位镜（缺素材）也算**有失败件** → 退出码 1（成片仍产出）
            self.assertEqual(rc, 1)
            entry = ws.manifest["stages"]["build"]
            self.assertEqual(entry["status"], "partial")
            self.assertEqual(entry["placeholders"], 1)
            self.assertFalse(ws.is_stage_done("build"), "有占位就不算完成，run 必须重跑补渲")

    def test_no_placeholders_is_done(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws, _ = self._run(td, [])
            entry = ws.manifest["stages"]["build"]
            self.assertEqual(entry["status"], "done")
            self.assertEqual(entry["placeholders"], 0)
            self.assertTrue(ws.is_stage_done("build"))

    def test_unreadable_final_is_rejected(self) -> None:
        # D23：final.mp4 也要复读；空成片不得被记成成功
        with tempfile.TemporaryDirectory() as td:
            ws, rc = self._run(td, [], final_ok=False)
            self.assertEqual(rc, 2)
            self.assertNotIn("build", ws.manifest.get("stages", {}))


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"track")
    return p


if __name__ == "__main__":
    unittest.main()
