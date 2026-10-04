"""素材获取（`lvs assets`）—— 票据 09 + 18 + 19 + 41。

对每个 shot 按 **spec §8.1 的固定优先级**取素材，命中即停：

    ① library_asset 已钉死？            → 直接用该文件
    ② source=library？                  → 只查素材库
         未命中且策略是 library（票 41）→ 回退本地生图（source 不变，resolved_by=local）
         其余情况                      → 该镜失败
    ③ source=graphic（图文/图表 beat）？ → 本地排版成卡片（票据 28/34），不调 ComfyUI
    ④ 允许翻库 且 素材库命中？           → 用之（resolved_by=library）
    ⑤ 否则按 source 分支：
         pexels → 检索下载实拍视频
         local  → 调 ComfyUI 生图

**来源策略**（`source_mode`，票 41）在执行前先把实拍镜的 `source` 统一成选定的那一支
（`sources.apply_mode`，本模块只调不实现）；强制模式下第 ④ 步会被关掉。

工程约束：
- **幂等**：产物已存在（非空）即跳过，`--force` 才重做
- **失败隔离**：单镜失败只标记该镜，其余继续（票据 09）
- **绝不改原文件**：素材库命中是复制/硬链接（票据 18）
- **不静默失败**：Pexels key 缺失 / ComfyUI 未启动 → 给出可照做的中文提示；
  库未命中的自动回退也**逐镜报出来**（否则用户以为库里真有这些素材）
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lvs import cards, graphic, handoff, imagegen, library, runlog, sources
from lvs import prompting
from lvs import artifact
from lvs.config import Config
from lvs.ffmpeg import FFmpegError, run as ff_run, tools
from lvs.progress import track
from lvs import workspace as ws_mod
from lvs.workspace import Workspace, stage_status
from lvs.errors import LvsError, EXIT_FAILED, EXIT_USAGE
from lvs import breaker as breaker_mod

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

PEXELS_ENDPOINT = "https://api.pexels.com/videos/search"
CACHE_DIRNAME = Path(".work") / "_cache" / "pexels"


class AssetError(LvsError, RuntimeError):
    """配置级错误（会让整条 assets 阶段无法进行，如 Pexels key 缺失）。"""

    exit_code = EXIT_USAGE


@dataclass
class Resolution:
    ok: bool
    resolved_by: str | None = None
    path: Path | None = None
    reason: str = ""
    info: dict[str, Any] = field(default_factory=dict)


# ---- 路径与幂等 ------------------------------------------------------------


# 逐镜产物的命名与分支：定义在 workspace（布局的唯一 owner），这里只做转发
shot_name = ws_mod.shot_name
ASSET_BRANCHES = ws_mod.ASSET_BRANCHES


@dataclass
class CardBench:
    """一次 run 内的卡片工作台（票据 35）。

    一个 `[画面位]` 里的同一条 beat 往往铺满十几个镜（small3 里 33 个图文镜只有 5 条 beat），
    所以**内容按 beat 算一次、PNG 只栅格化一次**，其余镜直接复用同一张 ——
    否则会渲出十几张正文完全一样的图。

    `content`：beat 文本 → 卡片内容
    `rendered`：beat 文本 → 本次已渲出的第一份 PNG（后续镜复制它）
    """

    content: dict[str, cards.Card] = field(default_factory=dict)
    rendered: dict[str, Path] = field(default_factory=dict)

    @classmethod
    def build(cls, shots: list[dict[str, Any]]) -> "CardBench":
        bench = cls()
        for beat, group in cards.beat_groups(shots).items():
            bench.content[beat] = cards.card_for(beat, [s.get("narration") or "" for s in group])
        return bench


def parse_only(raw: Any) -> set[str] | None:
    """`--only local,graphic` → `{"local","graphic"}`；空 → None（不筛选）。"""
    if not raw:
        return None
    items = {p.strip().lower() for p in str(raw).split(",") if p.strip()}
    bad = items - set(ASSET_BRANCHES)
    if bad:
        raise AssetError(
            f"--only 只支持 {', '.join(ASSET_BRANCHES)}，收到 {', '.join(sorted(bad))}"
        )
    return items or None


def only_matches(shot: dict[str, Any], only: set[str] | None) -> bool:
    """本次是否处理该镜（`--only` 按 `source` 筛选；用于 studio 的分步取材）。"""
    return only is None or str(shot.get("source") or "") in only


def parse_shot_ids(raw: Any) -> set[int] | None:
    """`--shots 1-10,25` → `{1..10, 25}`；空 → None（不筛选）。

    为什么需要它：本项目的出图是**最贵的一步**（一集实测 400+ 镜、按小时计），
    而"先出 10 张看看对不对"是最常用的动作 —— 没有按镜号筛选就只能整集重跑。
    `newmuyu` 那侧的 `gen_images.py --shots` 早有这个能力，主 CLI 一直缺。

    ★ 解析**失败要报错**，不能静默返回 None：把 `--shots 1..10`（写错的区间）当成
    "不筛选"会让它闷头跑完整集几百张 —— 与用户的意图正好相反，且几小时后才发现。
    """
    if not raw:
        return None
    out: set[int] = set()
    for part in str(raw).replace("，", ",").split(","):
        token = part.strip()
        if not token:
            continue
        m = re.fullmatch(r"(\d+)\s*[-–~]\s*(\d+)", token)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                lo, hi = hi, lo  # `10-1` 当 `1-10`，不比大小写错就崩
            out.update(range(lo, hi + 1))
            continue
        if token.isdigit():
            out.add(int(token))
            continue
        raise AssetError(
            f"--shots 写法看不懂：{token!r}。\n"
            "  支持逗号分隔的镜号与区间，如 `--shots 1-10,25,40-42`。"
        )
    return out or None


def shot_selected(shot: dict[str, Any], ids: set[int] | None) -> bool:
    """本次是否处理该镜（`--shots` 按镜号筛选）。镜号取 `id`，缺号时按 1-based 序位。"""
    if ids is None:
        return True
    try:
        sid = int(shot.get("id"))
    except (TypeError, ValueError):
        return True  # 没镜号的老数据不筛，免得一张都不出
    return sid in ids


def _fmt_ids(ids: list[int], *, max_show: int = 24) -> str:
    """把镜号压缩成紧凑区间串（`1-10,25`）—— 几百个镜号平铺出来没法看。"""
    if not ids:
        return "（无）"
    runs: list[str] = []
    start = prev = ids[0]
    for cur in ids[1:]:
        if cur == prev + 1:
            prev = cur
            continue
        runs.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = cur
    runs.append(f"{start}-{prev}" if start != prev else str(start))
    if len(runs) > max_show:
        return ",".join(runs[:max_show]) + f" …（共 {len(ids)} 镜）"
    return ",".join(runs)


def branches_for(
    shot: dict[str, Any],
    *,
    allow_library: bool = True,
    mode: str = sources.MODE_AUTO,
) -> tuple[str, ...]:
    """该镜**允许**命中已有素材的分支，按优先级排。

    关键：只看与当前 `source`（及 `library_asset` 钉死）匹配的分支。这样用户改了
    `source` 后重跑，**不会**误用上一次留在别的分支里的旧文件。

    `mode` 是任务级策略（票 41）：强制模式下**只认这一支**，连 §8.1 的「翻库优先」
    也一并关掉 —— 否则选了 pexels 却端出库里的文件，用户根本看不懂画面哪来的。
    例外是 `library`：它带着「未命中就回退本地生图」的第二步，所以是两支。
    """
    if shot.get("library_asset"):
        return ("library",)
    src = str(shot.get("source") or "")
    if src == prompting.KIND_GRAPHIC:
        return ("graphic",)

    mode = sources.normalize(mode)
    if mode in (sources.MODE_PEXELS, sources.MODE_LOCAL):
        return (mode,)
    if mode == sources.MODE_LIBRARY:
        return (sources.MODE_LIBRARY, sources.MODE_LOCAL)

    if src == sources.MODE_LIBRARY:
        # auto 下手动把 source 改成 library 的镜：就是"只用库"，没命中算失败
        if shot.get("resolved_by") == sources.MODE_LOCAL:
            # 例外：`resolved_by=local` 说明这个 source 是**库策略**写上、且上次已经
            # 回退生图过的（D32 的留痕），产物就留在 assets/local/。不认它就等于每次
            # 切回「自动」都白判死一次。人手改成 library 的镜没有这个标记，语义不变。
            return (sources.MODE_LIBRARY, sources.MODE_LOCAL)
        return (sources.MODE_LIBRARY,)
    if src in ("pexels", "local"):
        # 非 library 的镜也可能在「翻库」这一步命中素材库（§8.1 ③ 先于 ④）
        return ("library", src) if allow_library else (src,)
    return ("library", "pexels", "local")


def _file_digest(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _request_key(shot: dict[str, Any]) -> str:
    """决定"这一镜该取什么素材"的字段指纹（票据 46）。

    缓存原先**只按镜号**命中 `assets/<branch>/shot-NNN.*`。镜表一旦重排
    （重新钉图、增删分镜、改词），同一个镜号就对应到另一条旁白，
    而旧素材仍被判为"已有" —— 实测重新钉图后 60 镜里 **52 镜静默复用了上一版的图**，
    成片画面全错位却不报任何错。指纹写进 `assets/_src/shot-NNN.key`，对不上即作废。
    """
    parts = [
        str(shot.get("library_asset") or ""),
        str(shot.get("source") or ""),
        str(shot.get("style") or ""),
        str(shot.get("kind") or ""),
        "|".join(str(k) for k in (shot.get("keywords") or [])),
        str(shot.get("scene") or ""),
        str(shot.get("visual") or ""),
        str(shot.get("narration") or ""),
    ]
    # 参数签名统一走 `artifact.signature`（不截断，与原来的全长度 digest 一致）
    return artifact.signature(*parts, length=0)


def _src_dir(ws: Workspace) -> Path:
    return ws.path("assets", "_src")


def read_src_record(ws: Workspace, sid: int) -> str | None:
    """读该镜上次取素材时的请求指纹；没有记录（老任务）返回 None。"""
    try:
        return (_src_dir(ws) / f"{shot_name(sid)}.key").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def write_src_record(ws: Workspace, shot: dict[str, Any]) -> None:
    """记下该镜本次取素材的请求指纹，供下次复用前比对。"""
    d = _src_dir(ws)
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{shot_name(int(shot['id']))}.key").write_text(
            _request_key(shot) + "\n", encoding="utf-8")
    except OSError:
        pass


def _pinned_matches(shot: dict[str, Any], cached: Path) -> bool:
    """钉死的镜：缓存必须与 `library_asset` 指向的文件**内容一致**。

    这条不依赖指纹记录，所以对**老任务**（还没有 `.key` 记录）也立刻生效 ——
    重新钉图后镜号整体错位的那批缓存就是这样被挡下来的。
    """
    pinned = shot.get("library_asset")
    if not pinned:
        return True
    src = Path(str(pinned)).expanduser()
    if not src.is_file():
        return False
    try:
        return _file_digest(src) == _file_digest(cached)
    except OSError:
        return False


def existing_asset(
    ws: Workspace,
    shot: dict[str, Any],
    *,
    allow_library: bool = True,
    mode: str = sources.MODE_AUTO,
) -> Path | None:
    """该 shot 是否已有素材 —— **只在与它来源策略匹配的分支里找**。

    还要过两道校验，否则镜表一变就会静默复用错图（票据 46）：
      ① 钉死镜：缓存内容必须与 `library_asset` 一致；
      ② 其余镜：缓存时的请求指纹必须与当前一致（无记录的老任务放行）。
    """
    key = _request_key(shot)
    recorded = read_src_record(ws, int(shot["id"]))
    for branch in branches_for(shot, allow_library=allow_library, mode=mode):
        d = ws.path("assets", branch)
        if not d.is_dir():
            continue
        for p in sorted(d.glob(f"{shot_name(int(shot['id']))}.*")):
            if not (p.is_file() and p.stat().st_size > 0):
                continue
            if not _pinned_matches(shot, p):
                continue
            if recorded is not None and recorded != key:
                continue
            return p
    return None


def _materialize(src: Path, dst: Path) -> str:
    """把素材库文件搬进任务目录：优先**硬链接**（省盘、不改原文件），失败则复制。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    if src.suffix.lower() == dst.suffix.lower() and len(src.suffix) >= 1:
        try:
            os.link(src, dst)          # 同盘硬链接：不占额外空间，也不改原文件
            return "hardlink"
        except OSError:
            pass
    shutil.copy2(src, dst)
    return "copy"


# ---- Pexels ----------------------------------------------------------------


class PexelsClient:
    def __init__(self, api_key: str, cache_dir: Path, timeout: int = 30) -> None:
        if requests is None:
            raise AssetError("未安装 `requests`，无法下载 Pexels 素材。请 pip install requests。")
        self.api_key = api_key
        self.cache_dir = cache_dir
        self.timeout = timeout

    def search(self, keywords: list[str], per_page: int = 15) -> list[dict[str, Any]]:
        query = " ".join(k for k in keywords if k).strip()
        if not query:
            return []
        resp = requests.get(
            PEXELS_ENDPOINT,
            params={"query": query, "orientation": "landscape", "per_page": per_page},
            headers={"Authorization": self.api_key},
            timeout=self.timeout,
        )
        if resp.status_code == 401:
            raise AssetError("Pexels 拒绝请求（401）：`pexels.api_key` 无效，请在 config.toml 更正。")
        if resp.status_code >= 400:
            raise AssetError(f"Pexels 返回 HTTP {resp.status_code}：{resp.text[:200]}")
        return resp.json().get("videos", []) or []

    @staticmethod
    def pick_file(video: dict[str, Any], target_w: int = 1920) -> dict[str, Any] | None:
        """挑一个横屏 mp4：优先分辨率 ≥ 目标里最小的那个，否则取最大的。"""
        files = [f for f in video.get("video_files", []) if f.get("file_type") == "video/mp4"]
        if not files:
            return None
        landscape = [f for f in files if (f.get("width") or 0) >= (f.get("height") or 0)]
        pool = landscape or files
        bigger = [f for f in pool if (f.get("width") or 0) >= target_w]
        if bigger:
            return min(bigger, key=lambda f: f.get("width") or 0)
        return max(pool, key=lambda f: f.get("width") or 0)

    def download(self, url: str, dest: Path, retries: int = 3) -> Path:
        """下载到缓存（按 URL 命名，天然去重），再复制到 dest。不留下 0 字节坏文件。"""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        cached = self.cache_dir / f"{key}.mp4"
        if not (cached.is_file() and cached.stat().st_size > 0):
            last: Exception | None = None
            for attempt in range(retries):
                tmp = cached.with_suffix(".part")
                try:
                    with requests.get(url, stream=True, timeout=self.timeout) as r:
                        r.raise_for_status()
                        with open(tmp, "wb") as fh:
                            for chunk in r.iter_content(chunk_size=1 << 16):
                                if chunk:
                                    fh.write(chunk)
                    if tmp.stat().st_size == 0:
                        raise OSError("下载内容为空")
                    tmp.replace(cached)
                    break
                except Exception as exc:  # 网络类
                    last = exc
                    tmp.unlink(missing_ok=True)
                    time.sleep(1.5 * (attempt + 1))
            else:
                raise AssetError(f"下载失败（重试 {retries} 次）：{url}（{last}）")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cached, dest)
        return dest


# ---- 单镜解析 --------------------------------------------------------------


def _comfy_extra(config: Config) -> dict[str, Any]:
    """把 config.toml `[comfyui]` 里的可覆盖项收集成模板占位符值。

    只收集**通用旋钮与文件路径**（尺寸/批量/负向提示词/模型文件名）。模型专用的采样
    参数（采样器/步数/cfg/shift）**不在此列** —— 它们固定在 `workflows/*.json` 模板里（D11）。

    ★ 例外：**去噪强度（denoise）是 img2img 的通用旋钮，不是模型专用参数**，
    所以它允许从 `[cast].ref_denoise` 覆盖。这是 v2 方案 §4.2 明确要求"可调"的那一个
    （0.45–0.60：太高换脸、太低换不出场景），逐镜调参要靠它。
    """
    keys = (
        ("comfyui.clip", "CLIP"),
        ("comfyui.vae", "VAE"),
        ("comfyui.width", "WIDTH"),
        ("comfyui.height", "HEIGHT"),
        ("comfyui.batch", "BATCH"),
        ("comfyui.negative", "NEGATIVE"),
        # 去噪强度住在 [cast] 段（它只对参考图那一档有意义）
        ("cast.ref_denoise", "DENOISE"),
    )
    extra: dict[str, Any] = {}
    for cfg_key, token in keys:
        value = config.get(cfg_key)
        if value is not None:
            extra[token] = value
    return extra


def _shot_prompt(shot: dict[str, Any], lock) -> str:  # noqa: ANN001 - CastLock | None
    """这一镜真正送进模型的提示词。

    v2 的关键一步：`{NAME}` 槽位在这里被替换成**定妆库里冻结的锚定描述**。
    替换放在最靠近 `generate()` 的地方，而不是在 shots 阶段 —— 这样无论提示词是
    LLM 写的、启发式写的、还是人手改的，人物一致性都走同一条路，不可能漏。

    ★ 光替换还不够：`prompt` 由 LLM 改写，常把 `{NAME}` 丢掉（实测 002 首跑
    126/399 镜丢失），而 G2 判据 `slots_of_shots` 看的是 visual∪prompt **并集**，
    闸门照样放行 → 图出了却没有锚定。所以这里以 `visual`（拍摄稿原话）为槽位真源，
    prompt 缺哪个槽位就把哪句前置补回来，让**生效路径与判据一致**。
    """
    from lvs import cast as cast_mod

    prompt = str(shot.get("prompt") or "")
    visual = str(shot.get("visual") or "")
    raw = prompt or visual
    if prompt and visual:
        missing = {n for n in cast_mod.SLOT.findall(visual)} - set(
            cast_mod.SLOT.findall(prompt)
        )
        if missing:
            raw = visual.rstrip("。. ") + ". " + prompt
    return cast_mod.apply_slots(raw, lock) if lock is not None else raw


def _placeholder_png(
    shot: dict[str, Any], dst: Path, *, width: int, height: int
) -> Resolution:
    """**离线占位图**：用 ffmpeg 画一张带镜号的纯色图，不需要 ComfyUI。

    用途只有一个：让 **端到端测试** 能在没有 GPU / 没有 ComfyUI / 没有网络的机器上
    把 `parse → shots → assets → voice → build` 整条链跑通。

    为什么必须**走 `_gen_local` 这一个入口**（而不是在测试里自己造假产物）：
    测试要验证的是 `assets` 的**真实循环** —— 逐镜分支、失败隔离、熔断、契约校验、
    计数写回、门禁接线。自己造假产物就绕过了这些，等于没测。
    所以离线只是**换掉"像素从哪来"**，其余一行不改。

    画幅取 `[comfyui].width/height`（与真生图一致），使下游的尺寸假设也被覆盖到。
    """
    ffmpeg, _ = tools()
    # 颜色按镜号轮换，方便肉眼区分（与 run.py 的 demo 占位图同一手法）
    palette = ["0x1b2a41", "0x3d2b1f", "0x2b3a2b", "0x2a2238"]
    color = palette[(int(shot["id"]) - 1) % len(palette)]
    dst.parent.mkdir(parents=True, exist_ok=True)
    label = f"shot {int(shot['id']):03d}"
    vf = (
        f"drawtext=text='{label}':fontcolor=white@0.85:fontsize=64:"
        f"x=(w-text_w)/2:y=(h-text_h)/2"
    )
    try:
        ff_run([
            ffmpeg, "-y", "-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}",
            "-frames:v", "1", "-vf", vf, str(dst),
        ])
    except FFmpegError as exc:
        return Resolution(False, reason=f"占位图生成失败：{str(exc).splitlines()[0]}")
    return Resolution(True, "local", dst, info={"placeholder": True})


_GRANULARITY_BEAT = "beat"
GRANULARITIES = ("shot", "beat")


def image_granularity(config: Config) -> str | None:
    """`[shots].image_granularity` 的**唯一读取点**。没配返回 `None`。

    ★ 这个键**没有默认值**：当"同一画面位的多个镜"真的有分歧时（见
    `beats_spanning_many`），必须由人显式选一个 —— 它会明显改变成片观感
    （一个画面撑一段话 vs 镜头往里推），不该由程序替你决定。
    """
    value = str(config.get("shots.image_granularity") or "").strip().lower()
    return value or None


def _beat_reuse_on(config: Config) -> bool:
    """"同一条 beat 只出一张图"开着没有。

    ★ 抽成函数是为了让**建缓存**与**用缓存**两处判据同源 ——
    本项目的头号坑型就是"同一件事写在两处，改漏一处不报错"。
    这里踩过一次：开关读对了，但 `bench` 没建出来，于是复用静默失效。
    """
    return image_granularity(config) == _GRANULARITY_BEAT


def beats_spanning_many(shots: list[dict[str, Any]]) -> dict[str, int]:
    """**跨了多个生图镜**的画面位 → 各自的镜数。只统计一条 beat 铺 ≥2 镜的。

    只有这些画面位才让 `shot` / `beat` 产生分歧；铺 1 镜的画面位两种选法一样。
    """
    counts: dict[str, int] = {}
    for s in shots:
        if s.get("source") != "local":
            continue
        beat = _beat_of(s)
        if beat:
            counts[beat] = counts.get(beat, 0) + 1
    return {b: n for b, n in counts.items() if n > 1}


def granularity_question(shots: list[dict[str, Any]]) -> str:
    """把"该选 shot 还是 beat"写成一个**可以直接问用户**的问题（带决策所需的数字）。

    为什么要带数字：这个选择本质是"省时间 vs 保留景别推进"的权衡，
    没有数字就只能凭感觉。带上"要生成多少张 / 花多久"才问得下去。
    """
    shared = beats_spanning_many(shots)
    n_local = sum(1 for s in shots if s.get("source") == "local")
    n_shot = len({(s.get("prompt") or s.get("visual") or "").strip()
                  for s in shots if s.get("source") == "local"})
    n_beat = n_shot - sum(n - 1 for n in shared.values())
    top = max(shared.values()) if shared else 0
    minutes = lambda n: n * 36 / 60  # noqa: E731 - 实测单张约 36 秒
    return (
        "[图粒度] ★ 必须先定这个：同一画面位的多个镜，是各出一张图，还是共用一张？\n"
        f"  本集 {n_local} 个生图镜，其中 {len(shared)} 个画面位跨了多个镜（最多一条铺 {top} 镜）。\n"
        f"    选 shot → 要生成约 {n_shot} 张（约 {minutes(n_shot):.0f} 分钟）"
        "：保留「远景→近景→特写」的镜头推进\n"
        f"    选 beat → 只要生成约 {n_beat} 张（约 {minutes(n_beat):.0f} 分钟）"
        "：同一画面位共用一张，观感更「静」、更像插图铺陈\n"
        "  这个选择会明显改变成片观感，所以**没有默认值**。请在配置里显式写一个：\n"
        "    [shots]\n"
        '    image_granularity = "shot"    # 每镜一张\n'
        '    image_granularity = "beat"    # 同一画面位共用一张\n'
        "  ⚠ 改完要**重新执行本命令**（配置只在进程启动时读一次）。"
    )


def _beat_of(shot: dict[str, Any]) -> str:
    """这一镜所属的 beat（= `visual`）。空串表示"没有 beat 可归"（不去重）。"""
    return str(shot.get("visual") or "").strip()


def _remember_beat(
    shot: dict[str, Any], dst: Path, *, config: Config, bench: "CardBench | None",
) -> None:
    """把刚产出的这张图记进 beat 缓存，供同 beat 的后续镜复制。

    ★ **只在开了那一档位时才记**。否则缓存会悄悄改变"每镜一张"的既有行为 ——
    那正是本项目最忌讳的"没说要改，却改了"。
    """
    if bench is None or not _beat_reuse_on(config):
        return
    beat = _beat_of(shot)
    if beat:
        bench.rendered.setdefault(beat, dst)


def _reuse_beat_image(
    shot: dict[str, Any], dst: Path, *, config: Config, bench: "CardBench | None",
) -> bool:
    """`[shots].image_granularity = "beat"` 时：同一条 beat 只出一张图，其余镜复制它。

    ## 为什么需要这个

    实测（《雨月物语》白峰，2026-10-03）：旁白 6750 字 → **435 镜**（文风是"短句断行"，
    平均 15.5 字/句，所以一句≈一镜）。而**每镜都单独出一张图** →
    按 `prompt` 去重仍有 **390 张**（3.9 小时）。

    但稿子里作者**只声明了 46 个画面位**，而且同一个画面位里的多个镜
    本来就是"同一个画面的镜头推进"—— 出 390 张等于把一个画面拆成十几张。

    ## 为什么可以直接复用 `bench.rendered`

    它是 `dict[beat 文本 → 已产出的 PNG]`，**与 kind 无关**，
    所以 graphic 与 local 共用同一份缓存，不必各写一套。
    键用 beat（= `shot["visual"]`）是安全的：`_llm_enrich` 的 `kinds` 记忆
    （跨批次共享）**已经保证"同一条 beat 只有一个 kind"**，
    所以不存在"同一 beat 既是卡片又是生图"的撞键。

    返回 True 表示"已复制、不要再生成"。
    """
    if not _beat_reuse_on(config):
        return False
    beat = _beat_of(shot)
    if not beat or bench is None:
        return False
    done = bench.rendered.get(beat)
    if done is not None and Path(done).is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(done, dst)          # 同一条 beat 只生成一次
        return True
    return False


def _gen_local(
    shot: dict[str, Any],
    *,
    ws: Workspace,
    config: Config,
    comfy: imagegen.ComfyClient | None,
    name: str,
    lock=None,  # noqa: ANN001 - CastLock | None
    bench: "CardBench | None" = None,
) -> Resolution:
    """本地生图这一支。抽出来是因为它有两个入口：source=local，以及库未命中的回退（票 41）。"""
    dst = ws.path("assets", "local", f"{name}.png")

    # ★ 同一条 beat 复用（`[shots].image_granularity = "beat"`）——
    # 放在 placeholder 之前：复用与"像素从哪来"无关，两条后端都该省这次生成。
    if _reuse_beat_image(shot, dst, config=config, bench=bench):
        return Resolution(True, "local", dst, info={"reused_beat": _beat_of(shot)})

    # ★ 离线后端：换掉"像素从哪来"，其余（循环 / 隔离 / 熔断 / 计数）一行不改。
    # 见 `_placeholder_png` 的说明 —— 这是端到端测试能自动跑的前提。
    if str(config.get("comfyui.backend", "comfyui")).lower() == "placeholder":
        res = _placeholder_png(
            shot, dst,
            width=int(config.get("comfyui.width", 1344) or 1344),
            height=int(config.get("comfyui.height", 768) or 768),
        )
        _remember_beat(shot, dst, config=config, bench=bench)
        return res


    if comfy is None:
        return Resolution(False, reason="ComfyUI 客户端不可用（配置或依赖缺失）")
    # seed 允许逐镜覆盖（studio 的「换一张」就是把它 +1），默认仍是 全局 seed + 镜号
    seed = int(shot.get("seed") or (int(config.get("comfyui.seed", 42)) + int(shot["id"])))

    # ★ L2（参考图）：先问"这一镜有没有可用的参考图"，再据此选模板。
    # 顺序不能反 —— 反了会出现"选了参考图模板却没有图可喂"，报错离根因很远。
    ref, ref_why = _reference_for(shot, config, lock)
    try:
        info = imagegen.generate(
            _shot_prompt(shot, lock),
            dst,
            base_url=config.get("comfyui.base_url", "http://127.0.0.1:8188"),
            workflow=_workflow_for(config, ref_available=ref is not None),
            model=config.get("comfyui.model", "z_image_turbo_int8_convrot.safetensors"),
            seed=seed,
            extra=_comfy_extra(config),
            client=comfy,
            ref_image=ref,
        )
    except imagegen.ComfyError as exc:
        return Resolution(False, reason=str(exc).splitlines()[0])
    info["ref_note"] = ref_why
    _remember_beat(shot, dst, config=config, bench=bench)
    return Resolution(True, "local", dst, info=info)


def _reference_for(
    shot: dict[str, Any], config: Config, lock: Any,
) -> tuple[Path | None, str]:  # noqa: ANN401 - CastLock | None
    """这一镜该用哪张参考图（L2）。返回 `(参考图路径, 说明)`；不该用时路径为 `None`。

    ★ 三条**全满足**才给参考图：

    1. 配了 `[cast].ref_workflow` —— 用户明确要开参考图这一档
    2. 本镜提示词里含人物槽位（`{NAOKO}`）
    3. 该角色 **`bindable`** —— `state` 已批准 **且** 参考图文件真的在

    第 3 条为什么不能省：`bindable` 为假 = "这张脸还没定下来"。
    拿一张**未批准**的候选去锚定，等于把未经审核的脸固化成全集标准 ——
    比不给参考图更糟，因为它是**一致地错**。

    多人同框只取一个（排序后第一个）：当前 img2img 方案**一次只喂一张参考图**。
    这是方案的限制不是疏忽 —— 多角色同框要么拆镜，要么等 IP-Adapter 那一档
    （那台 ComfyUI 上还没有该节点，见 `docs/一致性现状核对.md`）。
    """
    if not config.get("cast.ref_workflow"):
        return None, "未配置 [cast].ref_workflow（一致性只用 L1 文字锚定）"
    if lock is None:
        return None, "没有定妆库（lock.json）"

    blob = f"{shot.get('visual') or ''} {shot.get('prompt') or ''}"
    from lvs import cast as cast_mod

    names = sorted(set(cast_mod.SLOT.findall(blob)))
    if not names:
        return None, "本镜无人物槽位"

    usable: list[str] = []
    for n in names:
        entry = lock.characters.get(n)
        if entry is not None and entry.bindable and entry.primary:
            usable.append(n)
    if not usable:
        return None, f"角色未批准或无参考图（{'/'.join(names)}）→ 跑 `lvs cast --approve`"
    pick = usable[0]
    return Path(lock.characters[pick].primary).expanduser(), f"用 {pick} 的冻结参考图"


def _report_reference_plan(shots: list[dict[str, Any]], config: Config, lock: Any) -> None:  # noqa: ANN401
    """开跑前把"本集参考图档（L2）用不用得上"说清楚。

    ★ 三处会**悄悄失效**，任何一处含糊都会让人以为开了 L2、其实还是纯文生图：

    | 失效原因 | 表现 |
    |---|---|
    | 模板文件不存在 | 配置错 → **当场抛**（复用 `_workflow_for` 的报错，单一真源） |
    | 角色未批准（`state` 不是已批准 / 参考图不在） | 逐镜回退文生图 |
    | 本集根本没有人物槽位 | 整集都不用 |

    后两种不是错误（完全合法），但**必须说出来** —— 否则用户会把"没生效"当成"生效了"。
    """
    # 先验模板。复用 `_workflow_for` 的报错文本，免得两处说法不一致。
    _workflow_for(config, ref_available=True)

    total = len(shots)
    will = 0
    why_not: dict[str, int] = {}
    for shot in shots:
        ref, why = _reference_for(shot, config, lock)
        if ref is not None:
            will += 1
        else:
            why_not[why] = why_not.get(why, 0) + 1

    if will:
        print(f"[参考图 L2] {will}/{total} 镜会用**冻结的定妆照**作参考图（img2img 低去噪）")
    else:
        print(f"[参考图 L2] 本集 {total} 镜**都不会**用参考图 —— 一致性只有 L1 文字锚定")
    for why, n in sorted(why_not.items(), key=lambda kv: -kv[1]):
        print(f"[参考图 L2]   {n} 镜回退纯文生图：{why}")


def _report_granularity_plan(shots: list[dict[str, Any]]) -> None:
    """开跑前报出"本次要**生成**多少张图、复用多少镜"。

    ★ 为什么要报（真踩过）：`[shots].image_granularity` **只在进程启动时读一次**。
    用户改了配置却没重启那个正在跑的 assets 进程 → 静默按老档跑、一镜一张，
    而输出里**一个字都没提粒度**，跑了近一小时才发现没生效。

    这正是本项目最忌讳的**静默失效**：开关看着开了，行为没变，且没有任何提示。
    报出"复用 N 镜 / 生成 M 张"之后，数字对不上就能立刻发现。
    """
    local = [s for s in shots if s.get("source") == "local"]
    if not local:
        return
    beats: dict[str, int] = {}
    for s in local:
        beat = _beat_of(s)
        if beat:
            beats[beat] = beats.get(beat, 0) + 1
    will_gen = len(beats) + sum(1 for s in local if not _beat_of(s))
    reused = len(local) - will_gen
    if reused <= 0:
        print(f"[图粒度] beat：本批 {len(local)} 个生图镜，但**没有可复用的画面位** → 仍要各出一张")
        return
    print(
        f"[图粒度] beat：{len(local)} 个生图镜 → 只**生成 {will_gen} 张**，"
        f"复用 {reused} 镜（{len(beats)} 个画面位）"
    )


def _workflow_for(config: Config, *, ref_available: bool) -> str:
    """这一镜用哪个工作流模板。**换模板 = 换一致性档位**，不动代码（D11）。

    `ref_available=True` 表示调用方**确实拿到**了一张参考图（见 `_reference_for`）。
    只有到那时才选参考图模板 —— 反过来做（只凭"有槽位"就选模板）会让
    `generate()` 因为"模板要参考图但没给"而报错，而真正的根因是"没有可用的参考图"，
    报错的地方离根因太远，最难查。

    配了 `[cast].ref_workflow` 但模板文件不存在时**当场报错**，并指出**正确的配置键**。
    （原先这种错要等到生图时才炸，且提示写的是 `[comfyui].workflow` ——
    键名是错的，用户会去改一个跟问题无关的配置，怎么改都不对。）
    """
    default = config.get("comfyui.workflow", "zimage_turbo.json")
    if not ref_available:
        return default
    name = str(config.get("cast.ref_workflow") or "")
    if not name:
        return default
    if not imagegen.template_path(name).is_file():
        raise AssetError(
            f"`[cast].ref_workflow = {name!r}`，但 `workflows/{name}` 不存在。\n"
            "  （注意是 `[cast].ref_workflow`，**不是** `[comfyui].workflow`。）\n"
            "  项目自带的参考图模板是 `zimage_scene_ref.json`；"
            "把 `[cast].ref_workflow` 留空即走默认文生图模板。"
        )
    return name


def resolve_shot(
    shot: dict[str, Any],
    *,
    ws: Workspace,
    config: Config,
    index: dict[str, Any] | None,
    allow_library: bool,
    min_score: int,
    pexels: PexelsClient | None,
    comfy: imagegen.ComfyClient | None,
    bench: "CardBench | None" = None,
    mode: str = sources.MODE_AUTO,
    lock=None,  # noqa: ANN001 - CastLock | None；定妆库，供 {NAME} 注入与参考图选型
) -> Resolution:
    """按 §8.1 顺序为单个 shot 取素材。`mode` 是任务级来源策略（票 41）。"""
    sid = int(shot["id"])
    name = shot_name(sid)
    mode = sources.normalize(mode)

    # ① 钉死
    pinned = shot.get("library_asset")
    if pinned:
        src = Path(pinned).expanduser()
        if src.is_file():
            dst = ws.path("assets", "library", f"{name}{src.suffix.lower() or '.png'}")
            how = _materialize(src, dst)
            return Resolution(True, "library", dst, info={"pinned": str(src), "how": how})
        return Resolution(False, reason=f"library_asset 指向的文件不存在：{src}")

    keywords = shot.get("keywords") or []
    if not keywords:
        keywords = [shot.get("visual") or shot.get("narration") or ""]

    # ② source=library：先查素材库；策略=library 时未命中就**回退本地生图**
    if shot.get("source") == sources.MODE_LIBRARY:
        hit = library.best_match(index, keywords, min_score=min_score) if index else None
        if not hit:
            if mode == sources.MODE_LIBRARY or shot.get("resolved_by") == sources.MODE_LOCAL:
                # source 保持 library —— 它是「意向」；实际走了哪支由 resolved_by 记。
                # branches_for 同时认 library 与 local 两支，所以重跑仍幂等。
                # 第二个条件是 D32 的留痕：库策略回退过的镜，切回「自动」后也该继续
                # 回退 —— 否则切回自动会把它们全判死，而产物就躺在 assets/local/ 里。
                res = _gen_local(shot, ws=ws, config=config, comfy=comfy, name=name, lock=lock, bench=bench)
                if res.ok:
                    res.info["fallback"] = "library→local"
                else:
                    # ★ 回退也失败时，**必须说清是"库没找到"导致的**。
                    # 否则用户只看到"ComfyUI 不可达" —— 而他根本没打算用 ComfyUI
                    # （明确传了 --source library），完全不知道真正的问题是关键词没匹配上。
                    # 这是实测踩出来的：小白配好图片文件夹后第一集必挂在这一步。
                    res.reason = (
                        f"素材库没找到匹配的图（关键词：{'/'.join(str(k) for k in keywords[:3])}）"
                        f" → 回退本地生图也失败：{res.reason}\n"
                        f"      两个办法：① 把图片**改名为包含这些关键词**（如「草地.png」）；"
                        f"② 或配好 LLM key 让拆镜给出更好的关键词（`lvs shots --force`）"
                    )
                return res
            return Resolution(False, reason=f"source=library 但素材库未命中（关键词 {'/'.join(keywords[:3])}）")
        src = Path(hit["path"])
        if not src.is_file():
            return Resolution(False, reason=f"素材库索引中的文件已不存在：{src}")
        dst = ws.path("assets", "library", f"{name}{src.suffix.lower()}")
        how = _materialize(src, dst)
        return Resolution(True, "library", dst, info={"score": None, "how": how, "src": str(src)})

    # ③ 图文/图表 beat：本地排版成卡片，**不调 ComfyUI**（票据 28/34/35）
    if shot.get("source") == prompting.KIND_GRAPHIC:
        beat = shot.get("visual") or ""
        card = (bench.content.get(beat) if bench is not None else None) or cards.card_for(
            beat, [shot.get("narration") or ""]
        )
        dst = ws.path("assets", "graphic", f"{name}.png")
        done = bench.rendered.get(beat) if bench is not None else None
        try:
            if done is not None and Path(done).is_file():
                shutil.copy2(done, dst)      # 同一条 beat 只栅格化一次（票据 35）
            else:
                graphic.render(card, dst)
                if bench is not None:
                    bench.rendered[beat] = dst
        except graphic.GraphicError as exc:
            return Resolution(False, reason=str(exc))
        return Resolution(
            True, prompting.KIND_GRAPHIC, dst,
            info={"kind": card.kind, "items": list(card.items), "source": card.source},
        )

    # ④ 翻库优先（只在 auto 策略下——强制模式已经指定了要看哪一支）
    if mode == sources.MODE_AUTO and allow_library and index:
        scored = library.search(index, keywords, min_score=min_score)
        if scored:
            hit, score = scored[0]
            src = Path(hit["path"])
            if src.is_file():
                dst = ws.path("assets", "library", f"{name}{src.suffix.lower()}")
                how = _materialize(src, dst)
                return Resolution(True, "library", dst, info={"score": score, "how": how, "src": str(src)})

    # ⑤ 按 source 分支
    if shot.get("source") == sources.MODE_LOCAL:
        return _gen_local(shot, ws=ws, config=config, comfy=comfy, name=name, lock=lock, bench=bench)

    # pexels
    if pexels is None:
        return Resolution(False, reason="Pexels key 未配置（config.toml [pexels].api_key）")
    try:
        videos = pexels.search(keywords)
    except AssetError as exc:
        return Resolution(False, reason=str(exc))
    if not videos:
        return Resolution(False, reason=f"Pexels 无结果（关键词 {'/'.join(keywords[:3])}）")
    chosen = None
    picked_file = None
    for video in videos:
        f = PexelsClient.pick_file(video)
        if f:
            chosen, picked_file = video, f
            break
    if not picked_file:
        return Resolution(False, reason="Pexels 结果里没有可用的横屏 mp4")
    dst = ws.path("assets", "pexels", f"{name}.mp4")
    try:
        pexels.download(picked_file["link"], dst)
    except AssetError as exc:
        return Resolution(False, reason=str(exc))
    return Resolution(True, "pexels", dst, info={"video_id": chosen.get("id"), "width": picked_file.get("width")})


# ---- 命令入口 --------------------------------------------------------------


def run_command(config: Config, ws: Workspace, args) -> int:  # noqa: ANN001
    force = bool(getattr(args, "force", False))
    allow_library = not bool(getattr(args, "no_library", False))
    only = parse_only(getattr(args, "only", None))

    try:
        data = ws.load_shots(error=AssetError)
    except AssetError as exc:
        print(str(exc))
        return 2

    shots = data.get("shots", [])
    if not shots:
        print("shots.json 里没有分镜。")
        return 2

    # 阶段间契约：下游拿到的分镜表结构不对就**当场停下**，别按默认值跑出错产物。
    # 消息会点名是 `lvs shots` 产的、缺什么、该重跑哪条命令（见 `lvs/handoff.py`）。
    msg = handoff.check_shots_or_message(
        shots, stage="素材（assets）", consumer="assets",
        command=f"lvs shots --task {ws.task}",
    )
    if msg:
        print(msg)
        return 2

    # ---- 素材来源策略（票 41）----
    # 策略是「意向」，就在这儿落成逐镜的 source。放在 selected 之前算，因为
    # `--only` 是按 source 筛的 —— 顺序反了就会筛出一批即将被改掉的镜。
    try:
        mode = sources.resolve(getattr(args, "source", None), sources.mode_of(data))
    except sources.SourceModeError as exc:
        print(str(exc))
        return 2
    recorded = data.get("source_mode")
    changed = sources.apply_mode(data, mode)
    if mode != sources.MODE_AUTO:
        extra = f"，其中 {changed} 个实拍镜改了来源" if changed else ""
        print(f"素材来源策略：{sources.LABELS[mode]}{extra}")
    if changed or recorded != mode:
        # 策略本身是用户的意图，即便下面失败也要留下（否则下次重跑又变回 auto）
        ws.write_shots(data)

    if mode == sources.MODE_LIBRARY and not allow_library:
        print("来源策略是「本地素材库」，但 --no-library 把库关掉了 —— 两者矛盾，无从取素材。")
        return 2

    selected = [s for s in shots if only_matches(s, only)]
    if only is not None:
        print(f"只取这些来源：{'/'.join(sorted(only))}（其余 {len(shots) - len(selected)} 镜本次不动）")

    # ---- 镜号筛选（试水/补镜）----
    # 放在定妆闸门与出图之前：445 镜的一集要跑几小时，"先出 10 张看看"是常规动作。
    # ★ 闸门仍然照走 —— 只出 10 镜**不等于**可以不冻脸（这 10 镜里可能就有出场人物）。
    try:
        ids = parse_shot_ids(getattr(args, "shots", None))
    except AssetError as exc:
        print(str(exc))
        return 2
    if ids is not None:
        before = len(selected)
        selected = [s for s in selected if shot_selected(s, ids)]
        hit = {int(s.get("id") or 0) for s in selected}
        missing = sorted(i for i in ids if i not in hit)
        print(f"只做这些镜号：{_fmt_ids(sorted(i for i in ids if i in hit))}"
              f"（本次 {len(selected)} 镜；其余 {before - len(selected)} 镜不动）")
        if missing:
            # 报出来而不是忽略：`--shots 1-10` 里夹着超出范围的镜号，
            # 常常说明有人按 1-based 还是 0-based 数错了。
            print(f"  ⚠ 这些镜号在本次范围内不存在，已忽略：{_fmt_ids(missing)}")
        if not selected:
            print("按镜号筛完之后一个都不剩 —— 检查一下镜号范围（本集镜号见 `lvs board --task`）。")
            return 0

    # ---- 出图闸门（v2 / G2）：人物没冻结就不许生成 ----
    # 这是**报错**不是警告。理由是实测代价：没有冻结的脸，整集的分镜图全部作废
    # （2026-10-01，479 张）。宁可现在停下来 30 秒，也别两小时后再来一遍。
    lock = None
    try:
        from lvs import cast as cast_mod

        cast_root = cast_mod.cast_dir(config)
        lock = cast_mod.load_lock(cast_root)
    except Exception as exc:  # noqa: BLE001 - 定妆库坏掉不该让整个 assets 不可用
        print(f"[定妆库] 读取失败，本次跳过人物一致性注入：{exc}")
        lock = None

    if lock is not None and not bool(getattr(args, "no_cast_gate", False)):
        missing = cast_mod.gate_missing(selected, lock)
        if missing:
            print(cast_mod.gate_message(missing, lock, cast_mod.cast_dir(config)))
            return 2
        if lock.characters:
            ready = len(lock.bindable_characters())
            used = cast_mod.slots_of_shots(selected)
            if ready and not used:
                # 闸门"通过"其实是因为**本集根本没有人物槽位** —— 这跟"人物已冻结"是
                # 两码事。不说清楚的话，用户会以为一致性保护生效了（其实一点没生效）。
                print(
                    f"[定妆库] {ready} 个人物已冻结，但**本集的画面位里没有任何 {{NAME}} 槽位** ——\n"
                    "  人物一致性注入**未生效**。想让它生效，在拍摄稿的画面位里写成\n"
                    "     [画面位] [场景] {角色名} 侧身走过校墙，背景只有枯枝\n"
                    "  名字要与定妆库登记名一致（`lvs cast` 可看清单）。"
                )
            elif ready:
                print(
                    f"[定妆库] {ready} 个人物已冻结；本集用到 {len(used)} 个"
                    f"（{', '.join(sorted(used))}），提示词里的槽位会注入冻结描述"
                )

    # ---- 图粒度：**没定就不许开跑** ----
    #
    # ★ 为什么是"拦住"而不是"给个默认值"：这个选择直接决定成片观感
    # （一个画面撑一段话 vs 镜头往里推），而且代价差 3 倍算力 ——
    # 让程序替人决定，等于把一个创作决定混进默认配置里，人不会发现。
    #
    # 只在**真有分歧时**才拦：铺 1 镜的画面位，shot / beat 两种选法完全一样，
    # 那时问也是白问（用户明确要的是"不懂就问我"，不是"逢事必问"）。
    gran = image_granularity(config)
    if gran is None:
        if beats_spanning_many(selected):
            print(granularity_question(selected))
            return 2
    elif gran not in GRANULARITIES:
        print(
            f"`[shots].image_granularity = {gran!r}` 不认识。\n"
            f"  只接受：{' / '.join(GRANULARITIES)}\n"
            "    shot = 每镜一张（保留景别推进）\n"
            "    beat = 同一画面位共用一张（省算力）"
        )
        return 2

    # ---- L2 参考图档：**开跑前**把"到底用不用得上"说清楚 ----
    #
    # 为什么放在循环之前：参考图档有三处会悄悄失效（模板缺 / 角色未批准 / 本集无槽位），
    # 逐镜报错会淹没在几百行输出里，用户会**以为开了 L2、其实还是纯文生图**。
    # 整段汇总一次，一眼可见。模板缺失属配置错，在这里**当场抛**（不等到第 300 镜）。
    if config.get("cast.ref_workflow"):
        _report_reference_plan(selected, config, lock)

    # ---- 图粒度：开跑前说清"这一集要生成多少张图" ----
    #
    # 为什么必须报：`[shots].image_granularity` 是**省算力的关键旋钮**，
    # 但它**只在进程启动时读一次**。改完配置不重启，就会**静默按老档跑**。
    # 实测踩到（2026-10-03）：用户改配置后没有重启 assets，
    # 28 镜全部单独生图，一小时白花，而输出里**一句话都没提粒度**。
    #
    # 所以这里主动报出"多少镜、预计生成多少张、复用多少"，一眼可核。
    if _beat_reuse_on(config):
        _report_granularity_plan(selected)

    # ---- 素材库索引（未配置/不存在 → 静默跳过） ----
    index: dict[str, Any] | None = None
    min_score = int(config.get("library.min_score", 1))
    if allow_library:
        dirs = [d for d in (config.get("library.dirs", []) or []) if d]
        if dirs:
            existing = [d for d in dirs if Path(d).expanduser().is_dir()]
            if existing:
                index = library.build_index(
                    existing,
                    recursive=bool(config.get("library.recursive", True)),
                    root=ws.root,
                    reindex=force,
                )
                print(f"素材库：{index['count']} 个文件（{'缓存' if index.get('from_cache') else '重新扫描'}）")
            else:
                print("素材库目录不存在，跳过翻库。")
        else:
            print("未配置本地素材库，跳过翻库。")

    if mode == sources.MODE_LIBRARY and not index:
        print(
            "来源策略是「本地素材库」，但库里没有可用索引 —— 本次所有实拍镜都会回退本地生图。\n"
            "  想用库就先在 config.toml 配好 [library].dirs 并跑 `lvs library index`。"
        )

    # ---- 图文/图表渲染器 ----
    need_graphic = any(
        s.get("source") == prompting.KIND_GRAPHIC and not s.get("library_asset") for s in selected
    )
    if need_graphic and not graphic.available():
        print(
            "有分镜需要图文/图表卡片，但渲染条件不满足（缺 Pillow 或系统中文字体）。\n"
            "  请 pip install Pillow，或把画面模式改成 photo（`lvs shots --visual photo`）。"
            "相关分镜将标记失败，其余分镜继续。"
        )

    # ---- Pexels ----
    need_pexels = any(
        s.get("source") == "pexels" and not s.get("library_asset") for s in selected
    )
    pexels: PexelsClient | None = None
    if need_pexels:
        key = config.get("pexels.api_key")
        if not key:
            # ★ **不中断整条，逐镜隔离**。
            #
            # 原先这里 `return 2` 中止整个阶段 —— 但那不符合本项目自己的原则：
            # "Pexels key 没配"只影响 **source=pexels 的那几镜**，其余镜（local / library /
            # graphic）本该照常出图。硬拦的后果是实测过的：
            # `source_mode="auto"` + 空 key 时，13 镜里有 8 镜本该出图，却因为 5 镜要 Pexels
            # 而**一张都不出**（退出码 2），用户完全不知道是"那 5 镜"的锅。
            #
            # 下游 `resolve_shot` 本就有 `pexels is None` 的处理（逐镜记失败原因），
            # 所以这里只要不拦，行为就是对的：坏的镜标记失败、好的镜照常，最后返回 1。
            pexels_count = sum(
                1 for s in selected
                if s.get("source") == "pexels" and not s.get("library_asset")
            )
            print(
                f"注意：有 {pexels_count} 个分镜的来源是 Pexels，但 `pexels.api_key` 为空 ——\n"
                "  这些镜会标记失败，**其余分镜照常出图**（逐镜隔离）。想救它们，三选一：\n"
                "    ① 填 key：config.toml 的 [pexels] api_key（https://www.pexels.com/api/ 免费）\n"
                "    ② 改成用本地生图：`lvs shots --task <名> --source local --force`\n"
                "    ③ 单改某几镜：编辑 shots.json 把它们的 \"source\" 改成 \"local\""
            )
        else:
            pexels = PexelsClient(str(key), ws.root / CACHE_DIRNAME)

    # ---- ComfyUI ----
    # 除了 source=local 的镜，策略=library 时的**回退**也要它 —— 所以这里要一起算上，
    # 否则库没命中的镜会死在"客户端不可用"上。
    fallback_needs_local = mode == sources.MODE_LIBRARY and any(
        s.get("source") == sources.MODE_LIBRARY and not s.get("library_asset") for s in selected
    )
    need_local = fallback_needs_local or any(
        s.get("source") == sources.MODE_LOCAL and not s.get("library_asset") for s in selected
    )
    comfy: imagegen.ComfyClient | None = None
    offline_backend = str(config.get("comfyui.backend", "comfyui")).lower() == "placeholder"
    if need_local and offline_backend:
        # 离线后端：不起 ComfyUI 客户端、不做健康检查、不查 GPU 守卫 ——
        # 像素由 ffmpeg 造，跟显卡/服务都没关系。这样"没有 ComfyUI 的机器"
        # 也能把整条 assets 循环跑完（端到端测试就靠它）。
        print("本地生图后端：placeholder（离线占位图，不连 ComfyUI）")
    elif need_local:
        base = config.get("comfyui.base_url", "http://127.0.0.1:8188")
        comfy = imagegen.ComfyClient(base)
        if not comfy.health():
            if fallback_needs_local and not mode == sources.MODE_LOCAL:
                # 这里的"需要本地生图"**只是因为库未命中的兜底**，不是用户要生图。
                # 直接说"请先启动 ComfyUI"会把人吓住（他明明只配了素材库）——
                # 实测踩过：小白因此以为必须装 ComfyUI 才能用。
                print(
                    f"提示：素材库未命中的镜会尝试回退本地生图，但 ComfyUI 不可达（{base}）。\n"
                    "  命中素材库的镜不受影响、照常出片；未命中的镜会报错并告诉你怎么改。\n"
                    "  想让未命中的镜也能出图，再启动 ComfyUI（见 README「本地生图」）。"
                )
            else:
                print(
                    f"有分镜需要本地生图，但 ComfyUI 不可达：{base}\n"
                    "  请先启动 ComfyUI（见 README「本地生图」）。"
                    "相关分镜将标记失败，其余分镜继续。"
                )
        else:
            from lvs import guard

            try:
                note = guard.check("imagegen", config)
                if note:
                    print(f"[GPU 守卫] {note}")
            except guard.GPUConflict as exc:
                print(f"[GPU 守卫] {exc}")
                print("  为避免 OOM，本次不进行本地生图；相关分镜将标记失败。")

    # ---- 逐镜解析（失败隔离 + 熔断） ----
    done = skipped = 0
    failures: list[tuple[int, str]] = []
    by_source: dict[str, int] = {}
    fell_back: list[int] = []      # 库里没命中、回退生图成功的镜（票 41）
    # 连续失败熔断：ComfyUI/TTS 服务挂了会**逐镜失败到底**，654 镜就是 654 次超时。
    # 「连续」这个信号说明问题不在单件、而在环境 —— 到阈值就停（见 `lvs/breaker.py`）。
    breaker = breaker_mod.Breaker(breaker_mod.limit_from(config))
    tripped = False
    processed = 0
    log_on = runlog.enabled(config)

    # 卡片按 beat 建一次内容、只栅格化一次（票据 35）；用全部镜而非 selected，
    # 这样"只跑 graphic 子集"时旁白回退也不会缺料。
    #
    # ★ `bench` 现在有**两个**用途，所以两种情况下都要建：
    #   ① graphic 卡片：同一 beat 只栅格化一次（票据 35）
    #   ② `[shots].image_granularity = "beat"`：同一 beat 只**生图**一次
    # 原先只在 `need_graphic` 时建 —— 纯 local 的项目（比如雨月物语）拿不到 bench，
    # 复用会**静默不生效**。实测踩到：开关打开、数字没变，因为 bench 是 None。
    # （又一次"定义了却从不接线"——所以这里加了 `_beat_reuse_on` 让两处判据同源。）
    bench = CardBench.build(shots) if (need_graphic or _beat_reuse_on(config)) else None

    def _progress() -> None:
        """每镜一记（票 26）：跑到一半被中断时，盘上要留着"跑到哪了"。"""
        ws.mark_stage_progress("assets", done=done, skipped=skipped,
                               failed=len(failures), total=len(selected))

    for shot in track(selected, "素材"):
        sid = int(shot["id"])
        processed += 1
        if not force:
            existed = existing_asset(ws, shot, allow_library=allow_library, mode=mode)
            if existed:
                shot["resolved_by"] = existed.parent.name
                shot["status"] = "done"
                shot["asset_path"] = str(existed)
                write_src_record(ws, shot)   # 老任务补记指纹，下次才比对得上
                skipped += 1
                if log_on:
                    runlog.event(ws, "assets", "shot_skip", shot=sid,
                                 by=shot.get("resolved_by") or "")
                _progress()
                # ★ 刻意**不**喂给熔断器：跳过 = "这个镜的产物早已在盘上"，
                # 既没验证服务是活的（不算成功），也不是失败。喂进去会污染
                # "连续失败"这个信号 —— 而熔断全靠它区分"单件坏"与"环境坏"。
                continue
        res = resolve_shot(
            shot, ws=ws, config=config, index=index, allow_library=allow_library,
            min_score=min_score, pexels=pexels, comfy=comfy, bench=bench, mode=mode,
            lock=lock,
        )
        if res.ok:
            shot["resolved_by"] = res.resolved_by
            shot["status"] = "done"
            # 与「跳过」分支保持一致：成功也要写回产物路径（下游与人工排查都看它）
            if res.path is not None:
                shot["asset_path"] = str(res.path)
            shot.pop("error", None)  # 上一次失败留下的原因要清掉
            if res.resolved_by == "library" and res.info.get("src") and not shot.get("library_asset"):
                shot["library_asset"] = res.info["src"]
            # ★ 逐镜记下"这一镜的图是怎么来的、用没用参考图"。
            #
            # 为什么值得写进 shots.json：翻 300 张图时最想知道的就是
            # "这一镜为什么脸不一样" —— 答案要么是"它没用参考图"（空镜/角色未批准），
            # 要么是"用了但 denoise 没调好"。不记下来就只能猜。
            # 只在**真的用了**时写，免得给每一镜都塞一堆空字段。
            if res.info.get("ref_source"):
                shot["reference_image"] = res.info["ref_source"]
            if res.info.get("workflow"):
                shot["gen_workflow"] = res.info["workflow"]
            write_src_record(ws, shot)
            if res.info.get("fallback"):
                fell_back.append(sid)
            done += 1
            by_source[res.resolved_by or "?"] = by_source.get(res.resolved_by or "?", 0) + 1
        else:
            shot["status"] = "failed"
            shot["error"] = res.reason
            failures.append((sid, res.reason))
        _progress()

        # 轨迹：一镜一行。长跑崩了之后，"哪一镜开始出问题、是不是同一类错误连着来"
        # 全靠它（见 `lvs/runlog.py`）。观测失败不影响主流程。
        if log_on:
            runlog.event(
                ws, "assets", "shot_ok" if res.ok else "shot_fail",
                shot=sid, by=res.resolved_by or "", reason="" if res.ok else res.reason[:200],
            )

        # 熔断判定：成功会清零连续计数；连续失败到阈值即停。
        if breaker.record(res.ok, index=processed):
            tripped = True
            if log_on:
                runlog.event(ws, "assets", "breaker_tripped",
                             consecutive=breaker.consecutive, at_index=processed,
                             remaining=len(selected) - processed)
            print(
                breaker.message(
                    stage="素材（assets）",
                    remaining=len(selected) - processed,
                    command=f"lvs assets --task {ws.task}",
                )
            )
            break

    # 写回 shots.json（resolved_by / status / library_asset）
    ws.write_shots(data)

    total = len(shots)
    print(f"素材获取完成：新取 {done}，跳过 {skipped}，失败 {len(failures)}（共 {total} 镜）")
    if tripped:
        print(
            f"  ⚠ 已熔断：连续失败 {breaker.consecutive} 次后停止，"
            f"本次只处理了 {processed}/{len(selected)} 镜。"
        )
    if by_source:
        print("  来源：" + "，".join(f"{k} {v}" for k, v in sorted(by_source.items())))
    if failures:
        print("  失败明细（前 10）：")
        for sid, reason in failures[:10]:
            print(f"    - shot {sid:03d}：{reason}")
        print("  提示：修好原因后重跑 `lvs assets`（已成功的镜会跳过）。")
    if fell_back:
        # 自动回退必须是**可见**的：库里没命中却被静默补上，用户会以为库里真有这些素材
        shown = "、".join(str(s) for s in fell_back[:12])
        print(
            f"  库未命中、已回退本地生图：{len(fell_back)} 镜"
            f"（{shown}{' …' if len(fell_back) > 12 else ''}）"
        )
        print("  想留在库里？改这些镜的关键词，或放宽 config.toml 的 [library].min_score。")

    # 记录**每镜产物**，而不是笼统的 shots.json —— 这样删掉任一素材即触发重跑（§7.1）
    outputs = [ws.path("shots.json")]
    outputs += [
        Path(s["asset_path"]) for s in shots
        if s.get("asset_path") and Path(s["asset_path"]).is_file()
    ]
    ws.mark_stage(
        "assets",
        outputs=outputs,
        status=stage_status(len(failures)),
        total=total, done=done, skipped=skipped, failed=len(failures),
    )
    # ★ 有失败件就返回 EXIT_FAILED（1），不再一律 0 ——
    # 否则 `errors.EXIT_FAILED`（"跑完了但有失败件"）永远不会被这个阶段返回，
    # 脚本与 `lvs run` 都会以为一切正常（文档与实现不符，正是本项目反复吃亏的形状）。
    return EXIT_FAILED if failures else 0
