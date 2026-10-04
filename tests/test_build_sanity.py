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
