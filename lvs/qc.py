"""生图巡检（`lvs qc`）—— 分块检查，别等跑完才发现全批废了。

## 为什么必须"定期检查"

2026-10-01 的实测代价：**479 张图跑了 10 小时**，跑完才发现整批用的是旧版提示词
（manifest 里还带着 `green public telephone booth`）。如果每 20 张抽一次 manifest，
10 分钟就能发现。

Z-Image Turbo 的失败模式恰好都是**批量性**的：否定句被画出来、年代道具淋洒、
空镜长出一个人。这类故障早 1 块发现，省后面 N 块的算力与电。

## 三层检查

| 层 | 查什么 | 判据 |
|---|---|---|
| 完整性 | 文件非零、能解码、尺寸符合预期 | 硬错误 |
| 彩度   | 整张偏彩（Z-Image 偶发） | 均值 > 阈值 → **自动转灰度**，原图移入 `_fixed/` |
| 人数   | 空镜不该有人；人物镜该有脸 | 有 cv2 才算，没有就跳过 |

外加一条：**manifest 的 prompt 字段抽检** —— 这是抓"提示词版本漂移"的唯一手段。

## 修正的两条纪律（踩过坑）

1. 转灰度后**必须把原彩色图移出图片目录**（`_fixed/<name>.orig.png`），
   否则后续 glob 会把它当成正式镜图。
2. **不能只看文件夹**判断版本 —— 必须读 manifest 里的 `prompt` 字段。
"""

from __future__ import annotations
from lvs import handoff
from lvs.errors import LvsError, EXIT_USAGE

import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

LEVEL_ERROR = "error"
LEVEL_WARN = "warn"

FIXED_DIRNAME = "_fixed"

# 彩度阈值：整张图的有彩像素比例超标即判"偶发彩色"
DEFAULT_CHROMA_MAX = 5.0

# 巡检分块大小
DEFAULT_BLOCK = 20


class QcError(LvsError, RuntimeError):
    """巡检配置级错误。"""

    exit_code = EXIT_USAGE


@dataclass
class Issue:
    level: str
    code: str
    shot_id: int
    message: str
    path: str = ""
    hint: str = ""

    def render(self) -> str:
        mark = "✗" if self.level == LEVEL_ERROR else "!"
        return f"  {mark} [Q{self.code}] shot {self.shot_id:03d}：{self.message}" + (
            f"\n      → {self.hint}" if self.hint else ""
        )


@dataclass
class ImageStats:
    shot_id: int
    path: str
    bytes: int = 0
    width: int = 0
    height: int = 0
    chroma: float = -1.0
    faces: int = -1          # -1 = 未检测
    ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BlockReport:
    block: int
    first: int
    last: int
    stats: list[ImageStats] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    prompt_versions: dict[str, int] = field(default_factory=dict)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == LEVEL_ERROR]

    def to_dict(self) -> dict[str, Any]:
        return {
            "block": self.block,
            "range": [self.first, self.last],
            "checked": len(self.stats),
            "errors": len(self.errors),
            "warns": len(self.issues) - len(self.errors),
            "fixed": self.fixed,
            "prompt_versions": self.prompt_versions,
            "images": [s.to_dict() for s in self.stats],
            "issues": [asdict(i) for i in self.issues],
        }


# ---- 图像能力（可降级） ----------------------------------------------------

try:  # pragma: no cover - 环境相关
    from PIL import Image  # type: ignore

    _HAS_PIL = True
except Exception:  # noqa: BLE001
    Image = None  # type: ignore[assignment]
    _HAS_PIL = False


def _make_face_detector(model_path: Path | None = None) -> tuple[Any, str]:
    """**唯一**的人脸检测器加载点。返回 `(detector 或 None, 说明或失败原因)`。

    ★ 为什么必须有这么一处：`_load_face_counter` 与 `face_model_status` 都要
    "找权重 → 建检测器"，**同一件事写两遍就是本项目最痛恨的那类隐患**
    ——两处判据迟早会漂（一处说能用、另一处说不能用）。

    所以：定位权重、`cv2` 可用性、能否解析该 onnx，**全部只在这里判一次**，
    两个调用方共用结论。

    权重位置：`[qc].face_model` 优先，其次仓库 `models/yunet.onnx`。
    **不写死任何项目的绝对路径。**

    ★★ Windows 上 cv2 的 ONNX 读取器**吃不了非 ASCII 路径**（2026-10-03 实锤：
    同一份字节，`models/yunet.onnx` 能加载，`村上春树/…/yunet.onnx` 报
    `Can't read ONNX file`）。而用户的权重天然放在中文书名目录下 ——
    之前把这误诊成"权重退化"查了很久。所以：路径带中文时**先复制到
    ASCII 缓存再加载**，字节不动，只动位置。
    """
    try:  # pragma: no cover - 环境相关
        import cv2  # type: ignore
    except Exception:  # noqa: BLE001
        return None, "没装 opencv-python（`pip install opencv-python`）"

    from lvs.config import PROJECT_ROOT

    candidates: list[Path] = []
    if model_path is not None:
        candidates.append(Path(model_path).expanduser())
    candidates.append(PROJECT_ROOT / "models" / "yunet.onnx")

    # ★ 显式配置的权重不存在 → **大声报错**，不静默回落到仓库权重。
    #   否则用户把 `[qc].face_model` 指到挪走/改名的文件，qc 照样"通过"，
    #   用的却是另一份权重 —— 报告与配置不符，正是本项目最痛恨的静默。
    if model_path is not None and not candidates[0].is_file():
        return None, f"配置的 `[qc].face_model` 不存在：{candidates[0]}"

    model = next((p for p in candidates if p.is_file()), None)
    if model is None:
        return None, "找不到人脸模型权重（`[qc].face_model` 或 `models/yunet.onnx`）"

    load_path = _ascii_cache_copy(model)
    try:  # pragma: no cover - 环境相关
        detector = cv2.FaceDetectorYN.create(str(load_path), "", (320, 320), 0.6, 0.3, 5000)
    except Exception as exc:  # noqa: BLE001
        first = str(exc).splitlines()[0][:140]
        return None, f"cv2 读不了这个 onnx（{first}）"
    return detector, str(model)


def _ascii_cache_copy(model: Path) -> Path:
    """非 ASCII 路径 → 复制到 ASCII 缓存目录；已是 ASCII 就原样返回。

    缓存按 `artifact.signature(路径, 尺寸, mtime)` 命名：换权重自动换缓存，
    不会读到陈旧副本。复制失败（无权限等）则返回原路径 —— 让后续 create
    报真实错误，而不是在这里拦死。
    """
    text = str(model)
    try:
        text.encode("ascii")
        return model
    except UnicodeEncodeError:
        pass
    try:  # pragma: no cover - 环境相关
        from lvs.artifact import signature
        import shutil
        import tempfile

        cache_dir = Path(tempfile.gettempdir()) / "lvs_face_model"
        cache_dir.mkdir(parents=True, exist_ok=True)
        st = model.stat()
        # 缓存键 = 路径+尺寸+mtime（不自算内容哈希 —— 哈希是 artifact 的活，
        # 架构判据 `test_hashing_lives_in_the_allowlist` 会拦）
        digest = signature(str(model), st.st_size, st.st_mtime_ns)
        cached = cache_dir / f"yunet_{digest}.onnx"
        if not cached.is_file():
            shutil.copyfile(model, cached)
        return cached
    except Exception:  # noqa: BLE001
        return model


def _load_face_counter(model_path: Path | None = None) -> Callable[[Any], int] | None:
    """尽力加载一个人脸计数器；加载不出来返回 None（巡检降级，不阻断）。

    失败**原因**请用 `face_model_status()` 取 —— 这里只负责"能不能用"。
    """
    detector, _why = _make_face_detector(model_path)
    if detector is None:
        return None

    def count(img_bgr: Any) -> int:
        try:
            h, w = img_bgr.shape[:2]
            detector.setInputSize((w, h))
            _, faces = detector.detect(img_bgr)
            return 0 if faces is None else len(faces)
        except Exception:  # noqa: BLE001
            return -1

    return count


_FACE_COUNTER: Callable[[Any], int] | None | bool = False


def configure_face_model(model_path: Path | None, *, enabled: bool = True) -> None:
    """由 CLI 在开跑前调一次：指定权重位置，或整体关掉人脸检测。

    `enabled=False` 必须**显式把哨兵置成 None**。否则 `_FACE_COUNTER` 停在
    `False`（"还没找过"的哨兵），第一次 `measure()` 又惰性去加载 —— 配置里
    写的 `enable_faces = false` 就成了摆设（实测过这个坑）。
    """
    global _FACE_COUNTER
    _FACE_COUNTER = _load_face_counter(model_path) if enabled else None


def face_model_status(model_path: Path | None = None) -> tuple[bool, str]:
    """人脸检测器**能不能加载**。返回 `(可用, 原因或路径)`。

    ★ 抽成公开函数是为了让 `doctor` 与 `qc` **共用一份判据** ——
    否则"体检说没问题、巡检却静默跳过"这种自相矛盾必然会再次出现。

    为什么需要它：`_load_face_counter` 把失败原因**吞掉**了（只返回 None）。
    "静默降级"是本项目最痛恨的一类故障 —— 报告看起来正常，检查其实没跑。

    实现上只是 `_make_face_detector` 的布尔包装 —— **不要在别处再判一遍**。
    """
    detector, why = _make_face_detector(model_path)
    return (detector is not None), why


def face_counter() -> Callable[[Any], int] | None:
    """惰性单例：第一次调用才去找模型，找不到就记住"没有"。"""
    global _FACE_COUNTER
    if _FACE_COUNTER is False:
        _FACE_COUNTER = _load_face_counter()
    return _FACE_COUNTER  # type: ignore[return-value]


# ---- 单图检查 --------------------------------------------------------------


def measure(path: Path, shot_id: int = 0) -> ImageStats:
    """读一张图的客观指标。不解码/文件缺失 → `ok=False`，不抛。"""
    stats = ImageStats(shot_id=shot_id, path=str(path))
    try:
        stats.bytes = path.stat().st_size
    except OSError:
        stats.ok = False
        return stats
    if stats.bytes == 0:
        stats.ok = False
        return stats
    if not _HAS_PIL:
        return stats
    try:
        with Image.open(path) as img:  # type: ignore[union-attr]
            img = img.convert("RGB")
            stats.width, stats.height = img.size
            stats.chroma = mean_chroma(img)
            counter = face_counter()
            if counter is not None:
                import numpy as np  # type: ignore

                rgb = np.asarray(img)
                bgr = rgb[:, :, ::-1].copy()
                stats.faces = counter(bgr)
    except Exception:  # noqa: BLE001 - 坏图不该让整块巡检崩
        stats.ok = False
    return stats


def mean_chroma(img: Any) -> float:
    """平均彩度：逐像素 `max(R,G,B) - min(R,G,B)` 的均值。

    灰度图必然接近 0；整张偏彩（Z-Image 偶发）会明显抬起来。阈值经验值 5。
    """
    try:
        small = img.resize((128, 128))
        px = list(small.getdata())
    except Exception:  # noqa: BLE001
        return -1.0
    if not px:
        return -1.0
    total = 0
    for r, g, b in px:
        total += max(r, g, b) - min(r, g, b)
    return total / len(px)


def inspect_image(
    path: Path,
    shot_id: int,
    *,
    expect_faces: int | None = None,
    expected_size: tuple[int, int] | None = None,
    chroma_max: float = DEFAULT_CHROMA_MAX,
    fix: bool = False,
    monochrome: bool = True,
) -> tuple[ImageStats, list[Issue]]:
    """检查一张图。`fix=True` 时对彩度异常**就地转灰度**（原图移入 `_fixed/`）。

    `monochrome=False` 时**跳过彩度检查**。「必须纯黑白」是**风格属性**而不是流水线常数：
    换成油画 / 水彩 / 赛博题材之后，彩度超标正是**对的**。
    判据来自本任务的 `[shots].style` 对应预设的 `monochrome` 标记（见 `lvs/styles.py`）。
    """
    issues: list[Issue] = []
    stats = measure(path, shot_id)

    if not path.is_file():
        issues.append(Issue(LEVEL_ERROR, "Q001", shot_id, "产物文件不存在", str(path),
                            "该镜要重出：lvs assets --task <任务名>"))
        return stats, issues
    if stats.bytes == 0:
        issues.append(Issue(LEVEL_ERROR, "Q002", shot_id, "产物是 0 字节空文件", str(path),
                            "中断留下的残片，删掉重跑"))
        return stats, issues
    if not stats.ok:
        issues.append(Issue(LEVEL_ERROR, "Q003", shot_id, "图片无法解码（可能被截断）", str(path),
                            "删掉重跑该镜"))
        return stats, issues

    if expected_size and (stats.width, stats.height) != expected_size:
        issues.append(Issue(LEVEL_WARN, "Q004", shot_id,
                            f"尺寸 {stats.width}×{stats.height}，期望 {expected_size[0]}×{expected_size[1]}",
                            str(path), "检查 config 的 [comfyui].width/height"))

    if monochrome and stats.chroma > chroma_max:
        if fix:
            moved = to_grayscale(path)
            if moved is not None:
                issues.append(Issue(LEVEL_WARN, "Q005", shot_id,
                                    f"彩度 {stats.chroma:.2f} 超标，已自动转灰度（原图留 {FIXED_DIRNAME}/）",
                                    str(path), ""))
                stats.chroma = 0.0
            else:
                issues.append(Issue(LEVEL_ERROR, "Q005", shot_id,
                                    f"彩度 {stats.chroma:.2f} 超标，且自动修正失败", str(path),
                                    "手工处理：PIL 转 L 再转 RGB"))
        else:
            issues.append(Issue(LEVEL_WARN, "Q005", shot_id,
                                f"彩度 {stats.chroma:.2f} 超标（应为黑白）", str(path),
                                "加 --fix 自动转灰度"))

    if expect_faces is not None and stats.faces >= 0:
        if expect_faces == 0 and stats.faces > 0:
            issues.append(Issue(LEVEL_WARN, "Q006", shot_id,
                                f"空镜里检出 {stats.faces} 张脸（Z-Image 最顽固的失败模式）",
                                str(path),
                                "换 seed 通常救不回来；改提示词：别用 blank/empty，改用有纹理的实体"))
        elif expect_faces > 0 and stats.faces == 0:
            issues.append(Issue(LEVEL_WARN, "Q007", shot_id,
                                f"人物镜未检出人脸（期望约 {expect_faces} 张）", str(path),
                                "检查提示词里有没有性别词与个体差异锁定句"))

    return stats, issues


def to_grayscale(path: Path) -> Path | None:
    """就地转灰度；原图移到同目录 `_fixed/<名>.orig.png`。返回原图新路径。"""
    if not _HAS_PIL:
        return None
    try:
        fixed_dir = path.parent / FIXED_DIRNAME
        fixed_dir.mkdir(parents=True, exist_ok=True)
        backup = fixed_dir / f"{path.stem}.orig{path.suffix}"
        if not backup.exists():
            shutil.copy2(path, backup)
        with Image.open(path) as img:  # type: ignore[union-attr]
            gray = img.convert("L").convert("RGB")
            gray.save(path)
        return backup
    except Exception:  # noqa: BLE001
        return None


# ---- 分块巡检 --------------------------------------------------------------


def expected_faces_for(shot: dict[str, Any], lock=None) -> int | None:  # noqa: ANN001
    """这一镜"应该有几张脸"。拿不准返回 None（= 不检查这条）。

    判据顺序（**先看来源，再看槽位**）：
    - `graphic` / `pexels` / `library` → None。卡片是排版的、实拍是真实素材，
      「该有几张脸」本就说不准，硬判只会制造噪声。
    - `local`（本地生图）→ 按拍摄稿里 `{NAME}` 槽位去重计数；
      没有槽位就 None（空镜长人这条判不出来，别假装能判）。
    """
    if str(shot.get("source") or "") != "local":
        return None
    if str(shot.get("resolved_by") or "") not in ("", "local"):
        return None

    from lvs import cast as cast_mod

    blob = f"{shot.get('visual') or ''} {shot.get('prompt') or ''} {shot.get('scene') or ''}"
    slots = cast_mod.SLOT.findall(blob)
    if slots:
        # 多人同框按去重后的人数。注意「背影/侧影」构图脸可能不可见，
        # 所以这条只作**告警**用，绝不参与硬判定。
        return len(set(slots))
    return None


def _monochrome_for(config, shots: list[dict[str, Any]]) -> bool:  # noqa: ANN001
    """本任务是否要求纯黑白。

    判据链：镜上记的 `style`（最准，逐镜记录）→ config `[shots].style` → 默认。
    **读不到风格时按"不要求"处理** —— 宁可少报一条彩度告警，
    也不要因为风格表坏了就拦下整批图（那是"因为工具坏了而挡住生产"）。
    """
    try:
        from lvs.styles import build_registry

        reg = build_registry(config)
        name = next((s.get("style") for s in shots if s.get("style")), None)
        if not name:
            name = config.get("shots.style")
        return reg.monochrome(name)
    except Exception:  # noqa: BLE001 - 风格取不到不该阻断巡检
        return False


def inspect_block(
    shots: list[dict[str, Any]],
    block_index: int,
    *,
    lock=None,  # noqa: ANN001
    chroma_max: float = DEFAULT_CHROMA_MAX,
    expected_size: tuple[int, int] | None = None,
    fix: bool = False,
    monochrome: bool = True,
) -> BlockReport:
    """巡检一块（一批镜）。`shots` 应已带 `asset_path`。"""
    report = BlockReport(
        block=block_index,
        first=int(shots[0]["id"]) if shots else 0,
        last=int(shots[-1]["id"]) if shots else 0,
    )
    for shot in shots:
        sid = int(shot.get("id") or 0)
        # 只巡检**本地生图**的产物：卡片是排版出来的、pexels 是实拍，彩度判据不适用
        if str(shot.get("resolved_by") or "") != "local":
            continue
        raw = shot.get("asset_path")
        if not raw:
            report.issues.append(Issue(LEVEL_WARN, "Q010", sid, "本镜标为成功但没有 asset_path", "",
                                       "状态不一致，重跑该镜"))
            continue
        stats, issues = inspect_image(
            Path(raw), sid,
            expect_faces=expected_faces_for(shot, lock),
            expected_size=expected_size,
            chroma_max=chroma_max,
            fix=fix,
            monochrome=monochrome,
        )
        report.stats.append(stats)
        report.issues.extend(issues)
        if any(i.code == "Q005" and "已自动转灰度" in i.message for i in issues):
            report.fixed.append(str(raw))
    return report


def check_prompt_versions(shots: list[dict[str, Any]]) -> dict[str, int]:
    """抽检 `prompt` 字段，统计"提示词版本指纹"分布。

    这是抓"换了提示词却没重出图"的唯一手段 —— 只看文件夹名一定看不出来（血泪）。
    """
    versions: dict[str, int] = {}
    for shot in shots:
        prompt = str(shot.get("prompt") or "")
        if not prompt:
            continue
        digest = _prompt_fingerprint(prompt)
        versions[digest] = versions.get(digest, 0) + 1
    return versions


def _prompt_fingerprint(prompt: str) -> str:
    """提示词的粗指纹：取前 24 个词 + 总长。不引入哈希依赖，好读好对。"""
    words = prompt.split()
    head = " ".join(words[:24])
    return f"{len(prompt)}c|{head}"


# ---- 汇总与落盘 ------------------------------------------------------------


def write_reports(ws, reports: list[BlockReport]) -> Path:  # noqa: ANN001
    """把每块结果写成 json + 一份人读的 summary.md。"""
    qc_dir = ws.path("qc")
    qc_dir.mkdir(parents=True, exist_ok=True)

    for rep in reports:
        (qc_dir / f"qc-block-{rep.block:03d}.json").write_text(
            json.dumps(rep.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    all_issues = [i for rep in reports for i in rep.issues]
    errors = [i for i in all_issues if i.level == LEVEL_ERROR]
    warns = [i for i in all_issues if i.level == LEVEL_WARN]
    fixed = [p for rep in reports for p in rep.fixed]
    checked = sum(len(rep.stats) for rep in reports)

    lines = [
        f"# 生图巡检 · {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"- 检查 {checked} 张｜error {len(errors)}｜warn {len(warns)}｜自动转灰度 {len(fixed)}",
        "",
    ]
    if fixed:
        lines += ["## 已自动转灰度（原图在 `_fixed/`）", ""]
        lines += [f"- `{p}`" for p in fixed]
        lines.append("")

    def section(title: str, items: list[Issue]) -> None:
        lines.append(f"## {title}（{len(items)}）")
        lines.append("")
        if not items:
            lines.append("- 无")
        else:
            redo = sorted({i.shot_id for i in items})
            lines.append(f"- 待重出镜号：{', '.join(str(s) for s in redo)}")
            lines.append("")
            for i in items[:60]:
                lines.append(i.render())
        lines.append("")

    section("硬错误", errors)
    section("告警", warns)

    # 提示词版本
    versions: dict[str, int] = {}
    for rep in reports:
        for k, v in rep.prompt_versions.items():
            versions[k] = versions.get(k, 0) + v
    if len(versions) > 1:
        lines += [
            "## ⚠ 提示词版本不一致",
            "",
            f"检出 {len(versions)} 种提示词指纹 —— 说明镜表里有不同批次的提示词混在一起，",
            "典型原因是**换了提示词却没重出图**（旧图被当成已有产物跳过）。",
            "",
        ]
        for k, v in sorted(versions.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {v} 镜：`{k[:100]}`")
        lines.append("")

    summary = qc_dir / "summary.md"
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")

    redo = sorted({i.shot_id for i in errors} | {i.shot_id for i in warns if i.code in {"Q006", "Q007"}})
    (qc_dir / "redo-queue.json").write_text(
        json.dumps({"shots": redo, "at": datetime.now().isoformat(timespec="seconds")},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


# ---- 命令入口 --------------------------------------------------------------


def run_command(config, ws, args) -> int:  # noqa: ANN001 - 由 cli 传入
    enabled = bool(config.get("qc.enable_faces", True))
    face_model = config.get("qc.face_model")
    configure_face_model(Path(str(face_model)) if face_model else None, enabled=enabled)

    # ★ 人脸检测不可用时**必须说出来**（原先它静默跳过 Q006/Q007）。
    #
    # 实测踩到：`[qc].face_model` 指着一个**权重退化的** onnx（结构是 YuNet，
    # 但 `obj` 输出恒为 0、`cls` 对"纯黑图"和"人像照"给出**几乎相同**的值）。
    # 于是 Q006「空镜长人」—— 本项目的注释里写着这是"Z-Image 最顽固的失败模式" ——
    # 一次都没真正跑过，而报告看起来一切正常。
    #
    # 这正是本项目最痛恨的一类故障：**静默降级**。宁可多说一句，也不要让人以为查过了。
    if enabled and face_counter() is None:
        _ok, _why = face_model_status(
            Path(str(face_model)).expanduser() if face_model else None
        )
        print(
            "[巡检] ⚠ 人脸检测**不可用** —— Q006（空镜里长人）与 Q007（人物镜没脸）"
            "本次**没有检查**。\n"
            f"  原因：{_why}\n"
            "  影响：这两条恰是「空镜长人」「单人镜溢出」唯一的自动判据，"
            "漏掉它们等于这一块**全靠人眼**。\n"
            "  想修：放一个可用的 YuNet onnx 到 `models/yunet.onnx`"
            "，或把 `[qc].face_model` 指过去；"
            "确不需要就设 `[qc].enable_faces = false`（这样它不再提示）。"
        )

    try:
        data = ws.load_shots(error=QcError)
    except QcError as exc:
        print(str(exc))
        return 2

    # 阶段间契约：巡检结果会直接决定 G3 门禁放不放行，坏数据会让它放行错的产物。
    # 尤其 `id` 缺失会变成 0、重复 `id` 会让逐镜对位错乱（素材/期望人数全跟着错）。
    shots_all = data.get("shots") or []
    msg = handoff.check_shots_or_message(
        shots_all, stage="生图巡检（qc）", consumer="qc",
        command=f"lvs shots --task {getattr(ws, 'task', '')}",
    )
    if msg:
        print(msg)
        return 2

    shots = [s for s in shots_all if s.get("status") == "done" or s.get("asset_path")]
    if not shots:
        print("没有已成功的分镜可巡检。先跑 `lvs assets`。")
        return 2

    only = getattr(args, "only", None)
    if only:
        wanted = _parse_ids(str(only))
        shots = [s for s in shots if int(s.get("id") or 0) in wanted]

    block_size = int(getattr(args, "block", None) or config.get("qc.block", DEFAULT_BLOCK) or DEFAULT_BLOCK)
    chroma_max = float(config.get("qc.chroma_max", DEFAULT_CHROMA_MAX))
    fix = bool(getattr(args, "fix", False)) or bool(config.get("qc.fix_chroma", False))
    mono = _monochrome_for(config, shots)
    if not mono:
        print("  风格不要求单色 → 跳过彩度检查（彩度超标在彩色题材里是正常的）")

    exp_size = None
    if config.has("comfyui.width") and config.has("comfyui.height"):
        exp_size = (int(config.get("comfyui.width")), int(config.get("comfyui.height")))

    lock = None
    try:
        from lvs import cast as cast_mod

        lock = cast_mod.load_lock(cast_mod.cast_dir(config))
    except Exception:  # noqa: BLE001 - 定妆库缺失不影响巡检
        lock = None

    reports: list[BlockReport] = []
    for idx, start in enumerate(range(0, len(shots), block_size), start=1):
        chunk = shots[start:start + block_size]
        rep = inspect_block(
            chunk, idx, lock=lock, chroma_max=chroma_max,
            expected_size=exp_size, fix=fix, monochrome=mono,
        )
        rep.prompt_versions = check_prompt_versions(chunk)
        reports.append(rep)
        errs = len(rep.errors)
        print(
            f"  块 {idx}：{rep.first:03d}–{rep.last:03d} 检查 {len(rep.stats)} 张，"
            f"error {errs}，warn {len(rep.issues) - errs}"
            + (f"，转灰度 {len(rep.fixed)}" if rep.fixed else "")
        )

    summary = write_reports(ws, reports)
    total_err = sum(len(r.errors) for r in reports)
    total_warn = sum(len(r.issues) for r in reports) - total_err
    print(f"\n巡检完成：检查 {sum(len(r.stats) for r in reports)} 张，error {total_err}，warn {total_warn}")
    print(f"  报告：{summary}")
    print(f"  待重出：{ws.path('qc', 'redo-queue.json')}")

    # 缩略图拼版（--sheet）：给多模态模型"一眼看一批"，点名坏镜号。
    # 分色条 = 本次巡检结论（红 error / 黄 warn / 绿通过 / 灰缺图）——
    # 模型只要报镜号，不用复述问题描述，省大量 token。
    if getattr(args, "sheet", False):
        levels: dict[int, str] = {}
        for rep in reports:
            for i in rep.issues:
                if i.level == LEVEL_ERROR:
                    levels[i.shot_id] = LEVEL_ERROR    # error 盖过 warn
                else:
                    levels.setdefault(i.shot_id, LEVEL_WARN)
        items = [(int(s.get("id") or 0), s.get("asset_path")) for s in shots]
        sheets = write_contact_sheets(ws, items, levels)
        if sheets:
            print(f"  拼版：{len(sheets)} 页 → {sheets[0].parent}")
            for p in sheets:
                print(f"    {p.name}")

    return 1 if total_err else 0


def _parse_ids(spec: str) -> set[int]:
    """`1-10,15` → `{1..10, 15}`。"""
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a.strip().isdigit() and b.strip().isdigit():
                out.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            out.add(int(part))
    return out


# ---- 缩略图拼版（contact sheet，给多模态模型"一眼看一批"）--------------------
#
# 为什么要有它：逐张把原图喂给多模态模型太贵（一张 1344×768 动辄上千 token，
# 479 张就是几十万）。而巡检要回答的问题往往只是"这批里哪几张坏"——
# 把一批图缩成**一张带镜号的拼版**，模型看一张图、点名坏镜号，
# 就能只重跑那几张。省 token 的关键在：缩略图 + 镜号标注 + 分色状态条。
#
# 状态条配色沿用巡检三态：绿=通过，黄=告警，红=硬错误，灰=缺图。

SHEET_COLS = 6                     # 每行几格
SHEET_ROWS = 5                     # 每页几行 → 30 张/页（对齐"每 30 张巡一次"）
SHEET_CELL_W = 320                 # 格宽（缩略图等宽）
SHEET_THUMB_H = 180                # 缩略图高（16:9）
SHEET_LABEL_H = 26                 # 镜号条高
SHEET_PAD = 6                      # 格间距
SHEET_BG = (24, 26, 30)
COLOR_OK = (46, 160, 67)
COLOR_WARN = (210, 160, 30)
COLOR_ERR = (220, 60, 60)
COLOR_MISSING = (120, 120, 120)


def sheet_page_count(n: int, cols: int = SHEET_COLS, rows: int = SHEET_ROWS) -> int:
    """n 张图要几页拼版（纯函数，便于单测）。"""
    per = max(1, cols) * max(1, rows)
    return (max(0, n) + per - 1) // per


def _sheet_font(size: int = 15):  # noqa: ANN202
    """镜号条用的字体：优先 truetype（清晰），退回默认位图字体。"""
    try:
        from PIL import ImageFont  # type: ignore

        for name in ("arial.ttf", "DejaVuSans.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except Exception:  # noqa: BLE001
                continue
        return ImageFont.load_default()
    except Exception:  # noqa: BLE001
        return None


def write_contact_sheets(
    ws,  # noqa: ANN001
    items: list[tuple[int, str | None]],
    levels: dict[int, str] | None = None,
    *,
    cols: int = SHEET_COLS,
    rows: int = SHEET_ROWS,
) -> list[Path]:
    """把一批镜图拼成分页缩略图，返回生成的 PNG 路径列表。

    - `items`：`(镜号, 图路径或 None)`，按镜号排序后分页；
    - `levels`：`镜号 → 最严重问题级别`（error > warn），决定状态条颜色；
    - 没装 PIL → 打印原因返回 `[]`（降级不阻断，与巡检其它能力一致）；
    - 图读不出来 → 画"缺图"灰格，**不让一张坏图毁掉整版**。

    产物落在 `.work/<任务>/qc/contact-sheet-NN.png`。
    """
    if not _HAS_PIL:
        print("  ⚠ 没装 Pillow，跳过缩略图拼版（`pip install pillow`）")
        return []
    if not items:
        return []

    from PIL import Image as PILImage  # type: ignore
    from PIL import ImageDraw  # type: ignore

    levels = levels or {}
    ordered = sorted(items, key=lambda t: t[0])
    per = max(1, cols) * max(1, rows)
    font = _sheet_font()

    cell_w, thumb_h, label_h, pad = cell_metrics()
    grid_w = pad + cols * (cell_w + pad)
    grid_h = pad + rows * (thumb_h + label_h + pad)

    qc_dir = ws.path("qc")
    qc_dir.mkdir(parents=True, exist_ok=True)
    out_paths: list[Path] = []

    for page in range(sheet_page_count(len(ordered), cols, rows)):
        chunk = ordered[page * per:(page + 1) * per]
        canvas = PILImage.new("RGB", (grid_w, grid_h), SHEET_BG)
        draw = ImageDraw.Draw(canvas)

        for idx, (sid, raw) in enumerate(chunk):
            cx = pad + (idx % cols) * (cell_w + pad)
            cy = pad + (idx // cols) * (thumb_h + label_h + pad)
            # ---- 缩略图 ----
            thumb = None
            if raw and Path(raw).is_file():
                try:
                    with PILImage.open(raw) as im:  # type: ignore[union-attr]
                        thumb = im.convert("RGB")
                        thumb.thumbnail((cell_w, thumb_h))
                except Exception:  # noqa: BLE001 - 坏图当缺图处理
                    thumb = None
            if thumb is not None:
                ox = cx + (cell_w - thumb.width) // 2
                canvas.paste(thumb, (ox, cy))
            else:
                draw.rectangle([cx, cy, cx + cell_w - 1, cy + thumb_h - 1],
                               fill=(40, 42, 48))
                draw.text((cx + cell_w // 2 - 30, cy + thumb_h // 2 - 8),
                          "缺图/坏图", fill=(180, 180, 180), font=font)
            # ---- 镜号 + 状态条 ----
            ly = cy + thumb_h
            lvl = levels.get(sid, "")
            bar = (COLOR_ERR if lvl == LEVEL_ERROR
                   else COLOR_WARN if lvl == LEVEL_WARN
                   else COLOR_MISSING if thumb is None else COLOR_OK)
            draw.rectangle([cx, ly, cx + cell_w - 1, cy + thumb_h + label_h - 1],
                           fill=bar)
            draw.text((cx + 8, ly + 5), f"{sid:03d}", fill=(255, 255, 255), font=font)

        out = qc_dir / f"contact-sheet-{page + 1:02d}.png"
        canvas.save(out)
        out_paths.append(out)

    return out_paths


def cell_metrics() -> tuple[int, int, int, int]:
    """拼版格子的尺寸（纯函数）：(格宽, 缩略图高, 标签高, 间距)。"""
    return SHEET_CELL_W, SHEET_THUMB_H, SHEET_LABEL_H, SHEET_PAD
