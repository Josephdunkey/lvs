"""build 阶段单元测试（票据 13/14/15）。多数只测滤镜串与编排逻辑；段渲染与产物校验会真跑 ffmpeg。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lvs import build


class KenBurnsTest(unittest.TestCase):
    def test_modes_produce_zoompan(self):
        for mode in ("zoom-in", "zoom-out", "pan-right", "pan-up"):
            vf = build.kenburns_filter(mode, 0.15, 90, 1920, 1080, 30)
            self.assertIn("zoompan", vf, mode)
            self.assertIn("fps=30", vf, mode)

    def test_none_mode_no_zoompan(self):
        vf = build.kenburns_filter("none", 0.15, 90, 1920, 1080, 30)
        self.assertNotIn("zoompan", vf)
        self.assertIn("crop=1920:1080", vf)

    def test_frames_in_filter(self):
        vf = build.kenburns_filter("zoom-in", 0.2, 120, 1280, 720, 30)
        self.assertIn("d=120", vf)


class KenBurnsSupersampleTest(unittest.TestCase):
    """票据 29：zoompan 的整数量化是抖动根源 → 高分辨率算、lanczos 降回，压到亚像素。"""

    def test_supersample_1_is_legacy_chain(self):
        vf = build.kenburns_filter("zoom-in", 0.15, 100, 1920, 1080, 30, supersample=1)
        self.assertIn("scale=3840:2160", vf)   # 预放大 2×
        self.assertIn("s=1920x1080", vf)       # zoompan 直接出成片尺寸
        self.assertNotIn("lanczos", vf)

    def test_default_renders_4k_then_downscales(self):
        vf = build.kenburns_filter("zoom-in", 0.15, 100, 1920, 1080, 30)
        self.assertIn("s=3840x2160", vf)       # zoompan 在 4K 上算
        self.assertIn("scale=1920:1080:flags=lanczos", vf)

    def test_supersample_4_also_raises_prescale(self):
        vf = build.kenburns_filter("zoom-in", 0.15, 100, 1920, 1080, 30, supersample=4)
        self.assertIn("scale=7680:4320", vf)   # 预放大 4×（内存上限）
        self.assertIn("s=3840x2160", vf)

    def test_supersample_capped_at_4(self):
        vf = build.kenburns_filter("zoom-in", 0.15, 100, 1920, 1080, 30, supersample=99)
        self.assertIn("scale=7680:4320", vf)
        self.assertIn("s=3840x2160", vf)

    def test_none_mode_ignores_supersample(self):
        vf = build.kenburns_filter("none", 0.15, 100, 1920, 1080, 30, supersample=4)
        self.assertNotIn("zoompan", vf)
        self.assertNotIn("lanczos", vf)


class MotionSequenceTest(unittest.TestCase):
    """票据 45：同素材的相邻镜要把运镜方向对调，否则接缝处「推到 1.15 → 跳回 1.0 → 再推」。

    `zoom-in` 的 z 是 1.0 → 1+amount，`zoom-out` 恰好 1+amount → 1.0，
    所以推入接拉出时接缝处缩放值相同，动作是连续的。
    """

    def _shot(self, sid, asset, kind="scene"):
        return {"id": sid, "asset_path": asset, "kind": kind}

    def test_same_asset_neighbor_flips(self):
        shots = [self._shot(1, "a.png"), self._shot(2, "a.png")]
        self.assertEqual(build.motion_sequence(shots, "zoom-in", 0.15), ["zoom-in", "zoom-out"])

    def test_three_in_a_row_alternates(self):
        shots = [self._shot(i, "a.png") for i in (1, 2, 3)]
        self.assertEqual(
            build.motion_sequence(shots, "zoom-in", 0.15),
            ["zoom-in", "zoom-out", "zoom-in"],
        )

    def test_zoom_out_base_flips_to_zoom_in(self):
        shots = [self._shot(1, "a.png"), self._shot(2, "a.png")]
        self.assertEqual(build.motion_sequence(shots, "zoom-out", 0.15), ["zoom-out", "zoom-in"])

    def test_different_asset_untouched(self):
        shots = [self._shot(1, "a.png"), self._shot(2, "b.png")]
        self.assertEqual(build.motion_sequence(shots, "zoom-in", 0.15), ["zoom-in", "zoom-in"])

    def test_graphic_card_does_not_flip(self):
        """图文卡片是静止的（运镜只会让它更难读），不参与续运动。"""
        shots = [self._shot(1, "a.png"), self._shot(2, "a.png", kind="graphic")]
        self.assertEqual(build.motion_sequence(shots, "zoom-in", 0.15), ["zoom-in", "zoom-in"])

    def test_pan_mode_untouched(self):
        shots = [self._shot(1, "a.png"), self._shot(2, "a.png")]
        self.assertEqual(build.motion_sequence(shots, "pan-right", 0.15), ["pan-right", "pan-right"])

    def test_missing_asset_untouched(self):
        shots = [{"id": 1}, {"id": 2}]
        self.assertEqual(build.motion_sequence(shots, "zoom-in", 0.15), ["zoom-in", "zoom-in"])


class SubtitleStyleTest(unittest.TestCase):
    def test_style_escaped(self):
        self.assertEqual(build._escape_style("FontName='A'"), "FontName=A")


class SegmentCacheTest(unittest.TestCase):
    """片段复用的判定。

    踩过的坑：片段缓存只看**时长**（占位片段时长与目标一致），于是「先 build（缺素材→占位）
    再补素材」时，重跑 build 会一直复用占位黑帧 —— 成片永远补不上真画面。
    契约：占位片段一旦本镜有了真素材，**不再复用**。
    """

    def test_force_always_rerenders(self):
        self.assertFalse(build._segment_reusable(
            exists=True, seg_dur=3.0, target_dur=3.0, force=True,
            was_placeholder=False, has_asset=True))

    def test_missing_segment_not_reusable(self):
        self.assertFalse(build._segment_reusable(
            exists=False, seg_dur=0.0, target_dur=3.0, force=False,
            was_placeholder=False, has_asset=True))

    def test_duration_mismatch_not_reusable(self):
        self.assertFalse(build._segment_reusable(
            exists=True, seg_dur=2.0, target_dur=3.0, force=False,
            was_placeholder=False, has_asset=True))

    def test_matching_segment_reusable(self):
        self.assertTrue(build._segment_reusable(
            exists=True, seg_dur=3.0, target_dur=3.0, force=False,
            was_placeholder=False, has_asset=True))

    def test_placeholder_reused_while_asset_still_missing(self):
        self.assertTrue(build._segment_reusable(
            exists=True, seg_dur=3.0, target_dur=3.0, force=False,
            was_placeholder=True, has_asset=False))

    def test_placeholder_rerendered_once_asset_appears(self):
        self.assertFalse(build._segment_reusable(
            exists=True, seg_dur=3.0, target_dur=3.0, force=False,
            was_placeholder=True, has_asset=True))

    def test_placeholder_marker_roundtrip(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            seg = Path(td)
            build._save_placeholders(seg, {5, 1, 3})
            self.assertEqual(build._load_placeholders(seg), {1, 3, 5})

    def test_placeholder_marker_missing_is_empty(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(build._load_placeholders(Path(td)), set())


class SegmentOutputGuardTest(unittest.TestCase):
    """产物校验（回归修复）：ffmpeg 可能 **rc=0 却一帧都没编**。

    实测：静止卡片段（`none` 模式）少了 `-loop 1`，产出 261 字节空壳、`duration()` 读不出，
    而 `finalize` 报「片段：110 个（占位 0 个）」一切正常 —— 成片只剩 64.7s 而旁白 447.3s。
    所以**不能只看返回码**。
    """

    def test_empty_file_is_rejected(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.mp4"
            bad.write_bytes(b"")          # 典型的"只有 moov、没有帧"
            with self.assertRaises(build.BuildError):
                build.assert_segment_rendered(bad, 4.0)

    def test_missing_file_is_rejected(self):
        from pathlib import Path
        with self.assertRaises(build.BuildError):
            build.assert_segment_rendered(Path("no-such-segment.mp4"), 4.0)


class StaticSegmentRenderTest(unittest.TestCase):
    """静止卡片段必须真的产出帧（回归：曾整段 0 帧）。这两个测试会真跑 ffmpeg。"""

    @classmethod
    def setUpClass(cls):
        from lvs.ffmpeg import FFmpegError, tools
        try:
            cls.ffmpeg, _ = tools()
        except FFmpegError as exc:  # pragma: no cover
            raise unittest.SkipTest(f"ffmpeg 不可用：{exc}") from None

    def _make_png(self, tmp):
        try:
            from PIL import Image
        except ModuleNotFoundError:  # pragma: no cover
            self.skipTest("Pillow 不可用")
        from lvs import graphic
        p = Path(tmp) / "card.png"
        Image.new("RGB", (graphic.W, graphic.H), (20, 22, 26)).save(p)
        return p

    def test_none_mode_produces_frames(self):
        from lvs.ffmpeg import duration
        with tempfile.TemporaryDirectory() as td:
            src = self._make_png(td)
            dst = Path(td) / "static.mp4"
            build._render_image_segment(self.ffmpeg, src, dst, 2.0, "none", 0.15, 2)
            dur = duration(dst)
            self.assertIsNotNone(dur, "静止段整段 0 帧（-loop 1 丢了）")
            self.assertAlmostEqual(dur, 2.0, delta=0.2)

    def test_kenburns_mode_still_works(self):
        from lvs.ffmpeg import duration
        with tempfile.TemporaryDirectory() as td:
            src = self._make_png(td)
            dst = Path(td) / "kb.mp4"
            build._render_image_segment(self.ffmpeg, src, dst, 1.0, "zoom-in", 0.15, 1)
            self.assertAlmostEqual(duration(dst), 1.0, delta=0.2)

    def test_guard_passes_for_real_output(self):
        with tempfile.TemporaryDirectory() as td:
            src = self._make_png(td)
            dst = Path(td) / "static.mp4"
            build._render_image_segment(self.ffmpeg, src, dst, 1.0, "none", 0.15, 2)
            build.assert_segment_rendered(dst, 1.0)   # 不抛即通过


class ShortAssetModeTest(unittest.TestCase):
    """`build.short_asset` 只认 loop / freeze。

    别的值（含空串）对**短素材**既不循环也不冻结 → 片段比目标短 → `_segment_reusable`
    判时长不符 → 每轮重渲、永不收敛。所以在渲染前就要挡住非法值。
    """

    def test_valid_modes_accepted(self):
        for mode in ("loop", "freeze"):
            self.assertEqual(build.normalize_short_mode(mode), mode)

    def test_invalid_mode_rejected(self):
        for mode in ("", "bounce", "yes", None):
            with self.assertRaises(build.BuildError, msg=repr(mode)):
                build.normalize_short_mode(mode)

    def test_build_segments_checks_mode_before_rendering(self):
        from lvs.config import Config
        from lvs.workspace import Workspace

        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(task="t", root=Path(td)).ensure()
            cfg = Config({"build": {"short_asset": "bounce"}}, None)
            # 在真正碰 ffmpeg 之前就该炸出来（所以不 mock tools，也不能依赖装了 ffmpeg）
            with self.assertRaises(build.BuildError):
                build.build_segments(ws, [], cfg, force=False)


if __name__ == "__main__":
    unittest.main()


class SegmentKeyTest(unittest.TestCase):
    """票据 48：片段复用原先**只比时长** —— 镜表重排后同镜号可能恰好同长却换了素材，
    旧片段被静默复用，成片里就是别的一镜的画面。指纹必须覆盖"决定片段长什么样"的输入。
    """

    def _key(self, asset="a.png", duration=3.0, motion="zoom-in", supersample=2,
             short_mode="loop", placeholder=False, asset_content=None):
        return build.segment_key(
            asset=asset, duration=duration, motion=motion, supersample=supersample,
            short_mode=short_mode, placeholder=placeholder, asset_content=asset_content,
        )

    def test_same_inputs_same_key(self) -> None:
        self.assertEqual(self._key(), self._key())

    def test_key_changes_with_asset(self) -> None:
        self.assertNotEqual(self._key(asset="a.png"), self._key(asset="b.png"))

    def test_key_changes_with_motion(self) -> None:
        self.assertNotEqual(self._key(motion="zoom-in"), self._key(motion="zoom-out"))

    def test_key_changes_with_duration(self) -> None:
        self.assertNotEqual(self._key(duration=3.0), self._key(duration=3.4))

    def test_key_changes_with_asset_content(self) -> None:
        """路径不变、文件内容被原地替换（graphic 卡顶替成 scene 图）→ 必须换指纹。

        2026-10-04 实测坑：22 张黑卡替换 PNG 内容后路径/时长/运镜全不变，
        旧片段指纹照样匹配被复用，23 张黑卡漏进重编后的成片。
        """
        self.assertNotEqual(
            self._key(asset_content="100:111"), self._key(asset_content="100:222"))

    def test_same_asset_content_same_key(self) -> None:
        """双侧断言的另一侧：内容没变就得复用，否则每次 build 全量重渲。"""
        self.assertEqual(self._key(asset_content="100:111"), self._key(asset_content="100:111"))

    def test_default_content_is_stable(self) -> None:
        """不传 asset_content（老调用/占位）→ 与自身一致，不因缺省值抖动。"""
        self.assertEqual(self._key(), self._key(asset_content=None))

    def test_placeholder_key_stable(self) -> None:
        self.assertEqual(self._key(placeholder=True), self._key(asset=None, placeholder=True))
        self.assertEqual(self._key(placeholder=True), build.PLACEHOLDER_KEY)

    def test_missing_record_not_reusable(self) -> None:
        """盘上没有指纹记录 → 不可复用（老片段一律重渲，宁可慢不可错）。"""
        self.assertFalse(build._segment_reusable(
            exists=True, seg_dur=3.0, target_dur=3.0, force=False,
            was_placeholder=False, has_asset=True, recorded=None, want_key="abc"))

    def test_mismatched_key_not_reusable(self) -> None:
        self.assertFalse(build._segment_reusable(
            exists=True, seg_dur=3.0, target_dur=3.0, force=False,
            was_placeholder=False, has_asset=True, recorded="old", want_key="new"))

    def test_matching_key_reusable(self) -> None:
        self.assertTrue(build._segment_reusable(
            exists=True, seg_dur=3.0, target_dur=3.0, force=False,
            was_placeholder=False, has_asset=True, recorded="same", want_key="same"))


class SegmentContentIntegrationTest(unittest.TestCase):
    """接线守护：素材**原地换内容**（路径不变）→ build_segments 必须重渲该片段。

    单测 `segment_key` 只守指纹函数本身；若调用点不传 `asset_content`（b 型半失效），
    指纹函数再对也没用 —— 2026-10-04 黑卡漏进成片就是这么发生的：
    指纹函数与调用点都存在，唯独内容维度没接上。
    """

    @classmethod
    def setUpClass(cls):
        from lvs.ffmpeg import tools, FFmpegError
        try:
            cls.ffmpeg, _ = tools()
        except FFmpegError as exc:  # pragma: no cover
            raise unittest.SkipTest(f"ffmpeg 不可用：{exc}") from None

    def _render_once(self, td: str, png: Path) -> Path:
        """跑一次 build_segments（force=False），返回片段路径。"""
        from lvs.config import Config
        from lvs.workspace import Workspace
        ws = Workspace(task="t", root=Path(td)).ensure()
        cfg = Config({"build": {"kenburns": "none"}}, None)
        shot = {"id": 1, "start": 0.0, "end": 1.0, "asset_path": str(png)}
        build.build_segments(ws, [shot], cfg, force=False)
        return ws.path("segments") / "shot-001.mp4"

    def test_inplace_content_swap_forces_rerender(self):
        from lvs.ffmpeg import duration
        with tempfile.TemporaryDirectory() as td:
            try:
                from PIL import Image
            except ModuleNotFoundError:  # pragma: no cover
                self.skipTest("Pillow 不可用")
            png = Path(td) / "card.png"
            Image.new("RGB", (320, 180), (0, 0, 0)).save(png)
            seg = self._render_once(td, png)
            self.assertTrue(seg.is_file())
            keys1 = build._load_seg_keys(seg.parent)
            mt1 = seg.stat().st_mtime_ns
            # 同路径、同尺寸诉求，只换像素内容（模拟 graphic 卡被 scene 图顶替）
            import time
            Image.new("RGB", (320, 180), (255, 255, 255)).save(png)
            time.sleep(0.02)   # 保证 mtime 真的推进（文件系统时间粒度）
            seg = self._render_once(td, png)
            keys2 = build._load_seg_keys(seg.parent)
            self.assertNotEqual(
                keys1.get(1), keys2.get(1),
                "原地换内容后指纹没变 → 旧片段会被静默复用（黑卡漏进成片的坑）")
            self.assertNotEqual(
                mt1, seg.stat().st_mtime_ns, "片段没有被重渲")
            self.assertIsNotNone(duration(seg))
