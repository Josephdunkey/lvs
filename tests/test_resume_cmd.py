"""`lvs resume`（P1 / T2）—— 续跑一条命令给全貌。

## 这组守什么

* **门禁向量 / 下一道门 / 下一条命令 / 最近失败件** 四样一次给全；
* `--json` **≤ 40 行**，且含 `next_gate` / `next_command` 字段（agent 的判据）；
* 命令是**可直接粘贴**的（带 `--task`、带 `--config`）；
* 门禁判定与 `lvs gate` **同一份真源**（`pipeline.statuses`），不另立一套；
* 任何异常都降级成 `error` 字段，绝不把"看一眼"变成 traceback。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from lvs import pipeline, resume


def _sts(open_ids: set[str], *, files: dict[str, int] | None = None,
         bad_criterion: set[str] | None = None) -> list[SimpleNamespace]:
    """造一组 GateStatus 替身（顺序与真源一致）。"""
    files = files or {}
    bad = bad_criterion or set()
    return [
        SimpleNamespace(
            gate=g,
            open=g.id in open_ids,
            files=files.get(g.id, 0),
            criterion_ok=g.id not in bad,
            criterion_reason="（替身）",
        )
        for g in pipeline.GATES
    ]


# ---- 下一条命令 --------------------------------------------------------------


def test_nothing_approved_points_at_parse() -> None:
    nxt, cmd = resume._next_command("T1", _sts(set()), "")
    assert nxt == "G0 script"
    assert cmd.startswith("lvs parse")


def test_missing_artifacts_point_at_the_stage_command() -> None:
    nxt, cmd = resume._next_command("T1", _sts({"G0", "G1"}), "")
    assert nxt == "G2 cast"
    assert cmd.startswith("lvs cast --extract")


def test_ready_gate_points_at_the_approve_command() -> None:
    """产物齐了 = 该人审放行，不是该重跑。"""
    nxt, cmd = resume._next_command("T1", _sts({"G0"}, files={"G1": 3}), "")
    assert nxt == "G1 shots"
    assert "--approve G1" in cmd


def test_failed_criterion_points_back_at_its_stage() -> None:
    """判据没过时"去批准"是错的方向 —— 该先把那一步跑通。"""
    nxt, cmd = resume._next_command("T1", _sts({"G0"}, files={"G1": 3},
                                               bad_criterion={"G1"}), "")
    assert nxt == "G1 shots"
    assert cmd.startswith("lvs shots --task T1")


def test_all_open_points_at_publish() -> None:
    nxt, cmd = resume._next_command("T1", _sts({g.id for g in pipeline.GATES}), "")
    assert nxt == ""
    assert cmd.startswith("lvs publish --task T1")


def test_config_is_appended_so_the_command_is_copy_pasteable() -> None:
    _, cmd = resume._next_command("T1", _sts(set()), "config.雨月物语.toml")
    assert "--config config.雨月物语.toml" in cmd


def test_every_gate_has_a_command() -> None:
    """六道门都得有对应命令 —— 漏一个就会打印出 "lvs gate --next" 这种废话。"""
    for gate in pipeline.GATES:
        assert gate.key in resume.GATE_COMMANDS, gate.key


# ---- 渲染 -------------------------------------------------------------------


def _row(**kw) -> dict:
    row = {
        "task": "T1", "config": "config.toml",
        "gates": {g.id: g.id in {"G0", "G1"} for g in pipeline.GATES},
        "open": 2, "total": 6,
        "next_gate": "G2 cast",
        "next_command": "lvs cast --extract --task T1 --config config.toml",
        "next_ready": 0,
        "recent_failures": ["10-05 14:22 assets shot_fail #12"],
    }
    row.update(kw)
    return row


def test_json_single_task_is_under_40_lines_and_has_the_fields() -> None:
    """★ 判据：`--json` ≤ 40 行，且含 `next_gate` / `next_command`。"""
    text = resume.format_json([_row()])
    assert len(text.splitlines()) <= 40, text
    payload = json.loads(text)
    assert payload["next_gate"] == "G2 cast"
    assert payload["next_command"].startswith("lvs cast")
    assert set(payload["gates"]) == {"G0", "G1", "G2", "G3", "G4", "G5"}


def test_json_multi_task_stays_under_40_lines() -> None:
    rows = [_row(task=f"UGE{i:02d}") for i in range(1, 6)]
    text = resume.format_json(rows)
    assert len(text.splitlines()) <= 40, text
    assert len(json.loads(text)["tasks"]) == 5


def test_json_empty_selection_is_valid() -> None:
    assert json.loads(resume.format_json([])) == {}


def test_text_lists_task_gate_vector_and_next() -> None:
    text = resume.format_text([_row()])
    assert "任务 T1" in text
    assert "G0✔" in text and "G2✘" in text
    assert "下一道 G2 cast" in text
    assert "下一条：lvs cast" in text
    assert "shot_fail #12" in text


def test_text_says_all_open_out_loud() -> None:
    text = resume.format_text([_row(next_gate="", next_command="lvs publish --task T1")])
    assert "六门全开" in text


def test_text_without_tasks_gives_a_hint() -> None:
    assert "lvs init" in resume.format_text([])


def test_text_shows_errors_instead_of_hiding_them() -> None:
    text = resume.format_text([_row(error="RuntimeError: boom", gates={}, open=0, total=0)])
    assert "门禁判定出错" in text and "boom" in text


# ---- 端到端（临时任务目录）---------------------------------------------------


def _temp_task(tmp_path: Path, monkeypatch, name: str = "RT1") -> Path:
    task_dir = tmp_path / ".work" / name
    task_dir.mkdir(parents=True)
    (task_dir / "manifest.json").write_text(
        json.dumps({"version": 2, "task": name, "created_at": "2026-10-05T00:00:00",
                    "stages": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(resume, "PROJECT_ROOT", tmp_path)
    return task_dir


def test_report_on_a_real_task(tmp_path: Path, monkeypatch) -> None:
    _temp_task(tmp_path, monkeypatch)
    row = resume.report("RT1")
    assert row["task"] == "RT1"
    if not row["error"]:
        assert set(row["gates"]) == {"G0", "G1", "G2", "G3", "G4", "G5"}
        assert row["next_gate"].startswith("G0")
        assert row["next_command"].startswith("lvs parse")
    assert row["recent_failures"] == []


def test_recent_failures_reads_runlog(tmp_path: Path, monkeypatch) -> None:
    import datetime

    task_dir = _temp_task(tmp_path, monkeypatch)
    logs = task_dir / "logs"
    logs.mkdir()
    today = datetime.date.today().strftime("%Y%m%d")
    (logs / f"run-{today}.jsonl").write_text(
        json.dumps({"at": "2026-10-05T14:22:00", "stage": "assets",
                    "event": "shot_fail", "shot": 12, "reason": "Timeout"}) + "\n",
        encoding="utf-8",
    )
    row = resume.report("RT1")
    assert row["recent_failures"]
    assert "shot_fail #12" in row["recent_failures"][0]


def test_run_command_json_is_under_40_lines(tmp_path: Path, monkeypatch, capsys) -> None:
    _temp_task(tmp_path, monkeypatch)
    code = resume.run_command(None, SimpleNamespace(task="RT1", json=True, all=False,
                                                    config=None))
    assert code == 0
    out = capsys.readouterr().out
    assert len(out.splitlines()) <= 40, out
    payload = json.loads(out)
    assert "next_gate" in payload and "next_command" in payload


def test_run_command_text(tmp_path: Path, monkeypatch, capsys) -> None:
    _temp_task(tmp_path, monkeypatch)
    assert resume.run_command(None, SimpleNamespace(task="RT1", json=False, all=False,
                                                    config=None)) == 0
    assert "任务 RT1" in capsys.readouterr().out


def test_unknown_task_lists_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(resume, "PROJECT_ROOT", tmp_path)
    assert resume.run_command(None, SimpleNamespace(task="NOPE", json=False, all=False,
                                                    config=None)) == 0
    assert "没有任务" in capsys.readouterr().out
