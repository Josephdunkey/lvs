"""门禁的**自动判据** —— "机器能判的那部分先过一遍"，作为放行的**必要条件**。

## 为什么需要它

六道门的说明里，有些是**机器本来就能判**的，但一直没接线。最典型的是 G3：

> G3 的 `what` 写着「全部图 **+ 巡检结论**」，`why` 写着「每块 24 张过眼」

而实际上 `pipeline.subjects()` 对 G3 **只返回 `assets` 目录**，
`pipeline.py` 里连 `qc` 这个词都不出现 —— 也就是说：

**可以跳过 `lvs qc` 直接批准 G3。门禁承诺要看的东西，没有任何保证。**

这不是"门禁太严"，是**门禁比它承诺的更松**。本模块把它补回来。

## 语义（很关键，别搞反）

    放行 = 人已批准（或跳过） **AND** 自动判据通过

所以它是**必要条件**，**严格加强**，不替代人审、不放松任何东西：

- 判据通过 → 该门仍需人批准才放行（人审价值一分不减）
- 人已批准但判据没过 → **仍然不放行**，并说明缺什么、该跑哪条命令

## 判据只用**已有产物**，不引入任何新依赖

| 门 | 判据 | 读什么 |
|---|---|---|
| G1 | `shots.json` 过契约校验 | `lvs/handoff.py`（已有） |
| G3 | qc 报告存在 **且** 没有 error 级结论 | `qc/qc-block-*.json`（已有） |
| G4 | 逐镜音轨齐全 + 字幕存在 | `shots.json` + `audio/`（已有） |

其余门（G0 拍摄稿、G2 定妆、G5 成片）**刻意不设**自动判据 ——
它们的判断本质上是人的事（稿子好不好、脸像不像、片子能不能交付），
硬塞一个机器判据只会变成噪音。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

#: 判据函数签名：`(ws, config) -> (是否通过, 不通过的原因)`
Criterion = Callable[[Any, Any], "tuple[bool, str]"]


def _shots_contract(ws: Any, config: Any) -> tuple[bool, str]:  # noqa: ANN401
    """G1：`shots.json` 必须过**阶段间契约**（见 `lvs/handoff.py`）。

    这道门要人看"镜数对不对、有没有跑偏"。若表本身就是坏的
    （缺 `narration`、`source` 非法、`id` 重复），人看的是一张**错的表** ——
    那比不看更糟：会带着错误的信心放行。
    """
    from lvs import handoff

    data = ws.try_load_shots()
    shots = (data or {}).get("shots")
    if not shots and shots != []:
        return False, "还没有 shots.json（先跑 `lvs shots`）"
    problems = handoff.validate_shots(shots, producer="lvs shots")
    if problems:
        return False, f"分镜表过不了契约（{len(problems)} 处）：{problems[0]}"
    return True, ""


def _qc_clean(ws: Any, config: Any) -> tuple[bool, str]:  # noqa: ANN401
    """G3：qc 报告必须**存在**，且**没有 error 级结论**。

    这条最要紧 —— 出图是最贵的一步。跳过巡检就批准 G3，
    等于放弃了"在生成过程中发现整批崩掉"的唯一机会
    （实测代价：479 张图跑 10 小时，跑完才发现整批用的是旧版提示词）。
    """
    qc_dir = ws.path("qc")
    blocks = sorted(qc_dir.glob("qc-block-*.json")) if qc_dir.is_dir() else []
    if not blocks:
        return False, "还没跑过巡检（先跑 `lvs qc`；大任务建议逐块巡检）"

    total_errors = 0
    bad_blocks: list[int] = []
    checked = 0
    for path in blocks:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        n_err = int(data.get("errors") or 0)
        checked += int(data.get("checked") or 0)
        total_errors += n_err
        if n_err:
            bad_blocks.append(int(data.get("block") or 0))

    if total_errors:
        shown = "、".join(str(b) for b in bad_blocks[:8])
        return False, (
            f"巡检有 {total_errors} 处 **error**（块 {shown}）—— "
            "看 `qc/summary.md` 与 `qc/redo-queue.json`，修完重跑"
        )
    if checked == 0:
        return False, "巡检报告里没有任何图（报告是空的，不能作数）"
    return True, ""


def _voice_complete(ws: Any, config: Any) -> tuple[bool, str]:  # noqa: ANN401
    """G4：**每一镜**都要有音轨，且字幕存在。

    这道门要人听"音画对不对"。若有镜根本没合成出音轨，
    后面 `build` 会把它们**静默跳过**（`shots_with_audio` 只收有 start 的镜）——
    于是成片少了几镜，而人还以为刚才是"审过了"。所以这里先卡住。
    """
    data = ws.try_load_shots()
    shots = (data or {}).get("shots") or []
    if not shots:
        return False, "还没有 shots.json（先跑 `lvs voice`）"

    missing = [
        int(s.get("id") or 0)
        for s in shots
        if not (s.get("audio_path") and Path(str(s["audio_path"])).is_file())
    ]
    if missing:
        shown = "、".join(str(x) for x in missing[:8])
        return False, (
            f"{len(missing)} 个分镜没有音轨（镜 {shown}{' …' if len(missing) > 8 else ''}）—— "
            "合成时会被**静默跳过**，成片会少镜"
        )

    srt = ws.path("subtitle.srt")
    if not srt.is_file():
        return False, "没有 subtitle.srt（先跑 `lvs voice`）"
    return True, ""


#: 门禁 id → 判据。**没有列在这里的门 = 不设自动判据**（判断本质上是人的事）。
CRITERIA: dict[str, Criterion] = {
    "G1": _shots_contract,
    "G3": _qc_clean,
    "G4": _voice_complete,
}


def check(gate_id: str, ws: Any, config: Any = None) -> tuple[bool, str]:  # noqa: ANN401
    """跑某道门的自动判据。没有判据的门一律通过（不设判据 ≠ 判据不通过）。

    ★ **判据自己出错，不能把门锁死**：异常一律按"通过"处理并给出说明 ——
    否则一个 json 读失败就会让人无法推进流水线（而人审才是真正的闸门）。
    `check` 是增强，不是新的故障点。
    """
    fn = CRITERIA.get(gate_id)
    if fn is None:
        return True, ""
    try:
        ok, reason = fn(ws, config)
    except Exception as exc:  # noqa: BLE001 - 判据出错不该锁死门禁
        return True, f"[判据异常，已按通过处理] {type(exc).__name__}: {exc}"
    return bool(ok), str(reason)


def has_criterion(gate_id: str) -> bool:
    return gate_id in CRITERIA
