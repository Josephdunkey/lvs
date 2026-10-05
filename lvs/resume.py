"""`lvs resume` —— 续跑开场**一条命令**给全貌（P1 / T2）。

过去续跑要跑 3–4 条命令才拼出"我在哪、下一步敲什么"：`status` 看任务、
`gate --next` 看门禁、`run` 的失败尾巴看最近失败件。这里把它们合成一条：

* **门禁向量**：六道门的开闭（`G0..G5`）+ 已放行数
* **下一道门**：`next_gate` = `"G2 cast"`（全开时为空）
* **下一条命令**：`next_command` —— 可直接粘贴执行的那一条
* **最近失败件**：runlog 里最后的逐镜失败 / 熔断 / 预算超限

`--json` **≤ 40 行**（单任务缩进输出；多任务一行一任务），字段含
`next_gate` / `next_command`。门禁判定**走 `pipeline.statuses` 同一份真源**，
不另立一套 —— 与 `lvs gate` / `lvs status` 不会打架。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lvs import runlog
from lvs.config import Config, ConfigError, PROJECT_ROOT
from lvs.pipeline import statuses as gate_statuses
from lvs.workspace import Workspace

WORK_DIRNAME = ".work"

#: 门禁 → 该跑的命令模板（`{task}` 占位）。★ 与 `pipeline._stage_hint` 的分工：
#: 那个是给人看的一句话；这里是**可直接执行的命令行**（多带 `--task`/`--config`）。
GATE_COMMANDS: dict[str, str] = {
    "script": "lvs parse <拍摄稿.md> --task {task}",
    "shots": "lvs shots --task {task}",
    "cast": "lvs cast --extract --task {task}",
    "images": "lvs assets --task {task}",
    "voice": "lvs voice --task {task}",
    "final": "lvs build --task {task}",
}

#: 全部放行后该干什么。
PUBLISH_COMMAND = "lvs publish --task {task}"

#: `--json` 里最多列几条最近失败。
MAX_FAILURES = 3


def _resolve_task(name: str | None) -> str | None:
    if name:
        return str(name).strip()
    return None


def _config_for(name: str, override: Path | None) -> tuple[Config, str]:
    """给任务挑一份 config（G2 的指纹靠它定位 cast lock；带错 = 误判 stale）。

    返回 (config, 名字)。名字进 `--config` 提示；没有就返回空 config（宁可标
    "指纹不可信"，也不要瞎猜一份，与 `summary.CONFIG_FOR_PREFIX` 同一取舍）。
    """
    candidates: list[Path] = []
    if override is not None:
        candidates.append(Path(override))
    else:
        from lvs import summary

        for prefix, fname in summary.CONFIG_FOR_PREFIX.items():
            if name.upper().startswith(prefix):
                candidates.append(PROJECT_ROOT / fname)
                break
    for path in candidates:
        if path.is_file():
            try:
                return Config.load(path), path.name
            except ConfigError:
                break
    return Config.empty(), ""


def _recent_failures(ws: Workspace, limit: int = MAX_FAILURES) -> list[str]:
    """runlog 里最近的失败件（人话一行一条）。"""
    try:
        rows = runlog.read(ws)
    except Exception:  # noqa: BLE001 - 摘要是增强，绝不因日志坏掉而失败
        return []
    bad = [
        r for r in rows
        if r.get("event") in ("shot_fail", "breaker_tripped", "llm_budget_over")
        or (r.get("event") == "stage_end" and r.get("exit_code"))
    ]
    out: list[str] = []
    for row in bad[-limit:]:
        at = str(row.get("at") or "")[5:16].replace("T", " ")
        stage = str(row.get("stage") or "?")
        kind = str(row.get("event") or "?")
        extra = ""
        if row.get("shot") is not None:
            extra = f" #{row.get('shot')}"
        elif row.get("reason"):
            extra = f" {row.get('reason')}"
        out.append(f"{at} {stage} {kind}{extra}".strip())
    return out


def _next_command(task: str, sts: list[Any], config_name: str) -> tuple[str, str]:
    """(下一道门文本, 下一条命令)。全开时门文本为空、命令是 publish。"""
    suffix = f" --config {config_name}" if config_name else ""
    nxt = next((s for s in sts if not s.open), None)
    if nxt is None:
        return "", PUBLISH_COMMAND.format(task=task) + suffix
    gate = nxt.gate
    if nxt.criterion_ok and nxt.files > 0:
        return (
            f"{gate.id} {gate.key}",
            f'lvs gate --task {task} --approve {gate.id} --note "<批语>"{suffix}',
        )
    cmd = GATE_COMMANDS.get(gate.key, f"lvs gate --task {task} --next")
    return f"{gate.id} {gate.key}", cmd.format(task=task) + suffix


def report(name: str, override: Path | None = None) -> dict[str, Any]:
    """一个任务的续跑报告（任何异常都降级成 `error` 字段，不抛）。"""
    config, config_name = _config_for(name, override)
    ws = Workspace.read(name, root=PROJECT_ROOT)
    try:
        sts = gate_statuses(ws, config=config if config_name else None)
    except Exception as exc:  # noqa: BLE001 - 门禁判据自己出错按"不知道"处理
        return {
            "task": name, "config": config_name, "gates": {}, "open": 0, "total": 0,
            "next_gate": "", "next_command": "", "recent_failures": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
    opened = sum(1 for s in sts if s.open)
    next_gate, next_command = _next_command(name, sts, config_name)
    return {
        "task": name,
        "config": config_name,
        "gates": {s.gate.id: bool(s.open) for s in sts},
        "open": opened,
        "total": len(sts),
        "next_gate": next_gate,
        "next_command": next_command,
        "next_ready": bool(next((s.files for s in sts if not s.open), 0)),
        "recent_failures": _recent_failures(ws),
        "error": "",
    }


def _select(task: str | None, show_all: bool) -> list[str]:
    task = _resolve_task(task)
    if task:
        return [task] if (PROJECT_ROOT / WORK_DIRNAME / task).is_dir() else []
    from lvs import summary

    return [p.name for p in summary.select_tasks(summary.task_dirs(),
                                                 prefix="UGE", show_all=show_all)]


def format_json(rows: list[dict[str, Any]]) -> str:
    """单任务 → 缩进（好读且远低于 40 行）；多任务 → 一行一任务（总行数也低）。"""
    if len(rows) <= 1:
        return json.dumps(rows[0] if rows else {}, ensure_ascii=False, indent=2)
    lines = ['{', '  "tasks": [']
    for i, row in enumerate(rows):
        comma = "," if i < len(rows) - 1 else ""
        lines.append("    " + json.dumps(row, ensure_ascii=False) + comma)
    lines += ["  ]", "}"]
    return "\n".join(lines)


def format_text(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ("（没有任务）开一个：`lvs init --name <任务名>`；"
                "看全部：`lvs resume --all`")
    out: list[str] = []
    for row in rows:
        marks = " ".join(
            f"{gid}{'✔' if ok else '✘'}" for gid, ok in row.get("gates", {}).items()
        ) or "-"
        out.append(f"任务 {row['task']} · 门禁 {marks} （{row['open']}/{row['total']}）")
        if row.get("error"):
            out.append(f"  ! 门禁判定出错：{row['error']}")
        if row.get("next_gate"):
            out.append(f"  下一道 {row['next_gate']}")
            out.append(f"  下一条：{row['next_command']}")
        else:
            out.append(f"  🎬 六门全开 —— {row['next_command']}")
        fails = row.get("recent_failures") or []
        out.append("  最近失败：" + ("；".join(fails) if fails else "无"))
    return "\n".join(out)


def run_command(config_path: Path | None, args: Any) -> int:  # noqa: ANN401 - 由 cli 传入
    """`lvs resume` 的入口。**不需要配置文件**（按任务前缀自己找，找不到就标 ⚠）。"""
    names = _select(getattr(args, "task", None), bool(getattr(args, "all", False)))
    override = config_path if getattr(args, "config", None) else None
    rows = [report(name, override) for name in names]
    if getattr(args, "json", False):
        print(format_json(rows))
        return 0
    print(format_text(rows))
    return 0
