"""成片自检（`lvs qc --final`）：**ffprobe + N 帧抽检 + 音频静音/削波 + 字幕存在**。

## 为什么要有它（提案 §3 B3）

G5"成片"是六道门里**唯一没有任何机器判据**的一道 —— `criteria.py` 里写着
"成片能不能交付本质上是人的事"，这句话对，但它带来一个缺口：
**机器能查的那部分也没人查**。实测踩过的形状（都与"不报错、只是片子不对"同族）：

- `build` 因为僵尸 ffmpeg 卡在 shot-233 → 成片少了后半段，没人知道；
- 音轨整轨静音（配音失败但片段还在）→ 画面好好的，就是没声音；
- 字幕没烧上 / `subtitle.srt` 是空的 → 交出去才发现；
- 抽帧一抽是黑帧（图没生成、编码出错）。

本模块把这些**能量化的**先过一遍，出一份 `qc/final-report.json`（结构见
`schemas/qc.schema.json`）与一段人话结论。

## ★ 默认只报警不改判（本模块最重要的一条语义）

`qc --final` 的结论**不进 G5 门禁判定** —— `criteria.CRITERIA` 里没有、也不许有 `G5`：
门禁是"人批准 AND 判据通过"，而"片子能不能交付"最终是人的判断（`criteria.py` 的原则不破）。
这里做的是**给人一双更好的眼睛**，不是替人放行：

- 它**只读**：不写 `gates.json`、不写 `manifest.json`、不动 `shots.json`；
- 它失败（退出码 1）只说明"有几条能量化的判据没过"，G5 该不该批仍然是人说了算；
- `build` 阶段本来就有一条终检（音轨存在 / 接近静音 / 整片黑屏，见 `build.sanity_*`）——
  那条是**自动流水线的一部分**；本命令是**手动、更深**的一次（多查分辨率、抽帧、字幕）。

## 分层

`inspect_film()` 是深模块：给它一个视频（+ 字幕）→ 一份结构化报告，判据全在里面；
`run_command()` 只负责"路径怎么定、报告写哪、怎么打印"。测试因此可以
既钉住纯逻辑（黑帧阈值 / srt 解析 / 报告结构），又用 slow 组跑真 ffmpeg。
"""

from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from lvs import artifact, ffmpeg
from lvs.errors import EXIT_FAILED, EXIT_OK, EXIT_USAGE

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"

#: 默认抽几帧。4 帧 = 头/次头/次尾/尾，够抓住"整片黑"与"某段没图"。
DEFAULT_FRAMES = 4
#: 抽出来的帧留在这里（PNG）—— **人还要看一眼**，不是纯中间产物。
FRAME_DIRNAME = "frames"
REPORT_NAME = "final-report.json"

#: 判"黑帧"的两个闸门（都过才算）：平均亮度极低**且**几乎没有起伏。
#: 只要起伏够大（有内容的暗场戏）就不算黑帧 —— 宁可不报，也别把合法的夜景当故障。
BLACK_MEAN_LUMA = 6.0
BLACK_STD_LUMA = 8.0
#: 整轨平均音量低于此值 = 近乎静音（与 `[build].sanity_min_mean_db` 同口径）。
SILENT_MEAN_DB = -60.0
#: 峰值贴到这个数以上 = 可能削波（只警告）。
CLIP_MAX_DB = -0.1
#: 分辨率低于这个数只**警告**（多半不是成片，但小样片也合法）。
MIN_WIDTH = 640
MIN_HEIGHT = 360
#: 字幕最后一条离片尾差太远 → 警告（可能只烧了前半段）。
SUBTITLE_COVER_MIN = 0.8

_RE_SRT_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


# ---- 纯逻辑（fast 组可测，不碰 ffmpeg）--------------------------------------


def hms(seconds: float | None) -> str:
    """秒 → `H:MM:SS`（给人看的摘要用）。"""
    if seconds is None:
        return "?"
    total = int(max(0.0, float(seconds)))
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def is_black(mean_luma: float, std_luma: float) -> bool:
    """一帧是不是**黑帧**：平均亮度极低且几乎没有起伏（见 `BLACK_MEAN_LUMA`）。"""
    return float(mean_luma) <= BLACK_MEAN_LUMA and float(std_luma) <= BLACK_STD_LUMA


def frame_times(duration: float, count: int = DEFAULT_FRAMES) -> list[float]:
    """在片长里均分取 `count` 个时间点（取每段的**中点**，避开片头片尾的黑/淡入）。"""
    count = max(1, int(count))
    if duration is None or duration <= 0:
        return []
    return [duration * (i + 0.5) / count for i in range(count)]


def srt_last_end(text: str) -> float | None:
    """`.srt` 里最后一条字幕的结束时间（秒）；解析不出返回 None。"""
    last: float | None = None
    for line in text.splitlines():
        stamp = _RE_SRT_TIME.findall(line)
        if len(stamp) < 2:      # 一条时间轴有两个时间码：`00:00:01,000 --> 00:00:03,000`
            continue
        h, m, s, ms = stamp[1]
        last = int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000
    return last


def srt_cue_count(text: str) -> int:
    """`.srt` 有几条字幕（按时间轴行数点）。"""
    return sum(1 for line in text.splitlines() if len(_RE_SRT_TIME.findall(line)) >= 2)


def frame_metrics(path: Path) -> tuple[float, float] | None:
    """一帧的 `(平均亮度, 亮度标准差)`（0–255）。没 Pillow / 读不出来 → None。"""
    try:
        from PIL import Image, ImageStat
    except ImportError:  # 没有 image extra：黑帧判据跳过（说实话，不装作查过）
        return None
    try:
        with Image.open(path) as img:
            gray = img.convert("L")
            gray.thumbnail((64, 64))
            stat = ImageStat.Stat(gray)
        return float(stat.mean[0]), float(stat.stddev[0])
    except (OSError, ValueError):
        return None


def _check(cid: str, level: str, summary: str, **detail: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"id": cid, "level": level, "summary": summary}
    if detail:
        out["detail"] = detail
    return out


def _worst_level(checks: list[dict[str, Any]]) -> str:
    if any(c["level"] == LEVEL_ERROR for c in checks):
        return LEVEL_ERROR
    if any(c["level"] == LEVEL_WARN for c in checks):
        return LEVEL_WARN
    return LEVEL_OK


# ---- 真跑 ffmpeg 的四条判据 ---------------------------------------------------


def _fps(stream: dict[str, Any]) -> float | None:
    """`avg_frame_rate`（`30000/1001` 这种）→ float。"""
    raw = str(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "")
    if "/" in raw:
        num, _, den = raw.partition("/")
        try:
            return float(num) / float(den) if float(den) else None
        except ValueError:
            return None
    try:
        return float(raw) or None
    except ValueError:
        return None


def probe_checks(video: Path) -> tuple[list[dict[str, Any]], float | None]:
    """ffprobe：文件在不在 / 时长 / 流 / 分辨率 / 帧率。"""
    info = ffmpeg.probe(video)
    streams = info.get("streams") or []
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    duration: float | None = None
    fmt = info.get("format") or {}
    for raw in (fmt.get("duration"), (video_stream or {}).get("duration")):
        try:
            if raw:
                duration = float(raw)
                break
        except (TypeError, ValueError):
            continue

    size_mb = video.stat().st_size / (1024 * 1024) if video.is_file() else 0.0
    if video_stream is None:
        probe = _check("probe", LEVEL_ERROR, "ffprobe 没读出视频流 —— 文件不是可解码的视频")
    elif not duration:
        probe = _check("probe", LEVEL_ERROR, "读不出时长（文件可能截断/损坏）")
    else:
        probe = _check("probe", LEVEL_OK, f"时长 {hms(duration)}（{duration:.1f}s）｜{size_mb:.1f} MB",
                       duration_s=duration, size_bytes=int(size_mb * 1024 * 1024))

    if video_stream is None:
        return [probe, _check("streams", LEVEL_ERROR, "没有视频流")], duration

    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    fps = _fps(video_stream)
    codec = str(video_stream.get("codec_name") or "?")
    # ★ 音轨"在不在"要看**流在不在**，不能看 codec_name 有没有 ——
    # 没装 ffprobe 时 `probe()` 走 `ffmpeg -i` 降级解析，它认得出音轨但不给 codec_name
    # （实测：摘要印"无音轨"、audio 那条却印"音轨存在"，自相矛盾）。
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    acodec = str(audio_stream.get("codec_name") or "?") if audio_stream else None
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        level = LEVEL_WARN
        tail = f"（低于 {MIN_WIDTH}x{MIN_HEIGHT}，多半不是成片？）"
    else:
        level = LEVEL_OK
        tail = ""
    if audio_stream is None:
        audio_part = "｜**无音轨**"
    elif acodec and acodec != "?":
        audio_part = f"｜音频 {acodec}"
    else:
        audio_part = "｜有音轨（编码未读出）"
    summary = (f"{width}x{height}" + (f" @ {fps:.2f}fps" if fps else "") + f"｜{codec}"
               + audio_part + tail)
    streams_check = _check("streams", level, summary, width=width, height=height,
                           fps=fps, vcodec=codec, acodec=acodec)
    return [probe, streams_check], duration


def frames_check(video: Path, duration: float | None, *, count: int,
                 frame_dir: Path | None) -> dict[str, Any]:
    """抽 N 帧：每帧**存在**且**不是黑帧**。"""
    times = frame_times(duration or 0.0, count)
    if not times:
        return _check("frames", LEVEL_ERROR, "读不出时长，抽不了帧（先修 probe 那条）")

    made: list[dict[str, Any]] = []
    tmp: tempfile.TemporaryDirectory[str] | None = None
    outdir = frame_dir
    if outdir is None:
        tmp = tempfile.TemporaryDirectory(prefix="lvs-qc-frames-")
        outdir = Path(tmp.name)
    try:
        outdir.mkdir(parents=True, exist_ok=True)
        bad: list[str] = []
        for idx, at in enumerate(times):
            out = outdir / f"frame-{idx + 1}.png"
            try:
                ffmpeg.run([ffmpeg.tools()[0], "-hide_banner", "-nostats", "-v", "error",
                            "-ss", f"{at:.3f}", "-i", str(video), "-frames:v", "1", "-y", str(out)],
                           timeout=180)
            except ffmpeg.FFmpegError as exc:
                bad.append(f"第 {idx + 1} 帧抽不出来（{at:.1f}s）：{str(exc).splitlines()[0]}")
                continue
            if not out.is_file() or out.stat().st_size == 0:
                bad.append(f"第 {idx + 1} 帧没落盘（{at:.1f}s）")
                continue
            metrics = frame_metrics(out)
            if metrics is None:
                made.append({"at": round(at, 2), "path": str(out)})
                continue
            mean_luma, std_luma = metrics
            entry = {"at": round(at, 2), "path": str(out),
                     "mean_luma": round(mean_luma, 1), "std_luma": round(std_luma, 1)}
            made.append(entry)
            if is_black(mean_luma, std_luma):
                bad.append(f"第 {idx + 1} 帧是**黑帧**（{at:.1f}s，平均亮度 {mean_luma:.1f}）")

        if bad:
            return _check("frames", LEVEL_ERROR,
                          f"{len(bad)}/{len(times)} 帧有问题：" + "；".join(bad[:4]), frames=made)
        judged = sum(1 for m in made if "mean_luma" in m)
        tail = "" if judged == len(made) else "（没装 Pillow，只验了帧存在）"
        return _check("frames", LEVEL_OK,
                      f"{len(made)}/{len(times)} 帧抽检通过（存在且非黑帧）{tail}", frames=made)
    finally:
        if tmp is not None:
            tmp.cleanup()


def audio_check(video: Path) -> dict[str, Any]:
    """音轨存在 + 整轨静音（error）/ 削波（warn）。"""
    if not ffmpeg.has_audio_stream(video):
        return _check("audio", LEVEL_ERROR, "成片**没有音轨** —— 交出去只有画面")
    mean_db, max_db = ffmpeg.volume_stats_db(video)
    detail = {"mean_db": mean_db, "max_db": max_db}
    if mean_db is None:
        return _check("audio", LEVEL_WARN, "音轨存在，但音量测不出来（volumedetect 没给结论）", **detail)
    if mean_db == float("-inf") or mean_db <= SILENT_MEAN_DB:
        return _check("audio", LEVEL_ERROR,
                      f"音频近乎静音（平均 {mean_db:.1f} dBFS ≤ {SILENT_MEAN_DB:.0f}）", **detail)
    text = f"音轨存在｜平均 {mean_db:.1f} dBFS" + (f"｜峰值 {max_db:.1f} dBFS" if max_db is not None else "")
    if max_db is not None and max_db >= CLIP_MAX_DB:
        return _check("audio", LEVEL_WARN, text + " —— 峰值贴 0 dBFS，可能削波", **detail)
    return _check("audio", LEVEL_OK, text, **detail)


def subtitle_check(srt: Path | None, duration: float | None) -> dict[str, Any]:
    """字幕存在 + 有内容（+ 末条离片尾别太远）。"""
    if srt is None or not Path(srt).is_file():
        return _check("subtitle", LEVEL_ERROR,
                      f"没有字幕文件（{srt or '未指定'}）—— 先跑 `lvs voice`，或确认成片是否要烧字幕")
    path = Path(srt)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _check("subtitle", LEVEL_ERROR, f"字幕读不了：{exc}")
    cues = srt_cue_count(text)
    if cues == 0:
        return _check("subtitle", LEVEL_ERROR, f"{path.name} 里没有任何字幕条目（空文件？）",
                      path=str(path), cues=0)
    last = srt_last_end(text)
    detail = {"path": str(path), "cues": cues, "last_end_s": last}
    if duration and last and last < duration * SUBTITLE_COVER_MIN:
        return _check("subtitle", LEVEL_WARN,
                      f"{path.name}：{cues} 条，但末条停在 {hms(last)}（片长 {hms(duration)}）"
                      " —— 字幕可能只覆盖了前半段", **detail)
    return _check("subtitle", LEVEL_OK, f"{path.name}：{cues} 条字幕", **detail)


# ---- 汇总 --------------------------------------------------------------------


def inspect_film(video: Path | str, *, subtitle: Path | str | None = None, frames: int = DEFAULT_FRAMES,
                 frame_dir: Path | None = None, task: str = "", now: datetime | None = None) -> dict[str, Any]:
    """跑完四条判据，返回报告（结构见 `schemas/qc.schema.json`）。"""
    video = Path(video)
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    if not video.is_file():
        report = {
            "stage": "qc-final", "task": task, "video": str(video),
            "subtitle": str(subtitle) if subtitle else None,
            "checked_at": stamp, "ok": False, "duration_s": None,
            "counts": {LEVEL_OK: 0, LEVEL_WARN: 0, LEVEL_ERROR: 1},
            "checks": [_check("probe", LEVEL_ERROR, f"找不到成片文件：{video}")],
        }
        return report

    checks, duration = probe_checks(video)
    if checks[-1]["level"] == LEVEL_ERROR:      # 没有视频流：抽帧必然全失败，直接说清
        checks.append(_check("frames", LEVEL_ERROR, "没有可解码的视频流，抽帧跳过"))
    else:
        checks.append(frames_check(video, duration, count=int(frames), frame_dir=frame_dir))
    checks.append(audio_check(video))
    checks.append(subtitle_check(subtitle, duration))
    counts = {
        LEVEL_OK: sum(1 for c in checks if c["level"] == LEVEL_OK),
        LEVEL_WARN: sum(1 for c in checks if c["level"] == LEVEL_WARN),
        LEVEL_ERROR: sum(1 for c in checks if c["level"] == LEVEL_ERROR),
    }
    return {
        "stage": "qc-final",
        "task": task,
        "video": str(video),
        "subtitle": str(subtitle) if subtitle else None,
        "checked_at": stamp,
        "ok": counts[LEVEL_ERROR] == 0,
        "duration_s": duration,
        "counts": counts,
        "checks": checks,
    }


def render(report: dict[str, Any]) -> str:
    """报告 → 人话（一行一条判据）。"""
    lines = [
        f"成片自检：{report.get('video', '')}",
        f"  结论：{'✓ 通过' if report.get('ok') else '✗ 有 error'}（"
        f"ok {report['counts'][LEVEL_OK]} / warn {report['counts'][LEVEL_WARN]} / "
        f"error {report['counts'][LEVEL_ERROR]}）",
    ]
    mark = {LEVEL_OK: "✓", LEVEL_WARN: "⚠", LEVEL_ERROR: "✗"}
    for check in report.get("checks", []):
        lines.append(f"  {mark.get(check['level'], '·')} [{check['id']}] {check['summary']}")
    if not report.get("ok"):
        lines.append("  ★ 它只说明「有几条能量化的判据没过」，**不自动改变 G5 门禁** —— 片子交不交还是人定。")
    return "\n".join(lines)


def resolve_video(config: Any, ws: Any, args: Any) -> Path:  # noqa: ANN401
    """成片路径：`--video` → `.work/<task>/final.mp4`。"""
    explicit = getattr(args, "video", None)
    if explicit:
        return Path(str(explicit))
    return Path(str(ws.path("final.mp4")))


def resolve_subtitle(video: Path, explicit: str | None = None) -> Path | None:
    """字幕：`--subtitle` 优先 → 否则成片同目录的 `subtitle.srt` / 同名 `.srt`。

    显式给 `none`（或空串）表示**这片子没有单独的字幕文件**（字幕烧进画面了）——
    那条判据就跳过，而不是误报成 error。
    """
    if explicit is not None:
        text = str(explicit).strip()
        if text.lower() in ("", "none", "-"):
            return None
        return Path(text)
    for candidate in (video.parent / "subtitle.srt", video.with_suffix(".srt")):
        if candidate.is_file():
            return candidate
    return None


def run_command(config: Any, ws: Any, args: Any) -> int:  # noqa: ANN401 - 由 cli 传入
    """`lvs qc --final`。退出码：0 通过 / 1 有 error / 2 成片文件不存在。"""
    video = resolve_video(config, ws, args)
    if not video.is_file():
        print(f"找不到成片：{video}\n  先跑 `lvs build`，或用 `--video <路径>` 指定。")
        return EXIT_USAGE

    frames = int(getattr(args, "frames", None) or DEFAULT_FRAMES)
    frame_dir = ws.path("qc", FRAME_DIRNAME) if ws.dir.is_dir() else None
    subtitle = resolve_subtitle(video, getattr(args, "subtitle", None))
    report = inspect_film(video, subtitle=subtitle, frames=frames,
                          frame_dir=frame_dir, task=str(getattr(ws, "task", "")))

    out = getattr(args, "out", None)
    if out:
        report_path: Path | None = Path(str(out))
    elif ws.dir.is_dir():
        report_path = ws.path("qc", REPORT_NAME)
    else:
        report_path = None
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        artifact.atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(render(report))
        if report_path is not None:
            print(f"  报告：{report_path}")
    return EXIT_OK if report["ok"] else EXIT_FAILED
