"""graphic 卡不进正片：用邻近 scene 镜的画面顶替其 PNG（音频/字幕不动）。

用户拍板 2026-10-04：「graphics 里的内容不要进正片」。
graphic 分支产出的是**黑底画面描述卡**（把给模型看的 visual 描述排成文字），
不该给观众看；但这些镜的旁白是正文，音频与字幕必须保留 —— 所以只换画面：
- 目标镜：`kind == "graphic"`（或 `source == "graphic"`）且有 asset_path
- 画面来源：时间轴上**最近的前一张 scene 镜**（找不到就用后一张）
- 原卡先挪 `.work/<task>/_superseded/graphic-cards/`，再覆盖
- 幂等：目标内容已与来源一致则跳过

用法：
    python scripts/neutralize_graphics.py UGE01 UGE02 [--dry-run]
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DRY = "--dry-run" in sys.argv
TASKS = [a for a in sys.argv[1:] if not a.startswith("--")]


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def process(task: str) -> int:
    ws = ROOT / ".work" / task
    shots_path = ws / "shots.json"
    if not shots_path.is_file():
        print(f"[skip] {shots_path} 不存在")
        return 1
    shots = json.loads(shots_path.read_text(encoding="utf-8"))["shots"]

    def is_scene(s: dict) -> bool:
        return s.get("kind") != "graphic" and s.get("source") != "graphic" and s.get("asset_path")

    def is_graphic(s: dict) -> bool:
        return s.get("kind") == "graphic" or s.get("source") == "graphic"

    graphics = [s for s in shots if is_graphic(s) and s.get("asset_path")]
    if not graphics:
        print(f"[{task}] 没有 graphic 镜，无需处理")
        return 0

    backup_dir = ws / "_superseded" / "graphic-cards"
    replaced = skipped = missing = 0
    for idx, g in enumerate(shots):
        if not is_graphic(g) or not g.get("asset_path"):
            continue
        dst = Path(g["asset_path"])
        if not dst.is_file():
            print(f"  !! shot {g['id']:03d} asset 不存在：{dst}")
            missing += 1
            continue
        # 最近的前一张 scene 镜；没有就往后找
        src_shot = None
        for j in range(idx - 1, -1, -1):
            if is_scene(shots[j]):
                src_shot = shots[j]
                break
        if src_shot is None:
            for j in range(idx + 1, len(shots)):
                if is_scene(shots[j]):
                    src_shot = shots[j]
                    break
        if src_shot is None:
            print(f"  !! shot {g['id']:03d} 找不到可用 scene 镜")
            missing += 1
            continue
        src = Path(src_shot["asset_path"])
        if not src.is_file():
            print(f"  !! shot {g['id']:03d} 来源图不存在：{src}")
            missing += 1
            continue
        if md5(dst) == md5(src):
            skipped += 1
            continue
        print(f"  shot {g['id']:03d} ← shot {src_shot['id']:03d} "
              f"({src.name})；原卡 → _superseded/graphic-cards/")
        if not DRY:
            backup_dir.mkdir(parents=True, exist_ok=True)
            bak = backup_dir / dst.name
            if not bak.exists():
                shutil.move(str(dst), str(bak))
            shutil.copyfile(src, dst)
        replaced += 1

    print(f"[{'dry-run' if DRY else '已写盘'}] {task}: graphic {len(graphics)}，"
          f"替换 {replaced}，已一致跳过 {skipped}，问题 {missing}")
    return 1 if missing else 0


def main() -> int:
    if not TASKS:
        print(__doc__)
        return 2
    rc = 0
    for task in TASKS:
        rc |= process(task)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
