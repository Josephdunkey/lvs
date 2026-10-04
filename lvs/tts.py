"""配音与字幕（`lvs voice`）—— 票据 10 + 11 + 12 + 20 + 21。

核心设计：**逐分镜合成**——每个分镜的音频时长 = 该镜画面时长，
天然解决"画面与旁白同步"（spec §10）。

可插拔后端（对外语义对齐 OpenAI `/v1/audio/speech`）：
- `EdgeTTSBackend`       一期默认，免费联网，**带词边界时间戳**（字幕直接可用）
- `OpenAISpeechBackend`  二期，指向**你自己启动的**本地 Qwen3-TTS 服务（OpenAI 兼容接口），**不返回时间戳**，
  字幕走"强制对齐"（票据 21）

产物：
- `audio/shot-NNN.mp3|wav`   逐镜音频
- `audio/shot-NNN.json`      词边界（若有），供字幕与幂等复用
- `audio/narration.mp3`      拼接整轨（镜间插入可配置静音）
- `subtitle.srt`             全程字幕
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from lvs import artifact
from lvs import breaker as breaker_mod
from lvs import runlog
from lvs import handoff
from lvs.config import Config
from lvs.ffmpeg import AUDIO_AC, AUDIO_AR, FFmpegError, duration as ff_duration, run as ff_run, tools
from lvs.progress import track
from lvs.workspace import Workspace, stage_status
from lvs.errors import LvsError, EXIT_FAILED

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover
    requests = None  # type: ignore[assignment]


class TTSError(LvsError, RuntimeError):
    """配音失败。"""

    exit_code = EXIT_FAILED


@dataclass
class Boundary:
    offset: float          # 相对该镜音频起点的秒
    duration: float
    text: str


@dataclass
class SynthResult:
    path: Path
    duration: float
    boundaries: list[Boundary] = field(default_factory=list)


# ---- 后端接口 --------------------------------------------------------------


class TTSBackend(Protocol):
    name: str

    def synthesize(self, text: str, out_path: Path, voice: str) -> SynthResult: ...


class EdgeTTSBackend:
    """微软 edge-tts。免费、中文音色多，且能拿到词边界时间戳。"""

    name = "edge"

    # 实测（票据 47）：edge-tts 偶发返回空音频（`No audio was received. Please verify
    # that your parameters are correct.`），同一条文本同参数重试即可成功 —— 是服务端抖动，
    # 不是文本/参数问题。60 镜里一次跑挂 4 镜，重跑一轮就好了。
    # 参数本身合法，所以这里重试；失败三次才真报错。
    ATTEMPTS = 3
    RETRY_WAIT = 1.5

    def __init__(
        self,
        rate: str = "+0%",
        volume: str = "+0%",
        pitch: str = "+0Hz",
        boundary: str = "WordBoundary",
    ) -> None:
        self.rate = rate
        self.volume = volume
        self.pitch = pitch
        # edge-tts 7.x 默认只给句边界；改成词边界，字幕对齐更准
        self.boundary = boundary

    def _once(self, text: str, voice: str) -> tuple[bytes, list[Boundary]]:
        import edge_tts

        async def _run() -> tuple[bytes, list[Boundary]]:
            comm = edge_tts.Communicate(
                text, voice, rate=self.rate, volume=self.volume,
                pitch=self.pitch, boundary=self.boundary,
            )
            audio = bytearray()
            bounds: list[Boundary] = []
            async for chunk in comm.stream():
                ctype = chunk.get("type")
                if ctype == "audio":
                    audio.extend(chunk["data"])
                elif ctype in ("WordBoundary", "SentenceBoundary"):
                    # edge-tts 的时间单位是 100 纳秒
                    bounds.append(
                        Boundary(
                            offset=chunk["offset"] / 1e7,
                            duration=chunk["duration"] / 1e7,
                            text=chunk.get("text", ""),
                        )
                    )
            return bytes(audio), bounds

        return asyncio.run(_run())

    def synthesize(self, text: str, out_path: Path, voice: str) -> SynthResult:
        try:
            import edge_tts  # noqa: F401
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise TTSError("未安装 edge-tts。请 pip install edge-tts（或改用 openai_speech 后端）。") from exc

        audio: bytes = b""
        bounds: list[Boundary] = []
        last: Exception | None = None
        for attempt in range(1, self.ATTEMPTS + 1):
            try:
                audio, bounds = self._once(text, voice)
            except Exception as exc:      # noqa: BLE001 — 网络/服务端抖动一律重试
                last = exc
                audio = b""
            if audio:
                break
            if attempt < self.ATTEMPTS:
                time.sleep(self.RETRY_WAIT * attempt)
        if not audio:
            raise TTSError(
                f"edge-tts 合成失败（需要联网，已重试 {self.ATTEMPTS} 次）："
                f"{last or '服务端未返回音频'}"
            )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)
        dur = ff_duration(out_path) or (bounds[-1].offset + bounds[-1].duration if bounds else 0.0)
        return SynthResult(out_path, dur, bounds)


class OpenAISpeechBackend:
    """本地 Qwen3-TTS 的 OpenAI 兼容服务。

    指向你在 `[tts].base_url` 里配的服务地址（任何 OpenAI `/v1/audio/speech` 兼容实现）。
    官方示例见 Qwen3-TTS 仓库的 `examples/openai_server.py`（只支持克隆音色）。
    """

    name = "openai_speech"

    def __init__(
        self,
        base_url: str,
        response_format: str = "wav",
        timeout: int = 600,
        instruct: str = "",
        mode: str = "",
        language: str = "chinese",
        ref_audio: str = "",
        ref_text: str = "",
        xvec_only: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.response_format = response_format
        self.timeout = timeout
        self.instruct = instruct
        self.mode = mode
        self.language = language
        self.ref_audio = ref_audio
        self.ref_text = ref_text
        self.xvec_only = xvec_only

    def _path(self, sub: str) -> str:
        base = self.base_url
        if base.endswith("/v1"):
            base = base[:-3]
        return f"{base}{sub}"

    def health(self) -> bool:
        if requests is None:
            return False
        try:
            r = requests.get(self._path("/health"), timeout=3)
            return r.status_code < 500
        except Exception:
            return False

    def synthesize(self, text: str, out_path: Path, voice: str) -> SynthResult:
        if requests is None:
            raise TTSError("未安装 `requests`，无法调用本地 TTS 服务。")
        url = self._path("/v1/audio/speech")
        # 扩展字段只在配置里真的填了时才发 —— 保证对只认基础字段的
        # 官方 openai_server.py 仍然兼容（它收到多余字段会忽略，但少发更稳）。
        payload: dict[str, Any] = {
            "input": text,
            "voice": voice,
            "response_format": self.response_format,
        }
        if self.instruct:
            payload["instruct"] = self.instruct
        if self.mode:
            payload["mode"] = self.mode
        if self.language:
            payload["language"] = self.language
        if self.ref_audio:
            payload["ref_audio"] = self.ref_audio
            payload["ref_text"] = self.ref_text
            payload["xvec_only"] = bool(self.xvec_only)
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
        except Exception as exc:
            raise TTSError(
                f"本地 TTS 服务不可达：{url}\n"
                f"  请先启动服务（见 README「本地配音」），确认端口与 [tts].base_url 一致。\n  原因：{exc}"
            ) from exc
        if resp.status_code >= 400:
            raise TTSError(f"本地 TTS 返回 HTTP {resp.status_code}：{resp.text[:200]}")
        if not resp.content:
            raise TTSError("本地 TTS 返回了空音频")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(resp.content)
        dur = ff_duration(out_path) or 0.0
        return SynthResult(out_path, dur, boundaries=[])  # 该接口不返回时间戳 → 字幕需强制对齐


class SilentTTSBackend:
    """**静音**后端：按字数估时长、生成等长静音轨。**完全离线，不吃显存、不联网。**

    ## 为什么需要它

    端到端测试要能在**没有网络、没有 ComfyUI、没有 TTS 服务**的机器上跑通
    `parse → shots → assets → voice → build`。既有的两个后端都做不到：
    `edge` 要联网、`openai_speech` 要吃显存。而"整条链没断"这件事
    **只能靠离线自动测试守住**（真机跑一次是数小时，不可能每次改动都跑）。

    ## 时长怎么估

    中文配音大约 **4.5 字/秒**（含标点停顿）。这里按 `chars / rate` 估，
    并对标点加一点停顿权重 —— 不需要准，只要**量级对**（后续对齐、字幕、合成
    都按它算时间轴，估得太离谱会让成片时长明显失真）。

    ## 边界时间戳

    返回空 `boundaries` —— 字幕会走 `build_srt` 的"按字数均摊"回退分支
    （与 `openai_speech` 后端同一条回退路径）。这样测的就是**真实回退逻辑**，
    而不是某个只在测试里存在的分支。
    """

    name = "silent"

    #: 中文配音语速（字/秒）。取值来自 edge-tts 中文音色的常见实测值。
    CHARS_PER_SEC = 4.5
    #: 每个标点额外算的停顿时长（秒）。
    PAUSE_PER_PUNCT = 0.18
    #: 时长下限，避免空文本/极短句产出 0 秒音轨（下游会当成"没有音频"）。
    MIN_DURATION = 0.35

    _PUNCT = "，。！？；：、,.!?;:"

    def estimate_duration(self, text: str) -> float:
        """按字数 + 标点估时长（秒）。"""
        chars = sum(1 for c in (text or "") if not c.isspace())
        pauses = sum(1 for c in (text or "") if c in self._PUNCT)
        return max(self.MIN_DURATION, chars / self.CHARS_PER_SEC + pauses * self.PAUSE_PER_PUNCT)

    def synthesize(self, text: str, out_path: Path, voice: str) -> SynthResult:
        dur = self.estimate_duration(text)
        ffmpeg, _ = tools()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        ff_run([
            ffmpeg, "-y", "-f", "lavfi",
            "-i", f"anullsrc=r={AUDIO_AR}:cl={'mono' if AUDIO_AC == 1 else 'stereo'}",
            "-t", f"{dur:.3f}", "-c:a", "pcm_s16le", str(out_path),
        ])
        actual = ff_duration(out_path) or dur
        return SynthResult(out_path, actual, boundaries=[])


#: **全部**受支持的 TTS 后端 —— 唯一真源。
#:
#: ★ 为什么要有这个常量：加 `silent` 时只改了 `make_backend`，忘了 `doctor`，
#: 于是体检对着一个完全正常的配置报"未知后端"（误导用户去查不存在的问题）。
#: 现在 `doctor` 的提示与测试都从它取，**加后端只改这一处**。
SUPPORTED_BACKENDS: tuple[str, ...] = ("edge", "openai_speech", "silent")


def make_backend(config: Config) -> TTSBackend:
    backend = str(config.get("tts.backend", "edge")).lower()
    if backend == "silent":
        # 离线端到端测试用（也供"只想走通骨架"的人手动选）
        return SilentTTSBackend()
    if backend == "edge":
        return EdgeTTSBackend(
            rate=str(config.get("tts.rate", "+0%")),
            volume=str(config.get("tts.volume", "+0%")),
            pitch=str(config.get("tts.pitch", "+0Hz")),
            boundary=str(config.get("tts.boundary", "WordBoundary")),
        )
    if backend in {"openai_speech", "openai"}:
        base = config.get("tts.base_url", "http://127.0.0.1:8000")
        be = OpenAISpeechBackend(
            str(base),
            instruct=str(config.get("tts.instruct", "")),
            mode=str(config.get("tts.mode", "")),
            language=str(config.get("tts.language", "chinese")),
            ref_audio=str(config.get("tts.ref_audio", "")),
            ref_text=str(config.get("tts.ref_text", "")),
            xvec_only=bool(config.get("tts.xvec_only", False)),
        )
        if not be.health():
            raise TTSError(
                f"配置为 openai_speech，但服务未就绪：{base}\n"
                "  请先启动本地 TTS 服务（任何 OpenAI `/v1/audio/speech` 兼容实现，"
                "如 Qwen3-TTS 的 `examples/openai_server.py`），"
                "并确认端口与 `[tts].base_url` 一致，再重跑 `lvs voice`。"
            )
        return be
    raise TTSError(
        f"未知的 TTS 后端 `{backend}`（应为 {' / '.join(SUPPORTED_BACKENDS)}）。"
    )


# ---- 字幕：字符位置 → 时间 ------------------------------------------------


def _locate(narration: str, needle: str, start: int) -> tuple[int, int] | None:
    """在 narration 里从 `start` 往后找 `needle`，返回 `(起始字序号, 命中字数)`。

    找不到时逐级裁短前缀再试：edge-tts 的词边界与原文并不逐字相等 ——
    标点不进边界（`，。！？——、` 全被吞掉）、空白可能被改写、
    数字/英文有时被读成中文。裁短前缀是为了在"读法不同"时仍能定位到起点，
    剩下的差值交给曲线上的线性插值，比整体漂移小得多。
    """
    if not needle:
        return None
    idx = narration.find(needle, start)
    if idx >= 0:
        return idx, len(needle)
    compact = re.sub(r"\s+", "", needle)
    if compact and compact != needle:
        idx = narration.find(compact, start)
        if idx >= 0:
            return idx, len(compact)
    for cut in range(len(needle) - 1, 0, -1):
        idx = narration.find(needle[:cut], start)
        if idx >= 0:
            return idx, cut
    return None


def char_time_curve(
    boundaries: list[Boundary], narration: str = ""
) -> list[tuple[int, float]]:
    """由词边界构出 `(累计字数, 时间)` 曲线，用于按字数插值取时间。

    **必须对照原文定位**，不能直接把边界文本的字数累加当字号（票据 44）：
    `split_display_lines` 给出的字序号是**含标点**的真实下标，而 edge-tts 的
    词边界**不含标点** —— 实测第一章 66/66 镜的边界字数都比原文少 2~21 字，
    照旧累加会让整条字幕越到句尾越滞后（用户听感即"声音字幕不同步"）。
    这里改为在 `narration` 中顺序 `find` 每个边界文本，拿到它在原文里的真实下标；
    定位不到就**不推进字号**（只记时间），让后续边界自行纠回。

    `narration` 为空时退回"按边界文本长度累加"的旧行为，便于单独测曲线。
    """
    if not boundaries:
        return []
    pts: list[tuple[int, float]] = [(0, boundaries[0].offset)]
    pos = 0
    for b in boundaries:
        text = b.text or ""
        if narration and text:
            hit = _locate(narration, text, pos)
            nxt = (hit[0] + hit[1]) if hit else pos
        elif not narration:
            nxt = pos + len(text)   # 无原文可依：旧行为
        else:
            nxt = pos
        pts.append((nxt, b.offset + b.duration))
        pos = nxt
    # 单调化：裁短前缀偶尔会定位到更靠前的下标，必须夹住，否则插值会倒退
    cleaned: list[tuple[int, float]] = [pts[0]]
    for c, t in pts[1:]:
        pc, pt = cleaned[-1]
        cleaned.append((max(pc, c), max(pt, t)))
    return cleaned


def time_at_chars(curve: list[tuple[int, float]], n: int) -> float:
    """在曲线上取"第 n 个字结束"的时刻（线性插值）。"""
    if not curve:
        return 0.0
    if n <= curve[0][0]:
        return curve[0][1]
    for (c0, t0), (c1, t1) in zip(curve, curve[1:]):
        if c0 <= n <= c1:
            if c1 == c0:
                return t1
            ratio = (n - c0) / (c1 - c0)
            return t0 + (t1 - t0) * ratio
    return curve[-1][1]


# 行首禁则：这些字符不该出现在字幕行开头
_NO_LINE_START = "。！？，、；：）)」』”’…～!?.,;:"
# 行末禁则：这些字符不该出现在字幕行末尾
_NO_LINE_END = "（(「『《【“‘"


def _apply_kinsoku(
    lines: list[tuple[int, int, str]], text: str, max_chars: int, min_tail: int = 5
) -> list[tuple[int, int, str]]:
    """把断行结果按「禁则」微调：行首不吃标点、行末不留左括号、不剩过短的尾行。

    硬切（`len(buf) >= max_chars + 6`）会切在句中的任意位置，于是常出现
    「行首顶着一个逗号」甚至「整行只有标点」的字幕。这里把这些字符挪到相邻行，
    并把过短的尾行并回上一行（并回去后若超出上限则保持原样）。
    拼接结果仍然逐字等于原文。
    """
    if not lines:
        return []
    spans: list[list[int]] = [[s, e] for s, e, _ in lines]
    out: list[list[int]] = [spans[0]]
    for s, e in spans[1:]:
        # 行末禁则：上一行末尾是左括号 → 挪到本行开头
        while out and out[-1][1] > out[-1][0] and text[out[-1][1] - 1] in _NO_LINE_END:
            out[-1][1] -= 1
            s -= 1
        # 行首禁则：本行开头是收尾标点 → 挪到上一行末尾
        while s < e and text[s] in _NO_LINE_START and out:
            out[-1][1] = s + 1
            s += 1
        out = [sp for sp in out if sp[1] > sp[0]]
        if s < e:
            out.append([s, e])
    if not out:
        return [(0, len(text), text)] if text else []

    # 过短的尾行并回上一行（并完不超过上限才并）
    merged: list[list[int]] = [out[0]]
    for s, e in out[1:]:
        if e - s < min_tail and merged and (e - merged[-1][0]) <= max_chars + 6:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e, text[s:e]) for s, e in merged]


def split_display_lines(text: str, max_chars: int) -> list[tuple[int, int, str]]:
    """把旁白切成字幕行，返回 `(起始字序号, 结束字序号, 文本)`，保证拼回等于原文。"""
    text = text or ""
    if not text:
        return []
    lines: list[tuple[int, int, str]] = []
    start = 0
    buf: list[str] = []
    i = 0
    for ch in text:
        buf.append(ch)
        i += 1
        if len(buf) >= max_chars and (ch in "。！？，、；：!?.,;:）)」』”" or len(buf) >= max_chars + 6):
            lines.append((start, i, "".join(buf)))
            start = i
            buf = []
    if buf:
        lines.append((start, i, "".join(buf)))
    return _apply_kinsoku(lines, text, max_chars)


def _fmt_ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(shots: list[dict[str, Any]], boundaries_map: dict[int, list[Boundary]], max_chars: int) -> str:
    """由分镜时间轴 + 词边界（或字符比例估算）生成 SRT。"""
    entries: list[tuple[float, float, str]] = []
    for shot in shots:
        sid = int(shot["id"])
        start = float(shot.get("start") or 0.0)
        end = float(shot.get("end") or start)
        narration = shot.get("narration") or ""
        lines = split_display_lines(narration, max_chars)
        if not lines:
            continue
        bounds = boundaries_map.get(sid) or []
        curve = char_time_curve(bounds, narration) if bounds else []
        total_chars = max(1, len(narration))
        span = max(0.05, end - start)
        for a, b, text in lines:
            if curve:
                t0 = start + time_at_chars(curve, a)
                t1 = start + time_at_chars(curve, b)
            else:  # 估算：按字数比例分到该镜时长
                t0 = start + span * (a / total_chars)
                t1 = start + span * (b / total_chars)
            if t1 <= t0:
                t1 = t0 + 0.4
            entries.append((t0, t1, text.strip()))

    # 单调化，避免重叠（SRT 要求时间递增）
    out: list[str] = []
    prev_end = 0.0
    for idx, (t0, t1, text) in enumerate(entries, start=1):
        if t0 < prev_end:
            t0 = prev_end
        if t1 <= t0:
            t1 = t0 + 0.3
        prev_end = t1
        out.append(f"{idx}\n{_fmt_ts(t0)} --> {_fmt_ts(t1)}\n{text}\n")
    return "\n".join(out)


# ---- 强制对齐（票据 12 回退 / 票据 21） ------------------------------------

_WHISPER_CACHE: dict[tuple[str, str], Any] = {}


def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401

        return True
    except Exception:
        return False


def _register_cuda_dlls() -> None:
    """让 ctranslate2 找到 pip 装的 cuBLAS / cuDNN。

    pip 安装的 nvidia-*-cu12 只把 DLL 放在 site-packages/nvidia/*/bin，
    不会自动进 PATH；Windows 下不注册就找不到 cudnn64_*.dll。
    """
    if os.name != "nt":
        return
    try:
        import site  # noqa: PLC0415

        for base in site.getsitepackages():
            for sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_nvrtc/bin"):
                d = Path(base) / sub
                if d.is_dir():
                    os.add_dll_directory(str(d))
    except Exception:
        pass


def _resolve_whisper_device(device: str) -> tuple[str, str]:
    """把配置里的 device 归一成 (faster-whisper 的 device, compute_type)。

    auto 优先用 GPU：medium 在 CPU 上跑单镜要约 10 分钟（实测 579s），
    在 4060 上只要 6.4s —— 差 90 倍，绝不能默默退回 CPU。
    """
    if device == "cpu":
        return "cpu", "int8"
    if device in ("cuda", "gpu"):
        return "cuda", "float16"
    # auto：探测 CUDA，可用则用 GPU
    try:
        import ctranslate2  # type: ignore

        if ctranslate2.get_cuda_device_count() > 0:
            _register_cuda_dlls()
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def _whisper_model(size: str, device: str) -> Any:
    key = (size, device)
    if key not in _WHISPER_CACHE:
        from faster_whisper import WhisperModel  # type: ignore

        real_device, compute = _resolve_whisper_device(device)
        try:
            _WHISPER_CACHE[key] = WhisperModel(size, device=real_device, compute_type=compute)
        except Exception:
            # GPU 初始化失败（缺 DLL / 显存不足）→ 退回 CPU，别让整条对齐链断掉
            if real_device == "cpu":
                raise
            _WHISPER_CACHE[key] = WhisperModel(size, device="cpu", compute_type="int8")
    return _WHISPER_CACHE[key]


def whisper_boundaries(
    audio_path: Path, *, size: str = "small", device: str = "auto"
) -> list[Boundary] | None:
    """用 faster-whisper 对**单镜音频**做词级对齐（票据 21）。

    逐镜对齐能天然限制误差累积。未安装 faster-whisper 时返回 None（调用方降级为估算）。
    """
    if not whisper_available():
        return None
    try:
        model = _whisper_model(size, device)
        segments, _info = model.transcribe(
            str(audio_path), word_timestamps=True, language="zh", beam_size=1
        )
        bounds: list[Boundary] = []
        for seg in segments:
            for w in (getattr(seg, "words", None) or []):
                start = float(getattr(w, "start", 0.0))
                end = float(getattr(w, "end", start))
                text = str(getattr(w, "word", "") or "")
                bounds.append(Boundary(start, max(0.02, end - start), text))
        return bounds or None
    except Exception:
        return None


# ---- 整轨拼接 --------------------------------------------------------------


def _normalize(src: Path, dst: Path) -> None:
    ffmpeg, _ = tools()
    dst.parent.mkdir(parents=True, exist_ok=True)
    ff_run([
        ffmpeg, "-y", "-i", str(src),
        "-ar", str(AUDIO_AR), "-ac", str(AUDIO_AC), "-c:a", "pcm_s16le",
        str(dst),
    ])


def _norm_stale(src: Path, norm: Path) -> bool:
    """归一化件要不要（重）做：不存在，或比源音频旧。

    ★ 只查 is_file 时，重合成的镜头会**静默拼进旧 wav** —— 改了配音却不生效，
    全程无报错（实测 2026-10-04，用户报「读了〔原文〕」后排查发现）。
    """
    if not norm.is_file():
        return True
    return norm.stat().st_mtime < src.stat().st_mtime


def concat_track(ws: Workspace, shots: list[dict[str, Any]], gap: float) -> Path:
    """按时间轴拼出 `audio/narration.mp3`（镜间插入 gap 静音）。"""
    ffmpeg, _ = tools()
    norm_dir = ws.path("audio", "norm")
    norm_dir.mkdir(parents=True, exist_ok=True)

    # 静音段
    silence = norm_dir / "silence.wav"
    if gap > 0:
        ff_run([
            ffmpeg, "-y", "-f", "lavfi",
            "-i", f"anullsrc=r={AUDIO_AR}:cl={'mono' if AUDIO_AC == 1 else 'stereo'}",
            "-t", f"{gap:.3f}", "-c:a", "pcm_s16le", str(silence),
        ])

    list_lines: list[str] = []
    for idx, shot in enumerate(shots):
        src = Path(shot["audio_path"])
        norm = norm_dir / f"{src.stem}.wav"
        # ★ 源音频比归一化件新就重做（见 `_norm_stale`）：否则重合成的镜头
        #   会静默拼进旧 wav —— 改了配音却不生效，全程无报错。
        if _norm_stale(src, norm):
            _normalize(src, norm)
        list_lines.append(f"file '{norm.as_posix()}'")
        if gap > 0 and idx != len(shots) - 1:
            list_lines.append(f"file '{silence.as_posix()}'")

    list_file = ws.path("audio", "concat.txt")
    list_file.write_text("\n".join(list_lines) + "\n", encoding="utf-8")

    out = ws.path("audio", "narration.mp3")
    ff_run([
        ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c:a", "libmp3lame", "-b:a", "192k", "-ar", str(AUDIO_AR), str(out),
    ])
    return out


# ---- 命令入口 --------------------------------------------------------------


def _existing_boundaries(ws: Workspace, sid: int) -> list[Boundary] | None:
    path = ws.path("audio", f"shot-{sid:03d}.json")
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return [Boundary(b["offset"], b["duration"], b["text"]) for b in data.get("boundaries", [])]


def _text_sha1(text: str) -> str:
    return artifact.signature(text or "", length=0)  # 参数签名统一走 artifact


def _audio_meta(ws: Workspace, sid: int) -> dict[str, Any] | None:
    path = ws.path("audio", f"shot-{sid:03d}.json")
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _audio_reusable(ws: Workspace, sid: int, out: Path, voice: str, narration: str) -> bool:
    """已有音频能否复用。

    只看"文件存在"是不够的（票据 47）：`--force` 重合成**失败时不会删掉旧文件**，
    下一轮就把它当成功产物跳过 —— 旧音色、旧文本的音频于是留在片子里。
    实测第一章换音色后 4 镜（035/042/044/046）残留上一版的 Yunjian 音频，
    其中一镜的旁白已从 11 字改到 168 字，时长差 3.6s vs 44s 却毫无报错。

    要求：① 音色与本次一致；② 有文本指纹时文本一致；③ 没有 json 记录的一律重做
    （无从校验，宁可多合一次）。
    """
    if not (out.is_file() and out.stat().st_size > 0 and ff_duration(out)):
        return False
    meta = _audio_meta(ws, sid)
    if meta is None:
        return False
    if str(meta.get("voice") or "") != str(voice):
        return False
    digest = meta.get("text_sha1")
    if digest and digest != _text_sha1(narration):
        return False
    return True


def run_command(config: Config, ws: Workspace, args) -> int:  # noqa: ANN001
    force = bool(getattr(args, "force", False))
    try:
        data = ws.load_shots(error=TTSError)
    except TTSError as exc:
        print(str(exc))
        return 2
    shots = data.get("shots", [])
    if not shots:
        print("shots.json 里没有分镜。")
        return 2

    # 阶段间契约：分镜表结构不对就当场停下（见 `lvs/handoff.py`）。
    msg = handoff.check_shots_or_message(
        shots, stage="配音（voice）", consumer="voice",
        command=f"lvs shots --task {ws.task}",
    )
    if msg:
        print(msg)
        return 2

    try:
        backend = make_backend(config)
    except TTSError as exc:
        print(str(exc))
        return 2

    voice = str(config.get("tts.voice", "zh-CN-YunxiNeural"))
    gap = float(config.get("voice.gap", 0.3))
    max_chars = int(config.get("voice.subtitle_max_chars", 18))
    ext = "mp3" if backend.name == "edge" else "wav"

    # 只有**吃显存**的后端才走 GPU 守卫：
    #   `edge`   联网合成，不占显存
    #   `silent` 纯 ffmpeg 静音轨，不占显存
    #   `openai_speech` 本地模型，要显存 → 这才是守卫存在的理由
    if backend.name not in ("edge", "silent"):
        from lvs import guard

        try:
            note = guard.check("tts", config)
            if note:
                print(f"[GPU 守卫] {note}")
        except guard.GPUConflict as exc:
            print(f"[GPU 守卫] {exc}")
            return 2

    print(f"配音后端：{backend.name}（音色 {voice}，镜间静音 {gap}s）")

    # ---- 逐镜合成（失败隔离 + 熔断） ----
    boundaries_map: dict[int, list[Boundary]] = {}
    failures: list[tuple[int, str]] = []
    done = skipped = 0
    # 连续失败熔断：TTS 服务掉线会**逐镜失败到底**，到阈值就停（见 `lvs/breaker.py`）。
    breaker = breaker_mod.Breaker(breaker_mod.limit_from(config))
    tripped = False
    processed = 0
    log_on = runlog.enabled(config)
    try:
        tools()
    except FFmpegError as exc:
        print(str(exc))
        return 2

    def _progress() -> None:
        """每镜一记（票 26）：跑到一半被中断时，盘上要留着"跑到哪了"。

        字段名与收尾那次 `mark_stage` 保持一致，也与素材阶段同用 `done`
        —— "已完成数"只有一个名字，免得看板去兜两个（票 42 审查）。
        """
        ws.mark_stage_progress("voice", done=done, skipped=skipped,
                               failed=len(failures), total=len(shots))

    for shot in track(shots, "配音"):
        sid = int(shot["id"])
        processed += 1
        out = ws.path("audio", f"shot-{sid:03d}.{ext}")
        if not force and _audio_reusable(ws, sid, out, voice, shot["narration"]):
            shot["audio_path"] = str(out)
            shot["audio_duration"] = ff_duration(out)
            bounds = _existing_boundaries(ws, sid)
            if bounds is not None:
                boundaries_map[sid] = bounds
            skipped += 1
            _progress()
            continue
        try:
            res = backend.synthesize(shot["narration"], out, voice)
        except TTSError as exc:
            shot["status"] = "failed"
            shot["error"] = str(exc)
            failures.append((sid, str(exc).splitlines()[0]))
            _progress()
            if log_on:
                runlog.event(ws, "voice", "shot_fail", shot=sid,
                             reason=str(exc).splitlines()[0][:200])
            if breaker.record(False, index=processed):
                tripped = True
                if log_on:
                    runlog.event(ws, "voice", "breaker_tripped",
                                 consecutive=breaker.consecutive, at_index=processed,
                                 remaining=len(shots) - processed)
                print(
                    breaker.message(
                        stage="配音（voice）",
                        remaining=len(shots) - processed,
                        command=f"lvs voice --task {ws.task}",
                    )
                )
                break
            continue
        _progress()
        breaker.record(True)
        if log_on:
            runlog.event(ws, "voice", "shot_ok", shot=sid)
        shot["audio_path"] = str(res.path)
        shot["audio_duration"] = res.duration
        shot["status"] = "done"
        if res.boundaries:
            boundaries_map[sid] = res.boundaries
        ws.path("audio", f"shot-{sid:03d}.json").write_text(
            json.dumps(
                {
                    "voice": voice,
                    "duration": res.duration,
                    "text_sha1": _text_sha1(shot["narration"]),
                    "boundaries": [{"offset": b.offset, "duration": b.duration, "text": b.text} for b in res.boundaries],
                },
                ensure_ascii=False, indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        done += 1

    ok_shots = [s for s in shots if s.get("audio_path") and Path(s["audio_path"]).is_file()]
    if not ok_shots:
        print("没有任何分镜合成成功，无法继续。")
        for sid, reason in failures[:10]:
            print(f"  - shot {sid:03d}：{reason}")
        return 2

    # ---- 时间轴写回（严格单调） ----
    t = 0.0
    for idx, shot in enumerate(ok_shots):
        dur = float(shot.get("audio_duration") or 0.0)
        shot["start"] = round(t, 3)
        shot["end"] = round(t + dur, 3)
        t = shot["end"] + (gap if idx != len(ok_shots) - 1 else 0.0)

    # ---- 拼接整轨 ----
    try:
        narration = concat_track(ws, ok_shots, gap)
    except FFmpegError as exc:
        print(f"整轨拼接失败：{exc}")
        return 2

    # ---- 字幕 ----
    # 没有词边界的镜头（如本地 TTS 不返回时间戳）→ 尝试强制对齐，再不行按字数估算
    align_mode = str(config.get("voice.align", "auto")).lower()
    missing = [s for s in ok_shots if int(s["id"]) not in boundaries_map]
    aligned = 0
    if missing and align_mode != "estimate" and whisper_available():
        t_align = time.time()
        print(f"  对 {len(missing)} 个缺时间戳的镜头做 whisper 强制对齐…")
        for shot in missing:
            bounds = whisper_boundaries(
                Path(shot["audio_path"]),
                size=str(config.get("voice.whisper_size", "small")),
                device=str(config.get("voice.whisper_device", "auto")),
            )
            if bounds:
                boundaries_map[int(shot["id"])] = bounds
                aligned += 1
        print(f"  对齐完成：{aligned}/{len(missing)}，耗时 {time.time() - t_align:.1f}s")

    srt = build_srt(ok_shots, boundaries_map, max_chars)
    srt_path = ws.path("subtitle.srt")
    srt_path.write_text(srt, encoding="utf-8")
    n_lines = srt.count(" --> ")

    if not boundaries_map:
        why = (
            "未安装 faster-whisper（pip install faster-whisper 可启用精确对齐）"
            if align_mode != "estimate"
            else "已指定 voice.align=estimate"
        )
        print(f"  注意：无词边界时间戳，字幕为**按字数估算**（{why}）；")
        print("        需要精确对齐请启用票据 21 的强制对齐。")
    elif aligned == 0 and len(missing) == len(ok_shots):
        print("  注意：后端不返回时间戳且未启用对齐，字幕按字数估算。")

    # 写回 shots.json
    ws.write_shots(data)
    # 记录**每镜音频**，删掉任一即触发重跑（§7.1）
    outputs = [narration, srt_path, ws.path("shots.json")]
    outputs += [Path(s["audio_path"]) for s in ok_shots if Path(s["audio_path"]).is_file()]
    ws.mark_stage(
        "voice",
        outputs=outputs,
        status=stage_status(len(failures)),
        # `total` 必须给：结果信封的 `counts` 读它（少了它，agent 看到
        # "done=3 total=0" 这种自相矛盾的数，而 `assets` 一直都给了）。
        total=len(shots), done=done, skipped=skipped, failed=len(failures),
    )

    total_dur = ok_shots[-1]["end"]
    print(f"配音完成：合成 {done}，跳过 {skipped}，失败 {len(failures)}")
    if tripped:
        print(
            f"  ⚠ 已熔断：连续失败 {breaker.consecutive} 次后停止，"
            f"本次只处理了 {processed}/{len(shots)} 镜。"
        )
    print(f"  整轨：{narration}（{total_dur:.1f}s）")
    print(f"  字幕：{srt_path}（{n_lines} 行）")
    if failures:
        for sid, reason in failures[:10]:
            print(f"    - shot {sid:03d}：{reason}")
    # ★ 有失败件就返回 EXIT_FAILED（1）—— 同 `lvs assets` 的理由：退出码要如实。
    return EXIT_FAILED if failures else 0
