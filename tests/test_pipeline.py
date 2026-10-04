"""流水线门禁账本（`lvs gate`）的测试。

这个模块的价值只有一条：**fail-closed**。
所以测试的重点不是"批准之后能不能过"，而是"什么情况下**必须拦**"：

- 没批准 → 拦
- 批准之后产物被改了 → **也要拦**（这条最容易漏，也最贵）
- 打回/跳过不写原因 → 拦（理由：没有原因的打回，下游只能靠猜）

最后一条是账本的全部意义所在 —— 少了它，"批准"就只是一句无法追溯的话。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lvs import pipeline as pl


class _Ws:
    """最小 Workspace 替身：只需要 `task` / `path` / `ensure`（pipeline 只用到这三个）。"""

    def __init__(self, root: Path, task: str = "t1"):
        self.root = root
        self.task = task

    @property
    def dir(self) -> Path:
        return self.root / ".work" / self.task

    def path(self, *parts: str) -> Path:
        return self.dir.joinpath(*parts)

    def ensure(self) -> "_Ws":
        self.dir.mkdir(parents=True, exist_ok=True)
        return self


class _Cfg:
    def __init__(self, data=None):
        self._d = data or {}

    def get(self, key, default=None):
        return self._d.get(key, default)


@pytest.fixture()
def ws(tmp_path: Path) -> _Ws:
    w = _Ws(tmp_path)
    w.ensure()
    (w.path("shots.json")).write_text('{"shots": []}', encoding="utf-8")
    return w


def _approve_shots(ws_: _Ws, **kw) -> pl.GateStatus:
    return pl.approve(ws_, "G1", config=_Cfg(), **kw)


# ---- 门禁定义 --------------------------------------------------------------


def test_gate_ids_and_keys_are_unique():
    ids = [g.id for g in pl.GATES]
    keys = [g.key for g in pl.GATES]
    assert len(set(ids)) == len(ids)
    assert len(set(keys)) == len(keys)
    assert ids == ["G0", "G1", "G2", "G3", "G4", "G5"]


def test_every_gate_explains_why_it_must_stop():
    """每道门都要写清"为什么必须停" —— 不写清，下一个人就会去关掉它。"""
    for g in pl.GATES:
        assert g.why.strip(), f"{g.id} 缺 why"
        assert g.what.strip(), f"{g.id} 缺 what"


def test_by_stage_maps_gates_to_run_stages():
    """与 `run.py` 的阶段名对齐；G5 是终点门禁（没有下游阶段）。"""
    assert [g.id for g in pl.BY_STAGE["shots"]] == ["G0"]
    assert [g.id for g in pl.BY_STAGE["assets"]] == ["G1", "G2"]
    assert [g.id for g in pl.BY_STAGE["voice"]] == ["G3"]
    assert [g.id for g in pl.BY_STAGE["build"]] == ["G4"]
    assert "parse" not in pl.BY_STAGE, "parse 免费且可逆，不该被门禁挡住"
    assert [g.id for g in pl.GATES if not g.stage] == ["G5"]


def test_resolve_gate_accepts_id_key_and_title():
    assert pl.resolve_gate("G0").key == "script"
    assert pl.resolve_gate("g0").key == "script"
    assert pl.resolve_gate("cast").id == "G2"
    assert pl.resolve_gate("成片").id == "G5"
    with pytest.raises(pl.GateError):
        pl.resolve_gate("G99")


def test_enforce_enabled_defaults_to_true():
    """默认**生效** —— 关掉人审必须是显式动作。"""
    assert pl.enforce_enabled(_Cfg()) is True
    assert pl.enforce_enabled(_Cfg({"pipeline.enforce": False})) is False


# ---- 指纹 ------------------------------------------------------------------


def test_fingerprint_of_nothing_is_blank(tmp_path: Path):
    assert pl.fingerprint([tmp_path / "nope"]) == ""


def test_fingerprint_changes_when_file_changes(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("one", encoding="utf-8")
    before = pl.fingerprint([f])
    f.write_text("two-longer", encoding="utf-8")
    assert pl.fingerprint([f]) != before


def test_fingerprint_is_order_independent(tmp_path: Path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("a", encoding="utf-8")
    b.write_text("b", encoding="utf-8")
    assert pl.fingerprint([a, b]) == pl.fingerprint([b, a])


def test_fingerprint_covers_directory_contents(tmp_path: Path):
    d = tmp_path / "assets"
    d.mkdir()
    (d / "a.png").write_text("x", encoding="utf-8")
    before = pl.fingerprint([d])
    (d / "b.png").write_text("y", encoding="utf-8")
    assert pl.fingerprint([d]) != before, "新增文件必须改变指纹"


def test_count_files_counts_nested(tmp_path: Path):
    d = tmp_path / "assets" / "local"
    d.mkdir(parents=True)
    (d / "a.png").write_text("x", encoding="utf-8")
    (d / "b.png").write_text("y", encoding="utf-8")
    assert pl.count_files([tmp_path / "assets"]) == 2
    assert pl.count_files([tmp_path / "assets" / "local" / "a.png"]) == 1
    assert pl.count_files([tmp_path / "missing"]) == 0


# ---- 批准 / 打回 / 跳过 ----------------------------------------------------


def test_approve_then_require_passes(ws: _Ws):
    _approve_shots(ws)
    pl.require(ws, "G1", config=_Cfg())  # 不抛 = 通过
    assert pl.evaluate(ws, pl.BY_ID["G1"], config=_Cfg()).state == pl.STATE_APPROVED


def test_require_blocks_when_pending(ws: _Ws):
    with pytest.raises(pl.GateBlocked) as ei:
        pl.require(ws, "G1", config=_Cfg())
    msg = str(ei.value)
    assert "G1" in msg
    assert "lvs gate" in msg, "拦住时必须给出下一步命令"
    assert "为什么" in msg or "为什么要停" in msg


def test_approve_refuses_when_no_subject_files(ws: _Ws):
    """产物还没生成时"批准"是空头支票 —— 必须拒掉。"""
    with pytest.raises(pl.GateError) as ei:
        pl.approve(ws, "G3", config=_Cfg())   # assets 目录不存在
    assert "还没生成" in str(ei.value)


def test_edit_after_approve_makes_the_gate_stale(ws: _Ws):
    """★ 本模块的核心：批准的是**某一版**产物。改了就作废。"""
    _approve_shots(ws)
    (ws.path("shots.json")).write_text('{"shots": [{"id": 1}]}', encoding="utf-8")
    st = pl.evaluate(ws, pl.BY_ID["G1"], config=_Cfg())
    assert st.state == pl.STATE_STALE
    assert not st.open


def test_stale_gate_blocks_and_explains(ws: _Ws):
    _approve_shots(ws)
    (ws.path("shots.json")).write_text('{"shots": [{"id": 1}]}', encoding="utf-8")
    with pytest.raises(pl.GateBlocked) as ei:
        pl.require(ws, "G1", config=_Cfg())
    msg = str(ei.value)
    assert "失效" in msg
    assert "指纹" in msg, "要让人看懂是哪一版变了"


def test_reject_requires_a_note(ws: _Ws):
    with pytest.raises(pl.GateError) as ei:
        pl.reject(ws, "G1", config=_Cfg())
    assert "原因" in str(ei.value)


def test_reject_then_blocks_even_if_files_unchanged(ws: _Ws):
    _approve_shots(ws)
    pl.reject(ws, "G1", note="提示词跑偏了", config=_Cfg())
    st = pl.evaluate(ws, pl.BY_ID["G1"], config=_Cfg())
    assert st.state == pl.STATE_REJECTED
    assert not st.open


def test_skip_requires_a_note_and_then_opens(ws: _Ws):
    with pytest.raises(pl.GateError):
        pl.skip(ws, "G1", config=_Cfg())
    st = pl.skip(ws, "G1", note="纯空镜集，无提示词可审", config=_Cfg())
    assert st.open
    assert pl.evaluate(ws, pl.BY_ID["G1"], config=_Cfg()).state == pl.STATE_SKIPPED


def test_skip_is_recorded_with_who_and_why(ws: _Ws):
    pl.skip(ws, "G3", by="agent", note="历史任务补齐", config=_Cfg())
    raw = json.loads(ws.path(pl.LEDGER_NAME).read_text(encoding="utf-8"))
    rec = raw["gates"]["G3"]
    assert rec["note"] == "历史任务补齐"
    assert rec["by"] == "agent"
    assert rec["at"], "跳过也要记时间"


# ---- 求值与查询 ------------------------------------------------------------


def test_next_gate_is_the_first_unopened(ws: _Ws):
    assert pl.next_gate(ws, config=_Cfg()).gate.id == "G0"
    pl.skip(ws, "G0", note="测试", config=_Cfg())
    pl.approve(ws, "G1", config=_Cfg())
    assert pl.next_gate(ws, config=_Cfg()).gate.id == "G2"


def test_all_open_reports_none(ws: _Ws):
    for g in pl.GATES:
        pl.skip(ws, g.id, note="测试放行", config=_Cfg())
    assert pl.next_gate(ws, config=_Cfg()) is None


def test_reset_clears_one_or_all(ws: _Ws):
    _approve_shots(ws)
    pl.skip(ws, "G0", note="x", config=_Cfg())
    assert pl.reset(ws, "G1") == 1
    assert pl.evaluate(ws, pl.BY_ID["G1"], config=_Cfg()).state == pl.STATE_PENDING
    assert pl.reset(ws) == 1
    assert pl.load(ws).gates == {}


def test_corrupt_ledger_fails_closed(ws: _Ws):
    """账本坏了要退到"全部未批准"，而不是"全部放行"。"""
    _approve_shots(ws)
    ws.path(pl.LEDGER_NAME).write_text("{ not json", encoding="utf-8")
    assert pl.evaluate(ws, pl.BY_ID["G1"], config=_Cfg()).state == pl.STATE_PENDING


def test_legacy_record_without_fingerprint_is_not_accepted(ws: _Ws):
    """老账本没有 fingerprint 字段 —— 不能当成有效批准。"""
    ws.path(pl.LEDGER_NAME).write_text(
        json.dumps({"version": 1, "task": "t1",
                    "gates": {"G1": {"state": "approved", "at": "x", "files": 1}}}),
        encoding="utf-8",
    )
    assert pl.evaluate(ws, pl.BY_ID["G1"], config=_Cfg()).state == pl.STATE_STALE


def test_ledger_roundtrip(ws: _Ws):
    _approve_shots(ws, by="user", note="看过了")
    led = pl.load(ws)
    assert led.task == "t1"
    assert led.gates["G1"].note == "看过了"
    pl.save(ws, led)
    assert pl.load(ws).gates["G1"].by == "user"


def test_statuses_json_marks_ready_vs_not(ws: _Ws):
    """Agent 要能区分"该去跑命令"和"该去看产物" —— 这两件事下一步完全不同。"""
    data = pl.statuses_json(ws, pl.statuses(ws, config=_Cfg()))
    by_key = {g["key"]: g for g in data["gates"]}
    assert by_key["shots"]["ready"] is True
    assert by_key["images"]["ready"] is False
    assert data["next"]["key"] == "script"
    assert data["all_open"] is False


def test_block_message_lists_subject_paths(ws: _Ws):
    st = pl.evaluate(ws, pl.BY_ID["G1"], config=_Cfg())
    msg = pl.block_message(ws, st)
    assert "shots.json" in msg


# ---- 与 `lvs run` 的接线 ----------------------------------------------------
#
# `run_command` 里的顺序是一个真 bug：先跑 GPU 前置检查、再跑门禁，
# 结果是用户在 G0 没审的时候被告知"请关掉 ComfyUI" ——
# **真正的原因被一个无关的提示盖住了**。比不给提示更糟。
#
# 语义上也说不通：门禁问的是"**该不该**往下走"，守卫问的是"这一步**能不能**跑"。
# 该不该是更上位的问题，必须先问。


class _Args:
    def __init__(self, **kw):
        self.task = "t1"
        self.force = False
        self.skip_gates = False
        self.no_library = False
        self.no_llm = False
        self.visual = None
        self.source = None
        self.style = None
        self.manuscript = None
        self.demo = False
        self.__dict__.update(kw)


def _run_ws(tmp_path: Path):
    """造一个"有拍摄稿、没分镜"的任务 —— `lvs run` 会停在 G0。"""
    from lvs.workspace import Workspace

    ws = Workspace(task="t1", root=tmp_path)
    ws.ensure()
    manuscript = tmp_path / "稿件.md"
    manuscript.write_text("# t\n\n[画面位] [场景] 一片草地\n", encoding="utf-8")
    ws.path("parse.json").write_text(
        json.dumps({"source": str(manuscript)}, ensure_ascii=False), encoding="utf-8"
    )
    return ws


def test_next_stage_returns_first_undone(tmp_path: Path):
    from lvs import run as run_mod

    ws = _run_ws(tmp_path)
    assert run_mod._next_stage(ws, _Args()) == "shots"
    # `is_stage_done` 会核对产物文件真的在 —— 所以先落文件再标阶段
    ws.path("shots.json").write_text('{"shots": []}', encoding="utf-8")
    for stage in ("shots", "assets", "voice", "build"):
        ws.mark_stage(stage, outputs=[ws.path("shots.json")])
    assert run_mod._next_stage(ws, _Args()) is None
    # --force 时即使都做完也要从 shots 起重跑
    assert run_mod._next_stage(ws, _Args(force=True)) == "shots"


def test_run_reports_gate_before_gpu_guard(tmp_path: Path, monkeypatch, capsys):
    """★ G0 未批准时，`lvs run` 必须先报门禁，**不能**去惊动 GPU 守卫。"""
    from lvs import guard as guard_mod
    from lvs import run as run_mod

    ws = _run_ws(tmp_path)
    touched: list[str] = []
    monkeypatch.setattr(guard_mod, "check", lambda st, cfg: touched.append(st) or None)

    code = run_mod.run_command(_Cfg({"tts.backend": "openai_speech"}), ws, _Args())
    out = capsys.readouterr().out
    assert code == 3, "门禁未过应以 3 退出（而不是守卫的 2）"
    assert "G0" in out
    assert touched == [], "门禁没放行就不该去探测 GPU"
    assert "关掉 ComfyUI" not in out


def test_run_reaches_guard_once_gates_are_skipped(tmp_path: Path, monkeypatch):
    """确认接了门禁之后，守卫**没有**被绕过。"""
    from lvs import guard as guard_mod
    from lvs import run as run_mod

    ws = _run_ws(tmp_path)
    touched: list[str] = []
    monkeypatch.setattr(guard_mod, "check", lambda st, cfg: touched.append(st) or None)

    run_mod.run_command(_Cfg({"tts.backend": "openai_speech"}), ws, _Args(skip_gates=True))
    assert touched == ["tts"], "--skip-gates 时守卫必须照常生效"


# ---- `--keep-going` 的语义（本轮实装） --------------------------------------
#
# 之前 `run.py` 的 docstring 里写着"用 `--keep-going` 时不因单阶段失败而中断"，
# 但**这个开关根本不存在** —— 文档当愿望用。实装时最容易搞错的边界是：
#
#   失败的阶段 → 可以跳过（用户要的就是这个）
#   门禁拦住的阶段 → **不能跳过**（跳过去就等于放弃人审，而门禁是这套流水线存在的理由）


class _KeepWs:
    """最小 Workspace 替身：所有阶段都"未完成"。"""

    task = "t1"

    def __init__(self, root: Path):
        self.root = root
        (root / ".work" / self.task).mkdir(parents=True, exist_ok=True)

    @property
    def dir(self) -> Path:
        return self.root / ".work" / self.task

    def path(self, *parts: str) -> Path:
        return self.dir.joinpath(*parts)

    def ensure(self) -> "_KeepWs":
        return self

    def is_stage_done(self, stage: str, outputs=None) -> bool:  # noqa: ANN001, ARG002
        return False

    def record_lineage(self, *a, **k) -> None:  # noqa: ANN002, ANN003
        return None

    def try_load_shots(self):
        return {"shots": []}


class _KeepArgs:
    def __init__(self, **kw):
        self.task = "t1"
        self.force = False
        self.skip_gates = False
        self.keep_going = False
        self.manuscript = None
        self.demo = False
        self.__dict__.update(kw)


def _patch_impls(monkeypatch, *, fail_at: str, calls: list[str],
                 codes: dict[str, int] | None = None) -> None:
    """把每个阶段的实现换成一个记录调用、并按 `codes`（缺省 0）返回码的假函数。

    ★ 名字要按**阶段表**反查，不能从 impl 的模块名猜 ——
    `voice` 的实现是 `lvs.tts:run_command`，猜出来会记成 "tts"。
    """
    from lvs import stage as stage_mod

    by_impl = {st.impl: st.name for st in stage_mod.ALL if st.impl}
    codes = dict(codes or {})

    def fake_resolve(impl: str):
        name = by_impl.get(impl, impl)

        def _impl(config, ws, args):  # noqa: ANN001, ANN202
            calls.append(name)
            if name == fail_at:
                return codes.get(name, 2)
            return codes.get(name, 0)

        return _impl

    monkeypatch.setattr(stage_mod, "resolve_impl", fake_resolve)


def _seed_parse(ws: "_KeepWs") -> None:
    """放一个 parse.json，让 `_ensure_parse` 跳过 —— 否则它会在进入阶段循环前就退出。"""
    (ws.dir / "parse.json").write_text('{"segments": []}', encoding="utf-8")


def test_keep_going_continues_after_a_stage_failure(tmp_path: Path, monkeypatch):
    ws = _KeepWs(tmp_path)
    _seed_parse(ws)
    calls: list[str] = []
    _patch_impls(monkeypatch, fail_at="assets", calls=calls)

    from lvs import run as run_mod

    # 门禁单独测（见下一条）；这一条只验"失败是否继续"，所以跳过门禁
    code = run_mod.run_pipeline(_KeepCfg(), ws, _KeepArgs(keep_going=True, skip_gates=True))
    assert "assets" in calls
    assert "voice" in calls and "build" in calls, f"应继续跑后面的阶段，实得 {calls}"
    assert code == 2, "有失败件时退出码应带上失败（2），而不是 0"


def test_without_keep_going_it_stops_at_the_failure(tmp_path: Path, monkeypatch):
    ws = _KeepWs(tmp_path)
    _seed_parse(ws)
    calls: list[str] = []
    _patch_impls(monkeypatch, fail_at="assets", calls=calls)

    from lvs import run as run_mod

    code = run_mod.run_pipeline(_KeepCfg(), ws, _KeepArgs(keep_going=False, skip_gates=True))
    assert code == 2
    assert "voice" not in calls, "默认遇错即停"


def test_keep_going_does_NOT_skip_a_gate(tmp_path: Path, monkeypatch):
    """★ 最关键的一条：门禁拦住时，`--keep-going` 也必须停。

    门禁的语义是"等人做决定"，不是"出错" —— 跳过它等于放弃人审，
    而这套流水线的全部价值就在于那道门。
    """
    ws = _KeepWs(tmp_path)
    manuscript = tmp_path / "稿件.md"
    manuscript.write_text("# t\n\n[画面位] [场景] 一片草地\n", encoding="utf-8")
    (ws.dir / "parse.json").write_text(
        json.dumps({"source": str(manuscript)}, ensure_ascii=False), encoding="utf-8"
    )
    calls: list[str] = []
    _patch_impls(monkeypatch, fail_at="(never)", calls=calls)

    from lvs import run as run_mod

    code = run_mod.run_pipeline(_KeepCfg(), ws, _KeepArgs(keep_going=True))
    assert code == 3, "门禁未过应以 3 退出"
    assert "shots" not in calls, "门禁没过就不该执行该阶段"


# ---- 退出码 1（部分失败）：逐镜隔离，**继续**但如实报码 --------------------
#
# `assets` / `tts` / `build` 原先无论有没有失败件都 `return 0`，
# 于是 `errors.EXIT_FAILED`（"跑完了但有失败件"）形同虚设，
# 脚本与 `lvs run` 都以为一切正常。修好之后要钉住两件事：
#   1. 部分失败**默认就继续**（这是逐镜隔离的本意），不需要 `--keep-going`
#   2. 最终退出码是 1，不是 0 —— 否则"如实报码"等于没做


def test_partial_failure_continues_even_without_keep_going(tmp_path: Path, monkeypatch):
    ws = _KeepWs(tmp_path)
    _seed_parse(ws)
    calls: list[str] = []
    _patch_impls(monkeypatch, fail_at="(never)", calls=calls,
                 codes={"assets": 1})   # assets 有失败件：EXIT_FAILED

    from lvs import run as run_mod

    code = run_mod.run_pipeline(_KeepCfg(), ws, _KeepArgs(keep_going=False, skip_gates=True))
    assert "voice" in calls and "build" in calls, f"部分失败应继续，实得 {calls}"
    assert code == 1, "部分失败的最终退出码应是 1（EXIT_FAILED），不是 0"


def test_partial_failure_is_reported_in_summary(tmp_path: Path, monkeypatch, capsys):
    ws = _KeepWs(tmp_path)
    _seed_parse(ws)
    calls: list[str] = []
    _patch_impls(monkeypatch, fail_at="(never)", calls=calls, codes={"voice": 1})

    from lvs import run as run_mod

    code = run_mod.run_pipeline(_KeepCfg(), ws, _KeepArgs(keep_going=False, skip_gates=True))
    out = capsys.readouterr().out
    assert code == 1
    assert "失败件" in out, "成片产出了但有失败件，必须**说清**，不能静默"
    assert "build" in calls, "build 仍应执行"


def test_hard_failure_still_stops_without_keep_going(tmp_path: Path, monkeypatch):
    """与部分失败相对的一侧：硬失败（2）默认就该停。"""
    ws = _KeepWs(tmp_path)
    _seed_parse(ws)
    calls: list[str] = []
    _patch_impls(monkeypatch, fail_at="assets", calls=calls, codes={"assets": 2})

    from lvs import run as run_mod

    code = run_mod.run_pipeline(_KeepCfg(), ws, _KeepArgs(keep_going=False, skip_gates=True))
    assert code == 2
    assert "voice" not in calls, "硬失败默认应停下"


class _KeepCfg:
    def __init__(self, **kw):
        self._d = kw

    def get(self, key, default=None):
        return self._d.get(key, default)

    def has(self, key):
        return key in self._d
# ---- 账本损坏 / 原子写（2026-10-05 补，票据 47 同族）--------------------------
#
# ★ 旧实现的两处叠加伤害：① `save` 用 `write_text`（先截断再写）→ 中断留半截 JSON；
#   ② `load` 读到坏 JSON **静默**退空账本，而 `save` 一落盘就把原文覆盖没了。
#   结果是「6 道门全部重审」而没人知道为什么。下面三条钉死修复后的语义。


def test_corrupt_ledger_is_quarantined_not_silently_rebuilt(ws: _Ws, capsys):
    """★ 坏账本必须：①大声报 ②原样留证据 ③fail-closed。"""
    _approve_shots(ws)
    ws.path(pl.LEDGER_NAME).write_text("{ not json", encoding="utf-8")
    assert pl.evaluate(ws, pl.BY_ID["G1"], config=_Cfg()).state == pl.STATE_PENDING

    out = capsys.readouterr().out
    assert "损坏" in out, "坏账本必须出声（旧实现在这里完全静默）"
    assert ".corrupt" in out, "要告诉人证据存在哪"
    backup = ws.path(pl.LEDGER_NAME + ".corrupt")
    assert backup.read_text(encoding="utf-8") == "{ not json", "损坏内容必须原样保留"


def test_corrupt_ledger_self_heals_on_next_save(ws: _Ws):
    """坏账本不该把任务**锁死**：下一次 `save` 写出合法账本。"""
    _approve_shots(ws)
    ws.path(pl.LEDGER_NAME).write_text("{ not json", encoding="utf-8")
    pl.save(ws, pl.load(ws))
    data = json.loads(ws.path(pl.LEDGER_NAME).read_text(encoding="utf-8"))
    assert data["gates"] == {}


def test_ledger_save_is_atomic(ws: _Ws, monkeypatch):
    """写盘中断（os.replace 抛错）→ 账本**保持原内容**（不许变半截）。"""
    _approve_shots(ws)
    before = ws.path(pl.LEDGER_NAME).read_bytes()

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    led = pl.load(ws)
    led.gates.clear()
    try:
        pl.save(ws, led)
    except OSError:
        pass
    assert ws.path(pl.LEDGER_NAME).read_bytes() == before, "账本被写坏（非原子）"
    assert not list(ws.dir.glob("*.tmp")), "半成品临时文件没清掉"


def test_ledger_version_survives_hand_edited_garbage(ws: _Ws):
    """手改成 `"version": "v2"` → 不许崩（读不了账本 = 门禁全线不可用）。"""
    ws.path(pl.LEDGER_NAME).write_text('{"version": "v2", "gates": {}}', encoding="utf-8")
    assert pl.load(ws).gates == {}
