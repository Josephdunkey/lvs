"""shots.json 数据层修复（幂等，可重复跑）。

两件事（与 lvs/assets._shot_prompt 根修配套）：
1. 槽位补回：visual 含 {NAME} 而 prompt 缺 → 把 visual 整句前置到 prompt
   （与 _shot_prompt 的补回逻辑同构：visual.rstrip('。. ') + '. ' + prompt）。
2. 英文空镜词清理（cfg=1.0 无负向引导，空镜词会被真画出来）：
   an empty→a / a empty→a / the empty→the / 孤立 empty → 删。

用法: python scripts/fix_shots_slots.py <task_dir> [--dry-run]
只改 shots.json（先写 .bak-fixslots 备份），打印前后实测值。
"""
import json
import re
import shutil
import sys
from pathlib import Path

SLOT = re.compile(r"\{([A-Z_][A-Z0-9_]*)\}")
EMPTY = [
    (re.compile(r"\ban empty\b"), "a"),
    (re.compile(r"\ba empty\b"), "a"),
    (re.compile(r"\bthe empty\b"), "the"),
    (re.compile(r"\bempty\s+"), ""),
]


def fix_shot(shot):
    """返回 (改没改, 槽位补回数, empty清理数)。"""
    changed = False
    n_slot = 0
    n_empty = 0

    visual = str(shot.get("visual") or "")
    prompt = str(shot.get("prompt") or "")
    vis_slots = set(SLOT.findall(visual))
    if vis_slots and prompt:
        missing = vis_slots - set(SLOT.findall(prompt))
        if missing:
            shot["prompt"] = visual.rstrip("。. ") + ". " + prompt
            prompt = shot["prompt"]
            changed = True
            n_slot = len(missing)

    if prompt:
        new = prompt
        for pat, rep in EMPTY:
            new, k = pat.subn(rep, new)
            n_empty += k
        # 清理后遗留的双空格
        new = re.sub(r"  +", " ", new)
        if new != prompt:
            shot["prompt"] = new
            changed = True

    return changed, n_slot, n_empty


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    task_dir = Path(sys.argv[1])
    dry = "--dry-run" in sys.argv
    path = task_dir / "shots.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    shots = data["shots"]

    def count(shots):
        slots = sum(1 for s in shots if SLOT.search(str(s.get("prompt") or "")))
        empt = sum(
            len(re.findall(r"\bempty\b", str(s.get("prompt") or ""))) for s in shots
        )
        return slots, empt

    before = count(shots)
    n_fix_slot = n_fix_empty = n_shot = 0
    for s in shots:
        ch, a, b = fix_shot(s)
        if ch:
            n_shot += 1
            n_fix_slot += a
            n_fix_empty += b
    after = count(shots)

    print(f"shots={len(shots)} 改动镜数={n_shot} 槽位补回={n_fix_slot} empty清理={n_fix_empty}")
    print(f"before: slot_prompts={before[0]} empty={before[1]}")
    print(f"after : slot_prompts={after[0]} empty={after[1]}")

    if not dry and n_shot:
        shutil.copy2(path, path.with_suffix(".json.bak-fixslots"))
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"已写回 {path}（备份 {path.name}.bak-fixslots）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
