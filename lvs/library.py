"""本地素材库：扫描、索引、检索（票据 18 的索引/检索部分）。

用户可以把**已经下好或生成好**的素材放在一个（或多个）文件夹里，用
`config.toml` 的 `[library].dirs` 指定。本模块负责：

1. `scan()`    —— 扫描目录，收集图片/视频条目（含尺寸/时长/tags）
2. `build_index()` —— 生成并缓存 `library-index.json`
3. `search()`  —— 按 shot 的关键词给条目打加权重合度分，取最高者

设计约束（票据 18）：
- **绝不修改**素材库里的原文件（只读扫描；命中时由调用方复制/软链）
- 未配置素材库（`dirs` 为空或目录不存在）→ 静默跳过，流程不受影响
- ffprobe/Pillow 缺失时**降级**为"仅文件名标签"，不报错
- 索引缓存在 `.work/_library/`，跨任务复用；`--reindex` 强制重建
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from lvs import artifact
from lvs import media
from lvs.config import PROJECT_ROOT

# 图片/视频扩展名白名单 —— 真源在 `lvs.media`（缺陷 B02/Q02）。
# 这两个名字是本模块对外的历史 API，保留为**同一份 frozenset 的别名**
# （不是第二份字面量）；要加格式只改 `lvs/media.py`。
IMAGE_EXTS = media.IMAGE_EXTS
VIDEO_EXTS = media.VIDEO_EXTS

CACHE_DIRNAME = Path(".work") / "_library"
INDEX_NAME = "library-index.json"

_TOKEN_SPLIT = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
_CJK = re.compile(r"[\u4e00-\u9fff]")


# ---- 分词与标签 ------------------------------------------------------------


def tokens(text: str) -> list[str]:
    """把一段文本切成小写词元，保留中文与长度 ≥2 的英文/数字。"""
    out: list[str] = []
    for raw in _TOKEN_SPLIT.split(text or ""):
        if not raw:
            continue
        low = raw.lower()
        if len(low) >= 2 or _CJK.search(low):
            out.append(low)
    return out


def _load_sidecar_tags(path: Path) -> list[str]:
    """同名 sidecar：`x.jpg` ← `x.json`（取 `tags`）或 `x.txt`（逐行）。"""
    for suffix in (".json", ".txt"):
        side = path.with_suffix(suffix)
        if not side.is_file():
            continue
        try:
            if suffix == ".json":
                data = json.loads(side.read_text(encoding="utf-8"))
                tags = data.get("tags") if isinstance(data, dict) else data
                if isinstance(tags, list):
                    return [str(t) for t in tags]
            else:
                return [ln.strip() for ln in side.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
    return []


def _entry_tags(path: Path, root: Path) -> list[str]:
    tags: list[str] = []
    tags += tokens(path.stem)                      # 文件名
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    for part in rel.parts[:-1]:                    # 所在文件夹名
        tags += tokens(part)
    tags += tokens(root.name)                      # 根目录名
    tags += [t.lower() for t in _load_sidecar_tags(path)]
    # 去重保序
    seen: set[str] = set()
    uniq: list[str] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


# ---- 维度探测（可降级） ----------------------------------------------------


def _probe_image(path: Path) -> tuple[int | None, int | None]:
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:
        return None, None


def _probe_video(path: Path) -> tuple[int | None, int | None, float | None]:
    if not shutil.which("ffprobe"):
        return None, None, None
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            return None, None, None
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or [{}]
        stream = streams[0]
        fmt = data.get("format") or {}
        duration = float(fmt["duration"]) if fmt.get("duration") else None
        return (
            int(stream["width"]) if stream.get("width") else None,
            int(stream["height"]) if stream.get("height") else None,
            duration,
        )
    except Exception:
        return None, None, None


# ---- 数据模型 --------------------------------------------------------------


@dataclass
class Entry:
    path: str
    type: str                       # "image" | "video"
    tags: list[str] = field(default_factory=list)
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    size: int = 0
    mtime: float = 0.0


def scan(dirs: Iterable[str | Path], recursive: bool = True) -> list[Entry]:
    """扫描素材库目录，返回条目列表。目录不存在则跳过（不报错）。"""
    entries: list[Entry] = []
    for raw in dirs:
        root = Path(raw).expanduser()
        if not root.is_dir():
            continue
        walker = root.rglob("*") if recursive else root.glob("*")
        for path in walker:
            if not path.is_file():
                continue
            if media.is_image(path):
                kind = "image"
            elif media.is_video(path):
                kind = "video"
            else:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            entry = Entry(
                path=str(path),
                type=kind,
                tags=_entry_tags(path, root),
                size=stat.st_size,
                mtime=stat.st_mtime,
            )
            if kind == "image":
                entry.width, entry.height = _probe_image(path)
            else:
                entry.width, entry.height, entry.duration = _probe_video(path)
            entries.append(entry)
    entries.sort(key=lambda e: (e.type, e.path))
    return entries


# ---- 索引与缓存 ------------------------------------------------------------


def cache_path(root: Path = PROJECT_ROOT) -> Path:
    return root / CACHE_DIRNAME / INDEX_NAME


def _signature(dirs: list[str], recursive: bool) -> str:
    """廉价的失效签名：目录列表 + 递归开关 + 各根目录自身 mtime。"""
    payload = [str(Path(d).expanduser()) for d in dirs] + [str(recursive)]
    for d in dirs:
        p = Path(d).expanduser()
        try:
            payload.append(f"{p}:{p.stat().st_mtime_ns}")
        except OSError:
            payload.append(f"{p}:missing")
    return artifact.signature(*payload, length=0)


def load_index(root: Path = PROJECT_ROOT) -> dict[str, Any] | None:
    path = cache_path(root)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def build_index(
    dirs: list[str],
    recursive: bool = True,
    root: Path = PROJECT_ROOT,
    reindex: bool = False,
) -> dict[str, Any]:
    """生成索引；若缓存签名一致且未要求重建，直接复用。"""
    signature = _signature(dirs, recursive)
    if not reindex:
        cached = load_index(root)
        if cached and cached.get("signature") == signature:
            cached["from_cache"] = True
            return cached

    entries = scan(dirs, recursive=recursive)
    index = {
        "root": [str(Path(d).expanduser()) for d in dirs],
        "recursive": recursive,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "signature": signature,
        "count": len(entries),
        "from_cache": False,
        "entries": [asdict(e) for e in entries],
    }
    path = cache_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return index


# ---- 检索 ------------------------------------------------------------------


def score_entry(entry: dict[str, Any] | Entry, keywords: Iterable[str]) -> int:
    """加权重合度：词元交集 ×2 + 关键词在路径中的子串命中 ×1。"""
    tags = set(entry.tags if isinstance(entry, Entry) else entry.get("tags", []))
    path = (entry.path if isinstance(entry, Entry) else entry.get("path", "")).lower()

    kw_tokens: set[str] = set()
    substrings: list[str] = []
    for kw in keywords:
        kw = (kw or "").strip()
        if not kw:
            continue
        kw_tokens.update(tokens(kw))
        low = kw.lower()
        if len(low) >= 2:
            substrings.append(low)

    score = 2 * len(kw_tokens & tags)
    score += sum(1 for s in substrings if s in path)
    return score


def search(
    index: dict[str, Any], keywords: Iterable[str], min_score: int = 1
) -> list[tuple[dict[str, Any], int]]:
    """返回按分数降序的 (entry, score)，仅含 score ≥ min_score。"""
    keywords = list(keywords)
    scored: list[tuple[dict[str, Any], int]] = []
    for entry in index.get("entries", []):
        s = score_entry(entry, keywords)
        if s >= min_score:
            scored.append((entry, s))
    scored.sort(key=lambda pair: (-pair[1], pair[0]["path"]))
    return scored


def best_match(
    index: dict[str, Any], keywords: Iterable[str], min_score: int = 1
) -> dict[str, Any] | None:
    hits = search(index, keywords, min_score=min_score)
    return hits[0][0] if hits else None


# ---- 命令入口 --------------------------------------------------------------


def run_command(config, ws, args) -> int:  # noqa: ANN001 - 由 cli 传入
    dirs = config.get("library.dirs", []) or []
    recursive = bool(config.get("library.recursive", True))
    min_score = int(config.get("library.min_score", 1))

    if not any(dirs):
        print("未配置本地素材库（config.toml 的 [library].dirs 为空）。")
        print("  该功能可后续指定文件夹；未配置时 `lvs assets` 会静默跳过翻库。")
        return 0

    missing = [d for d in dirs if not Path(d).expanduser().is_dir()]
    if missing:
        print("以下素材库目录不存在，已跳过：")
        for d in missing:
            print(f"  - {d}")

    index = build_index(dirs, recursive=recursive, root=ws.root, reindex=bool(args.reindex))
    src = "缓存" if index.get("from_cache") else "重新扫描"
    print(f"素材库索引（{src}）：{index['count']} 个文件")
    for d in index["root"]:
        print(f"  - {d}")
    print(f"  产物：{cache_path(ws.root)}")

    kinds: dict[str, int] = {}
    for e in index["entries"]:
        kinds[e["type"]] = kinds.get(e["type"], 0) + 1
    if kinds:
        print("  分布：" + "，".join(f"{k} {v}" for k, v in sorted(kinds.items())))

    # 演示检索：用一个配置里的示例词，方便用户直观确认打分是否合理
    demo = config.get("library.demo_query", None)
    if demo:
        hits = search(index, tokens(demo) or [demo], min_score=min_score)
        print(f"  检索示例 {demo!r}：命中 {len(hits)} 条")
        for entry, s in hits[:5]:
            print(f"    [{s:>3}] {entry['path']}")
    return 0
