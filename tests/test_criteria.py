"""门禁的**自动判据**（`lvs/criteria.py`）与它接进门禁后的语义。

## 这组测试守的是什么

G3 的说明写着「全部图 **+ 巡检结论**」、`why` 写着「每块 24 张过眼」，
而实际上 `pipeline.subjects()` 对 G3 只返回 `assets` 目录 ——
**可以跳过 `lvs qc` 直接批准 G3**。门禁**比它承诺的更松**。

修法是加一层**必要条件**：`放行 = 人已批准/跳过 AND 自动判据通过`。

所以这里要钉死两侧：

- **严格加强**：判据没过时，**即使人已批准也不放行**（这是本轮核心断言）
- **不误伤**：判据通过时不影响人审；**没有判据的门照旧**；判据自己出错不许锁死门禁
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lvs import criteria, pipeline
from lvs.workspace import Workspace

G3 = "G3"
G1 = "G1"
G4 = "G4"


def _ws(tmp_path: Path) -> Workspace:
    return Workspace(task="t", root=tmp_path).ensure()


def _shots(ws: Workspace, shots: list[dict]) -> None:
    ws.write_shots({"title": "t", "count": len(shots), "shots": shots})


def _good_shot(i: int, **kw) -> dict:
    base = {"id": i, "source": "local", "narration": "一句。", "visual": "一片草地"}
    base.update(kw)
    return base


# ---- G1：分镜表过契约 -------------------------------------------------------


def test_g1_fails_when_shots_missing(tmp_path: Path):
    ok, why = criteria.check(G1, _ws(tmp_path))
    assert not ok and "shots" in why


def test_g1_passes_on_a_clean_table(tmp_path: Path):
    ws = _ws(tmp_path)
    _shots(ws, [_good_shot(1), _good_shot(2)])
    assert criteria.check(G1, ws) == (True, "")


def test_g1_fails_on_a_broken_table(tmp_path: Path):
    """人看的是一张**错的表**，比不看更糟 —— 会带着错误的信心放行。"""
    ws = _ws(tmp_path)
    _shots(ws, [_good_shot(1, source="bogus")])
    ok, why = criteria.check(G1, ws)
    assert not ok and "契约" in why


# ---- G3：巡检报告存在且没有 error -------------------------------------------


def test_g3_fails_when_qc_never_ran(tmp_path: Path):
    """★ 这是本轮修的核心：以前这条**根本不存在**，于是能直接批 G3。"""
    ok, why = criteria.check(G3, _ws(tmp_path))
    assert not ok
    assert "qc" in why


def test_g3_passes_with_a_clean_report(tmp_path: Path):
    ws = _ws(tmp_path)
    d = ws.path("qc")
    d.mkdir(parents=True, exist_ok=True)
    (d / "qc-block-001.json").write_text(
        json.dumps({"block": 1, "checked": 24, "errors": 0}), encoding="utf-8"
    )
    assert criteria.check(G3, ws) == (True, "")


def test_g3_fails_when_report_has_errors(tmp_path: Path):
    ws = _ws(tmp_path)
    d = ws.path("qc")
    d.mkdir(parents=True, exist_ok=True)
    (d / "qc-block-001.json").write_text(
        json.dumps({"block": 1, "checked": 24, "errors": 3}), encoding="utf-8"
    )
    ok, why = criteria.check(G3, ws)
    assert not ok and "error" in why


def test_g3_fails_when_report_checked_nothing(tmp_path: Path):
    """空报告**不能作数** —— "没检查"不是"检查通过"。"""
    ws = _ws(tmp_path)
    d = ws.path("qc")
    d.mkdir(parents=True, exist_ok=True)
    (d / "qc-block-001.json").write_text(
        json.dumps({"block": 1, "checked": 0, "errors": 0}), encoding="utf-8"
    )
    ok, why = criteria.check(G3, ws)
    assert not ok and "空" in why


def test_g3_ignores_unreadable_report(tmp_path: Path):
    ws = _ws(tmp_path)
    d = ws.path("qc")
    d.mkdir(parents=True, exist_ok=True)
    (d / "qc-block-001.json").write_text("{坏 json", encoding="utf-8")
    ok, why = criteria.check(G3, ws)
    assert not ok, "坏报告不能当成通过"


# ---- G4：音轨齐 + 字幕在 -----------------------------------------------------


def test_g4_fails_when_a_shot_has_no_audio(tmp_path: Path):
    ws = _ws(tmp_path)
    a = ws.path("audio", "shot-001.wav")
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_bytes(b"x")
    _shots(ws, [
        _good_shot(1, audio_path=str(a)),
        _good_shot(2),                      # 没有音轨
    ])
    ok, why = criteria.check(G4, ws)
    assert not ok and "音轨" in why


def test_g4_fails_when_subtitle_missing(tmp_path: Path):
    ws = _ws(tmp_path)
    a = ws.path("audio", "shot-001.wav")
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_bytes(b"x")
    _shots(ws, [_good_shot(1, audio_path=str(a))])
    ok, why = criteria.check(G4, ws)
    assert not ok and "subtitle" in why


def test_g4_passes_when_complete(tmp_path: Path):
    ws = _ws(tmp_path)
    a = ws.path("audio", "shot-001.wav")
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_bytes(b"x")
    _shots(ws, [_good_shot(1, audio_path=str(a))])
    ws.path("subtitle.srt").write_text("1\n", encoding="utf-8")
    assert criteria.check(G4, ws) == (True, "")


# ---- 没有判据的门 / 判据出错 -------------------------------------------------


def test_gates_without_criteria_always_pass():
    for gid in ("G0", "G2", "G5"):
        assert criteria.check(gid, None) == (True, "")
        assert not criteria.has_criterion(gid)


def test_criterion_error_does_not_lock_the_gate(tmp_path: Path, monkeypatch):
    """★ 判据自己出错**不能**把人锁死在门外 —— 人审才是真正的闸门。"""
    def boom(ws, config):  # noqa: ANN001, ANN202
        raise RuntimeError("读文件炸了")

    monkeypatch.setitem(criteria.CRITERIA, G3, boom)
    ok, why = criteria.check(G3, _ws(tmp_path))
    assert ok is True, "判据异常不该按不通过处理（那会锁死流水线）"
    assert "判据异常" in why, "但要说出来，不能装作没事"


# ---- 接进门禁后的语义：严格加强 ---------------------------------------------


def _approve(ws: Workspace, gid: str) -> pipeline.GateStatus:
    return pipeline.approve(ws, gid, by="user", note="看过了")


def test_approved_but_criterion_failed_still_not_open(tmp_path: Path, monkeypatch):
    """★★ 本轮核心断言：**判据没过时，即使人已批准也不放行**。

    （G3 的产物要存在才批得动，所以这里给它造一个 assets 目录。）
    """
    ws = _ws(tmp_path)
    (ws.path("assets")).mkdir(parents=True, exist_ok=True)
    (ws.path("assets", "shot-001.png")).write_bytes(b"x")

    st = _approve(ws, G3)
    assert st.state == pipeline.STATE_APPROVED, "人的批准要记进账本（留痕）"
    assert not st.criterion_ok
    assert not st.open, "★ 自动判据没过 → 不放行"


def test_after_criterion_passes_the_same_approval_opens_it(tmp_path: Path):
    """判据跑通后**无需重新批准** —— 批准是人的决定，判据是机器的必要条件。"""
    ws = _ws(tmp_path)
    (ws.path("assets")).mkdir(parents=True, exist_ok=True)
    (ws.path("assets", "shot-001.png")).write_bytes(b"x")
    _approve(ws, G3)
    assert not pipeline.evaluate(ws, pipeline.BY_ID[G3]).open

    qc = ws.path("qc")
    qc.mkdir(parents=True, exist_ok=True)
    (qc / "qc-block-001.json").write_text(
        json.dumps({"block": 1, "checked": 4, "errors": 0}), encoding="utf-8"
    )

    st = pipeline.evaluate(ws, pipeline.BY_ID[G3])
    assert st.state == pipeline.STATE_APPROVED
    assert st.open, "判据过了，同一份批准就该放行"


def test_gate_without_criterion_behaves_as_before(tmp_path: Path):
    """不设判据的门**行为与从前完全一致**（未批准不放行、批准即放行）。"""
    ws = _ws(tmp_path)
    # G0 的产物是**拍摄稿本身**（见 `subjects` 的 manuscript 分支）
    manuscript = tmp_path / "稿.md"
    manuscript.write_text("# t\n", encoding="utf-8")
    ws.path("parse.json").write_text(
        json.dumps({"source": str(manuscript)}, ensure_ascii=False), encoding="utf-8"
    )

    assert not pipeline.evaluate(ws, pipeline.BY_ID["G0"]).open
    _approve(ws, "G0")
    st = pipeline.evaluate(ws, pipeline.BY_ID["G0"])
    assert st.open and st.criterion_ok


def test_status_json_exposes_the_criterion(tmp_path: Path):
    """agent 要能看出"卡在人审"还是"卡在判据"。"""
    ws = _ws(tmp_path)
    (ws.path("assets")).mkdir(parents=True, exist_ok=True)
    (ws.path("assets", "shot-001.png")).write_bytes(b"x")
    _approve(ws, G3)

    payload = pipeline.statuses_json(ws, pipeline.statuses(ws))
    g3 = next(g for g in payload["gates"] if g["id"] == G3)
    assert g3["criterion_ok"] is False
    assert g3["open"] is False
    assert g3["criterion_reason"]
    assert g3["has_criterion"] is True


def test_skip_bypasses_the_criterion(tmp_path: Path):
    """★ `--skip` 是**显式越权**（必带 `--note`，留痕），不受判据约束。

    为什么：`--skip` 的意思是"这道门对本次任务不适用"，不是"我审过了"。
    若判据也能卡住它，那么当判据因客观原因过不了时（某张图确实坏了但你可接受），
    `--skip` 这条逃生门就**形同虚设** —— 一个 fail-closed 系统不能没有出口。
    """
    ws = _ws(tmp_path)
    (ws.path("assets")).mkdir(parents=True, exist_ok=True)
    (ws.path("assets", "shot-001.png")).write_bytes(b"x")

    st = pipeline.skip(ws, G3, by="user", note="纯占位集，巡检不适用")
    assert st.state == pipeline.STATE_SKIPPED
    assert not st.criterion_ok, "判据确实没过……"
    assert st.open, "……但显式跳过就该放行（否则没有出口）"


def test_skip_still_requires_a_note(tmp_path: Path):
    """越权必须留痕 —— 这是 `--skip` 能不受判据约束的**前提**。"""
    ws = _ws(tmp_path)
    (ws.path("assets")).mkdir(parents=True, exist_ok=True)
    (ws.path("assets", "shot-001.png")).write_bytes(b"x")
    with pytest.raises(pipeline.GateError):
        pipeline.skip(ws, G3, by="user", note="")


def test_gates_without_criteria_are_unaffected_by_skip_semantics(tmp_path: Path):
    """不设判据的门：skip 照旧放行（行为与从前完全一致）。"""
    ws = _ws(tmp_path)
    manuscript = tmp_path / "稿.md"
    manuscript.write_text("# t\n", encoding="utf-8")
    ws.path("parse.json").write_text(
        json.dumps({"source": str(manuscript)}, ensure_ascii=False), encoding="utf-8"
    )
    st = pipeline.skip(ws, "G0", by="user", note="测试")
    assert st.open and st.criterion_ok
