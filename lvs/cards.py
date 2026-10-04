"""卡片内容模型（票据 34/35）。

把一个 `graphic` beat 变成"屏幕上该显示的那张卡" —— 只决定**显示什么**，
不管**怎么画**（那是 `cardhtml` 与 `graphic` 的事）。

两条铁律：

1. **卡上是内容，不是指令。** 「三段原文并排浮现」写的是"要怎么剪"。
   这种 beat 回退到它所辖分镜的旁白，把真实记载取出来。
2. **卡上不带脚本脚手架。** 没有段落标题（「第一段 · 钩子落定」），
   也没有旁白页脚 —— 成片底部已经烧了同一条字幕，重复还会撞边框。
   所以 `Card` 里**只有** kind / items / source 三个字段，没有位置放它们。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from lvs import prompting

MAX_ITEMS = 4

KIND_STATEMENT = "statement"   # 2~3 个短值，横排大号（数据行）
KIND_COMPARE = "compare"       # 恰好两组，左右对照
KIND_TIMELINE = "timeline"     # 3~5 个节点，横向时间轴
KIND_LIST = "list"             # 3~4 行长文本，竖排条目

LAYOUTS = (KIND_STATEMENT, KIND_COMPARE, KIND_TIMELINE, KIND_LIST)

# 「时间轴：前403」这种模板头，切完箭头要摘掉
_HEAD = re.compile(r"^[^：:]{1,12}[：:]\s*")
# 顿号/逗号切数据行
_SPLIT = re.compile(r"[，,、；;]")
# 对照版式的提示词
_COMPARE_HINT = re.compile(r"vs|VS|对照|对比|分栏|并排")
# 「三十六」+ 单位：把数据行拆成"大号数字 + 小号单位"
_NUM = re.compile(r"^([0-9〇零一二三四五六七八九十百千万亿两]{2,8})\s*(.{1,6})$")

_NODE_MAX = 8    # 时间轴节点上限长度：再长就不是节点，是图表走势描述
_PART_MAX = 8    # 数据行每个值的上限长度：再长就别按顿号切了（免得把整句旁白切碎）
_VALUE_MAX = 8   # 能上"数据行"版式的值长上限


@dataclass(frozen=True)
class Card:
    """一张图文卡片要显示的内容。**故意没有 title / footer**（见模块 docstring）。"""

    kind: str
    items: tuple[str, ...]
    source: str = "beat"      # beat：取自 beat 自身；narration：回退取旁白


# ---- 内容提取 --------------------------------------------------------------


def split_items(text: str) -> list[str]:
    """从 beat 文本里抽出要显示的数据条目。

    三条分支，按可信度排：

    1. **引号原文**最可信（`高亮"斩首四万"vs"邑三十六"` → 两串数据）；
    2. **箭头串**是节点序列（`前403 → 前313 → …`），尾部的走势描述（`缓慢下滑`）丢掉；
    3. 都没有：若不含剪辑动词、且按顿号切开每段都够短，就是数据行；否则整句算一条。
    """
    raw = (text or "").strip()
    if not raw:
        return []

    quoted = [
        next((g for g in m.groups() if g), "").strip()
        for m in prompting.QUOTED_INNER.finditer(raw)
    ]
    quoted = [q for q in quoted if q]
    if quoted:
        return quoted[:MAX_ITEMS]

    if prompting.ARROW.search(raw):
        parts = [p.strip() for p in prompting.ARROW.split(raw) if p.strip()]
        if parts:
            parts[0] = _HEAD.sub("", parts[0]).strip()
            parts = [p for p in parts if p]
        nodes = [p for p in parts if len(p) <= _NODE_MAX]
        return (nodes if len(nodes) >= 2 else parts)[:MAX_ITEMS]

    body = prompting.card_text(raw)
    if not body:
        return []
    if not prompting.has_edit_verbs(raw):
        parts = [p.strip() for p in _SPLIT.split(body) if p.strip()]
        if len(parts) >= 2 and all(len(p) <= _PART_MAX for p in parts):
            return parts[:MAX_ITEMS]
    return [body]


def split_num(item: str) -> tuple[str, str] | None:
    """把 `三十六个邑` 拆成 `("三十六", "个邑")`，好让数字大、单位小。

    拆不出来返回 `None`（照原样显示）。要求数字至少两位 ——
    否则「三段原文」的「三」也会被当成数据。
    """
    match = _NUM.match((item or "").strip())
    if not match:
        return None
    number, unit = match.group(1), match.group(2).strip()
    return (number, unit) if unit else None


def is_instruction(text: str) -> bool:
    """这条 beat 写的是"要怎么剪"而不是"屏幕上有什么"。

    判定顺序很重要：**先看数据，再看动词**。
    `高亮"甲"vs"乙"` 也含剪辑动词「高亮」，但它带了引号 —— 引号里是数据，不是指令。
    """
    raw = (text or "").strip()
    if not raw:
        return True
    if prompting.QUOTED_INNER.search(raw):
        return False
    if prompting.ARROW.search(raw):
        return False
    return prompting.has_edit_verbs(raw)


def layout_of(text: str, items: Sequence[str]) -> str:
    """选版式。只看结构（有几个、多长），不去猜语义。"""
    raw = text or ""
    items = list(items or ())
    if "时间轴" in raw or "时间线" in raw:
        return KIND_TIMELINE
    if len(items) == 2 and (_COMPARE_HINT.search(raw) or prompting.QUOTED_INNER.search(raw)):
        return KIND_COMPARE
    if prompting.ARROW.search(raw) and len(items) >= 2:
        return KIND_TIMELINE
    if len(items) == 1:
        return KIND_STATEMENT
    if 2 <= len(items) <= 3 and all(len(i) <= _VALUE_MAX for i in items):
        return KIND_STATEMENT
    return KIND_LIST if items else KIND_STATEMENT


def _from_narration(narrations: Iterable[str]) -> list[str]:
    """回退内容：该 beat 所辖分镜的旁白。

    旁白里夹着"拍摄提示"（「先把三笔记载并排摆出来」），它同样带剪辑动词 ——
    丢掉；全被丢光则原样保留，宁可难看也别空着。
    """
    lines: list[str] = []
    for raw in narrations or ():
        line = (raw or "").strip()
        if line and line not in lines:
            lines.append(line)
    kept = [line for line in lines if not prompting.has_edit_verbs(line)]
    return (kept or lines)[:MAX_ITEMS]


def card_for(beat: str, narrations: Sequence[str] = ()) -> Card:
    """beat + 所辖旁白 → 一张卡的内容。

    beat 自带数据就用 beat；是指令就用旁白；两边都空则退回 beat 的残稿。

    边界（别夸大）：beat 与旁白**都**没内容时，`items` 会是空元组，渲染出来是一张空卡。
    实际跑不到 —— 每个分镜都有旁白 —— 但代码确实允许，这里如实写明。
    """
    beat = beat or ""
    source = "beat"
    items = [] if is_instruction(beat) else split_items(beat)
    if not items:
        items = _from_narration(narrations)
        source = "narration" if items else "beat"
    if not items:
        items = split_items(beat)
    return Card(kind=layout_of(beat, items), items=tuple(items[:MAX_ITEMS]), source=source)


# ---- 按 beat 归组 ----------------------------------------------------------


def beat_groups(shots: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """把一个 `[画面位]` 下的同一 beat 的多个分镜归到一起。

    small3 实跑里 33 个图文镜只对应 **5 条** beat —— 一条 beat 铺 17 个镜。
    按 beat 而不是按镜出卡，才不会渲出十几张一模一样的图。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for shot in shots or ():
        if shot.get("kind") != prompting.KIND_GRAPHIC and shot.get("source") != prompting.KIND_GRAPHIC:
            continue
        groups.setdefault(shot.get("visual") or "", []).append(shot)
    return groups
