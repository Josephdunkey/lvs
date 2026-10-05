"""voice 阶段单元测试（票据 10/11/12/20/21）。不合成音频、不联网。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

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

    def _ask(self, sid: int, out: Path, voice: str, narration: str,
             *, params_key: str = "k1", allow_legacy: bool = True):
        """问一次复用判定，并**数一数**起了几次 ffprobe（P02 的核心判据）。"""
        self.calls = 0

        def _fake_duration(_path):
            self.calls += 1
            return 1.0

        with mock.patch.object(tts, "ff_duration", _fake_duration):
            return tts._audio_reusable(self.ws, sid, out, voice, narration, params_key,
                                       allow_legacy=allow_legacy)

    def test_stale_voice_rejected(self) -> None:
        out = self._make(1, "zh-CN-YunjianNeural", "第一句")
        self.assertIsNone(self._ask(1, out, "zh-CN-YunxiNeural", "第一句"))

    def test_changed_text_rejected(self) -> None:
        out = self._make(1, "zh-CN-YunxiNeural", "第一句")
        self.assertIsNone(self._ask(1, out, "zh-CN-YunxiNeural", "改过的另一句旁白"))

    def test_matching_voice_and_text_reused(self) -> None:
        out = self._make(1, "zh-CN-YunxiNeural", "第一句")
        self.assertIsNotNone(self._ask(1, out, "zh-CN-YunxiNeural", "第一句"))

    def test_missing_meta_rejected(self) -> None:
        """没有 json 记录 → 无从校验，宁可多合一次。"""
        out = self._make(2, "", "", with_meta=False)
        self.assertIsNone(self._ask(2, out, "zh-CN-YunxiNeural", "第一句"))

    # ---- P02：复用判定**一次到位**（原实现每镜 2 次 ffprobe + 读 2 遍 json）----

    def test_ffprobe_runs_once_per_shot(self) -> None:
        out = self._make(1, "zh-CN-YunxiNeural", "第一句")
        self._ask(1, out, "zh-CN-YunxiNeural", "第一句")
        self.assertEqual(self.calls, 1, "复用路径每镜只该起一次 ffprobe（实测 0.21–0.59 s/次）")

    def test_boundaries_come_from_the_same_json_read(self) -> None:
        """边界与 meta 是**同一次**读盘的结果。"""
        out = self._make(3, "v", "第一句")
        path = self.ws.path("audio", "shot-003.json")
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta["boundaries"] = [{"offset": 0.1, "duration": 0.2, "text": "第一"}]
        path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

        got = self._ask(3, out, "v", "第一句")
        self.assertIsNotNone(got)
        self.assertEqual(len(got.boundaries), 1)
        self.assertAlmostEqual(got.boundaries[0].offset, 0.1)
        self.assertAlmostEqual(got.duration, 1.0)

    def test_broken_boundaries_do_not_kill_the_reuse_path(self) -> None:
        """坏边界记录 → 当作"没有边界"（下游走对齐），不许整轮 voice 死在复用分支（B13）。"""
        out = self._make(4, "v", "第一句")
        path = self.ws.path("audio", "shot-004.json")
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta["boundaries"] = [{"offset": "???"}]
        path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

        got = self._ask(4, out, "v", "第一句")
        self.assertIsNotNone(got)
        self.assertIsNone(got.boundaries)

    # ---- #3：合成参数进缓存键 ----

    def test_params_change_invalidates(self) -> None:
        """改 tts.rate / pitch / 换参考音频 → 必须重合成（原先一个字都不重合）。"""
        out = self._make(1, "zh-CN-YunxiNeural", "第一句")
        self._stamp_params(1, "k1")
        self.assertIsNone(self._ask(1, out, "zh-CN-YunxiNeural", "第一句", params_key="other"))

    def test_legacy_record_is_redone_when_params_provably_changed(self) -> None:
        """★ 迁移规则：旧记录没有指纹，但 manifest 能证明参数改过 → 必须重做。

        否则"改 rate 会重合成"这个修法对历史任务完全不起作用：
        改一次 rate，旧音频被"合理地"白复用一轮，参数指纹永远补不上。
        """
        out = self._make(1, "zh-CN-YunxiNeural", "第一句")
        self.assertIsNone(self._ask(1, out, "zh-CN-YunxiNeural", "第一句",
                                    allow_legacy=False))

    def _stamp_params(self, sid: int, key: str) -> None:
        path = self.ws.path("audio", f"shot-{sid:03d}.json")
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta["params_sha1"] = key
        path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    def test_legacy_meta_reused_but_flagged(self) -> None:
        """旧格式记录（没有参数指纹）：复用，但**如实标出来** —— 不静默、也不擅自全量重合成。"""
        out = self._make(1, "zh-CN-YunxiNeural", "第一句")
        got = self._ask(1, out, "zh-CN-YunxiNeural", "第一句")
        self.assertIsNotNone(got)
        self.assertTrue(got.params_unverified)

    def test_recorded_params_match_means_verified(self) -> None:
        out = self._make(5, "zh-CN-YunxiNeural", "第一句")
        self._stamp_params(5, "k1")

        got = self._ask(5, out, "zh-CN-YunxiNeural", "第一句")
        self.assertIsNotNone(got)
        self.assertFalse(got.params_unverified)


class ParamsKeyTest(unittest.TestCase):
    """参数指纹只对**真影响声音**的字段敏感（问的是"该不该重合成"，不是"配置变没变"）。"""

    def test_rate_change_changes_key(self) -> None:
        self.assertNotEqual(tts._params_key(tts.EdgeTTSBackend(rate="+0%"), "v"),
                            tts._params_key(tts.EdgeTTSBackend(rate="+20%"), "v"))

    def test_pitch_change_changes_key(self) -> None:
        self.assertNotEqual(tts._params_key(tts.EdgeTTSBackend(pitch="+0Hz"), "v"),
                            tts._params_key(tts.EdgeTTSBackend(pitch="-20Hz"), "v"))

    def test_voice_change_changes_key(self) -> None:
        self.assertNotEqual(tts._params_key(tts.EdgeTTSBackend(), "a"),
                            tts._params_key(tts.EdgeTTSBackend(), "b"))

    def test_same_params_same_key(self) -> None:
        self.assertEqual(tts._params_key(tts.EdgeTTSBackend(), "v"),
                         tts._params_key(tts.EdgeTTSBackend(), "v"))

    def test_base_url_is_not_in_the_key(self) -> None:
        """挪个端口不该触发全量重合成（那是几十分钟 GPU 的误伤）。"""
        self.assertEqual(tts._params_key(tts.OpenAISpeechBackend("http://127.0.0.1:8000"), "v"),
                         tts._params_key(tts.OpenAISpeechBackend("http://127.0.0.1:9000"), "v"))

    def test_legacy_rule_needs_manifest_evidence(self) -> None:
        """没有 manifest 证据时旧记录照旧复用；有证据且不一致 → 重做。"""
        self.assertTrue(tts._legacy_records_still_valid("", "k"))     # 旧 manifest：无从判
        self.assertTrue(tts._legacy_records_still_valid("k", "k"))     # 参数没变
        self.assertFalse(tts._legacy_records_still_valid("k1", "k2"))  # 参数变了 → 重做

    def test_ref_audio_content_change_invalidates(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        ref = Path(td) / "ref.wav"
        ref.write_bytes(b"x" * 100)
        before = tts._params_key(tts.OpenAISpeechBackend("http://x", ref_audio=str(ref)), "v")
        ref.write_bytes(b"x" * 200)
        after = tts._params_key(tts.OpenAISpeechBackend("http://x", ref_audio=str(ref)), "v")
        self.assertNotEqual(before, after, "同名参考音频被换过，只看路径是看不出来的")


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


class VoiceCommandReuseTest(unittest.TestCase):
    """命令级验收（#3 / P02）：真跑 `lvs.tts.run_command`，数一数 ffprobe 起过几次。

    ★ 为什么单元测试不够：`_audio_reusable` 自己少测一次≠调用方不再补测一次 ——
      P02 的坑就在调用方那一行（`shot["audio_duration"] = ff_duration(out)`）。
      这里用 `silent` 后端（ffmpeg 静音轨；离线、不联网、不吃显存）真跑三轮：

      | 轮 | 条件 | 期望 |
      |---|---|---|
      | 1 | 空目录 | 3 镜全合成，音频记录里带 `params_sha1`，manifest 里有 `voice_key` |
      | 2 | 什么都没改 | 3 镜全跳过 + **每镜只 1 次 ffprobe**（改前是每镜 2 次） |
      | 3 | 换音色 | 3 镜全重合成 |
    """

    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.root = Path(td)
        self.ws = Workspace(task="t", root=self.root).ensure()
        self.ws.write_shots({
            "title": "t", "source_mode": "local",
            "shots": [{"id": i, "narration": f"第{i}句。"} for i in (1, 2, 3)],
        })
        self.calls: list[str] = []

    def _run(self, *, force: bool = False, voice: str = "zh-CN-YunxiNeural") -> int:
        from lvs.config import Config

        config = Config({"tts": {"backend": "silent", "voice": voice},
                         "voice": {"align": "estimate"}}, None)
        real = tts.ff_duration

        def _counting(path):
            self.calls.append(Path(path).name)
            return real(path)

        with mock.patch.object(tts, "ff_duration", _counting):
            return tts.run_command(config, self.ws, argparse.Namespace(force=force))

    def _stage(self) -> dict:
        return Workspace.read("t", self.root).manifest["stages"]["voice"]

    @pytest.mark.slow
    def test_three_rounds(self) -> None:
        # ---- 轮 1：全合成 ----
        self.assertEqual(self._run(), 0)
        stage = self._stage()
        self.assertEqual((stage["done"], stage["skipped"]), (3, 0))
        self.assertIn("voice_key", stage, "参数指纹要落进 manifest（事后能查是哪套参数合成的）")
        for sid in (1, 2, 3):
            meta = json.loads(
                self.ws.path("audio", f"shot-{sid:03d}.json").read_text(encoding="utf-8"))
            self.assertIn("params_sha1", meta, "新合成的音频必须带上参数指纹")

        # ---- 轮 2：什么都没改 → 全跳过，且每镜只 1 次 ffprobe ----
        self.calls.clear()
        self.assertEqual(self._run(), 0)
        stage = self._stage()
        self.assertEqual((stage["done"], stage["skipped"]), (0, 3))
        probed = [n for n in self.calls if n.startswith("shot-")]
        self.assertEqual(len(probed), 3,
                         f"复用扫描每镜只该测一次时长；实测 {len(probed)} 次（改前是 6 次）")

        # ---- 轮 3：换音色 → 全部重合成 ----
        self.assertEqual(self._run(voice="zh-CN-YunjianNeural"), 0)
        stage = self._stage()
        self.assertEqual((stage["done"], stage["skipped"]), (3, 0))

    @pytest.mark.slow
    def test_force_resynthesizes_everything(self) -> None:
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._run(force=True), 0)
        stage = self._stage()
        self.assertEqual((stage["done"], stage["skipped"]), (3, 0))
