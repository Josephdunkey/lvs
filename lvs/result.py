"""**统一结果信封** —— 每条工作命令的机器可读结果，一份契约。

## 为什么需要它

Agent（或任何脚本）驱动这条流水线时，每个循环都要回答同一个问题：
**"刚才那步成了吗？坏了几件？下一步敲什么？"**

在此之前这些只能在**中文散文**里找 —— `lvs assets` 跑完只打印
「素材获取完成：新取 651，跳过 0，失败 3」。想让程序读，就得去正则中文；
而**措辞一改，脚本就断**（这是本项目反复踩过的坑型：给人看的字被程序依赖）。

这个模块把答案固定成**结构化字段**，各工作命令共用同一份 —— 而不是各自发明一套。

## 契约锁在哪一层

**只锁 5 个决策字段**（其余自由扩展）：

| 字段 | 类型 | 含义 |
|---|---|---|
| `ok` | bool | 完全成功（`exit_code == 0`） |
| `status` | str | `ok` / `partial` / `failed` / `bad-input` / `blocked` |
| `exit_code` | int | **与 CLI 退出码同一个值**（不另立一套状态码） |
| `counts.failed` | int | 失败件数（逐镜隔离下最常被问的那个数） |
| `next_hint` | str | 下一步该做什么（人话，可直接执行） |

其余字段（`seconds` / `artifacts` / `warnings` / `detail` …）**可以自由增删**，
不构成破坏性变更。理由：契约太紧会绑死实现，太松则 agent 站在流沙上 ——
**只锁"agent 用来做决策的那几个"**。

`tests/test_result.py::test_locked_fields_are_present` 钉住这 5 个字段永远在。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from lvs import errors

#: **不可破坏**的字段（agent 靠它们做决策）。加字段可以，改名/删除要先想清楚。
LOCKED_FIELDS: tuple[str, ...] = ("ok", "status", "exit_code", "counts", "next_hint")

#: 退出码 → 状态名。与 `errors.EXIT_*` 一一对应，**不另立一套**。
_STATUS_BY_CODE: dict[int, str] = {
    errors.EXIT_OK: "ok",
    errors.EXIT_FAILED: "partial",
    errors.EXIT_USAGE: "bad-input",
    errors.EXIT_BLOCKED: "blocked",
}


def status_of(code: int) -> str:
    """退出码 → 状态名。未知码按失败处理（宁可说"不对"，不要说"还行"）。"""
    return _STATUS_BY_CODE.get(int(code), "failed")


def _counts_from_manifest(ws: Any, stage: str) -> dict[str, int]:  # noqa: ANN001
    """从 manifest 读这一步的计数 —— 不新建计数通道，用阶段自己已写的那份。

    `stage == "run"` 是编排命令（跨全部阶段），此时**汇总**各阶段的计数 ——
    "跑一遍总共坏了几件"正是 agent 最想问的那个数。
    """
    stages: dict[str, Any] = {}
    try:
        stages = ws.manifest.get("stages") or {}
    except AttributeError:
        return {"total": 0, "done": 0, "skipped": 0, "failed": 0}

    if stage == "run":
        pick = stages.values()
    else:
        entry = stages.get(stage)
        pick = [entry] if entry else []

    out = {"total": 0, "done": 0, "skipped": 0, "failed": 0}
    for entry in pick:
        if not isinstance(entry, dict):
            continue
        for key in out:
            out[key] += int(entry.get(key) or 0)
    return out


def next_hint_for(ws: Any, stage: str, code: int) -> str:  # noqa: ANN001
    """下一步做什么（人话，可直接执行）。

    优先用**门禁的提示**（`gate --json` 已经在产出 `stage_hint`，措辞一致），
    失败/部分失败时给"重跑这条命令"的提示 —— 因为逐镜隔离下，
    重跑会自动跳过已成功的镜，这就是正确动作。
    """
    task = getattr(ws, "task", "")
    if code == errors.EXIT_BLOCKED:
        return f"这一步需要人审：lvs gate --task {task}（看状态后 --approve / --reject / --skip）"
    if code == errors.EXIT_FAILED:
        return f"有失败件。修好原因后重跑 `lvs {stage} --task {task}`（已成功的会跳过）"
    if code == errors.EXIT_USAGE:
        return "输入/前置条件不对 —— 按上面的报错改一下再来（别重试）"

    # 成功 → 问门禁"下一步该干嘛"（唯一真源，不在这里自己编）
    try:
        from lvs import pipeline as pipeline_mod

        return pipeline_mod.next_action_hint(ws)
    except Exception:  # noqa: BLE001 - 提示是增强，拿不到就不给，别让结果生成失败
        return ""


@dataclass
class Result:
    """一条工作命令的结果。`envelope()` 是它对外的样子。"""

    stage: str
    exit_code: int
    task: str = ""
    seconds: float = 0.0
    counts: dict[str, int] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    next_hint: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def envelope(self) -> dict[str, Any]:
        """转成对外信封。锁定字段在前，便于人读。"""
        env: dict[str, Any] = {
            "task": self.task,
            "stage": self.stage,
            "ok": self.exit_code == errors.EXIT_OK,
            "status": status_of(self.exit_code),
            "exit_code": int(self.exit_code),
            "counts": dict(self.counts),
            "next_hint": self.next_hint,
        }
        if self.seconds:
            env["seconds"] = round(float(self.seconds), 1)
        if self.artifacts:
            env["artifacts"] = list(self.artifacts)
        if self.warnings:
            env["warnings"] = list(self.warnings)
        if self.errors:
            env["errors"] = list(self.errors)
        env.update(self.extra)          # 自由扩展区（不属于契约）
        return env


def build(
    ws: Any,  # noqa: ANN001
    stage: str,
    code: int,
    *,
    seconds: float = 0.0,
    counts: dict[str, int] | None = None,
    artifacts: list[str] | None = None,
    warnings: list[str] | None = None,
    errors_: list[str] | None = None,
    **extra: Any,
) -> Result:
    """组装一个 `Result`。计数默认从 manifest 读（阶段自己已写过）。"""
    return Result(
        stage=stage,
        exit_code=int(code),
        task=str(getattr(ws, "task", "") or ""),
        seconds=seconds,
        counts=dict(counts) if counts is not None else _counts_from_manifest(ws, stage),
        artifacts=list(artifacts or []),
        warnings=list(warnings or []),
        errors=list(errors_ or []),
        next_hint=next_hint_for(ws, stage, int(code)),
        extra=dict(extra),
    )


def emit(payload: dict[str, Any]) -> None:
    """把信封打到 stdout（缩进 2、不转义中文 —— 与项目其他 JSON 输出一致）。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2))
