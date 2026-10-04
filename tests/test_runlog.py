"""运行轨迹与耗时（`lvs/runlog.py` + `Workspace.record_timing`）。

## 这组守什么

一次 `lvs assets` 是**数小时**。跑到一半崩了 / 被 Ctrl-C 了，
手上不该只剩一屏滚掉的 stdout —— 至少能回答：
"哪一镜开始出问题、是不是同一类错误连着来、熔断停在第几镜"。

两条纪律：
1. **两条入口的轨迹必须一致**：`run`（走 `run_stage`）与**直接敲单条命令**
   （agent 正是这么干的）都要记，且共用同一实现。
2. **观测不该变成新的故障点**：写日志/写耗时失败都不许影响阶段结果。
"""

from __future__ import annotations

import json
from pathlib import Path

from lvs import runlog
from lvs.workspace import Workspace


class _Cfg:
    def __init__(self, **kw):
        self._d = kw

    def get(self, key, default=None):
        return self._d.get(key, default)


def _ws(tmp_path: Path) -> Workspace:
    return Workspace(task="t", root=tmp_path).ensure()


# ---- 开关 -------------------------------------------------------------------


def test_enabled_by_default():
    """默认**开**：长跑没有轨迹等于黑盒。"""
    assert runlog.enabled(None)
    assert runlog.enabled(_Cfg())


def test_can_be_disabled():
    for val in (False, "false", "0", "no", "off", 0):
        assert not runlog.enabled(_Cfg(**{runlog.CONFIG_KEY: val})), val


def test_explicit_true_is_on():
    assert runlog.enabled(_Cfg(**{runlog.CONFIG_KEY: True}))


def test_broken_config_falls_back_to_on():
    class _Bad:
        def get(self, key, default=None):
            raise RuntimeError("boom")

    assert runlog.enabled(_Bad())


# ---- 写入与读回 -------------------------------------------------------------


def test_event_appends_json_lines(tmp_path: Path):
    ws = _ws(tmp_path)
    runlog.event(ws, "assets", "shot_ok", shot=1, by="local")
    runlog.event(ws, "assets", "shot_fail", shot=2, reason="下载超时")

    rows = runlog.read(ws)
    assert [r["event"] for r in rows] == ["shot_ok", "shot_fail"]
    assert rows[0]["shot"] == 1
    assert rows[1]["reason"] == "下载超时"
    assert all("at" in r and r["stage"] == "assets" for r in rows)


def test_log_lives_under_logs_dir(tmp_path: Path):
    ws = _ws(tmp_path)
    runlog.event(ws, "parse", "stage_end", exit_code=0)
    files = list(ws.path("logs").glob("run-*.jsonl"))
    assert len(files) == 1


def test_reading_missing_log_is_empty(tmp_path: Path):
    assert runlog.read(_ws(tmp_path)) == []


def test_broken_lines_are_skipped_not_raised(tmp_path: Path):
    ws = _ws(tmp_path)
    runlog.event(ws, "assets", "shot_ok", shot=1)
    path = next(ws.path("logs").glob("run-*.jsonl"))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{坏 json\n\n")

    rows = runlog.read(ws)
    assert len(rows) == 1, "坏行该跳过，不该让读取炸掉"


def test_failures_filters_shot_failures(tmp_path: Path):
    ws = _ws(tmp_path)
    runlog.event(ws, "assets", "shot_ok", shot=1)
    runlog.event(ws, "assets", "shot_fail", shot=2, reason="x")
    runlog.event(ws, "assets", "breaker_tripped", consecutive=8)

    fails = runlog.failures(ws)
    assert [r["shot"] for r in fails] == [2]


def test_write_failure_is_swallowed(tmp_path: Path, monkeypatch):
    """★ 观测失败**绝不**能影响主流程。"""
    ws = _ws(tmp_path)

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", boom)
    runlog.event(ws, "assets", "shot_ok", shot=1)   # 不该抛


# ---- 耗时落 manifest --------------------------------------------------------


def test_record_timing_writes_both_last_and_total(tmp_path: Path):
    ws = _ws(tmp_path)
    ws.mark_stage("assets", total=4, done=4, failed=0)
    ws.record_timing("assets", 120.4)
    ws.record_timing("assets", 80.0)

    entry = json.loads(ws.manifest_path.read_text(encoding="utf-8"))["stages"]["assets"]
    assert entry["last_seconds"] == 80.0
    assert entry["total_seconds"] == 200.4


def test_record_timing_does_not_disturb_the_status(tmp_path: Path):
    """★ 耗时是**另一个维度**，不许污染 `status`（那会让 `is_stage_done` 判错）。"""
    ws = _ws(tmp_path)
    ws.mark_stage("assets", status="partial", total=4, done=3, failed=1)
    ws.record_timing("assets", 42.0)
    entry = json.loads(ws.manifest_path.read_text(encoding="utf-8"))["stages"]["assets"]
    assert entry["status"] == "partial"
    assert entry["failed"] == 1


def test_record_timing_ignores_unknown_stage(tmp_path: Path):
    ws = _ws(tmp_path)
    ws.record_timing("nope", 1.0)          # 不该抛，也不该凭空建一个阶段


# ---- 两条入口共用同一实现 ---------------------------------------------------


def test_both_entry_points_share_one_helper(tmp_path: Path):
    """★ `run`（`run_stage`）与**直接敲命令**（`cli._run_and_report`）共用
    `stage.note_stage_end` —— 否则"逐条驱动"和"一键 run"的轨迹不一样，
    而 agent 恰恰是逐条驱动的。"""
    import inspect

    from lvs import cli, stage as stage_mod

    assert hasattr(stage_mod, "note_stage_end")
    assert "note_stage_end" in inspect.getsource(stage_mod.run_stage)
    assert "note_stage_end" in inspect.getsource(cli._run_and_report)


def test_note_stage_end_records_timing_and_event(tmp_path: Path):
    from lvs import stage as stage_mod

    ws = _ws(tmp_path)
    ws.mark_stage("assets", total=4, done=4)
    stage_mod.note_stage_end(ws, _Cfg(), "assets", 0, seconds=12.5)

    entry = json.loads(ws.manifest_path.read_text(encoding="utf-8"))["stages"]["assets"]
    assert entry["last_seconds"] == 12.5
    rows = runlog.read(ws)
    assert rows[-1]["event"] == "stage_end" and rows[-1]["exit_code"] == 0


def test_note_stage_end_respects_the_off_switch(tmp_path: Path):
    from lvs import stage as stage_mod

    ws = _ws(tmp_path)
    stage_mod.note_stage_end(ws, _Cfg(**{runlog.CONFIG_KEY: False}), "assets", 0, seconds=1.0)
    assert runlog.read(ws) == [], "关掉时不该写轨迹"
