"""流水线门禁账本（`lvs gate`）—— 把"人审过没有"变成可查询、可失效的状态。

## 为什么需要它

本项目已经把"生成"做得足够快，真正拖垮质量的是**没有门禁**：
2026-10-01 实测 479 张图跑了 10 小时，跑完才发现整批用的是旧版提示词。
问题不在模型，在于**没有任何一处强制"先看再跑"**。

调研过 7 个同类项目（ARIS-Movie-Director / AgentCine / SHOTWISE / Drama Skills /
Moyin / Toonflow / Pixo 的 50+ 流水线横评），共识只有两条：

1. **生成前锁资产**（定妆 → 再批量）—— 由 `lvs cast` 的内容级闸门负责
2. **生成中设门禁，且 fail-closed** —— 就是本模块

Pixo 对 50+ 开源流水线的横评说得最直白：

    one-click output is watchable, not publishable. ... The honest architecture
    keeps a human review gate per scene, which is precisely the part
    "one sentence → film" marketing erases.

ARIS-Movie-Director 的写法是：

    each layer consumes the prior LOCKED node, so a skipped step fails closed at
    the next gate, never shipping a wrong frame.

AgentCine 把它做成循环里的一个节点（`OBSERVE → THINK → CHECKPOINT? → ACT → REFLECT`）。

## 本模块的核心机制：**指纹绑定**

只记"G0 已批准"是不够的 —— 批准的是**某一版**稿子。稿子事后被改了，
"已批准"就成了一句谎言。这正是本项目踩过的幽灵故障：
改了定妆卡却读不到新描述（旧镜像把新描述盖住了），没人发现。

所以每次批准都记下**当时批准对象**的指纹（`relpath|size|mtime_ns` 的 sha1）。
校验时重算：指纹不符 → 该门禁**自动回到未批准**，并明确说"产物变了，请重审"。

这是 fail-closed 的落地：宁可让你重审一次，也不放过一版没人看过的产物。

指纹用 `size + mtime_ns` 而不是**内容哈希**，是个刻意的取舍：
- G3 门禁的对象是几百张图，逐张读内容要几秒；只 stat 是毫秒级
- 任何一次真实写入都会改 `mtime_ns` —— 覆盖、追加、替换全部拦得住
- 唯一的漏网：把文件内容改回**完全相同的大小**、同时把 mtime 也改回原值
  （需要刻意伪造）。对"防手滑"这个目标，这个漏网可以接受。
- 代价：复制/恢复文件会更新 mtime → 门禁判为 stale → 要求重审。
  这是**安全方向**的误报，可以接受（多说一句"请重审"，不会放过错产物）。

## 与 `lvs cast` 的关系

`lvs cast` 自己有一道**内容级**闸门（"本集出现的人物都冻结了吗"），它查的是
`lock.json` 里的 state。本模块的 G2 查的是**审批动作本身**（谁在什么时候批的、
批的是哪一版）。两者互补，都不省：

- 内容级闸门防"漏了某个人"
- 指纹门禁防"批的是 A 版、跑的是 B 版"
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from lvs import artifact
from lvs.artifact import count_files, fingerprint
from lvs.errors import BlockedError, UsageError
from lvs.workspace import Workspace

LEDGER_NAME = "gates.json"
LEDGER_VERSION = 1

#: `load()` 的哨兵：区分「账本文件不存在」与「文件在、但读不懂」。
#: 两者都退回空账本（fail-closed），但后者必须**大声报 + 留证据**。
_BAD_LEDGER = object()

STATE_PENDING = "pending"
STATE_APPROVED = "approved"
STATE_REJECTED = "rejected"
STATE_SKIPPED = "skipped"
# 派生状态（不落盘）：批准之后产物又变了
STATE_STALE = "stale"


class GateError(UsageError, RuntimeError):
    """门禁操作本身出错（参数不对、产物还没生成等）。"""


class GateBlocked(BlockedError, RuntimeError):
    """门禁未通过。消息面向用户与 Agent，必须说清"下一步敲什么命令"。

    刻意与 `GateError` 分开：这个错误是**正常流程的一部分**（人还没审），
    上层可以据此打印"等候审阅"而不是"出错了"。
    """


# ---- 门禁定义 ---------------------------------------------------------------


@dataclass(frozen=True)
class Gate:
    id: str
    key: str
    title: str
    what: str
    kinds: tuple[str, ...]
    stage: str = ""     # run.py 里它挡住的阶段（空 = 终点门禁，没有下游）
    why: str = ""       # 为什么这道门必须停 —— 写给"想跳过的人"看


# 顺序即流程顺序。`stage` 与 `run.py` 的阶段名对齐。
GATES: tuple[Gate, ...] = (
    Gate(
        "G0", "script", "拍摄稿", "结构 / 否定式 / 人物槽位", ("manuscript",), stage="shots",
        why="稿子写错，后面几百张图全部作废（2026-10-01 实测）。改一个字，比重跑十小时便宜。"
            "（`parse` 本身免费且可逆，不受门禁约束 —— 它的产物正是这道门的审阅对象。）",
    ),
    Gate(
        "G1", "shots", "分镜与提示词", "拆镜结果 + 每镜生图提示词", ("shots",), stage="assets",
        why="提示词定死画面。跑之前必须看：镜数对不对、有没有跑偏、有没有旧版残留。",
    ),
    Gate(
        "G2", "cast", "定妆参考", "人物脸已冻结并批准", ("cast",), stage="assets",
        why="没有冻结的脸，跨镜必然变脸 —— 这不是概率问题。",
    ),
    Gate(
        "G3", "images", "分镜图与巡检", "全部图 + 巡检结论", ("assets",), stage="voice",
        why="出图是最贵的一步。要在生成**过程中**巡检（每块 24 张过眼），别等全跑完才发现整批崩。",
    ),
    Gate(
        "G4", "voice", "配音与字幕", "逐镜音轨 + 字幕", ("voice",), stage="build",
        why="音画不同步、字幕错行，合成之后返工成本最高。",
    ),
    Gate(
        "G5", "final", "成片", "final.mp4 终审", ("final",), stage="",
        why="交付前最后一关：时长、音画、字幕、片尾署名、引用合规。",
    ),
)

BY_ID: dict[str, Gate] = {g.id: g for g in GATES}
BY_KEY: dict[str, Gate] = {g.key: g for g in GATES}

# `lvs gate --task X --at parse` 之类：按阶段名反查它前面的门禁
BY_STAGE: dict[str, list[Gate]] = {}
for _g in GATES:
    if _g.stage:
        BY_STAGE.setdefault(_g.stage, []).append(_g)


def resolve_gate(token: str) -> Gate:
    """`G0` / `g0` / `script` / `拍摄稿` 都能查到同一个门禁。"""
    key = (token or "").strip()
    if not key:
        raise GateError("没给门禁编号。可用：" + "、".join(f"{g.id}({g.key})" for g in GATES))
    for table in (BY_ID, BY_KEY):
        for k, g in table.items():
            if k.lower() == key.lower():
                return g
    for g in GATES:
        if g.title == key:
            return g
    raise GateError(f"不认识的门禁 `{token}`。可用：" + "、".join(g.id for g in GATES))


# ---- 批准对象（"这道门在守什么"） -------------------------------------------


def subjects(ws: Workspace, gate: Gate, config=None) -> list[Path]:  # noqa: ANN001
    """这道门守的产物路径。**必须从磁盘现状推**，不能靠 manifest 里的记录 ——
    manifest 是产物生成时写的，产物被删了它不会自己更新。"""
    out: list[Path] = []
    for kind in gate.kinds:
        if kind == "manuscript":
            p = _manuscript_of(ws)
            if p:
                out.append(p)
        elif kind == "shots":
            out.append(ws.path("shots.json"))
        elif kind == "cast":
            from lvs import cast as cast_mod

            out.append(cast_mod.lock_path(cast_mod.cast_dir(config)))
        elif kind == "assets":
            out.append(ws.path("assets"))
        elif kind == "voice":
            out.append(ws.path("audio"))
            out.append(ws.path("subtitle.srt"))
        elif kind == "final":
            out.append(ws.path("final.mp4"))
    return out


def _manuscript_of(ws: Workspace) -> Path | None:
    """本任务的拍摄稿。优先 parse.json 记的源路径，退到 source.md。"""
    parse_path = ws.path("parse.json")
    if parse_path.is_file():
        try:
            recorded = json.loads(parse_path.read_text(encoding="utf-8")).get("source")
        except (OSError, json.JSONDecodeError):
            recorded = None
        if recorded and Path(str(recorded)).is_file():
            return Path(str(recorded))
    fallback = ws.path("source.md")
    return fallback if fallback.is_file() else None


# ---- 指纹 -------------------------------------------------------------------


# 指纹实现已抽到 `lvs/artifact.py` —— 它与 `workspace`（判"上游变了没有"）
# 共用同一套判据。两套各写一遍必然漂移，然后没人知道该信哪个。


# ---- 账本 -------------------------------------------------------------------


@dataclass
class GateRecord:
    state: str = STATE_PENDING
    at: str = ""
    by: str = ""
    note: str = ""
    fingerprint: str = ""
    files: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GateRecord":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


@dataclass
class Ledger:
    task: str = ""
    version: int = LEDGER_VERSION
    gates: dict[str, GateRecord] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Ledger":
        raw = (data or {}).get("gates") or {}
        return cls(
            task=str((data or {}).get("task") or ""),
            version=artifact.safe_int((data or {}).get("version") or LEDGER_VERSION, LEDGER_VERSION),
            gates={str(k): GateRecord.from_dict(v) for k, v in raw.items() if isinstance(v, dict)},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "task": self.task,
            "gates": {k: asdict(v) for k, v in self.gates.items()},
        }


def ledger_path(ws: Workspace) -> Path:
    return ws.path(LEDGER_NAME)


def load(ws: Workspace) -> Ledger:
    """读门禁账本。**账本损坏不许静默重建为空**（见下）。"""
    p = ledger_path(ws)
    if not p.is_file():
        return Ledger(task=ws.task)
    data = artifact.load_json_safe(p, default=_BAD_LEDGER)
    if data is _BAD_LEDGER or not isinstance(data, dict):
        # ★ 旧实现：坏账本 → 直接返回空账本，而 `save` 一落盘就把原文**覆盖没了**。
        #   后果是「6 道门全部重审」而没有任何人知道为什么 —— 批准记录不是没有，
        #   只是读不懂；把它当「没批准过」处理可以（fail-closed），但**不许不出声**。
        #   做法：把损坏内容**原样另存** `.corrupt` 一份，再退回空账本。
        backup = p.with_name(p.name + ".corrupt")
        note = ""
        if not backup.exists():
            try:
                artifact.atomic_write_bytes(backup, p.read_bytes())
                note = f"已把损坏内容原样另存为 {backup.name}（供手工抢救）。"
            except OSError as exc:
                note = f"另存损坏内容失败（{exc}）—— 请注意它随时可能被覆盖。"
        else:
            note = f"损坏内容已在 {backup.name}（未再覆盖它）。"
        print(f"⚠ 门禁账本损坏（{p.name}）：不是合法 JSON，或不是一个对象。")
        print(f"  · {note}")
        print("  · 本次按「六道门全部未批准」处理（fail-closed，不会放行任何东西）。")
        print("  · 修好内容放回 gates.json 即可恢复；确认弃用就删掉 .corrupt。")
        return Ledger(task=ws.task)
    led = Ledger.from_dict(data)
    led.task = led.task or ws.task
    return led


def save(ws: Workspace, led: Ledger) -> Path:
    """**原子**写账本。

    ★ 为什么必须原子：账本一坏，6 道门全部重审（几百镜的批准白做）。
    `write_text` 是「先截断、再写」—— 中断即留下半截 JSON，故与 manifest、
    `shots.json` 共用 `artifact.atomic_write_text`。
    """
    ws.ensure()
    p = ledger_path(ws)
    artifact.atomic_write_text(
        p, json.dumps(led.to_dict(), ensure_ascii=False, indent=2) + "\n"
    )
    return p


def _now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


# ---- 求值 -------------------------------------------------------------------


@dataclass
class GateStatus:
    gate: Gate
    state: str          # pending / approved / rejected / skipped / stale
    record: GateRecord
    files: int = 0
    fingerprint: str = ""
    #: **自动判据**是否通过（没有判据的门恒为 True）。见 `lvs/criteria.py`。
    criterion_ok: bool = True
    criterion_reason: str = ""

    @property
    def open(self) -> bool:
        """是否放行（可以往下走）。

        ★ 语义是 **`人已批准` AND `自动判据通过`**；但 **`--skip` 是显式越权**。

        判据是**必要条件**、不是替代人审 —— 它把"门禁承诺要看、但机器本来就能判"
        的那部分补上（最典型的是 G3 承诺"含巡检结论"，此前**可以跳过 `lvs qc`
        直接批准**）。所以对 `approved` 而言，这一个 `and` 让门禁**严格更严**。

        ★ 为什么 `skipped` **不**受判据约束：`--skip` 是"我认为这道门对本次任务不适用"，
        它**必须带 `--note`**（留痕、可追溯），是一个**人的越权决定**，不是"我审过了"。
        若判据也能卡住 `--skip`，那么当判据因客观原因过不了时（某张图确实坏了但你可接受），
        `--skip` 这条逃生门就**形同虚设** —— 一个 fail-closed 系统不能没有出口。
        （真要连它一起关，用 `lvs run --skip-gates` 或 `[pipeline].enforce = false`，
        两者都是更明确、范围更大的开关。）
        """
        if self.state == STATE_SKIPPED:
            return True
        return self.state == STATE_APPROVED and self.criterion_ok


def evaluate(ws: Workspace, gate: Gate, *, config=None, led: Ledger | None = None) -> GateStatus:  # noqa: ANN001
    """算这道门的当前状态。**以磁盘现状为准**，不信任账本里的过期记录。

    另外算一遍**自动判据**（见 `lvs/criteria.py`）—— 它是放行的必要条件，
    与"人有没有批准"两件事分开记，好让状态信息说清"卡在哪一边"。
    """
    from lvs import criteria as criteria_mod

    led = led if led is not None else load(ws)
    rec = led.gates.get(gate.id) or GateRecord()
    paths = subjects(ws, gate, config)
    fp = fingerprint(paths)
    n = count_files(paths)
    ok, reason = criteria_mod.check(gate.id, ws, config)

    if rec.state in (STATE_SKIPPED,):
        return GateStatus(gate, STATE_SKIPPED, rec, n, fp, ok, reason)

    if rec.state == STATE_REJECTED:
        return GateStatus(gate, STATE_REJECTED, rec, n, fp, ok, reason)

    if rec.state == STATE_APPROVED:
        if not rec.fingerprint:
            # 老账本没有指纹字段：不能当作有效批准
            return GateStatus(gate, STATE_STALE, rec, n, fp, ok, reason)
        if rec.fingerprint != fp:
            return GateStatus(gate, STATE_STALE, rec, n, fp, ok, reason)
        return GateStatus(gate, STATE_APPROVED, rec, n, fp, ok, reason)

    return GateStatus(gate, STATE_PENDING, rec, n, fp, ok, reason)


def statuses(ws: Workspace, *, config=None) -> list[GateStatus]:  # noqa: ANN001
    led = load(ws)
    return [evaluate(ws, g, config=config, led=led) for g in GATES]


def next_gate(ws: Workspace, *, config=None) -> GateStatus | None:  # noqa: ANN001
    """第一道没放行的门。全放行则返回 None。"""
    for st in statuses(ws, config=config):
        if not st.open:
            return st
    return None


# ---- 动作 -------------------------------------------------------------------


def _stamp(ws: Workspace, gate: Gate, state: str, *, by: str, note: str, config=None) -> GateStatus:  # noqa: ANN001
    led = load(ws)
    paths = subjects(ws, gate, config)
    fp = fingerprint(paths)
    n = count_files(paths)
    led.gates[gate.id] = GateRecord(
        state=state, at=_now(), by=by or "", note=note or "", fingerprint=fp, files=n,
    )
    save(ws, led)
    # ★ 走 `evaluate` 重新求值，而不是自己拼一个 GateStatus ——
    # 否则自动判据（`criterion_ok`）会是默认的 True，调用方就看不到"批了但仍未放行"。
    return evaluate(ws, gate, config=config, led=led)


def approve(ws: Workspace, token: str, *, by: str = "", note: str = "", config=None) -> GateStatus:  # noqa: ANN001
    gate = resolve_gate(token)
    paths = subjects(ws, gate, config)
    if count_files(paths) == 0:
        raise GateError(
            f"{gate.id}（{gate.title}）的产物还没生成，无从批准。\n"
            f"  在等的是：{gate.what}\n"
            f"  先把它跑出来，再回来 `lvs gate --task {ws.task} --approve {gate.id}`。"
        )
    return _stamp(ws, gate, STATE_APPROVED, by=by, note=note, config=config)


def reject(ws: Workspace, token: str, *, by: str = "", note: str = "", config=None) -> GateStatus:  # noqa: ANN001
    gate = resolve_gate(token)
    if not (note or "").strip():
        # 打回必须留原因：否则返工的人不知道该改什么（这条在本项目里反复吃过亏）
        raise GateError(
            f"打回 {gate.id} 必须写原因：`--note \"…\"`。\n"
            "  没有原因的打回，下游只能靠猜 —— 而猜错的代价是重跑一遍。"
        )
    return _stamp(ws, gate, STATE_REJECTED, by=by, note=note, config=config)


def skip(ws: Workspace, token: str, *, by: str = "", note: str = "", config=None) -> GateStatus:  # noqa: ANN001
    gate = resolve_gate(token)
    if not (note or "").strip():
        raise GateError(
            f"跳过 {gate.id} 必须写原因：`--note \"…\"`。\n"
            "  跳过会被记进账本（谁、何时、为什么），请留下可追溯的一句话。"
        )
    return _stamp(ws, gate, STATE_SKIPPED, by=by, note=note, config=config)


def reset(ws: Workspace, token: str = "") -> int:
    """把门禁打回未批准。不给 token 时清空全部（改完东西重审时用）。"""
    led = load(ws)
    if token:
        gate = resolve_gate(token)
        led.gates.pop(gate.id, None)
        save(ws, led)
        return 1
    n = len(led.gates)
    led.gates.clear()
    save(ws, led)
    return n


# ---- run.py 用的守卫 --------------------------------------------------------


def enforce_enabled(config) -> bool:  # noqa: ANN001
    """门禁是否生效。默认**生效** —— 关掉它需要显式配置。"""
    return bool(config.get("pipeline.enforce", True))


def require(ws: Workspace, token: str, *, config=None, reason: str = "") -> None:  # noqa: ANN001
    """阶段入口守卫：未放行则抛 `GateBlocked`。"""
    gate = resolve_gate(token)
    st = evaluate(ws, gate, config=config)
    if st.open:
        return
    raise GateBlocked(block_message(ws, st, reason=reason))


def block_message(ws: Workspace, st: GateStatus, *, reason: str = "") -> str:
    """未放行时的提示。说清**三件事**：卡在哪、为什么、下一步敲什么。"""
    g = st.gate
    head = {
        STATE_PENDING: "还没审",
        STATE_STALE: "产物变了，之前的批准已失效",
        STATE_REJECTED: "已被打回",
    }.get(st.state, st.state)

    lines = [
        f"⛔ {g.id}·{g.title} 未放行（{head}）",
        "",
        f"  这道门守的是：{g.what}",
        f"  为什么要停：{g.why}",
    ]
    if reason:
        lines.append(f"  本次卡在：{reason}")
    if st.state == STATE_STALE:
        lines += [
            "",
            f"  批准时指纹 {st.record.fingerprint}（{st.record.at}，{st.record.files} 个文件）",
            f"  现在指纹   {st.fingerprint or '（产物已不在）'}（{st.files} 个文件）",
            "  —— 产物在批准之后被改动过。请重新看一遍，再决定是否放行。",
        ]
    elif st.record.note and st.state == STATE_REJECTED:
        lines.append(f"  打回原因：{st.record.note}")

    lines += [
        "",
        "  看产物：",
    ]
    subs = subjects(ws, g, None)
    if subs:
        for p in subs:
            lines.append(f"    {p}")
    else:
        # ★ 空列表必须给一句解释，不能留白。
        #   实测踩过：全新项目上 `lvs run` 卡在 G0，这里打印"看产物："后面什么都没有 ——
        #   用户完全不知道要去看什么，会以为工具坏了。
        lines.append("    （还没生成 —— 先跑 `lvs parse <拍摄稿.md> --task "
                     f"{ws.task}`，或直接再跑一次 `lvs run`（它会先补上 parse））")
    lines += [
        "",
        "  下一步（审完挑一条）：",
        f"    lvs gate --task {ws.task} --approve {g.id} --note \"看过了，OK\"",
        f"    lvs gate --task {ws.task} --reject  {g.id} --note \"哪里不对\"",
        f"    lvs gate --task {ws.task} --skip    {g.id} --note \"为什么可以跳过\"",
    ]
    return "\n".join(lines)


# ---- 命令前端 ---------------------------------------------------------------

_MARK = {
    STATE_APPROVED: "✅ 已批准",
    STATE_SKIPPED: "⏭ 已跳过",
    STATE_STALE: "⚠️ 已失效",
    STATE_REJECTED: "❌ 已打回",
    STATE_PENDING: "⬜ 未审",
}


def _stage_hint(gate: Gate) -> str:
    """没产物时该先跑什么命令。"""
    key = gate.key
    return {
        "script": "把拍摄稿放进来（并跑 `lvs parse` 建立任务）",
        "shots": "`lvs shots`",
        "cast": "`lvs cast --extract` → `--render` → 出审阅表",
        "assets": "`lvs assets`",
        "voice": "`lvs voice`",
        "final": "`lvs build`",
    }.get(key, "—")


def render_status(ws: Workspace, sts: list[GateStatus]) -> str:
    lines = [f"流水线门禁 · {ws.task}", ""]
    for st in sts:
        g = st.gate
        mark = _MARK.get(st.state, st.state)
        extra = ""
        if st.state == STATE_STALE:
            extra = f"（批准时 {st.record.files} 文件 → 现在 {st.files}）"
        elif st.files == 0:
            extra = "（产物还没生成）"
        elif st.record.note:
            extra = f"「{st.record.note}」"
        when = f"  {st.record.at}" if st.record.at and st.state == STATE_APPROVED else ""
        who = f"  {st.record.by}" if st.record.by and st.state == STATE_APPROVED else ""
        lines.append(f"  {g.id} {g.title:<10} {mark:<8}{when}{who}  {extra}")
        # 已批准但自动判据没过 → 必须说出来，否则"看起来 approved 却走不动"最迷惑人
        if st.state == STATE_APPROVED and not st.criterion_ok:
            lines.append(f"       ⛔ 自动判据未过，**仍未放行**：{st.criterion_reason}")
        # 跳过 + 判据没过 → 已放行，但要留个痕（免得日后以为"巡检也过了"）
        elif st.state == STATE_SKIPPED and not st.criterion_ok:
            lines.append(f"       ⚠ 此门被显式跳过，自动判据未过：{st.criterion_reason}")
    lines.append("")

    nxt = next((s for s in sts if not s.open), None)
    if nxt is None:
        lines.append("  🎬 全部放行 —— 可以交付。")
        return "\n".join(lines)

    g = nxt.gate
    lines.append(f"  ▶ 下一道要过的门：{g.id}·{g.title} —— {g.what}")
    if not nxt.criterion_ok:
        lines.append(f"    ⛔ 自动判据未过：{nxt.criterion_reason}")
        lines.append("      （这一步是**机器能判**的必要条件；跑完它仍需要人审放行）")
    elif nxt.files == 0:
        lines.append(f"    产物还没生成，先跑：{_stage_hint(g)}")
    else:
        lines.append(f"    产物已就绪（{nxt.files} 个文件），审完再放行：")
        lines.append(f"      lvs gate --task {ws.task} --approve {g.id} --note \"…\"")
    return "\n".join(lines)


def next_action_hint(ws: Workspace, *, config=None) -> str:  # noqa: ANN001
    """**下一道要过的门该做什么** —— 一句话，可直接照做。

    ★ 单一真源：`gate` 的打印、`--json` 的 `next.stage_hint`、以及结果信封的
    `next_hint` 都该用它。此前"产物未生成 → 跑哪条命令"与"产物已就绪 → 审完放行"
    是两个分支，三处各写一遍就会漂移（本项目的老坑型）。

    全部门都放行 / 没有下一道门时返回空串。
    """
    st = next_gate(ws, config=config)
    if st is None:
        return ""
    if not st.criterion_ok:
        # 判据没过时，"去看产物"是错的方向 —— 该说的是"先把这条跑通"
        return f"自动判据未过：{st.criterion_reason}"
    if st.files == 0:
        return _stage_hint(st.gate)
    return (
        f"产物已就绪（{st.files} 个文件）—— 审完再放行："
        f"lvs gate --task {ws.task} --approve {st.gate.id} --note \"…\""
    )


def statuses_json(ws: Workspace, sts: list[GateStatus]) -> dict[str, Any]:
    """给 Agent 消费的结构。刻意包含 `files`/`ready` —— Agent 需要据此区分
    "该去跑命令了"和"该去看产物了"，这两件事的下一步完全不同。

    另外给每道门带上**自动判据**的状态（`criterion_ok` / `has_criterion`）——
    agent 据此能分清"卡在人审"还是"卡在机器条件"，这两者要采取的动作不同。
    """
    from lvs import criteria as criteria_mod

    nxt = next((s for s in sts if not s.open), None)
    return {
        "task": ws.task,
        "gates": [
            {
                "id": s.gate.id,
                "key": s.gate.key,
                "title": s.gate.title,
                "what": s.gate.what,
                "state": s.state,
                "open": s.open,
                "files": s.files,
                "ready": s.files > 0,
                "criterion_ok": s.criterion_ok,
                "criterion_reason": s.criterion_reason,
                # 有没有机器可判的条件 —— agent 据此知道"光批准还不够"
                "has_criterion": criteria_mod.has_criterion(s.gate.id),
                "at": s.record.at,
                "by": s.record.by,
                "note": s.record.note,
                "fingerprint": s.fingerprint,
            }
            for s in sts
        ],
        "next": None if nxt is None else {
            "id": nxt.gate.id,
            "key": nxt.gate.key,
            "state": nxt.state,
            "ready": nxt.files > 0,
            "criterion_ok": nxt.criterion_ok,
            "criterion_reason": nxt.criterion_reason,
            "stage_hint": _stage_hint(nxt.gate),
            "action_hint": next_action_hint(ws),
        },
        "all_open": nxt is None,
    }


def run_command(config, ws, args) -> int:  # noqa: ANN001 - 由 cli 传入
    as_json = bool(getattr(args, "json", False))
    by = str(getattr(args, "by", None) or "user")
    note = str(getattr(args, "note", None) or "")

    try:
        if getattr(args, "approve", None):
            st = approve(ws, args.approve, by=by, note=note, config=config)
            if not as_json:
                print(f"✅ {st.gate.id}·{st.gate.title} 已批准（{st.files} 个文件，指纹 {st.fingerprint}）")
                if not st.criterion_ok:
                    # 批准已记入账本（人的决定要留痕），但门**仍然关着** —— 必须说清楚，
                    # 否则"明明 approved 却走不动"是最让人困惑的状态。
                    print(f"⛔ 但**仍未放行**：自动判据未过 —— {st.criterion_reason}")
                    print("   （判据是放行的必要条件；跑通它之后无需重新批准）")
        elif getattr(args, "reject", None):
            st = reject(ws, args.reject, by=by, note=note, config=config)
            if not as_json:
                print(f"❌ {st.gate.id}·{st.gate.title} 已打回：「{note}」")
        elif getattr(args, "skip", None):
            st = skip(ws, args.skip, by=by, note=note, config=config)
            if not as_json:
                print(f"⏭ {st.gate.id}·{st.gate.title} 已跳过：「{note}」（已记进账本）")
        elif getattr(args, "reset", None) is not None:
            n = reset(ws, args.reset)
            if not as_json:
                print(f"已重置 {n} 道门禁为未批准。改完记得重审。")
        else:
            sts = statuses(ws, config=config)
            if as_json:
                print(json.dumps(statuses_json(ws, sts), ensure_ascii=False, indent=2))
            elif getattr(args, "next", False):
                nxt = next((s for s in sts if not s.open), None)
                if nxt is None:
                    print("🎬 全部放行。")
                elif nxt.files == 0:
                    print(f"{nxt.gate.id}·{nxt.gate.title} 还没产物 → 先跑：{_stage_hint(nxt.gate)}")
                else:
                    print(f"{nxt.gate.id}·{nxt.gate.title} 待审（{nxt.files} 个文件）→ 看：")
                    for p in subjects(ws, nxt.gate, config):
                        print(f"  {p}")
            else:
                print(render_status(ws, sts))
    except (GateError, GateBlocked) as exc:
        print(str(exc))
        # ★ 用错误自带的语义，不要一律 2：
        #   `GateError`（=2，参数/前置不对，改了再来）与
        #   `GateBlocked`（=3，等人审 —— **不是错误**）必须分开 ——
        #   这正是 `errors.py` 反复强调的"2 与 3 混在一起，脚本就分不清该等人还是该报警"。
        return exc.exit_code

    return 0

