"""合成（`lvs build`）—— 票据 13 + 14 + 15。

把 `shots.json` 的时间轴 + 素材 + `narration.mp3` + `subtitle.srt` 合成 `final.mp4`：

1. **逐镜切片（13）**：按 shot 的 `start`/`end` 把素材精确切到该时长
   （图片 → Ken Burns；视频 → 缩放裁切，短于目标则循环/定格）
2. **静态图运动化（14）**：ffmpeg `zoompan`（缓慢推拉/平移）
3. **混音 + 烧字幕（15）**：并入旁白、按配置样式烧录 SRT，输出 H.264 + AAC

分段产物落在 `.work/<task>/segments/shot-NNN.mp4`，**幂等**：存在即跳过。
缺少素材的分镜会生成一块**占位黑帧**（保持音画同步，不整片失败）；占位镜号记在
`segments/.placeholders.json`，**素材补齐后重跑会把占位片段重渲成真画面**（详见 `_segment_reusable`）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lvs import assets as assets_mod
from lvs import handoff
from lvs import prompting
from lvs import artifact
from lvs.config import Config
from lvs.ffmpeg import (
    AUDIO_AR,
    PIX_FMT,
    VIDEO_FPS,
    VIDEO_H,
    VIDEO_W,
    FFmpegError,
    black_ratio,
    duration as ff_duration,
    has_audio_stream,
    mean_volume_db,
    run as ff_run,
    tools,
)
from lvs.progress import track
from lvs.workspace import Workspace, stage_status
from lvs.errors import LvsError, EXIT_FAILED

# 短素材（不足目标时长）的处理方式：`loop` 循环、`freeze` 定格帧。别的值对短素材两不沾，
# 会让片段永远比目标短（见 `normalize_short_mode`）。
SHORT_MODES = ("loop", "freeze")


class BuildError(LvsError, RuntimeError):
    """合成失败。"""

    exit_code = EXIT_FAILED


# 占位片段登记表：记下「上次因缺素材而生成的占位镜」。有了它，重跑时才能把「已有片段
# 是占位、但素材现在补上了」的情况识别出来并重渲（否则会一直复用占位黑帧）。
PLACEHOLDER_MARKER = ".placeholders.json"
# 片段内容指纹表：`shot id` → 该片段是"用什么素材、多长、什么运镜"渲出来的
SEG_KEYS_MARKER = ".segkeys.json"
PLACEHOLDER_KEY = "placeholder"
# 区分"调用方没提供指纹"（老调用/单测 → 跳过校验）与"提供了但盘上没有记录"（→ 不可复用）
_UNSET = object()


def _segment_reusable(
    *,
    exists: bool,
    seg_dur: float,
    target_dur: float,
    force: bool,
    was_placeholder: bool,
    has_asset: bool,
    recorded: Any = _UNSET,
    want_key: Any = _UNSET,
) -> bool:
    """已有片段可否复用（纯函数，便于单测）。

    两道合同：
      - 占位片段一旦本镜有了真素材就**不再复用** —— 时长校验挡不住它（占位片段时长与目标
        一致），不复用否则成片会永远停在占位黑帧；
      - 片段的内容指纹（素材+时长+运镜）必须与本次一致，否则镜表重排后会**静默复用错图**
        （票据 48）。
    `recorded` / `want_key` 都传了才做指纹校验；传 `None` 表示"盘上没有记录" → 不可复用。
    """
    if force or not exists:
        return False
    if abs(seg_dur - target_dur) >= 0.4:
        return False
    if recorded is not _UNSET and want_key is not _UNSET and recorded != want_key:
        return False
    return not (was_placeholder and has_asset)


def _load_placeholders(seg_dir: Path) -> set[int]:
    p = seg_dir / PLACEHOLDER_MARKER
    if not p.is_file():
        return set()
    try:
        return {int(x) for x in json.loads(p.read_text(encoding="utf-8"))}
    except (ValueError, OSError, TypeError):
        return set()


def _save_placeholders(seg_dir: Path, sids: set[int]) -> None:
    (seg_dir / PLACEHOLDER_MARKER).write_text(
        json.dumps(sorted(sids)) + "\n", encoding="utf-8"
    )


def _load_seg_keys(seg_dir: Path) -> dict[int, str]:
    p = seg_dir / SEG_KEYS_MARKER
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    try:
        return {int(k): str(v) for k, v in data.items()}
    except (ValueError, TypeError):
        return {}


def _save_seg_keys(seg_dir: Path, keys: dict[int, str]) -> None:
    (seg_dir / SEG_KEYS_MARKER).write_text(
        json.dumps({str(k): v for k, v in sorted(keys.items())}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def segment_key(
    *,
    asset: str | None,
    duration: float,
    motion: str,
    supersample: int,
    short_mode: str,
    placeholder: bool,
    asset_content: str | None = None,
) -> str:
    """片段的内容指纹（票据 48）。

    片段复用原先**只比时长**。镜表一重排，同一个镜号可能恰好时长相同，却换了素材 ——
    旧片段被静默复用，成片里就是别的一镜的画面。这和素材缓存（票据 46）是同一类坑。
    指纹覆盖"决定这一片段长什么样"的全部输入：素材路径、时长、运镜方向、
    超采样倍数、短素材策略。

    `asset_content`（素材文件的 size+mtime）修补同一坑型的第二个变体：**路径不变、
    内容被原地替换**（如 graphic 卡顶替成 scene 图）。此时路径/时长/运镜全都不变，
    旧黑卡片段指纹照样匹配被复用 —— 成片里就还留着已删除的卡片（2026-10-04 实测：
    002 的 24 张黑卡有 23 张漏进重编后的成片）。
    """
    if placeholder:
        return PLACEHOLDER_KEY
    # 参数签名统一走 `artifact.signature` —— 同一段 join→sha1→截断 原先在本文件、
    # `assets._request_key`、`library._signature` 里各写了一遍。
    return artifact.signature(
        asset or "", asset_content or "", f"{duration:.3f}", motion, supersample, short_mode
    )


def _asset_content_fp(src: Path | None) -> str | None:
    """素材文件的内容指纹（size+mtime_ns）。路径不存在 → None（走不上复用）。"""
    if src is None:
        return None
    try:
        st = src.stat()
    except OSError:
        return None
    return f"{st.st_size}:{st.st_mtime_ns}"


# ---- 滤镜 ------------------------------------------------------------------

DEFAULT_SUBTITLE_STYLE = (
    "FontName=Microsoft YaHei,FontSize=16,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,"
    "MarginV=42,Alignment=2"
)


KENBURNS_SUPERSAMPLE_MAX = 4

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _has_kenburns(shot: dict[str, Any]) -> bool:
    """该镜会不会走 Ken Burns（图像素材、且不是"要读的信息"的图文卡片）。"""
    asset = shot.get("asset_path")
    if not asset or shot.get("kind") == prompting.KIND_GRAPHIC:
        return False
    return Path(str(asset)).suffix.lower() in IMAGE_SUFFIXES


def motion_sequence(shots: list[dict[str, Any]], mode: str, amount: float) -> list[str]:
    """逐镜定运镜方向：**同素材的相邻镜把方向反过来**（推入↔拉出）。

    `build.kenburns` 是全局单值，本来不感知邻镜是否同图。于是当一张图被切成
    两个连续镜时，每镜都从头 `1.0 → 1+amount` 推一遍 —— 接缝处"推到 1.15 又跳回
    1.0 再推一次"，看起来一跳一跳（票据 45）。

    `zoom-in` 的 z 从 1.0 走到 1+amount，`zoom-out` 恰好从 1+amount 走回 1.0，
    所以"上一镜推入、下一镜拉出"在接缝处缩放值相同，动作是连续的。
    连续 3 镜以上同素材时按 in/out/in… 交替，每一处接缝都接得上。

    只对**图像素材**且非图文卡片镜生效：视频素材自带运动，占位帧与卡片本来就静止。
    """
    out: list[str] = []
    for i, shot in enumerate(shots):
        cur = mode
        if mode in ("zoom-in", "zoom-out") and i > 0:
            prev, here = shots[i - 1], shot
            a_prev, a_cur = prev.get("asset_path"), here.get("asset_path")
            if (
                a_cur and a_prev and str(a_cur) == str(a_prev)
                and _has_kenburns(prev) and _has_kenburns(here)
            ):
                # 取上一镜**实际**用的方向来对调，而不是对全局模式取反 ——
                # 连续 3 镜以上同素材时必须 in/out/in 交替，才会处处接得上。
                cur = "zoom-out" if out[i - 1] == "zoom-in" else "zoom-in"
        out.append(cur)
    return out


def kenburns_filter(
    mode: str, amount: float, frames: int, w: int, h: int, fps: int, supersample: int = 2
) -> str:
    """生成 Ken Burns 滤镜串。

    抖动来自 `zoompan` 把裁切窗的 x/y 与尺寸**取整** —— 连续推近于是变成
    「卡住几帧、跳 1 像素」的阶梯。解法是让它在更高分辨率上算、再 lanczos 降回成片尺寸，
    量化误差因此落到亚像素（实测帧间差异变异系数 0.2034 → 0.1191，锐度不变）。

    `supersample` 是预放大倍数（上限 4 是**内存**约束：4× 时单帧缓冲约 100 MB）：
      1 → 旧行为：预放大 2×、zoompan 直接出成片尺寸
      2 → 默认：预放大 2×、zoompan 出 2× 尺寸再 lanczos 降回
      4 → 预放大 4×、同样在 2× 尺寸上算 zoompan 再降回
    """
    ss = max(1, min(KENBURNS_SUPERSAMPLE_MAX, int(supersample)))
    pre_factor = max(2, ss)
    pre = (
        f"scale={w * pre_factor}:{h * pre_factor}:force_original_aspect_ratio=increase,"
        f"crop={w * pre_factor}:{h * pre_factor}"
    )
    if mode == "none":
        return (
            f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
            f"fps={fps},setsar=1,format={PIX_FMT}"
        )
    denom = max(1, frames - 1)
    center_x = "iw/2-(iw/zoom/2)"
    center_y = "ih/2-(ih/zoom/2)"
    if mode == "zoom-out":
        z = f"max({1 + amount}-{amount}*on/{denom},1.0)"
        x, y = center_x, center_y
    elif mode == "pan-right":
        z = f"{1 + amount}"
        x = f"(iw-iw/zoom)*on/{denom}"
        y = center_y
    elif mode == "pan-up":
        z = f"{1 + amount}"
        x = center_x
        y = f"(ih-ih/zoom)*(1-on/{denom})"
    else:  # zoom-in（默认）
        z = f"min(1+{amount}*on/{denom},{1 + amount})"
        x, y = center_x, center_y

    out_w, out_h = (w * 2, h * 2) if ss > 1 else (w, h)
    zoompan = f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={out_w}x{out_h}:fps={fps}"
    chain = f"{pre},{zoompan}"
    if ss > 1:
        chain += f",scale={w}:{h}:flags=lanczos"
    return f"{chain},setsar=1,format={PIX_FMT}"


def normalize_short_mode(value: Any) -> str:
    """校验 `build.short_asset`：只认 `loop` / `freeze`，其余一律 `BuildError`。

    非法值（含空串）对短素材既不循环也不冻结 → 片段比目标短 → `_segment_reusable`
    判时长不符 → 每轮重渲、永不收敛。所以在**渲染前**就挡住它，别让配置笔误变成死循环。
    """
    if value not in SHORT_MODES:
        raise BuildError(
            f"build.short_asset 只能是 {'/'.join(SHORT_MODES)}，收到 {value!r}"
        )
    return str(value)


def _video_fit_filter() -> str:
    """把视频素材缩放到成片尺寸（保持比例，居中裁切）。"""
    return (
        f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_W}:{VIDEO_H},fps={VIDEO_FPS},setsar=1,format={PIX_FMT}"
    )


# ---- 分段生成 --------------------------------------------------------------


def assert_segment_rendered(path: Path, expected: float) -> None:
    """产物校验：ffmpeg **返回码为 0 也可能一帧都没编**，所以不能只看 rc。

    实测（票据 31 修复）：静止卡片段少了 `-loop 1` 时，ffmpeg 打印
    "No filtered frames for output stream" 却仍以 rc=0 退出，产出 261 字节空壳 ——
    `duration()` 读不出来，而 build 一路报"片段 110 个（占位 0 个）"，
    成片只剩 64.7s 而旁白有 447.3s。**空产物必须当场炸出来。**
    """
    got = ff_duration(path)
    if got is None or got < 0.05:
        raise BuildError(
            f"片段渲染失败（{path.name}）：ffmpeg 返回成功但产物无有效帧"
            f"（期望 {expected:.2f}s，实际 {'不可读' if got is None else f'{got:.2f}s'}）"
        )
    if abs(got - expected) > max(0.5, expected * 0.25):
        print(f"  注意：{path.name} 时长 {got:.2f}s 与目标 {expected:.2f}s 偏差较大")


def _render_image_segment(
    ffmpeg: str, src: Path, dst: Path, duration: float, mode: str, amount: float,
    supersample: int = 2,
) -> None:
    frames = max(1, int(round(duration * VIDEO_FPS)))
    vf = kenburns_filter(mode, amount, frames, VIDEO_W, VIDEO_H, VIDEO_FPS, supersample)
    args = [ffmpeg, "-y"]
    if mode == "none":
        # 静图必须 `-loop 1`：Ken Burns 路径靠 zoompan 的 `d=` 自己复制帧，
        # 而静止路径只有 `fps=`，单帧输入下它**一帧都产不出来**（0 帧空壳）。
        args += ["-loop", "1"]
    args += [
        "-i", str(src),
        "-vf", vf,
        "-frames:v", str(frames),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", PIX_FMT,
        "-an", str(dst),
    ]
    ff_run(args)


def _render_video_segment(
    ffmpeg: str, src: Path, dst: Path, duration: float, short_mode: str
) -> None:
    vf = _video_fit_filter()
    src_dur = ff_duration(src) or 0.0
    args = [ffmpeg, "-y"]
    if src_dur and src_dur < duration - 0.05 and short_mode == "loop":
        args += ["-stream_loop", "-1"]
    args += ["-i", str(src)]
    if not src_dur or src_dur < duration - 0.05:
        if short_mode == "freeze":
            pad = max(0.1, duration - src_dur) if src_dur else duration
            vf = f"{vf},tpad=stop_mode=clone:stop_duration={pad:.3f}"
    args += [
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", PIX_FMT,
        "-an", str(dst),
    ]
    ff_run(args)


def _render_placeholder(ffmpeg: str, dst: Path, duration: float, sid: int) -> None:
    """缺素材时的占位：深色底 + 分镜号（保持音画同步）。"""
    vf = (
        f"drawbox=x=0:y=0:w={VIDEO_W}:h={VIDEO_H}:color=0x101418@1:t=fill,"
        f"drawtext=text='shot {sid:03d}':fontcolor=white@0.5:fontsize=48:"
        f"x=(w-text_w)/2:y=(h-text_h)/2,fps={VIDEO_FPS},format={PIX_FMT}"
    )
    ff_run([
        ffmpeg, "-y", "-f", "lavfi",
        "-i", f"color=c=0x101418:s={VIDEO_W}x{VIDEO_H}:r={VIDEO_FPS}",
        "-t", f"{duration:.3f}", "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", PIX_FMT,
        "-an", str(dst),
    ])


def build_segments(
    ws: Workspace, shots: list[dict[str, Any]], config: Config, force: bool, gap: float = 0.0
) -> tuple[list[Path], list[int]]:
    """逐镜生成片段，返回 (片段路径列表, 缺素材的分镜号列表)。

    关键：非末镜的片段时长 = 音频时长 + `gap`。这样画面轨总长与
    `narration.mp3`（含镜间静音）严格一致，**音画不漂移**。
    """
    short_mode = normalize_short_mode(config.get("build.short_asset", "loop"))
    ffmpeg, _ = tools()
    mode = str(config.get("build.kenburns", "zoom-in"))
    amount = float(config.get("build.zoom_amount", 0.15))
    supersample = int(config.get("build.kenburns_supersample", 2))
    seg_dir = ws.path("segments")
    seg_dir.mkdir(parents=True, exist_ok=True)
    prev_ph = _load_placeholders(seg_dir)

    ordered = sorted(shots, key=lambda s: float(s.get("start") or 0.0))
    # 逐镜运镜方向：同素材的相邻镜方向对调，接缝处缩放值相同（票据 45）
    motions = motion_sequence(ordered, mode, amount)
    keys = _load_seg_keys(seg_dir)
    paths: list[Path] = []
    current_ph: set[int] = set()
    saved_ph: set[int] = set(prev_ph)
    saved_keys: dict[int, str] = dict(keys)

    for idx, shot in enumerate(track(ordered, "合成")):
        sid = int(shot["id"])
        start = float(shot.get("start") or 0.0)
        end = float(shot.get("end") or start)
        # 末镜之后没有静音，故不加 gap
        dur = max(0.2, end - start) + (gap if idx != len(ordered) - 1 else 0.0)
        dst = seg_dir / f"shot-{sid:03d}.mp4"

        asset = shot.get("asset_path")
        src = Path(asset) if asset else None
        has_asset = bool(src and src.is_file())
        # 图文/图表卡片是"要读的信息"，运镜只会让它更难读（票据 28）→ 静止
        is_image = bool(has_asset and src.suffix.lower() in IMAGE_SUFFIXES)
        mode_for_shot = ("none" if shot.get("kind") == prompting.KIND_GRAPHIC
                         else motions[idx]) if is_image else short_mode
        want_key = segment_key(
            asset=asset, duration=dur, motion=mode_for_shot,
            supersample=supersample, short_mode=short_mode, placeholder=not has_asset,
            asset_content=_asset_content_fp(src) if has_asset else None,
        )
        seg_dur = (ff_duration(dst) or 0.0) if dst.is_file() else 0.0

        if _segment_reusable(
            exists=dst.is_file(), seg_dur=seg_dur, target_dur=dur, force=force,
            was_placeholder=sid in prev_ph, has_asset=has_asset,
            recorded=keys.get(sid), want_key=want_key,
        ):
            paths.append(dst)
            if not has_asset:
                current_ph.add(sid)   # 复用的仍是占位片段
        else:
            try:
                if not has_asset:
                    _render_placeholder(ffmpeg, dst, dur, sid)
                    current_ph.add(sid)
                elif is_image:
                    _render_image_segment(ffmpeg, src, dst, dur, mode_for_shot, amount, supersample)
                else:
                    _render_video_segment(ffmpeg, src, dst, dur, short_mode)
                assert_segment_rendered(dst, dur)
                keys[sid] = want_key      # 指纹只在**渲染成功**后落盘
            except BuildError as exc:
                # 一镜坏掉不该毁掉整片：退回占位帧保音画同步，并记入登记表（D17 之后能自动补渲）
                print(f"  ✗ {dst.name} 渲染失败：{exc}")
                print("    改用占位帧（修好后重跑 `lvs build` 会自动补渲）")
                _render_placeholder(ffmpeg, dst, dur, sid)
                current_ph.add(sid)
                keys.pop(sid, None)
            paths.append(dst)

        # 增量落盘：万一 build 中途被打断，占位登记表也不会丢（否则下次认不出占位片段）
        if current_ph != saved_ph:
            _save_placeholders(seg_dir, current_ph)
            saved_ph = set(current_ph)
        if keys != saved_keys:
            _save_seg_keys(seg_dir, keys)
            saved_keys = dict(keys)

    return paths, sorted(current_ph)


def concat_segments(ws: Workspace, segments: list[Path]) -> Path:
    ffmpeg, _ = tools()
    list_file = ws.path("segments", "concat.txt")
    list_file.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in segments) + "\n", encoding="utf-8"
    )
    out = ws.path("video-track.mp4")
    ff_run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(out)])
    return out


def _escape_style(style: str) -> str:
    return style.replace("'", "")


# ---- BGM（背景音乐）---------------------------------------------------------
#
# 为什么放在这里而不是单独阶段：BGM 只在**混音**这一步有意义（旁白已在
# narration.mp3 里），单独开阶段只会多一个产物、多一道门。
#
# 设计取自同类工具（NarratoAI 等）的两条经验：
#   1. BGM 是**锦上添花** —— 混音失败绝不允许毁掉成片，必须自动退回无 BGM 版本；
#   2. 音量策略简单可预期：固定低音量 + 首尾淡入淡出（旁白为主，BGM 为辅）。
# 闪避（sidechain duck）暂不做 —— 那是参数调优地狱，常量低音量已够用。


def bgm_source(config: Config) -> tuple[Path | None, str]:
    """返回 `(可用的 BGM 文件, 说明)`；没配/文件不在 → `(None, 原因)`。

    ★ 没配不是错误（BGM 是可选项）；**配了但文件不在是配置错误** ——
    调用方要把原因说出来，不许静默当成"没配"。
    """
    raw = str(config.get("bgm.file", "") or "").strip()
    if not raw:
        return None, ""
    path = Path(raw).expanduser()
    if not path.is_file():
        return None, f"配置的 [bgm].file 不存在：{path}"
    return path, ""


def _bgm_audio_filter(bgm_dur: float, total: float, config: Config) -> str:
    """BGM 音轨滤镜串：音量 + 淡入 + 淡出（`total` 是旁白总时长）。

    `normalize=0`（在 amix 上）是必须的：ffmpeg ≥4.4 的 amix 默认把各输入
    **各除以输入数**，会把旁白直接砍半 —— 那是"加了 BGM 旁白变小声"的典型事故。
    """
    def _f(key: str, default: float) -> float:
        try:
            return max(0.0, float(config.get(f"bgm.{key}", default)))
        except (TypeError, ValueError):
            return default

    vol = min(_f("volume", 0.25), 1.0)
    fade_in = _f("fade_in", 2.0)
    fade_out = _f("fade_out", 3.0)
    parts = [f"volume={vol:.3f}"]
    if fade_in > 0:
        parts.append(f"afade=t=in:d={fade_in:.2f}")
    if fade_out > 0 and total > fade_out:
        parts.append(f"afade=t=out:st={total - fade_out:.2f}:d={fade_out:.2f}")
    return ",".join(parts)


def finalize(
    ws: Workspace, video_track: Path, narration: Path, srt: Path, config: Config
) -> tuple[Path, bool]:
    """混音 + 烧字幕。返回 (输出路径, 是否成功烧字幕)。

    配了 `[bgm].file` 时把 BGM 循环铺满旁白时长混进去；**BGM 出任何问题都
    自动退回无 BGM 版本并说明原因** —— 配乐坏了不许毁掉整部成片。
    """
    bgm, why = bgm_source(config)
    if why:
        print(f"  ⚠ {why} —— 本次不加 BGM。")
    if bgm is not None:
        try:
            return _finalize(ws, video_track, narration, srt, config, bgm=bgm)
        except FFmpegError as exc:
            first = str(exc).splitlines()[0][:140]
            print("  ⚠ BGM 混音失败（文件损坏/编码器缺失？）—— 退回无 BGM 版本：")
            print(f"     {first}")
    return _finalize(ws, video_track, narration, srt, config, bgm=None)


def _finalize(
    ws: Workspace, video_track: Path, narration: Path, srt: Path, config: Config,
    *, bgm: Path | None = None,
) -> tuple[Path, bool]:
    """实际的混音 + 烧字幕实现（`finalize` 的 BGM 重试外壳包着它）。"""
    ffmpeg, _ = tools()
    style = _escape_style(str(config.get("build.subtitle_style", DEFAULT_SUBTITLE_STYLE)))
    out = ws.path("final.mp4")

    # BGM 滤镜与混音器（没配 BGM 时两者都为空，走与从前完全一样的命令）
    bgm_input: list[str] = []
    audio_chain = "1:a"
    bgm_mix = ""
    if bgm is not None:
        total = ff_duration(narration) or 0.0
        vol_filter = _bgm_audio_filter(0.0, total, config)
        bgm_input = ["-stream_loop", "-1", "-i", str(bgm)]
        bgm_mix = (
            f"[2:a]{vol_filter}[bgm];"
            "[1:a][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mix];"
            "[mix]alimiter=limit=0.98[a]"
        )
        audio_chain = "[a]"
        print(f"  BGM：{bgm.name}（音量 {config.get('bgm.volume', 0.25)}，循环铺满旁白时长）")

    burn = srt.is_file() and srt.stat().st_size > 0
    rel_srt = srt.name  # 用相对名，规避 Windows 盘符冒号
    if burn:
        video_part = f"[0:v]subtitles={rel_srt}:force_style='{style}'[v]"
        filters = video_part + (";" + bgm_mix if bgm_mix else "")
        map_a = "1:a" if not bgm_mix else audio_chain
        try:
            ff_run(
                [
                    ffmpeg, "-y", "-i", str(video_track), "-i", str(narration),
                    *bgm_input,
                    "-filter_complex", filters,
                    "-map", "[v]", "-map", map_a,
                    "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", PIX_FMT,
                    "-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_AR),
                    "-shortest", "-movflags", "+faststart", str(out),
                ],
                cwd=ws.dir,
            )
            return out, True
        except FFmpegError as exc:
            print("⚠️ 烧字幕失败（可能是 ffmpeg 缺 libass，或字体缺失），先输出无字幕版：")
            print("   " + str(exc).splitlines()[0])

    if bgm_mix:
        ff_run(
            [
                ffmpeg, "-y", "-i", str(video_track), "-i", str(narration),
                *bgm_input,
                "-filter_complex", bgm_mix,
                "-map", "0:v", "-map", audio_chain,
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_AR),
                "-shortest", "-movflags", "+faststart", str(out),
            ]
        )
        return out, False

    ff_run(
        [
            ffmpeg, "-y", "-i", str(video_track), "-i", str(narration),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_AR),
            "-shortest", "-movflags", "+faststart", str(out),
        ]
    )
    return out, False


def shots_with_audio(shots: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[int]]:
    """分离出「有时间轴（= 有音频）」的分镜，返回 (可渲染, 无音频的分镜号)。

    配音失败/未合成的分镜 `start` 为 None：它们不在 narration.mp3 里，就不该进画面轨，
    否则会被压到 t=0 渲染、导致整条时间轴错位。
    """
    silent = [int(s["id"]) for s in shots if s.get("start") is None]
    return [s for s in shots if s.get("start") is not None], silent


# ---- 命令入口 --------------------------------------------------------------


def _resolve_asset(ws: Workspace, shot: dict[str, Any]) -> str | None:
    """找该镜的素材：优先用 assets 阶段写回的 `asset_path`，否则按 source 分支回退。"""
    if shot.get("asset_path") and Path(shot["asset_path"]).is_file():
        return shot["asset_path"]
    found = assets_mod.existing_asset(ws, shot)
    return str(found) if found else None


# ---- 成片终检 --------------------------------------------------------------
#
# 为什么在 `assert_segment_rendered` 之外还要一层：那一层查的是**每个片段**，
# 这一层查的是**整片**。整片才有的故障（音轨整体丢了、整片是黑的、整片静音）
# 逐片段查不出来 —— 而它们恰恰是"流水线坏了"最强的信号。
#
# 做法参考 2026 年同类工具（OpenMontage 等）的成片终检：
# 音轨存在性 + 平均音量 + 黑屏占比。全部走 ffmpeg 分析 filter，**不落临时文件**。

#: 平均音量低于此值（dBFS）判"整片近乎静音"。
DEFAULT_MIN_MEAN_DB = -60.0
#: 黑屏时长占比超过此值判"整片基本是黑的"。
DEFAULT_BLACK_RATIO_MAX = 0.8


def final_sanity(path: Path, *, config: Config | None = None) -> tuple[list[str], list[str]]:
    """成片终检。返回 `(硬问题, 警告)`。

    - **硬问题**：产物不合格，一定不对（如成片没有音轨）
    - **警告**：可疑但不必然错（整片静音 / 整片黑屏）—— 有声书式全黑配图是合法风格，
      所以只报不拦。

    探测**读不到就跳过**（不妄下结论）：宁可不报，也不要因为工具环境问题拦下一部好片。
    """
    hard: list[str] = []
    warns: list[str] = []

    if not has_audio_stream(path):
        hard.append(
            "成片没有音轨 —— 旁白丢了。请检查 `lvs voice` 的产物，再重跑 `lvs build`。"
        )
        return hard, warns   # 没音轨时后面的音量探测没意义

    min_db = DEFAULT_MIN_MEAN_DB
    if config is not None:
        try:
            min_db = float(config.get("build.sanity_min_mean_db", min_db))
        except (TypeError, ValueError):
            min_db = DEFAULT_MIN_MEAN_DB
    vol = mean_volume_db(path)
    if vol is not None and vol <= min_db:
        warns.append(
            f"整片近乎静音（平均音量 {vol:.1f} dB，阈值 {min_db:.0f} dB）"
            "—— 检查配音是否真的合进去了。"
        )

    max_black = DEFAULT_BLACK_RATIO_MAX
    if config is not None:
        try:
            max_black = float(config.get("build.sanity_black_ratio_max", max_black))
        except (TypeError, ValueError):
            max_black = DEFAULT_BLACK_RATIO_MAX
    ratio = black_ratio(path)
    if ratio is not None and ratio >= max_black:
        warns.append(
            f"整片黑屏占比 {ratio:.0%}（阈值 {max_black:.0%}）"
            "—— 是不是画面轨没接上、或素材全缺？"
        )

    return hard, warns


def run_command(config: Config, ws: Workspace, args) -> int:  # noqa: ANN001
    force = bool(getattr(args, "force", False))
    shots_path = ws.path("shots.json")
    if not shots_path.is_file():
        print(f"未找到 {shots_path}。请先运行：lvs shots --task {ws.task}")
        return 2
    data = json.loads(shots_path.read_text(encoding="utf-8"))
    shots = data.get("shots", [])
    if not shots:
        print("shots.json 里没有分镜。")
        return 2

    # 阶段间契约：走到合成这一步才发现分镜表坏掉，返工成本最高（前面图/音都白做了）。
    msg = handoff.check_shots_or_message(
        shots, stage="合成（build）", consumer="build",
        command=f"lvs shots --task {ws.task}",
    )
    if msg:
        print(msg)
        return 2

    narration = ws.path("audio", "narration.mp3")
    if not narration.is_file():
        print(f"未找到 {narration}。请先运行：lvs voice --task {ws.task}")
        return 2

    # 配音失败/未合成的分镜没有时间轴（start 为 None），不进画面轨
    shots, silent = shots_with_audio(shots)
    if not shots:
        print("没有任何分镜带音频时间轴。请先运行：lvs voice")
        return 2
    if silent:
        print(f"  跳过 {len(silent)} 个无音频的分镜（不会进画面轨）：{silent[:12]}{' …' if len(silent) > 12 else ''}")

    # 把素材路径补进 shot（缺素材 → 占位）
    for shot in shots:
        shot["asset_path"] = _resolve_asset(ws, shot)

    try:
        tools()
        gap = float(config.get("voice.gap", 0.3))
        print("生成分镜片段…")
        segments, placeholders = build_segments(ws, shots, config, force, gap=gap)
        if not segments:
            print("没有可合成的片段。")
            return 2
        print(f"  片段：{len(segments)} 个（占位 {len(placeholders)} 个）")
        if placeholders:
            print(f"  缺素材的分镜：{placeholders[:12]}{' …' if len(placeholders) > 12 else ''}")

        print("拼接画面轨…")
        track = concat_segments(ws, segments)
        track_dur = ff_duration(track) or 0.0
        audio_dur = ff_duration(narration) or 0.0

        print("混音 + 烧字幕…")
        final, burned = finalize(ws, track, narration, ws.path("subtitle.srt"), config)
    except FFmpegError as exc:
        print(f"合成失败：{exc}")
        return 2

    final_dur = ff_duration(final) or 0.0
    try:
        assert_segment_rendered(final, final_dur)   # D23：成片也要复读，空产物当场炸
    except BuildError as exc:
        print(f"成片校验失败：{exc}")
        return 2

    # 整片终检（音轨 / 音量 / 黑屏）—— 逐片段查不出的那类故障在这里兜住。
    hard, sanity_warns = final_sanity(final, config=config)
    for w in sanity_warns:
        print(f"  ⚠ 成片终检：{w}")
    if hard:
        print("成片终检失败：")
        for h in hard:
            print(f"  ✗ {h}")
        return 2

    # 有占位片段 → 标 partial，好让 `lvs run` 重跑补渲（D17）。之前漏传 status，
    # 于是「有占位」被记成 done，`is_stage_done` 为真、整段跳过，补了素材也永不重渲。
    ws.mark_stage(
        "build",
        outputs=[final],
        status=stage_status(len(placeholders)),
        duration=round(final_dur, 2),
        subtitle_burned=burned,
        placeholders=len(placeholders),
        sanity_warnings=len(sanity_warns),
    )

    print(f"合成完成：{final}")
    print(f"  画面轨 {track_dur:.1f}s｜旁白 {audio_dur:.1f}s｜成片 {final_dur:.1f}s"
          f"（偏差 {abs(final_dur - audio_dur):.2f}s）")
    print(f"  字幕：{'已烧录' if burned else '未烧录'}")
    # ★ 有占位镜（缺素材）也算"有失败件"：成片是产出了，但缺件要如实反映在退出码上。
    return EXIT_FAILED if placeholders else 0
