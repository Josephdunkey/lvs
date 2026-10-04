"""阶段间产物契约（handoff contract）—— 让"上游产物坏了"在**下游入口**就说清。

## 为什么需要它

多阶段（或"多 agent"）流水线里，**最贵的失败不是崩溃，是错的东西被当成对的继续用**。
MAST 对 1642 条多 agent 执行轨迹的分析把失败分成三类，其中
「**交互错位 36.94%**」（context lost at handoffs）正是这里要防的：
一个环节产出的坏数据，成了下一个环节的**事实前提**。

本项目已经吃过这个形状的亏很多次（全都是"不报错，只是悄悄出错产物"）：

- 重新 `parse` 之后 `shots` 仍判已完成 → 拿旧解析往下跑（`workspace` 的**产物血缘**解决了它）
- 改了 `assets` 里的图、`voice` 仍判已完成 → 拿旧图配音（**目录血缘**解决了它，见 `artifact.py`）

但还有一层没做：**下游消费 `shots.json` 时，不校验它的结构**。
`lvs shots` 自己生成时会跑 `_validate`，可一旦有人手改了 `shots.json`
（这正是设计意图 —— 它是给人手改的真相源），`tts` / `assets` / `build`
会各自 `.get("narration")` 兜空、按默认值继续，产出**静默错误**的成片。

## 本模块做什么

把"一份分镜表必须长什么样"定义成**一处**，任何阶段拿到它都先验一遍；
不合格就点名**是哪个上游阶段产的、缺什么、该重跑哪条命令**，
而不是在下游深处抛一个看不懂的 `KeyError`。

## 分层

**零 lvs 依赖的叶子模块** —— 产出方（`shots`）与消费方（`tts` / `assets` / `build`）
都能引它而不成环。`tests/test_architecture.py` 会验证它确实还是叶子，
并把 `VALID_SOURCES` 与真源（`sources.MODES` / `prompting.KIND_GRAPHIC`）对齐。
"""

from __future__ import annotations

from typing import Any, Iterable

#: 一个分镜的 `source` 合法取值。
#: ★ 单一真源：`lvs shots` 产出时与下游消费时用的是**同一个集合**。
#: 与 `sources.MODES`（去掉 auto）∪ {`prompting.KIND_GRAPHIC`} 一致，
#: 由架构测试守着不许漂移。
VALID_SOURCES: frozenset[str] = frozenset({"pexels", "local", "library", "graphic"})

#: 一个分镜**完整**必须有的字段（用于 `lvs shots` 产出时的自检）。
#: - `id`：全流程按它对位（素材文件名、音轨文件名、字幕边界都靠它）
#: - `source`：决定走哪条素材分支
#: - `narration`：配音与字幕的原文
#: - `visual`：画面描述（生图提示词的来源）
REQUIRED_FIELDS: tuple[str, ...] = ("id", "source", "narration", "visual")


#: ★ **按消费者声明的必需字段**。
#:
#: 为什么不能"一套字段卡所有人"：`assets` 根本不用 `narration`（它只取图），
#: 用完整契约去卡它 = **误报** —— 而误报会无理由卡住流水线，
#: 让人不再信任校验器（本项目反复吃过这个亏：校验器的价值全在**报得准**）。
#:
#: 判据是"**缺了会静默出错**"，不是"这一阶段的所有字段"：
#: - `id` 对谁都必需 —— 缺了会变成 0、或重复，导致逐镜对位错乱（素材/音轨/字幕全错）
#: - `source` 对 `assets` / `qc` 必需 —— 缺了会**静默走错分支**（该生图的去翻库）
#: - `narration` 对 `voice` 必需 —— 缺了会合出空音轨
#: - 其余字段（`visual` / `prompt` …）各阶段都用 `.get(...)` 优雅降级，**不列入**
CONSUMER_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "assets": ("id", "source"),
    "voice": ("id", "narration"),
    "build": ("id", "narration"),
    "qc": ("id",),
}


def requirements_for(consumer: str) -> tuple[str, ...]:
    """某消费者需要哪些字段。没登记的一律按**完整契约**（从严）。"""
    return CONSUMER_REQUIREMENTS.get(consumer, REQUIRED_FIELDS)


def validate_shots(
    shots: Any,
    *,
    require: Iterable[str] = REQUIRED_FIELDS,
    valid_sources: Iterable[str] = VALID_SOURCES,
    producer: str = "lvs shots",
) -> list[str]:
    """校验一份分镜表。返回问题列表（空 = 通过）。

    只报"一定会让下游出错"的问题，不做风格评判 —— 校验器的价值全在**报得准**：
    漏报等于白做，误报会让人不再信任它（这个判断在本项目里反复吃过亏）。

    ★ 消息里**点名上游阶段**（`producer`）：下游不该只说"缺字段"，
    要说"这是谁产出的、该重跑哪条命令" —— 否则拿到报错的人无从下手。
    """
    require = tuple(require)
    valid_sources = frozenset(valid_sources)

    if not isinstance(shots, list):
        return [
            f"分镜表不是列表（{type(shots).__name__}）—— "
            f"产物结构不对，请重跑：{producer}"
        ]

    problems: list[str] = []
    seen: dict[Any, int] = {}
    for i, shot in enumerate(shots, 1):
        if not isinstance(shot, dict):
            problems.append(f"第 {i} 项不是对象（{type(shot).__name__}）—— 请重跑：{producer}")
            continue

        sid = shot.get("id")
        if "id" in require:
            if sid is None:
                problems.append(f"第 {i} 项缺 `id`（全流程按它对位）—— 请重跑：{producer}")
            elif sid in seen:
                problems.append(
                    f"`id={sid}` 重复（第 {seen[sid]} 项与第 {i} 项）—— "
                    f"重复的 id 会让后一项覆盖前一项的素材/音轨"
                )
            else:
                seen[sid] = i

        if "source" in require and shot.get("source") not in valid_sources:
            problems.append(
                f"`id={sid}` 的 `source` 非法：{shot.get('source')!r}"
                f"（合法值：{'/'.join(sorted(valid_sources))}）—— 请重跑：{producer}"
            )

        for field in ("narration", "visual"):
            if field in require and not str(shot.get(field) or "").strip():
                problems.append(f"`id={sid}` 缺 `{field}` —— 请重跑：{producer}")

    return problems


def render_problems(problems: list[str], *, stage: str, producer: str, command: str = "") -> str:
    """把问题列表渲染成一段给人看的、**可照做**的说明。

    三段：坏在哪 → 为什么必须停下 → 下一步敲什么。
    """
    lines = [
        f"✗ {stage}：上游产物 `shots.json` 不符合契约（{len(problems)} 处）",
        "",
        "  这是**阶段间交接**的检查：下游拿到的数据结构不对，",
        "  若继续跑只会产出静默错误的成片（不报错，只是图/音对不上）。",
        "",
    ]
    for p in problems[:20]:
        lines.append(f"    · {p}")
    if len(problems) > 20:
        lines.append(f"    …（其余 {len(problems) - 20} 处省略）")
    lines += [
        "",
        f"  产物来自：{producer}",
    ]
    if command:
        lines.append(f"  修复：重跑 `{command}`，或手工改 `shots.json` 后复验")
    return "\n".join(lines)


def check_shots_or_message(
    shots: Any,
    *,
    stage: str,
    producer: str = "lvs shots",
    command: str = "",
    consumer: str = "",
) -> str:
    """一步到位：合规返回空串，不合规返回**可打印的整段说明**。

    给各阶段的 `run_command` 直接用 —— `if msg: print(msg); return 2`。

    `consumer` 决定"要求哪些字段"（见 `CONSUMER_REQUIREMENTS`）——
    只要求**这个阶段真正会读到**的，避免无理由卡住流水线。
    不传 → 按完整契约（从严）。
    """
    problems = validate_shots(
        shots, require=requirements_for(consumer), producer=producer,
    )
    if not problems:
        return ""
    return render_problems(problems, stage=stage, producer=producer, command=command)
