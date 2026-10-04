"""把讲稿元标记（〔原文〕〔注〕〔附〕/半角形）从 spoken 层剥掉——存量数据版。

单一剥离点在 `lvs/parse.py::strip_stage_markers`（新版 parse 已带）；本脚本只处理
**老版 parse 写下的存量文件**：`.work/<task>/parse.json` 与 `shots.json`。
配音、字幕、时间轴全部只读 narration，源头剥掉即天然同步（用户拍板 2026-10-04：
「读的时候不要把[原文]读出来」）。

- 只动 spoken 字段（cold_open.text / segments.narration / flow(narration) /
  shots.narration）；excluded/uncertain 是溯源文本，保留原样
- 幂等：没有标记就跳过；写前自动备份 `<file>.bak-markers`（仅首份）
- `--dry-run` 只打印计数，不写盘

用法：
    python scripts/fix_narration_markers.py UGE01 UGE02 [--dry-run]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lvs.parse import strip_stage_markers  # noqa: E402  单一定义，勿在脚本里重写正则

DRY = "--dry-run" in sys.argv
TASKS = [a for a in sys.argv[1:] if not a.startswith("--")]

# spoken 字段访问器：(取值, 回写) —— 每个访问器对应 parse 输出里一处旁白
SpokenWalker = Callable[[dict[str, Any], Callable[[str], str | None]], int]


def _count(text: str) -> int:
    return sum(text.count(m) for m in
               ("〔原文〕", "〔注〕", "〔附〕", "[原文]", "[注]", "[附]"))


def _apply(text: str) -> str | None:
    new = strip_stage_markers(text)
    return new if new != text else None


def _walk_str(container: list, changed: int, counter: list[int]) -> int:
    """剥掉 list[str]（就地回写）；counter[0] 累计**写后**仍剩的标记数。"""
    if not isinstance(container, list):
        raise TypeError(f"_walk_str 只接受 list[str]，收到 {type(container).__name__}")
    for i, t in enumerate(container):
        if isinstance(t, str):
            new = _apply(t)
            final = new if new is not None else t
            counter[0] += _count(final)
            if new is not None:
                container[i] = new
                changed += 1
    return changed


def fix_parse(path: Path, write: bool) -> tuple[int, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    changed = 0
    left = [0]

    co = data.get("cold_open") or {}
    if isinstance(co.get("text"), list):
        changed += _walk_str(co["text"], 0, left)
    elif isinstance(co.get("text"), str):
        new = _apply(co["text"])
        final = new if new is not None else co["text"]
        left[0] += _count(final)
        if new is not None:
            co["text"] = new
            changed += 1

    for seg in data.get("segments") or []:
        changed += _walk_str(seg.get("narration") or [], 0, left)
        for f in seg.get("flow") or []:
            if f.get("kind") == "narration" and isinstance(f.get("text"), str):
                new = _apply(f["text"])
                final = new if new is not None else f["text"]
                left[0] += _count(final)
                if new is not None:
                    f["text"] = new
                    changed += 1

    if changed and write:
        _backup_write(path, data)
    return changed, left[0]


def fix_shots(path: Path, write: bool) -> tuple[int, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    changed = 0
    left = [0]
    for shot in data.get("shots") or []:
        narr = shot.get("narration")
        if isinstance(narr, str):
            new = _apply(narr)
            final = new if new is not None else narr
            left[0] += _count(final)
            if new is not None:
                shot["narration"] = new
                changed += 1
    if changed and write:
        _backup_write(path, data)
    return changed, left[0]


def _backup_write(path: Path, data: dict[str, Any]) -> None:
    bak = path.with_suffix(path.suffix + ".bak-markers")
    if not bak.exists():
        bak.write_bytes(path.read_bytes())
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def main() -> int:
    if not TASKS:
        print(__doc__)
        return 2
    rc = 0
    for task in TASKS:
        base = ROOT / ".work" / task
        for name, fn in (("parse.json", fix_parse), ("shots.json", fix_shots)):
            path = base / name
            if not path.is_file():
                print(f"[skip] {path} 不存在")
                continue
            changed, left = fn(path, write=not DRY)
            tag = "dry-run" if DRY else "已写盘"
            print(f"[{tag}] {task}/{name}: 改动 {changed} 条，写后标记 {left}")
            if left:
                rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
