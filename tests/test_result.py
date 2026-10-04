"""统一结果信封（`lvs/result.py`）—— **给 agent 的运行时契约**。

## 为什么这组测试重要

`--json` 存在的唯一理由，是让**程序**（agent / 脚本）能稳定地问三件事：
"成了吗 / 坏了几件 / 下一步敲什么"。

在它之前，这些答案只在**中文散文**里（「素材获取完成：新取 651，跳过 0，失败 3」），
让程序读就得正则中文 —— 而**措辞一改脚本就断**。

所以这里钉死的是**契约本身**（不是实现细节）：
1. 5 个**决策字段**必须永远在（少一个 = agent 站到流沙上）
2. `exit_code` 必须**等于 CLI 退出码**（不另立一套）
3. `status` 必须与 `exit_code` 一一对应（不能自相矛盾）
4. 信封构造失败**不能**让命令失败（它是增强）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lvs import errors, result
from lvs.workspace import Workspace


class _Ws:
    """最小 workspace 替身：信封只用到 `task` 与 `manifest`。"""

    def __init__(self, task: str = "t1", stages: dict | None = None):
        self.task = task
        self.manifest = {"stages": stages or {}}


# ---- 契约：5 个锁定字段 -----------------------------------------------------


def test_locked_fields_are_present():
    """★ 这 5 个字段是契约。删/改名 = 破坏性变更，必须先想清楚。"""
    env = result.build(_Ws(), "assets", 0).envelope()
    for f in result.LOCKED_FIELDS:
        assert f in env, f"锁定字段 `{f}` 不见了"


def test_locked_fields_are_exactly_as_documented():
    assert set(result.LOCKED_FIELDS) == {"ok", "status", "exit_code", "counts", "next_hint"}


def test_counts_always_has_the_four_keys():
    env = result.build(_Ws(), "assets", 1).envelope()
    assert set(env["counts"]) >= {"total", "done", "skipped", "failed"}


# ---- exit_code 必须与 CLI 退出码一致 ----------------------------------------


@pytest.mark.parametrize("code,expect", [
    (errors.EXIT_OK, "ok"),
    (errors.EXIT_FAILED, "partial"),
    (errors.EXIT_USAGE, "bad-input"),
    (errors.EXIT_BLOCKED, "blocked"),
])
def test_status_matches_exit_code(code: int, expect: str):
    """★ 状态名与退出码**一一对应** —— 不允许自相矛盾。"""
    env = result.build(_Ws(), "assets", code).envelope()
    assert env["exit_code"] == code
    assert env["status"] == expect
    assert env["ok"] is (code == errors.EXIT_OK)


def test_unknown_exit_code_is_reported_as_failed():
    """未知码**不要**说"还行" —— 宁可说不对。"""
    env = result.build(_Ws(), "assets", 99).envelope()
    assert env["status"] == "failed"
    assert env["ok"] is False


def test_exit_code_is_not_a_second_numbering():
    """`exit_code` 字段就是 CLI 的那个数，不是另立一套。"""
    for code in (0, 1, 2, 3):
        assert result.build(_Ws(), "x", code).envelope()["exit_code"] == code


# ---- 计数：从 manifest 读，不新建通道 ---------------------------------------


def test_counts_come_from_the_manifest():
    ws = _Ws(stages={"assets": {"done": 651, "skipped": 0, "failed": 3, "total": 654}})
    env = result.build(ws, "assets", errors.EXIT_FAILED).envelope()
    assert env["counts"] == {"total": 654, "done": 651, "skipped": 0, "failed": 3}


def test_run_aggregates_across_stages():
    """`run` 是跨阶段的：计数该**汇总**（"跑一遍总共坏了几件"）。"""
    ws = _Ws(stages={
        "shots": {"total": 4, "done": 4, "failed": 0},
        "assets": {"total": 4, "done": 3, "failed": 1},
        "voice": {"total": 4, "done": 4, "failed": 1},
    })
    env = result.build(ws, "run", errors.EXIT_FAILED).envelope()
    assert env["counts"]["failed"] == 2
    assert env["counts"]["done"] == 11


def test_missing_stage_yields_zero_counts_not_crash():
    env = result.build(_Ws(), "assets", 0).envelope()
    assert env["counts"] == {"total": 0, "done": 0, "skipped": 0, "failed": 0}


def test_workspace_without_manifest_does_not_crash():
    class _Bare:
        task = "t"

    env = result.build(_Bare(), "assets", 0).envelope()
    assert env["counts"]["failed"] == 0


# ---- next_hint ---------------------------------------------------------------


def test_blocked_hint_tells_you_the_gate_command():
    hint = result.next_hint_for(_Ws(), "assets", errors.EXIT_BLOCKED)
    assert "gate" in hint
    assert "t1" in hint


def test_failed_hint_tells_you_to_rerun_same_command():
    """★ 逐镜隔离下，正确动作就是**重跑同一条命令**（已成功的会跳过）。"""
    hint = result.next_hint_for(_Ws(), "assets", errors.EXIT_FAILED)
    assert "lvs assets" in hint
    assert "跳过" in hint


def test_bad_input_hint_says_do_not_retry():
    hint = result.next_hint_for(_Ws(), "assets", errors.EXIT_USAGE)
    assert "别重试" in hint or "不要重试" in hint


# ---- 信封是增强：失败也不能拖垮命令 -----------------------------------------


def test_extra_fields_are_free():
    """非锁定字段可以自由加（这是"只锁决策字段"的另一半）。"""
    env = result.build(_Ws(), "assets", 0, seconds=12.5, warnings=["x"]).envelope()
    assert env["seconds"] == 12.5
    assert env["warnings"] == ["x"]


def test_envelope_is_json_serializable():
    env = result.build(_Ws(), "assets", 0, artifacts=["a.png"]).envelope()
    assert json.loads(json.dumps(env, ensure_ascii=False))["stage"] == "assets"


# ---- 真机：--json 的输出是合法 JSON ----------------------------------------


def test_cli_emits_parseable_json(tmp_path: Path, capsys, monkeypatch):
    """整条 CLI 路径上，`--json` 的最后一段必须是可解析的 JSON。"""
    from lvs import cli

    ws = Workspace(task="t", root=tmp_path).ensure()
    args = cli.build_parser().parse_args(["assets", "--task", "t", "--json"])

    monkeypatch.setattr(cli, "_dispatch", lambda a, c, w: 1)
    code = cli._run_and_report(args, None, ws)

    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])
    assert code == 1
    assert payload["exit_code"] == code, "信封的 exit_code 必须等于真实退出码"
    assert payload["status"] == "partial"
