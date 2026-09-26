"""任务目录（`.work/<task>/`）与产物清单（`manifest.json`）。

设计要点（见 spec §7.1）：**文件即状态**。每个阶段把自己的产物与完成情况写进
`manifest.json`，阶段入口先读它判断能否跳过 —— 这是"断点续跑"的基础。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from lvs.config import PROJECT_ROOT

WORK_DIRNAME = ".work"
MANIFEST_NAME = "manifest.json"

# 任务目录下的固定子目录（spec §6）
SUBDIRS = (
    "audio",
    "assets/library",
    "assets/pexels",
    "assets/local",
    "shots-motion",
    "logs",
)


def slugify(name: str) -> str:
    """把任意字符串变成安全的目录名：保留中英文与数字，其余转 `-`。"""
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "-", name.strip(), flags=re.UNICODE)
    cleaned = cleaned.strip("-")
    return cleaned or "default"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Workspace:
    """某一次运行的工作目录。"""

    task: str
    root: Path = PROJECT_ROOT
    manifest: dict[str, Any] = field(default_factory=dict)

    # ---- 路径 -------------------------------------------------------------

    @property
    def dir(self) -> Path:
        return self.root / WORK_DIRNAME / self.task

    @property
    def manifest_path(self) -> Path:
        return self.dir / MANIFEST_NAME

    def path(self, *parts: str) -> Path:
        return self.dir.joinpath(*parts)

    # ---- 生命周期 ---------------------------------------------------------

    def ensure(self) -> "Workspace":
        """创建任务目录骨架，并初始化 `manifest.json`（幂等）。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        for sub in SUBDIRS:
            (self.dir / sub).mkdir(parents=True, exist_ok=True)

        if self.manifest_path.is_file():
            self.manifest = self._read_manifest()
        else:
            self.manifest = {"task": self.task, "created_at": _now(), "stages": {}}
            self._write_manifest()
        return self

    def _read_manifest(self) -> dict[str, Any]:
        try:
            with open(self.manifest_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            # 清单损坏不该让整条流水线崩掉 —— 重建即可
            return {"task": self.task, "created_at": _now(), "stages": {}}

    def _write_manifest(self) -> None:
        payload = json.dumps(self.manifest, ensure_ascii=False, indent=2)
        self.manifest_path.write_text(payload + "\n", encoding="utf-8")

    # ---- 阶段状态 ---------------------------------------------------------

    def stage_status(self, stage: str) -> str | None:
        entry = self.manifest.get("stages", {}).get(stage)
        return entry.get("status") if entry else None

    def is_stage_done(self, stage: str, outputs: list[Path] | None = None) -> bool:
        """阶段已完成 **且** 产物齐全才为 True（产物被删则应重跑）。"""
        if self.stage_status(stage) != "done":
            return False
        if outputs:
            return all(p.exists() for p in outputs)
        return True

    def mark_stage(
        self,
        stage: str,
        outputs: list[Path] | None = None,
        status: str = "done",
        **extra: Any,
    ) -> None:
        entry: dict[str, Any] = {"status": status, "at": _now()}
        if outputs:
            entry["outputs"] = [str(p) for p in outputs]
        entry.update(extra)
        self.manifest.setdefault("stages", {})[stage] = entry
        self._write_manifest()
