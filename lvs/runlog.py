"""运行轨迹（`logs/run-*.jsonl`）—— 长跑挂了之后还能回答"卡在哪一镜"。

## 为什么需要它

一次 `lvs assets` 是**数小时**（654 镜 × ~20s）。跑到第 8 小时崩了 / 被 Ctrl-C 了，
手上只剩一屏**滚掉的 stdout** —— 哪一镜开始出问题、是不是同一类错误连续发生、
熔断触发在哪一镜，全都答不上来。

一个 654 行的 JSONL（约 100 KB）就能回答全部这些问题，而且：
**不上框架、不起服务、不引依赖** —— 就是往磁盘追加行。

## 设计取舍

- **追加写、单文件、每日一个**：`logs/run-20261003.jsonl`
- **一行一个事件**：`{"at": …, "stage": …, "event": …, "shot": …, …}`
- **绝不因写日志而失败**：IO 出错一律吞掉。它是观测，不是主流程
  （同 `criteria` / 结果信封的取舍：增强不该变成新的故障点）
- **可关**：`[pipeline].run_log = false`（默认开）

## 事件词汇（刻意少）

| event | 何时 |
|---|---|
| `stage_start` / `stage_end` | 阶段进出（带耗时与退出码） |
| `shot_ok` / `shot_fail` / `shot_skip` | 逐镜结果（带镜号与原因） |
| `breaker_tripped` | 熔断触发（带连续失败数与停在第几镜） |
| `manifest_broken` | 清单读不出来（已留证为 `manifest.broken-*.json`，本轮按空清单继续，票 19） |
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

#: 日志开关的 config 键。
CONFIG_KEY = "pipeline.run_log"


def enabled(config: Any) -> bool:  # noqa: ANN401
    """是否记轨迹。默认**开**（长跑没有轨迹等于黑盒）；配 `false` 关闭。"""
    if config is None:
        return True
    try:
        raw = config.get(CONFIG_KEY, True)
    except Exception:  # noqa: BLE001 - 读配置失败按默认处理
        return True
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def _path(ws: Any) -> Path:  # noqa: ANN001
    return ws.path("logs", f"run-{datetime.now():%Y%m%d}.jsonl")


def event(ws: Any, stage: str, kind: str, **fields: Any) -> None:  # noqa: ANN401
    """追加一条事件。**任何失败都吞掉** —— 记日志不该让流水线失败。"""
    try:
        path = _path(ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        row: dict[str, Any] = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "stage": stage,
            "event": kind,
        }
        row.update(fields)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - 观测失败不该影响主流程
        pass


def read(ws: Any, *, date: str | None = None) -> list[dict[str, Any]]:  # noqa: ANN401
    """读回轨迹（供 `lvs board` / 排查用）。坏行跳过，不抛。"""
    path = ws.path("logs", f"run-{date}.jsonl") if date else _path(ws)
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def failures(ws: Any, *, date: str | None = None) -> list[dict[str, Any]]:  # noqa: ANN401
    """只取失败事件（排查时最常要的）。"""
    return [r for r in read(ws, date=date) if r.get("event") == "shot_fail"]
