"""ffmpeg / ffprobe 封装。

统一全片编码参数（spec §13 票据 13 的"同一约定"），并提供几个小工具：
- `tools()`          定位 ffmpeg / ffprobe（PATH → 仓库 .tools/ → imageio-ffmpeg 内置）
- `run()`            跑一条命令，失败时抛 `FFmpegError`（含尾部输出）
- `probe()`          读媒体信息（**无 ffprobe 时用 `ffmpeg -i` 降级解析**）
- `duration()`       媒体时长

设计取舍：**不引入 moviepy**（spec §12）—— 长视频下它内存与稳定性都差。
所有合成都是"原生 ffmpeg 命令行 + 少量 Python 编排"。

关于 ffprobe：Windows 上不装系统级 ffmpeg 也能跑 —— 若只有 `imageio-ffmpeg`
提供的 ffmpeg.exe，则时长/尺寸改由 `ffmpeg -i` 的 stderr 解析（精度足够）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from lvs.config import PROJECT_ROOT
from lvs.errors import LvsError, EXIT_FAILED

# ---- 全片统一编码约定（下游一律按这套参数，避免反复转码） --------------------

VIDEO_W = 1920
VIDEO_H = 1080
VIDEO_FPS = 30
PIX_FMT = "yuv420p"

AUDIO_AR = 44100          # 采样率
AUDIO_AC = 1              # 声道数（旁白单声道即可，体积小、下游不用再重采样）
AUDIO_BR = "192k"

LOCAL_TOOLS = PROJECT_ROOT / ".tools"

_RE_DURATION = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_RE_SIZE = re.compile(r"Video:.*?(\d{2,5})x(\d{2,5})")


class FFmpegError(LvsError, RuntimeError):
    """ffmpeg/ffprobe 执行失败。消息面向用户，含命令尾部输出便于排查。"""

    exit_code = EXIT_FAILED


def _local_binary(name: str) -> str | None:
    """仓库内 `.tools/<name>`（可选的便携安装位置）。"""
    candidate = LOCAL_TOOLS / name
    return str(candidate) if candidate.is_file() else None


def _imageio_ffmpeg() -> str | None:
    """退路：`imageio-ffmpeg` 自带的静态 ffmpeg（PyPI 装得上，无需系统安装）。"""
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def tools() -> tuple[str, str]:
    """返回 (ffmpeg, ffprobe)。ffprobe 可能为空串（调用方需容忍）。"""
    ffmpeg = shutil.which("ffmpeg") or _local_binary("ffmpeg.exe") or _imageio_ffmpeg()
    ffprobe = shutil.which("ffprobe") or _local_binary("ffprobe.exe") or ""
    if not ffmpeg:
        raise FFmpegError(
            "未找到 ffmpeg（`lvs build` / `lvs voice` 必需）。\n"
            "  A) 推荐：pip install imageio-ffmpeg（本项目的 venv 已内置）\n"
            "  B) 系统安装：winget install Gyan.FFmpeg（装完重开终端）\n"
            "  C) 或下载 https://www.gyan.dev/ffmpeg/builds/ 的 release-essentials，"
            "把 bin 目录加入 PATH。装好后用 `lvs doctor` 复检。"
        )
    return ffmpeg, ffprobe


def run(args: list[str], *, timeout: int = 7200, cwd: Path | None = None) -> str:
    """跑一条 ffmpeg/ffprobe 命令，返回 stdout。失败抛 `FFmpegError`。"""
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise FFmpegError(f"命令不可执行：{args[0]}（{exc}）") from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"命令超时（{timeout}s）：{' '.join(args[:6])} …") from exc

    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-12:])
        raise FFmpegError(f"ffmpeg 执行失败（{proc.returncode}）：\n  $ {' '.join(args[:8])} …\n{tail}")
    return proc.stdout or ""


def _probe_via_ffmpeg(path: Path) -> dict[str, Any]:
    """无 ffprobe 时的降级：解析 `ffmpeg -i` 的 stderr（duration / 尺寸）。"""
    ffmpeg, _ = tools()
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    text = proc.stderr or ""
    info: dict[str, Any] = {"format": {}, "streams": []}
    m = _RE_DURATION.search(text)
    if m:
        info["format"]["duration"] = str(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)))
    m2 = _RE_SIZE.search(text)
    if m2:
        info["streams"].append(
            {"codec_type": "video", "width": int(m2.group(1)), "height": int(m2.group(2))}
        )
    if "Audio:" in text:
        info["streams"].append({"codec_type": "audio"})
    return info


def probe(path: Path) -> dict:
    """探测媒体信息，返回统一结构的 dict（失败返回空 dict）。"""
    _, ffprobe = tools()
    if ffprobe:
        try:
            out = run(
                [
                    ffprobe, "-v", "error", "-print_format", "json",
                    "-show_format", "-show_streams", str(path),
                ],
                timeout=60,
            )
            return json.loads(out or "{}")
        except (FFmpegError, json.JSONDecodeError):
            return {}
    return _probe_via_ffmpeg(path)


def duration(path: Path) -> float | None:
    """媒体时长（秒）；读不到返回 None。"""
    p = Path(path)
    if not p.is_file():
        return None
    data = probe(p)
    fmt = data.get("format") or {}
    if fmt.get("duration"):
        try:
            return float(fmt["duration"])
        except (TypeError, ValueError):
            pass
    for stream in data.get("streams", []):
        if stream.get("duration"):
            try:
                return float(stream["duration"])
            except (TypeError, ValueError):
                continue
    return None


def has_audio_stream(path: Path) -> bool:
    """这个文件有没有音轨。读不到信息时**返回 True**（宁可漏报，不误报）。"""
    p = Path(path)
    if not p.is_file():
        return False
    data = probe(p)
    streams = data.get("streams")
    if not streams:
        # 探测失败（无 ffprobe 且降级解析也没读到）→ 不妄下结论
        return True
    return any(s.get("codec_type") == "audio" for s in streams)


def _run_filter(path: Path, filter_args: list[str], timeout: int = 300) -> str:
    """跑一个只做分析的 filter（`-f null -`），返回 stderr+stdout。

    分析类 filter 的结论都打在 stderr 上（`volumedetect` / `blackdetect`），
    而 `run()` 只回 stdout —— 所以这里自己收。
    **不因返回码非 0 抛错**：分析失败了就是"没结论"，由调用方按"跳过"处理。
    """
    ffmpeg, _ = tools()
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostats", "-i", str(path), *filter_args, "-f", "null", "-"],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stderr or "") + (proc.stdout or "")


_RE_VOLUME = re.compile(r"(mean|max)_volume:\s*(-?[\d.]+|-inf)\s*dB")


def _db_of(raw: str) -> float | None:
    """`volumedetect` 的 dB 串 → float。`-inf` 是**结论**（整轨静音），不是"没测出来"。"""
    if raw == "-inf":
        return float("-inf")
    try:
        return float(raw)
    except ValueError:
        return None


def volume_stats_db(path: Path) -> tuple[float | None, float | None]:
    """整轨音量 `(平均, 峰值)`（dBFS）；读不到给 None。

    用 `volumedetect`：`mean_volume: -inf dB` 表示**整轨静音**（返回 `float('-inf')`
    而不是 None，好让"确实是静音"与"没测出来"两种情形区分开）。
    峰值用来判**削波**：贴着 0 dBFS（≥ -0.1）说明某处已经削平了。

    一次 `volumedetect` 就把两个数都读出来 —— 成片自检里这两条判据总是要一起看的。
    """
    p = Path(path)
    if not p.is_file():
        return None, None
    text = _run_filter(p, ["-af", "volumedetect"])
    found = {kind: _db_of(raw) for kind, raw in _RE_VOLUME.findall(text)}
    return found.get("mean"), found.get("max")


def mean_volume_db(path: Path) -> float | None:
    """整轨平均音量（dBFS）；读不到返回 None（语义同 `volume_stats_db()[0]`）。"""
    return volume_stats_db(path)[0]


_RE_BLACK = re.compile(r"black_duration:\s*([\d.]+)")


def black_ratio(path: Path, *, threshold: float = 0.10, min_duration: float = 0.5) -> float | None:
    """黑屏时长占总时长的比例（0–1）；读不到返回 None。

    用 `blackdetect`：只对**连续黑屏**计时，所以"偶尔几帧黑"不会被算进来。
    这是 2026 年同类工具（OpenMontage 等）的成片终检做法之一：
    "整片基本是黑的"是流水线坏掉的强信号（图没生成、编码出错）。
    """
    p = Path(path)
    if not p.is_file():
        return None
    total = duration(p)
    if not total or total <= 0:
        return None
    text = _run_filter(
        p,
        ["-vf", f"blackdetect=d={min_duration}:pix_th={threshold}", "-an"],
    )
    spans = [float(x) for x in _RE_BLACK.findall(text)]
    if not spans:
        return 0.0    # 没有黑屏区间 = 0（这是结论，不是"读不到"）
    return min(1.0, sum(spans) / total)
