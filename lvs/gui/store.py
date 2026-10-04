"""GUI 的**只读数据层**（票据 36）。

界面不发明状态：一切来自 `.work/<task>/manifest.json`、`shots.json` 与**磁盘产物**
（D15「产物即状态」）。这里只做"读成界面要用的形状"，一个字节都不写。

一个必须记住的坑：`assets` / `voice` / `build` 这些阶段**只在跑完时才写 shots.json**，
所以运行中的进度**只能数盘上已落盘的逐镜产物**，读 shots.json 会一路卡在 0%（票 36 实测）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lvs import workspace as ws_mod
from lvs.config import PROJECT_ROOT

# ★ 阶段顺序与短名**不在这里定义** —— 唯一真源是 `lvs.stage`。
#
# 这一行原先是**第 7 份**阶段顺序副本（cli / gui-jobs / run / studio / pipeline /
# workspace+board 之外），而且**架构测试没扫到它** ——
# `tests/test_architecture.py` 的 `_modules()` 只 glob `lvs/*.py`，不含 `gui/` 子目录。
# 现在测试已扩展到子目录，这里也改成读真源。
from lvs.stage import BY_NAME as _BY_NAME, ORDER as STAGES

# 界面显示用的中文短名，从阶段表取（空则退回标题）
STAGE_LABELS = {name: (_BY_NAME[name].short or _BY_NAME[name].title) for name in STAGES}

# 逐镜产物的落点与后缀 —— 只有这些算进度。分支表来自 workspace（唯一 owner）
ASSET_BRANCHES = ws_mod.ASSET_BRANCHES
ASSET_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".mp4", ".mov", ".webm", ".mkv")
AUDIO_SUFFIXES = (".mp3", ".wav", ".m4a", ".aac", ".opus", ".flac")

# 界面上允许改的分镜字段（写回 shots.json —— 它是"人工可编辑的真相源"，D14）
EDITABLE_SHOT_FIELDS = ("prompt", "seed", "source", "narration")


@dataclass(frozen=True)
class Stage:
    """一个阶段的状态：`status` 来自 manifest（真相），`done/total` 来自盘上产物（实时）。"""

    name: str
    status: str = "pending"      # done / partial / skipped / pending
    at: str = ""
    note: str = ""
    done: int = 0
    total: int = 0

    @property
    def label(self) -> str:
        return STAGE_LABELS.get(self.name, self.name)

    @property
    def finished(self) -> bool:
        return self.status in ("done", "skipped")


@dataclass(frozen=True)
class Task:
    name: str
    dir: Path
    created_at: str = ""
    stages: tuple[Stage, ...] = ()
    shots_total: int = 0
    source_mode: str = "auto"
    has_final: bool = False
    final_duration: float | None = None
    updated: float = 0.0

    def stage(self, name: str) -> Stage | None:
        for st in self.stages:
            if st.name == name:
                return st
        return None

    @property
    def done_stages(self) -> int:
        return sum(1 for st in self.stages if st.finished)


# ---- 路径 ------------------------------------------------------------------


def work_root(root: Path = PROJECT_ROOT) -> Path:
    return Path(root) / ".work"


def task_dir(name: str, root: Path = PROJECT_ROOT) -> Path | None:
    """把任务名解析成 `.work/` 里的目录；**任何越界名字一律返回 None**。

    界面上的任务名来自 URL，所以必须钉死在 `.work` 之内（`../..`、绝对路径都不行）。
    """
    base = work_root(root).resolve()
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name:
        return None
    candidate = (base / name).resolve()
    if base not in candidate.parents:          # 必须真的是 .work 的子目录
        return None
    return candidate if candidate.is_dir() else None


# ---- 读取 ------------------------------------------------------------------


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _count_shots(directory: Path, suffixes: tuple[str, ...]) -> set[str]:
    """数出该目录里有哪些**逐镜**产物，返回 `shot-001` 这样的 stem 集合。"""
    if not directory.is_dir():
        return set()
    found: set[str] = set()
    try:
        for p in directory.iterdir():
            if p.is_file() and p.stem.startswith("shot-") and p.suffix.lower() in suffixes:
                found.add(p.stem)
    except OSError:
        return set()
    return found


def _assets_ready(ws_dir: Path) -> int:
    stems: set[str] = set()
    for branch in ASSET_BRANCHES:
        stems |= _count_shots(ws_dir / "assets" / branch, ASSET_SUFFIXES)
    return len(stems)


def read_shots(name: str, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """任务的 shots.json（缺失/坏掉 → 空 dict）。

    "宽容地读 shots.json"这套语义归 `Workspace.try_load_shots`（唯一 owner），
    这里只是把它按任务名取出来（票 42）。
    """
    return ws_mod.Workspace.read(name, root).try_load_shots()


def _build_stages(d: Path, manifest: dict[str, Any], shots_total: int) -> tuple[Stage, ...]:
    entries = manifest.get("stages") if isinstance(manifest.get("stages"), dict) else {}
    counts = {
        "parse": (1 if (d / "parse.json").is_file() else 0, 1),
        "shots": (1 if (d / "shots.json").is_file() else 0, 1),
        "assets": (_assets_ready(d), shots_total),
        "voice": (len(_count_shots(d / "audio", AUDIO_SUFFIXES)), shots_total),
        "build": (len(_count_shots(d / "segments", (".mp4",))), shots_total),
    }
    out: list[Stage] = []
    for stage in STAGES:
        entry = entries.get(stage) if isinstance(entries, dict) else None
        entry = entry if isinstance(entry, dict) else {}
        status = entry.get("status") or "pending"
        done, total = counts.get(stage, (0, 0))
        # 别在 total 未知时拿 done 去凑 —— 那会让 5/5 在跑到一半时就显示 100%。
        # 总数未知就老实报 0，由界面显示「—」（见 static/task.js 的 paintPipeline）。
        out.append(Stage(
            name=stage,
            status=str(status),
            at=str(entry.get("at") or ""),
            note=str(entry.get("note") or ""),
            done=done,
            total=total,
        ))
    return tuple(out)


def read_task(name: str, root: Path = PROJECT_ROOT) -> Task | None:
    d = task_dir(name, root)
    if d is None:
        return None

    manifest = _load_json(d / "manifest.json", {})
    manifest = manifest if isinstance(manifest, dict) else {}
    shots = read_shots(name, root)
    shots_list = shots.get("shots") if isinstance(shots.get("shots"), list) else []
    shots_total = len(shots_list) or int(shots.get("count") or 0)

    entries = manifest.get("stages") if isinstance(manifest.get("stages"), dict) else {}
    build_entry = entries.get("build") if isinstance(entries, dict) else None
    build_entry = build_entry if isinstance(build_entry, dict) else {}
    duration = build_entry.get("duration")
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None

    final = d / "final.mp4"
    try:
        updated = (d / "manifest.json").stat().st_mtime
    except OSError:
        updated = 0.0

    return Task(
        name=name,
        dir=d,
        source_mode=str(shots.get("source_mode") or "auto"),
        created_at=str(manifest.get("created_at") or ""),
        stages=_build_stages(d, manifest, shots_total),
        shots_total=shots_total,
        has_final=final.is_file() and final.stat().st_size > 0,
        final_duration=duration,
        updated=updated,
    )


def list_tasks(root: Path = PROJECT_ROOT) -> list[Task]:
    """列出所有任务。

    判据是「有 manifest.json **或** manuscript.md」—— 刚建、还没跑 parse 的任务
    只有拍摄稿没有 manifest，也得在列表里出现（否则用户建完任务、切回首页就找不到了）。
    `.work/_fixtures` 这类临时目录两者都没有，自然被排除。
    """
    base = work_root(root)
    if not base.is_dir():
        return []
    found: list[Task] = []
    try:
        for child in base.iterdir():
            if not child.is_dir():
                continue
            if not (child / "manifest.json").is_file() and not (child / "manuscript.md").is_file():
                continue
            task = read_task(child.name, root)
            if task is not None:
                found.append(task)
    except OSError:
        return []
    found.sort(key=lambda t: t.updated, reverse=True)
    return found
