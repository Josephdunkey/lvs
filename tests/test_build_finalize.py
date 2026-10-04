"""`build._finalize` 的**原子落盘**：`final.mp4` 先写 `.part` 再归位。

## 为什么单独测这一组

`build` 原先让 ffmpeg **直写** `final.mp4`。ffmpeg 是「先建容器、最后写 moov」，
中途被打断（Ctrl-C / 崩溃 / 磁盘满）会留下一个**能看大小、打不开**的 mp4
（`moov atom not found`）—— 它长得和成片一模一样，实测有人把它拷进 `07-成片/`
当成品发出去（票据 47，且**不记账**，`lvs status` 看不出来）。所以判据是：

1. 失败时：目录里**没有** `final.mp4`，或它保持**旧的完整内容**；半成品必被清掉
2. 成功时：内容与 ffmpeg 写进 `.part` 的完全一致，`.part` 不残留
3. 真跑一次 ffmpeg：`.part` 扩展名 + 显式 `-f mp4` 真能封装（不是纸上谈兵）

★ 第 3 条不能省：`.part` 猜不出封装格式，漏了 `-f mp4` 会报
"Unable to find a suitable output format" —— 而假 ffmpeg 永远发现不了这点。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lvs import build
from lvs.config import Config
from lvs.ffmpeg import FFmpegError
from lvs.workspace import Workspace


def _ws(tmp_path: Path) -> Workspace:
    ws = Workspace(task="t", root=tmp_path).ensure()
    ws.path("video_track.mp4").write_bytes(b"TRACK")
    ws.path("narration.wav").write_bytes(b"AUDIO")
    return ws


def _cfg() -> Config:
    return Config({"build": {}}, None)


def _finalize(ws: Workspace):  # noqa: ANN202
    return build._finalize(
        ws, ws.path("video_track.mp4"), ws.path("narration.wav"),
        ws.path("subtitle.srt"), _cfg(),
    )


def test_finalize_writes_part_then_commits(tmp_path: Path, monkeypatch):
    ws = _ws(tmp_path)
    monkeypatch.setattr(build, "tools", lambda: ("ffmpeg", ""))
    seen: dict = {}

    def fake_run(args, **kw):  # noqa: ANN001, ANN003, ANN202
        seen["target"] = Path(args[-1])
        seen["args"] = list(args)
        Path(args[-1]).write_bytes(b"MOVIE")      # 模拟 ffmpeg 真写盘
        return ""

    monkeypatch.setattr(build, "ff_run", fake_run)
    out, burned = _finalize(ws)

    assert out == ws.path("final.mp4")
    assert seen["target"].name == "final.mp4.part", "必须先写 .part（直写成片 = 中断留坏成品）"
    assert "-f" in seen["args"] and "mp4" in seen["args"], (
        "`.part` 扩展名推不出封装格式，必须显式 `-f mp4`"
    )
    assert out.read_bytes() == b"MOVIE", "原子归位不许改内容"
    assert not list(ws.dir.glob("*.part")), ".part 不该残留"
    assert burned is False


def test_finalize_leaves_no_fake_movie_when_interrupted(tmp_path: Path, monkeypatch):
    """★ 核心回归：中断后**不许**留下「能看大小、打不开」的 `final.mp4`。"""
    ws = _ws(tmp_path)
    monkeypatch.setattr(build, "tools", lambda: ("ffmpeg", ""))

    def fake_run(args, **kw):  # noqa: ANN001, ANN003, ANN202
        Path(args[-1]).write_bytes(b"HALF-WRITTEN")   # 容器建了、moov 还没写
        raise FFmpegError("killed")

    monkeypatch.setattr(build, "ff_run", fake_run)
    with pytest.raises(FFmpegError):
        _finalize(ws)

    assert not ws.path("final.mp4").exists(), "半成品被当成成片留在正式名上了"
    assert not list(ws.dir.glob("*.part")), "半成品 .part 必须清掉"


def test_finalize_keeps_previous_movie_when_interrupted(tmp_path: Path, monkeypatch):
    """重跑失败不许毁掉上一次的完整成片（这是「先写 .part」最大的实用收益）。"""
    ws = _ws(tmp_path)
    good = ws.path("final.mp4")
    good.write_bytes(b"GOOD-MOVIE")
    monkeypatch.setattr(build, "tools", lambda: ("ffmpeg", ""))

    def fake_run(args, **kw):  # noqa: ANN001, ANN003, ANN202
        Path(args[-1]).write_bytes(b"HALF")
        raise FFmpegError("killed")

    monkeypatch.setattr(build, "ff_run", fake_run)
    with pytest.raises(FFmpegError):
        _finalize(ws)

    assert good.read_bytes() == b"GOOD-MOVIE", "旧成片被半成品顶掉了"
    assert not list(ws.dir.glob("*.part"))


@pytest.mark.slow
def test_finalize_real_ffmpeg_writes_playable_mp4(tmp_path: Path):
    """真跑 ffmpeg（1 秒素材）：`.part` + `-f mp4` 必须真能封装成可探测的 mp4。"""
    from lvs.ffmpeg import probe, run as ff_run, tools

    try:
        ffmpeg, _ = tools()
    except FFmpegError as exc:  # pragma: no cover
        pytest.skip(f"ffmpeg 不可用：{exc}")

    ws = Workspace(task="t", root=tmp_path).ensure()
    track, narr = ws.path("video_track.mp4"), ws.path("narration.wav")
    # 轨道用 mpeg4（几乎所有构建都自带）：本用例只关心**封装**，不关心编码器
    ff_run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=10:d=1",
        "-pix_fmt", "yuv420p", "-c:v", "mpeg4", str(track),
    ])
    ff_run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
        "-t", "1", "-c:a", "pcm_s16le", str(narr),
    ])

    out, burned = build._finalize(ws, track, narr, ws.path("subtitle.srt"), _cfg())

    assert out.is_file() and out.stat().st_size > 0
    assert not ws.path("final.mp4.part").exists(), ".part 没被归位"
    info = probe(out)
    assert info.get("streams"), "成片探不出流 = 封装坏了（`.part` + `-f mp4` 这套没起作用）"
    assert burned is False


def test_finalize_raises_ffmpeg_error_when_ffmpeg_writes_nothing(tmp_path: Path, monkeypatch):
    """ffmpeg 返回 0 却没产物 → 必须是 `FFmpegError`，**不是裸 `FileNotFoundError`**。

    否则 `finalize` 的「BGM 失败 → 退回无 BGM」外壳接不住它，会直接炸穿到调用方。
    这条是**全量回归抓出来的**（`tests/test_bgm.py` 三例，2026-10-05）。
    """
    ws = _ws(tmp_path)
    monkeypatch.setattr(build, "tools", lambda: ("ffmpeg", ""))
    monkeypatch.setattr(build, "ff_run", lambda *a, **k: "")      # 假装成功，什么都不写

    with pytest.raises(FFmpegError) as ei:
        _finalize(ws)
    assert "final.mp4.part" in str(ei.value), "错误信息要点名缺的是哪个文件"
    assert not ws.path("final.mp4").exists()


def test_bgm_fallback_survives_a_missing_part(tmp_path: Path, monkeypatch):
    """BGM 第一次「成功但没产物」→ 退回无 BGM 重试，最终仍出成片（不许炸穿）。"""
    ws = _ws(tmp_path)
    bgm = tmp_path / "bgm.mp3"
    bgm.write_bytes(b"x")
    monkeypatch.setattr(build, "tools", lambda: ("ffmpeg", ""))
    monkeypatch.setattr(build, "ff_duration", lambda p: 1.0)
    calls: list[list[str]] = []

    def fake_run(args, **kw):  # noqa: ANN001, ANN003, ANN202
        calls.append(list(args))
        if len(calls) == 1:
            return ""            # 带 BGM 那次：静默失败（不产出）
        Path(args[-1]).write_bytes(b"MOVIE")
        return ""

    monkeypatch.setattr(build, "ff_run", fake_run)
    cfg = Config({"bgm": {"file": str(bgm)}}, None)
    out, burned = build.finalize(
        ws, ws.path("video_track.mp4"), ws.path("narration.wav"),
        ws.path("subtitle.srt"), cfg,
    )

    assert len(calls) == 2, "第一次带 BGM 失败后必须退回无 BGM 重试"
    assert out.read_bytes() == b"MOVIE"
    assert burned is False
