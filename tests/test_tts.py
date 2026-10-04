"""voice 阶段单元测试（票据 10/11/12/20/21）。不合成音频、不联网。"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lvs import tts
from lvs.workspace import Workspace


class CurveTest(unittest.TestCase):
    def test_time_at_chars_interpolates(self):
        bounds = [
            tts.Boundary(0.0, 0.5, "甲"),
            tts.Boundary(0.5, 0.5, "乙"),
            tts.Boundary(1.0, 0.5, "丙"),
        ]
        curve = tts.char_time_curve(bounds)
        self.assertAlmostEqual(tts.time_at_chars(curve, 0), 0.0)
        self.assertAlmostEqual(tts.time_at_chars(curve, 1), 0.5)
        self.assertAlmostEqual(tts.time_at_chars(curve, 3), 1.5)

    def test_empty_curve_safe(self):
        self.assertEqual(tts.time_at_chars([], 5), 0.0)

    def test_curve_aligns_to_raw_indices_when_punctuation_dropped(self):
        """票据 44：词边界不含标点，曲线必须按**原文真实下标**记录。

        否则字幕会越到句尾越滞后 —— 用户听感即"声音字幕不同步"。
        """
        narration = "甲乙，丙丁。"            # 6 字，其中 2 个标点不进边界
        bounds = [tts.Boundary(0.0, 0.5, "甲乙"), tts.Boundary(0.6, 0.5, "丙丁")]
        curve = tts.char_time_curve(bounds, narration)
        self.assertEqual(curve[-1][0], 5)      # "丙丁"结束于真实下标 5，而非边界累计的 4
        # 末尾那个没有边界的句号退化为曲线末时刻（不越过音频尾）
        self.assertAlmostEqual(tts.time_at_chars(curve, 6), 1.1)
        mid = tts.time_at_chars(curve, 3)
        self.assertGreater(mid, 0.5)
        self.assertLess(mid, 1.1)

    def test_curve_locates_text_inside_braces(self):
        """边界文本是花括号**里面**的字（edge-tts 吞掉 `{}`），仍要定位到真实下标。"""
        narration = "我是{主播名}，再见。"     # 11 字
        bounds = [
            tts.Boundary(0.0, 0.4, "我是"),
            tts.Boundary(0.5, 0.4, "主播名"),
            tts.Boundary(1.0, 0.4, "再见"),
        ]
        curve = tts.char_time_curve(bounds, narration)
        self.assertEqual(curve[-1][0], 10)     # "再见"结束于下标 10，而非累计的 7


class DisplayLinesTest(unittest.TestCase):
    def test_text_preserved(self):
        text = "公元前256年，周天子最后一次结账。天下再无共主。"
        lines = tts.split_display_lines(text, max_chars=10)
        self.assertEqual("".join(t for _, _, t in lines), text)

    def test_length_bound(self):
        text = "甲" * 100
        lines = tts.split_display_lines(text, max_chars=18)
        for _, _, t in lines:
            self.assertLessEqual(len(t), 24)  # max_chars + 少量标点余量

    def test_no_line_starts_with_punctuation(self):
        """硬切会切在句中的任意位置 → 行首禁则：逗号不能顶在字幕行开头。"""
        text = "甲" * 24 + "，乙丙丁"
        lines = tts.split_display_lines(text, max_chars=18)
        for _, _, t in lines:
            self.assertNotIn(t[0], "。！？，、；：）)")
        self.assertEqual("".join(t for _, _, t in lines), text)

    def test_lone_punctuation_tail_absorbed(self):
        """只剩一个标点的尾行不能单独成条字幕，要并回上一行。"""
        text = "甲" * 24 + "，"      # 硬切点正好落在逗号前
        lines = tts.split_display_lines(text, max_chars=18)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0][2], text)


class SrtTest(unittest.TestCase):
    def _shots(self):
        return [
            {"id": 1, "start": 0.0, "end": 2.0, "narration": "第一句。"},
            {"id": 2, "start": 2.3, "end": 4.0, "narration": "第二句。"},
        ]

    def test_srt_monotonic_and_nonempty(self):
        srt = tts.build_srt(self._shots(), {}, max_chars=18)
        self.assertGreater(srt.count(" --> "), 0)
        times = []
        for block in srt.strip().split("\n\n"):
            line = block.splitlines()[1]
            start = line.split(" --> ")[0]
            h, m, rest = start.split(":")
            s, ms = rest.split(",")
            times.append(int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000)
        self.assertEqual(times, sorted(times))

    def test_srt_text_matches_narration(self):
        srt = tts.build_srt(self._shots(), {}, max_chars=18)
        for text in ("第一句。", "第二句。"):
            self.assertIn(text, srt)

    def test_with_boundaries(self):
        bounds = {1: [tts.Boundary(0.0, 0.5, "第"), tts.Boundary(0.5, 0.5, "一"), tts.Boundary(1.0, 0.5, "句"), tts.Boundary(1.5, 0.4, "。")]}
        srt = tts.build_srt(self._shots(), bounds, max_chars=18)
        self.assertIn("第一句。", srt)


class BackendSelectionTest(unittest.TestCase):
    def test_unknown_backend_raises(self):
        class C:
            def get(self, k, d=None):
                return {"tts.backend": "nope"}.get(k, d)

        with self.assertRaises(tts.TTSError):
            tts.make_backend(C())

    def test_edge_backend_from_config(self):
        class C:
            def get(self, k, d=None):
                return {"tts.backend": "edge"}.get(k, d)

        self.assertEqual(tts.make_backend(C()).name, "edge")


if __name__ == "__main__":
    unittest.main()


class AudioReuseTest(unittest.TestCase):
    """票据 47：`--force` 重合成失败时**不删旧文件**，下一轮就把它当成功跳过。

    实测第一章换音色后 4 镜（035/042/044/046）残留上一版的 Yunjian 音频，
    其中一镜旁白已从 11 字改到 168 字（3.6s vs 44s）却毫无报错。
    """

    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.ws = Workspace(task="t", root=Path(td)).ensure()

    def _make(self, sid: int, voice: str, narration: str, *, with_meta: bool = True) -> Path:
        out = self.ws.path("audio", f"shot-{sid:03d}.mp3")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00" * 2048)
        if with_meta:
            meta = {"voice": voice, "duration": 1.0, "boundaries": [],
                    "text_sha1": tts._text_sha1(narration)}
            self.ws.path("audio", f"shot-{sid:03d}.json").write_text(
                json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return out

    def _ask(self, sid: int, out: Path, voice: str, narration: str) -> bool:
        with mock.patch.object(tts, "ff_duration", lambda _p: 1.0):
            return tts._audio_reusable(self.ws, sid, out, voice, narration)

    def test_stale_voice_rejected(self) -> None:
        out = self._make(1, "zh-CN-YunjianNeural", "第一句")
        self.assertFalse(self._ask(1, out, "zh-CN-YunxiNeural", "第一句"))

    def test_changed_text_rejected(self) -> None:
        out = self._make(1, "zh-CN-YunxiNeural", "第一句")
        self.assertFalse(self._ask(1, out, "zh-CN-YunxiNeural", "改过的另一句旁白"))

    def test_matching_voice_and_text_reused(self) -> None:
        out = self._make(1, "zh-CN-YunxiNeural", "第一句")
        self.assertTrue(self._ask(1, out, "zh-CN-YunxiNeural", "第一句"))

    def test_missing_meta_rejected(self) -> None:
        """没有 json 记录 → 无从校验，宁可多合一次。"""
        out = self._make(2, "", "", with_meta=False)
        self.assertFalse(self._ask(2, out, "zh-CN-YunxiNeural", "第一句"))


class NormStaleTest(unittest.TestCase):
    """重合成的源音频必须触发重新归一化，否则整轨**静默拼进旧 wav**。

    实测 2026-10-04：剥掉〔原文〕重合成后，norm 缓存不查新旧 → 旧音频原样进片。
    """

    def setUp(self) -> None:
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)
        self.src = self.td / "shot-001.mp3"
        self.norm = self.td / "shot-001.wav"
        self.src.write_bytes(b"src")

    def test_missing_norm_is_stale(self) -> None:
        self.assertTrue(tts._norm_stale(self.src, self.norm))

    def test_fresh_norm_reused(self) -> None:
        self.norm.write_bytes(b"norm")
        os.utime(self.src, (1000, 1000))
        os.utime(self.norm, (2000, 2000))
        self.assertFalse(tts._norm_stale(self.src, self.norm))

    def test_src_newer_than_norm_is_stale(self) -> None:
        """源被重新合成（mtime 更新）→ 归一化件必须重做。"""
        self.norm.write_bytes(b"norm")
        os.utime(self.norm, (1000, 1000))
        os.utime(self.src, (2000, 2000))
        self.assertTrue(tts._norm_stale(self.src, self.norm))
