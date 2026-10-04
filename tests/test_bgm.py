"""BGM 混音（`lvs/build.py` 的 bgm_source / _bgm_audio_filter / finalize）测试。

三条设计合同：
1. **没配不是错，配了但文件不在必须说出来**（不许静默当成"没配"）；
2. **BGM 出任何问题都不许毁掉成片** —— 自动退回无 BGM 版本（参考 NarratoAI）；
3. **amix 必须 `normalize=0`** —— ffmpeg ≥4.4 默认把各输入各除以输入数，
   会把旁白直接砍半（"加了 BGM 旁白变小声"的典型事故）。

前三组用桩（快）；最后一组真跑 ffmpeg，端到端验证"加了 BGM 音量确实上去、
时长不漂移"——那是 normalize=0 漏掉时最先暴露的地方。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lvs import build
from lvs.config import Config
from lvs.ffmpeg import FFmpegError, duration as ff_duration, mean_volume_db, tools
from lvs.workspace import Workspace


# ---- bgm_source 三态 ---------------------------------------------------------


def test_not_configured_is_not_an_error():
    """BGM 是可选项：没配 = (None, 空原因)，不报警。"""
    path, why = build.bgm_source(Config({}, None))
    assert path is None and why == ""


def test_configured_but_missing_file_reports_reason(tmp_path):
    """★ 配了但文件不在是配置错误 —— 原因必须说出来，不许静默。"""
    cfg = Config({"bgm": {"file": str(tmp_path / "nope.mp3")}}, None)
    path, why = build.bgm_source(cfg)
    assert path is None
    assert why and "不存在" in why and "[bgm].file" in why


def test_existing_file_is_returned(tmp_path):
    bgm = tmp_path / "bgm.mp3"
    bgm.write_bytes(b"x")
    path, why = build.bgm_source(Config({"bgm": {"file": str(bgm)}}, None))
    assert path == bgm and why == ""


def test_blank_string_treated_as_not_configured():
    path, why = build.bgm_source(Config({"bgm": {"file": "   "}}, None))
    assert path is None and why == ""


# ---- _bgm_audio_filter（滤镜串）---------------------------------------------


def _cfg(**bgm) -> Config:
    return Config({"bgm": bgm}, None)


def test_default_volume_and_fades():
    f = build._bgm_audio_filter(0.0, 100.0, _cfg())
    assert "volume=0.250" in f
    assert "afade=t=in:d=2.00" in f
    assert "afade=t=out:st=97.00:d=3.00" in f   # total - fade_out


def test_fade_out_start_is_total_minus_fade_out():
    f = build._bgm_audio_filter(0.0, 60.0, _cfg(fade_out=5.0))
    assert "st=55.00:d=5.00" in f


def test_no_fade_out_when_total_shorter_than_fade():
    """总时长比淡出还短时，st 会算成负数（非法）→ 干脆不加淡出。"""
    f = build._bgm_audio_filter(0.0, 2.0, _cfg(fade_out=3.0))
    assert "afade=t=out" not in f
    assert "afade=t=in" in f


def test_zero_fades_disabled():
    f = build._bgm_audio_filter(0.0, 100.0, _cfg(fade_in=0.0, fade_out=0.0))
    assert "afade" not in f and "volume=" in f


def test_volume_clamped_to_one():
    f = build._bgm_audio_filter(0.0, 100.0, _cfg(volume=5.0))
    assert "volume=1.000" in f


def test_bad_values_fall_back_to_defaults():
    f = build._bgm_audio_filter(0.0, 100.0, _cfg(volume="loud", fade_in="x"))
    assert "volume=0.250" in f and "afade=t=in:d=2.00" in f


# ---- finalize 的退回外壳 ------------------------------------------------------


def _run_finalize(monkeypatch, tmp_path, cfg: Config):
    """把 `_finalize` 换成桩，返回 (调用记录, finalize 返回值)。"""
    ws = Workspace(task="t", root=tmp_path).ensure()
    calls: list[Path | None] = []

    def fake(final_ws, video_track, narration, srt, config, *, bgm=None):
        calls.append(bgm)
        if bgm is not None:
            raise FFmpegError("fake: corrupt bgm")
        return ws.path("final.mp4"), False

    monkeypatch.setattr(build, "_finalize", fake)
    result = build.finalize(ws, Path("v.mp4"), Path("n.mp3"), Path("s.srt"), cfg)
    return calls, result


def test_bgm_failure_falls_back_to_no_bgm(monkeypatch, tmp_path):
    """★ 配乐坏了不许毁掉成片：第一次带 BGM 炸 → 第二次无 BGM 成功返回。"""
    bgm = tmp_path / "bgm.mp3"
    bgm.write_bytes(b"x")
    cfg = Config({"bgm": {"file": str(bgm)}}, None)
    calls, result = _run_finalize(monkeypatch, tmp_path, cfg)
    assert calls == [bgm, None], "应先试带 BGM，失败后退回无 BGM"
    assert result[0].name == "final.mp4"


def test_no_bgm_config_calls_finalize_once(monkeypatch, tmp_path):
    calls, _ = _run_finalize(monkeypatch, tmp_path, Config({}, None))
    assert calls == [None]


def test_missing_bgm_file_skips_retry(monkeypatch, tmp_path):
    """配了但文件不在：说明原因后直接走无 BGM，不该先失败一次。"""
    cfg = Config({"bgm": {"file": str(tmp_path / "gone.mp3")}}, None)
    calls, _ = _run_finalize(monkeypatch, tmp_path, cfg)
    assert calls == [None]


# ---- _finalize 的命令构造（normalize=0 是硬合同）-----------------------------


def _capture_finalize(monkeypatch, tmp_path, cfg: Config, *, bgm: Path | None):
    ws = Workspace(task="t", root=tmp_path).ensure()
    srt = ws.path("subtitle.srt")
    srt.write_text("", encoding="utf-8")   # 空 srt → 不烧字幕，走纯混音路径
    captured: list[list[str]] = []
    monkeypatch.setattr(build, "tools", lambda: ("ffmpeg", ""))
    monkeypatch.setattr(build, "ff_duration", lambda p: 100.0)
    def fake_run(args, cwd=None):      # noqa: ANN001, ANN202
        # ★ 桩也要「成功 = 产物真的存在」：早先只记 args、不落文件，
        #   于是掩盖了「ffmpeg 返回 0 却没产物」这条真故障（本轮 #2 原子化时被它绊到）。
        captured.append(list(args))
        assert str(args[-1]).endswith("final.mp4.part"), "必须先写 .part 再归位"
        Path(args[-1]).write_bytes(b"stub")

    monkeypatch.setattr(build, "ff_run", fake_run)
    build._finalize(ws, Path("v.mp4"), Path("n.mp3"), srt, cfg, bgm=bgm)
    assert captured, "ff_run 没被调用"
    return captured[0]


def test_bgm_command_loops_and_never_normalizes(monkeypatch, tmp_path):
    """★ amix 缺 normalize=0 → 旁白被砍半；-stream_loop -1 才能铺满短 BGM。"""
    bgm = tmp_path / "bgm.mp3"
    bgm.write_bytes(b"x")
    args = _capture_finalize(monkeypatch, tmp_path, _cfg(), bgm=bgm)
    filters = args[args.index("-filter_complex") + 1]
    assert "normalize=0" in filters, "amix 不写 normalize=0，旁白会被砍半"
    assert "amix=inputs=2" in filters and "alimiter" in filters
    assert "-stream_loop" in args, "BGM 短于成片时不循环就中途没声"


def test_no_bgm_command_unchanged(monkeypatch, tmp_path):
    """没配 BGM 时命令与从前完全一致：两个输入、无 filter_complex。"""
    args = _capture_finalize(monkeypatch, tmp_path, _cfg(), bgm=None)
    assert "-filter_complex" not in args
    assert args.count("-i") == 2
    assert "amix" not in " ".join(args)


def test_burn_subtitles_path_also_carries_bgm(monkeypatch, tmp_path):
    """烧字幕分支同样要接 BGM（两条路径当年是复制粘贴的，最容易漏一条）。"""
    ws = Workspace(task="t", root=tmp_path).ensure()
    srt = ws.path("subtitle.srt")
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
    bgm = tmp_path / "bgm.mp3"
    bgm.write_bytes(b"x")
    captured: list[list[str]] = []
    monkeypatch.setattr(build, "tools", lambda: ("ffmpeg", ""))
    monkeypatch.setattr(build, "ff_duration", lambda p: 100.0)
    def fake_run(args, cwd=None):      # noqa: ANN001, ANN202
        # ★ 桩也要「成功 = 产物真的存在」：早先只记 args、不落文件，
        #   于是掩盖了「ffmpeg 返回 0 却没产物」这条真故障（本轮 #2 原子化时被它绊到）。
        captured.append(list(args))
        assert str(args[-1]).endswith("final.mp4.part"), "必须先写 .part 再归位"
        Path(args[-1]).write_bytes(b"stub")

    monkeypatch.setattr(build, "ff_run", fake_run)
    build._finalize(ws, Path("v.mp4"), Path("n.mp3"), srt, _cfg(), bgm=bgm)
    filters = captured[0][captured[0].index("-filter_complex") + 1]
    assert "subtitles=" in filters and "amix=inputs=2" in filters
    assert "normalize=0" in filters


# ---- 真跑 ffmpeg 的端到端 ------------------------------------------------------


def _have_ffmpeg() -> bool:
    try:
        tools()
        return True
    except FFmpegError:
        return False


@pytest.mark.slow   # ★ 慢组：TestRealFfmpegBgm
class TestRealFfmpegBgm:
    """端到端：真的混一段音，验证 BGM 把音量抬上去、时长不漂移。

    这是 `normalize=0` 漏掉时最先暴露的地方 —— 漏了它，旁白砍半，
    "加了 BGM 的成片"反而比不加更轻。
    """

    @pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg 不可用")
    def test_bgm_raises_volume_without_changing_duration(self, tmp_path):
        ff, _ = tools()
        video = tmp_path / "v.mp4"
        narr = tmp_path / "n.m4a"
        bgm = tmp_path / "bgm.mp3"
        # 3s 静音画面
        subprocess.run(
            [ff, "-y", "-f", "lavfi", "-i", "color=c=black:s=320x180:r=25",
             "-t", "3", "-c:v", "libx264", "-preset", "ultrafast", str(video)],
            check=True, capture_output=True,
        )
        # 3s 旁白：0.3 幅度的 440Hz（约 -10.5 dBFS）
        subprocess.run(
            [ff, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
             "-af", "volume=0.3", str(narr)],
            check=True, capture_output=True,
        )
        # 1s BGM（会被循环铺满 3s）：满幅度 220Hz
        subprocess.run(
            [ff, "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=1",
             "-af", "volume=1.0", str(bgm)],
            check=True, capture_output=True,
        )

        ws = Workspace(task="bgm", root=tmp_path).ensure()
        srt = ws.path("subtitle.srt")
        srt.write_text("", encoding="utf-8")

        base_db = mean_volume_db(narr)
        out, _ = build._finalize(
            ws, video, narr, srt, _cfg(volume=0.6, fade_in=0.0, fade_out=0.0), bgm=bgm
        )
        mix_db = mean_volume_db(out)
        got_dur = ff_duration(out)

        assert base_db is not None and mix_db is not None
        # BGM 满幅度 ×0.6 叠在 0.3 幅度旁白上，平均功率必然明显上升；
        # 若 amix 把旁白砍半（normalize=0 漏掉），这里反而会下降。
        assert mix_db > base_db + 3.0, (
            f"加 BGM 后音量没上去（{base_db:.1f} → {mix_db:.1f} dB）——"
            "怀疑 amix normalize=0 丢失导致旁白被砍半"
        )
        assert got_dur is not None and abs(got_dur - 3.0) < 0.35, (
            f"成片时长 {got_dur}s 与旁白 3s 不符（BGM 输入不该拉长/缩短成片）"
        )

    @pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg 不可用")
    def test_corrupt_bgm_file_falls_back_end_to_end(self, tmp_path):
        """真实的坏 BGM（非音频文件）→ finalize 退回无 BGM 版本，成片照出。"""
        ff, _ = tools()
        video = tmp_path / "v.mp4"
        narr = tmp_path / "n.m4a"
        subprocess.run(
            [ff, "-y", "-f", "lavfi", "-i", "color=c=black:s=320x180:r=25",
             "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", str(video)],
            check=True, capture_output=True,
        )
        subprocess.run(
            [ff, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(narr)],
            check=True, capture_output=True,
        )
        bad = tmp_path / "bad.mp3"
        bad.write_bytes(b"this is not audio data" * 10)

        ws = Workspace(task="bgmfail", root=tmp_path).ensure()
        ws.path("subtitle.srt").write_text("", encoding="utf-8")
        cfg = Config({"bgm": {"file": str(bad)}}, None)

        out, _ = build.finalize(ws, video, narr, ws.path("subtitle.srt"), cfg)
        assert out.is_file() and (out.stat().st_size > 0)
        assert ff_duration(out) is not None and ff_duration(out) > 1.5
        assert mean_volume_db(out) is not None, "退回的无 BGM 版本必须有音轨"
