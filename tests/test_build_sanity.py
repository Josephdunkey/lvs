"""成片终检（`lvs/build.py::final_sanity`）的测试。

这一层查的是**整片**才有的故障（音轨整体丢了 / 整片静音 / 整片黑屏）——
逐片段查不出来，而它们恰恰是"流水线坏了"最强的信号。

测试把三个 ffmpeg 探测换掉（不真跑 ffmpeg，快且稳），
重点是**三态**：硬问题 / 警告 / 读不到就跳过（不妄下结论）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lvs import build


@pytest.fixture
def stub(monkeypatch):
    """替换三个探测：`audio` / `vol` / `ratio` 由测试指定。"""
    state = {"audio": True, "vol": -20.0, "ratio": 0.0}

    monkeypatch.setattr(build, "has_audio_stream", lambda p: state["audio"])
    monkeypatch.setattr(build, "mean_volume_db", lambda p: state["vol"])
    monkeypatch.setattr(build, "black_ratio", lambda p: state["ratio"])
    return state


def test_clean_film_has_no_findings(stub):
    hard, warns = build.final_sanity(Path("final.mp4"))
    assert hard == [] and warns == []


def test_missing_audio_track_is_a_hard_problem(stub):
    stub["audio"] = False
    hard, warns = build.final_sanity(Path("final.mp4"))
    assert hard and "音轨" in hard[0]
    assert warns == [], "没音轨时音量探测没意义，不该再报声"


def test_silent_film_is_a_warning_not_a_hard_error(stub):
    """全黑/静音可能是合法风格（有声书式），所以只报不拦。"""
    stub["vol"] = float("-inf")
    hard, warns = build.final_sanity(Path("final.mp4"))
    assert hard == []
    assert any("静音" in w for w in warns)


def test_mostly_black_film_is_a_warning(stub):
    stub["ratio"] = 0.95
    hard, warns = build.final_sanity(Path("final.mp4"))
    assert hard == []
    assert any("黑屏" in w for w in warns)


def test_unreadable_probes_are_skipped_not_flagged(stub):
    """★ 读不到就跳过 —— 宁可不报，也不要因工具环境问题拦下一部好片。"""
    stub["vol"] = None
    stub["ratio"] = None
    hard, warns = build.final_sanity(Path("final.mp4"))
    assert hard == [] and warns == []


def test_thresholds_are_configurable(stub):
    stub["vol"] = -30.0
    stub["ratio"] = 0.5

    class _Cfg:
        def get(self, key, default=None):
            if key == "build.sanity_min_mean_db":
                return -25.0
            if key == "build.sanity_black_ratio_max":
                return 0.4
            return default

    hard, warns = build.final_sanity(Path("final.mp4"), config=_Cfg())
    assert any("静音" in w for w in warns)      # -30 <= -25
    assert any("黑屏" in w for w in warns)      # 0.5 >= 0.4


def test_bad_threshold_value_falls_back_to_default(stub):
    stub["vol"] = -20.0

    class _Cfg:
        def get(self, key, default=None):
            return "not-a-number"

    hard, warns = build.final_sanity(Path("final.mp4"), config=_Cfg())
    assert warns == [], "坏阈值应退回默认（-60），-20 不该报静音"

# ---- 时长自检（票 20）--------------------------------------------------------
#
# 音画错位属于"逐片段看不出来、只有整段时长会响"的故障，所以这一组只做一件事：
# 把**实测**与**期望**摆在一起比，超容差就报。判据分两层 ——
#   `tts._verify_track_duration`：整轨（narration.mp3）vs 各镜时长之和；
#   `build.duration_drift`：成片 vs 旁白 vs 字幕时间轴。
# 一律 monkeypatch 探测函数 —— 不真跑 ffmpeg（真跑属于慢组）。


def test_track_duration_within_tolerance_passes(monkeypatch):
    """mp3 帧对齐量级的偏差是**正常**的，不许判死（否则每轮配音都误报）。"""
    from lvs import tts

    monkeypatch.setattr(tts, "ff_duration", lambda p: 100.2)
    tts._verify_track_duration(Path("audio/narration.mp3"), 100.0)      # 不该抛


def test_track_duration_mismatch_reports_numbers(monkeypatch):
    from lvs import tts

    monkeypatch.setattr(tts, "ff_duration", lambda p: 61.5)
    with pytest.raises(tts.TrackDurationError) as ctx:
        tts._verify_track_duration(Path("audio/narration.mp3"), 100.0)
    msg = str(ctx.value)
    assert "100.00" in msg and "61.50" in msg, "错误里必须带期望 / 实测秒数"
    assert "%" in msg and "narration.mp3" in msg, "错误里必须带偏差百分比与哪个文件"


def test_expected_track_duration_is_sum_of_segments():
    from lvs import tts

    shots = [{"audio_duration": 10.0}, {"audio_duration": 20.5}, {"audio_duration": 4.5}]
    assert tts._expected_track_duration(shots, 0.3) == pytest.approx(35.0 + 0.6)
    # 手改过的 shots.json 可能没有 audio_duration → 无从判断 → None（跳过自检，不误报）
    assert tts._expected_track_duration([{"audio_duration": 10.0}, {}], 0.3) is None


def test_final_duration_agrees_with_narration(monkeypatch):
    """成片与旁白等长 = `-shortest` 的正常结果，放行。"""
    monkeypatch.setattr(build, "ff_duration", lambda p: 600.0)
    assert build.duration_drift(Path("final.mp4"), Path("n.mp3"), timeline=600.0, task="t") is None


def test_final_duration_drift_reports_expectation_and_next_step(monkeypatch):
    """★ 成片只有旁白一半 —— 硬问题，且要说清"差多少 + 下一步跑什么"。"""
    seen = {"final.mp4": 300.0, "n.mp3": 600.0}

    def fake(p):  # noqa: ANN001, ANN202
        return seen.get(str(p))

    monkeypatch.setattr(build, "ff_duration", fake)
    msg = build.duration_drift(Path("final.mp4"), Path("n.mp3"), timeline=600.0, task="T1")
    assert msg is not None
    assert "300.0" in msg and "600.0" in msg and "%" in msg
    assert "lvs build --task T1 --force" in msg, "硬校验必须给出下一步"


def test_unreadable_durations_skip_the_check(monkeypatch):
    """读不到就跳过：工具环境问题不许拦下一部好片（同 `final_sanity` 的取舍）。"""
    monkeypatch.setattr(build, "ff_duration", lambda p: None)
    assert build.duration_drift(Path("final.mp4"), Path("n.mp3"), timeline=600.0) is None
