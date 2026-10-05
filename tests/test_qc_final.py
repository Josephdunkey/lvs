"""成片自检（`lvs qc --final` / `lvs/qc_final.py`）的守卫测试。

分两层，与模块本身的分层对齐：

- **fast**：纯逻辑（秒数格式化 / 抽帧时点 / 黑帧判据 / srt 解析）+ 报告结构 +
  "找不到成片"的早退 + **G5 只报警不改判**的架构判据（qc 不进 `criteria.CRITERIA`）；
- **slow**（`@pytest.mark.slow`）：真跑 ffmpeg —— 合成 2s 小片 → 通过；
  **抽掉音轨**的坏样本 → 必须 error。

★ fast 组保持**全离线、不真调 ffmpeg**（判据是"有没有 ffmpeg"，不是"跑没跑"）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lvs import criteria, pipeline, qc_final, schemas
from lvs.ffmpeg import FFmpegError, tools
from lvs.errors import EXIT_FAILED, EXIT_OK, EXIT_USAGE
from lvs.workspace import Workspace

REPO = Path(__file__).resolve().parents[1]

SRT = """\
1
00:00:00,000 --> 00:00:01,000
第一句

2
00:00:01,000 --> 00:00:02,000
第二句
"""


def _ns(**kw) -> SimpleNamespace:
    ns = SimpleNamespace(video=None, frames=None, out=None, json=False, task="t")
    for key, value in kw.items():
        setattr(ns, key, value)
    return ns


def _have_ffmpeg() -> bool:
    try:
        tools()
        return True
    except FFmpegError:
        return False


def _make_film(path: Path, *, seconds: float = 2.0, audio: bool = True) -> None:
    """合成一段 2s 小片：**蓝色**（不是黑帧，否则抽帧判据会红）+ 可选正弦音。"""
    ffmpeg_exe, _ = tools()
    cmd = [ffmpeg_exe, "-v", "error", "-y", "-f", "lavfi",
           "-i", f"color=c=blue:s=320x240:r=10:d={seconds}", "-t", str(seconds)]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    # ★ 输出选项必须在**所有 -i 之后** —— 写在输入前面会被当成那个输入的解码器
    # （实测：`-c:v libx264` 前置 → `Unknown decoder 'libx264'`）。
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd.append(str(path))
    subprocess.run(cmd, check=True, capture_output=True)


# ---- 纯逻辑（fast，不碰 ffmpeg）------------------------------------------------


def test_hms_formats_seconds():
    assert qc_final.hms(3671.4) == "1:01:11"
    assert qc_final.hms(None) == "?"
    assert qc_final.hms(-1) == "0:00:00"


def test_frame_times_spread_inside_the_film():
    """取每段**中点**：避开片头淡入 / 片尾黑场（实测踩过：取端点常抽到黑）。"""
    assert qc_final.frame_times(100.0, 4) == [12.5, 37.5, 62.5, 87.5]
    assert qc_final.frame_times(0.0, 4) == []
    assert qc_final.frame_times(10.0, 0) == [5.0]          # 至少一帧


def test_is_black_needs_low_mean_and_low_std():
    assert qc_final.is_black(2.0, 1.0) is True
    assert qc_final.is_black(2.0, 40.0) is False           # 有内容的暗场戏：不算黑帧
    assert qc_final.is_black(60.0, 1.0) is False           # 亮而平：也不算


def test_srt_parsing_counts_and_tail():
    assert qc_final.srt_cue_count(SRT) == 2
    assert qc_final.srt_last_end(SRT) == 2.0
    assert qc_final.srt_cue_count("没有时间轴\n") == 0
    assert qc_final.srt_last_end("") is None


# ---- 报告结构与早退（fast）----------------------------------------------------


def test_missing_film_gives_a_report_not_a_traceback(tmp_path):
    report = qc_final.inspect_film(tmp_path / "nope.mp4", task="t")
    assert report["ok"] is False
    assert report["counts"]["error"] == 1
    assert report["checks"][0]["id"] == "probe"
    assert qc_final.render(report).startswith("成片自检")


def test_report_matches_the_qc_contract(tmp_path):
    """报告结构由 `schemas/qc.schema.json` 管 —— 改结构要两边同时改。"""
    assert schemas.validate("qc", qc_final.inspect_film(tmp_path / "nope.mp4", task="t")) == []


def test_run_command_is_a_usage_error_without_a_film(tmp_path, capsys):
    """★ 早退也**不许建任务目录**（与 board/gate 同一条只读契约）。"""
    ws = Workspace(task="t", root=tmp_path)
    assert qc_final.run_command(None, ws, _ns()) == EXIT_USAGE
    assert not ws.dir.exists(), "找不到成片却把任务目录建出来了"
    assert "找不到成片" in capsys.readouterr().out


# ---- ★ 验收④：G5 自检默认**只报警不改判** ------------------------------------


def test_g5_is_still_a_human_decision():
    """`criteria.CRITERIA` 里**没有** G5，将来也不许有 —— 片子交不交是人的事。

    `qc --final` 只给"更好的眼睛"：它的结论**不进**门禁判定（见 lvs/qc_final.py 顶部）。
    """
    assert "G5" not in criteria.CRITERIA
    assert criteria.has_criterion("G5") is False
    assert "G5" in {gate.id for gate in pipeline.GATES}, "门还在，只是没有自动判据"


def test_every_criterion_gate_is_a_known_gate():
    """反向钉一下：判据表里的门 id 都是真门（防手滑写成 G9 这种没人读的键）。"""
    known = {gate.id for gate in pipeline.GATES}
    assert set(criteria.CRITERIA) <= known


# ---- 真跑 ffmpeg（slow）-------------------------------------------------------


@pytest.mark.slow
@pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg 不可用")
def test_qc_final_passes_on_a_real_short_film(tmp_path, capsys):
    ws = Workspace(task="t", root=tmp_path).ensure()
    film, srt = ws.path("final.mp4"), ws.path("subtitle.srt")
    _make_film(film)
    srt.write_text(SRT, encoding="utf-8", newline="\n")

    code = qc_final.run_command(None, ws, _ns())

    assert code == EXIT_OK, capsys.readouterr().out
    report = json.loads(ws.path("qc", qc_final.REPORT_NAME).read_text(encoding="utf-8"))
    assert report["ok"] is True and report["counts"]["error"] == 0
    assert schemas.validate("qc", report) == []
    assert sorted(p.name for p in ws.path("qc", qc_final.FRAME_DIRNAME).glob("*.png")) == [
        "frame-1.png", "frame-2.png", "frame-3.png", "frame-4.png"]
    # ★ 只读：不许凭空造出门禁账本 / 不许把 manifest 顶掉
    assert not ws.path("gates.json").exists()
    assert report["checks"][0]["id"] == "probe"


@pytest.mark.slow
@pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg 不可用")
def test_qc_final_errors_when_the_audio_track_is_gone(tmp_path, capsys):
    """★ 验收③后半：**抽掉音轨**的坏样本必须报错（这类片子画面好好的，只是没声音）。"""
    ws = Workspace(task="t", root=tmp_path).ensure()
    film, srt = ws.path("final.mp4"), ws.path("subtitle.srt")
    _make_film(film, audio=False)
    srt.write_text(SRT, encoding="utf-8", newline="\n")

    code = qc_final.run_command(None, ws, _ns())
    out = capsys.readouterr().out

    assert code == EXIT_FAILED, out
    report = json.loads(ws.path("qc", qc_final.REPORT_NAME).read_text(encoding="utf-8"))
    assert report["ok"] is False
    levels = {c["id"]: c["level"] for c in report["checks"]}
    assert levels["audio"] == "error"
    assert levels["frames"] == "ok", "抽掉音轨不该让别的判据跟着红（逐条独立）"
    assert "没有音轨" in out


@pytest.mark.slow
@pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg 不可用")
def test_cli_qc_final_exit_codes(tmp_path):
    """真敲 `python -m lvs qc --final`：好片 0 / 坏样本 1（验收③的端到端形态）。"""
    cfg = tmp_path / "config.toml"
    cfg.write_text("[app]\n", encoding="utf-8", newline="\n")
    good_dir, bad_dir = tmp_path / "good", tmp_path / "bad"
    good_dir.mkdir()
    bad_dir.mkdir()
    good, bad, out = good_dir / "final.mp4", bad_dir / "final.mp4", tmp_path / "report.json"
    _make_film(good)
    _make_film(bad, audio=False)
    for d in (good_dir, bad_dir):
        (d / "subtitle.srt").write_text(SRT, encoding="utf-8", newline="\n")

    def run(video: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "lvs", "qc", "--final", "--task", "qcprobe",
             "--video", str(video), "--out", str(out), "--config", str(cfg)],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", timeout=300,
        )

    first = run(good)
    assert first.returncode == 0, first.stdout + first.stderr
    assert json.loads(out.read_text(encoding="utf-8"))["ok"] is True

    second = run(bad)
    assert second.returncode == 1, second.stdout + second.stderr
    assert "没有音轨" in second.stdout
    assert json.loads(out.read_text(encoding="utf-8"))["ok"] is False
