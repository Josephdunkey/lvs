"""同一画面位（visual = beat）只留一张图：同组其余镜改用组内首张的图。

用户拍板 2026-10-04：「同一个图会出好多张，不用出这么多就不出这么多」。
- 001 当年按 `image_granularity="shot"` 出图：433 镜只有 110 个画面位，
  同画面位逐镜各出一张（近似重复）。本脚本**只挪文件、不重出图**：
  原图移到 `.work/<task>/_superseded/dedup-local/`，目标路径写入组内首张内容。
- 002 已是 beat 粒度（同组内容本就一致）→ 运行时全部"已一致跳过"，幂等。
- graphic 镜、visual 为空的镜不动。

用法：
    python scripts/dedupe_beat_images.py UGE01 UGE02 [--dry-run]
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
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

    # 分组：同一 visual（= _beat_of）一张图；graphic/空 visual 不参与
    groups: dict[str, list[dict]] = {}
    for s in shots:
        if s.get("kind") == "graphic" or s.get("source") == "graphic":
            continue
        v = str(s.get("visual") or "").strip()
        if v and s.get("asset_path"):
            groups.setdefault(v, []).append(s)

    before_files = {s["asset_path"] for g in groups.values() for s in g}
    before_unique = set()
    missing = 0
    for f in before_files:
        p = Path(f)
        if p.is_file():
            before_unique.add(md5(p))
        else:
            missing += 1

    backup_dir = ws / "_superseded" / "dedup-local"
    replaced = already = 0
    backup_bytes = 0
    for members in groups.values():
        members.sort(key=lambda s: int(s["id"]))
        canon = Path(members[0]["asset_path"])
        if not canon.is_file():
            print(f"  !! 组首图不存在：{canon}")
            missing += 1
            continue
        canon_md5 = md5(canon)
        for s in members[1:]:
            dst = Path(s["asset_path"])
            if not dst.is_file():
                print(f"  !! shot {s['id']:03d} 图不存在：{dst}")
                missing += 1
                continue
            if md5(dst) == canon_md5:
                already += 1
                continue
            print(f"  shot {s['id']:03d} ← 组首 shot {members[0]['id']:03d}（{canon.name}）")
            if not DRY:
                backup_dir.mkdir(parents=True, exist_ok=True)
                bak = backup_dir / dst.name
                if not bak.exists():
                    backup_bytes += dst.stat().st_size
                    shutil.move(str(dst), str(bak))
                shutil.copyfile(canon, dst)
            else:
                backup_bytes += dst.stat().st_size
            replaced += 1

    after_files = {s["asset_path"] for g in groups.values() for s in g}
    after_unique = set()
    for f in after_files:
        p = Path(f)
        if p.is_file():
            after_unique.add(md5(p))
    print(f"[{'dry-run' if DRY else '已写盘'}] {task}: 画面位组 {len(groups)}，"
          f"参与镜 {sum(len(g) for g in groups.values())}，"
          f"替换 {replaced}，已一致跳过 {already}")
    print(f"    唯一内容：{len(before_unique)} → {len(after_unique)}；"
          f"备份 {backup_bytes / 1e6:.0f} MB → _superseded/dedup-local/"
          + ("（dry-run 未落盘）" if DRY else ""))
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
