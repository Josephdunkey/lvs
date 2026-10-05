"""LLM 成本记账与预算闸门（`lvs/llm_cost.py`，P1 / B5 / T5）。

## 这组守什么

1. **估 → 留 → 对**：调用前按字符粗估、`cap` 模式下先拦；响应回来按真实
   `usage` 记账（钱花了就不抛，抛了只会把响应丢掉）；
2. **缺省保守**：没有 `[budget]` 段 = `observe`（只记不拦），与 OpenMontage 的
   默认一致；
3. **可观测**：每次调用落一条 `llm_cost` 事件，`lvs cost` 能按阶段汇总；
4. **不改变主路径**：没 `configure(ws=...)` 时不记账、不落盘、不抛。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lvs import llm, llm_cost, llm_cache
from lvs.config import Config
from lvs.errors import EXIT_BLOCKED, BlockedError, LvsError
from lvs.llm import LLMClient
from lvs.workspace import Workspace

MSGS = [{"role": "user", "content": "把这段话拆成 3 个分镜"}]
#: 够长 = 预估费用有明确量级（1000 tok ≈ $0.00027），阈值测试不必抠浮点
MSGS_LONG = [{"role": "user", "content": "x" * 4000}]


class _Resp:
    def __init__(self, status_code: int, body: str, payload=None) -> None:
        self.status_code = status_code
        self.text = body
        self._payload = payload

    def json(self):
        return self._payload


def _reply(text: str, usage: dict | None = None) -> dict:
    body: dict = {"choices": [{"message": {"content": text}}]}
    if usage is not None:
        body["usage"] = usage
    return body


def _transport(replies: list):
    calls: list[dict] = []

    def post(url, **kw):
        calls.append({"url": url, **kw})
        item = replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    post.calls = calls  # type: ignore[attr-defined]
    return post


@pytest.fixture(autouse=True)
def _clean():
    llm.set_transport(None)
    llm_cache.reset()
    llm_cost.reset()
    yield
    llm.set_transport(None)
    llm_cache.reset()
    llm_cost.reset()


def _client() -> LLMClient:
    return LLMClient("https://api.example.com/v1", "sk-test", "deepseek-chat")


def _config(tmp_path: Path, body: str) -> Config:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return Config.load(path)


# ---- `[budget]` 段 -----------------------------------------------------------


def test_defaults_to_observe(tmp_path: Path) -> None:
    """★ 缺省保守：不配 `[budget]` = observe（只记不拦）。"""
    cfg = _config(tmp_path, "[app]\nopenai_api_key = \"x\"\n")
    b = llm_cost.Budget.from_config(cfg)
    assert (b.mode, b.cap_usd, b.per_action_usd) == (llm_cost.MODE_OBSERVE, 0.0, 0.0)
    assert not b.enforcing


def test_reads_the_section(tmp_path: Path) -> None:
    cfg = _config(tmp_path, """
[budget]
mode = "cap"
cap_usd = 2.5
per_action_usd = 0.05
price_in_per_1m = 0.1
price_out_per_1m = 0.2
""")
    b = llm_cost.Budget.from_config(cfg)
    assert b.mode == llm_cost.MODE_CAP
    assert (b.cap_usd, b.per_action_usd) == (2.5, 0.05)
    assert b.enforcing
    assert llm_cost.price_for("whatever", b) == (0.1, 0.2, "config")


def test_bad_mode_falls_back_to_observe(tmp_path: Path) -> None:
    cfg = _config(tmp_path, "[budget]\nmode = \"yolo\"\ncap_usd = 1\n")
    assert llm_cost.Budget.from_config(cfg).mode == llm_cost.MODE_OBSERVE


def test_broken_values_do_not_crash(tmp_path: Path) -> None:
    cfg = _config(tmp_path, "[budget]\nmode = \"cap\"\ncap_usd = \"很多\"\n")
    b = llm_cost.Budget.from_config(cfg)
    assert b.cap_usd == 0.0 and b.mode == llm_cost.MODE_CAP


def test_no_config_is_observe() -> None:
    assert llm_cost.Budget.from_config(None).mode == llm_cost.MODE_OBSERVE


def test_describe_mentions_every_knob() -> None:
    text = llm_cost.Budget(mode="cap", cap_usd=3.0, per_action_usd=0.5).describe()
    assert "cap=$3.00" in text and "单次=$0.50" in text and "observe" not in text


# ---- 价格与估算 --------------------------------------------------------------


def test_usd_for_known_model() -> None:
    assert llm_cost.usd_for("deepseek-chat", 1_000_000, 1_000_000) == pytest.approx(1.37)


def test_unknown_model_uses_default_price() -> None:
    p_in, p_out, source = llm_cost.price_for("some-new-model")
    assert (p_in, p_out) == llm_cost.DEFAULT_PRICE and source == "default"


def test_prefix_match_on_model_name() -> None:
    assert llm_cost.price_for("deepseek-chat-0324")[2] == "table"


def test_estimate_tokens_from_chars() -> None:
    assert llm_cost.estimate_tokens([{"role": "user", "content": "x" * 400}]) == 100
    assert llm_cost.estimate_tokens([]) == 1
    assert llm_cost.estimate_tokens([{"role": "user"}]) == 1


# ---- 估 / 留（cap 硬拦）------------------------------------------------------


def test_reserve_is_a_noop_in_observe() -> None:
    llm_cost.configure(ws=None, config=None)
    assert llm_cost.reserve(MSGS, "deepseek-chat") >= 0.0


def test_reserve_blocks_over_per_action(tmp_path: Path) -> None:
    cfg = _config(tmp_path, "[budget]\nmode = \"cap\"\nper_action_usd = 0.0001\n")
    llm_cost.configure(config=cfg)
    with pytest.raises(llm_cost.BudgetExceeded) as exc:
        llm_cost.reserve(MSGS_LONG, "deepseek-chat")
    assert "per_action_usd" in str(exc.value)


def test_reserve_blocks_over_cap(tmp_path: Path) -> None:
    cfg = _config(tmp_path, "[budget]\nmode = \"cap\"\ncap_usd = 0.0001\n")
    llm_cost.configure(config=cfg)
    with pytest.raises(llm_cost.BudgetExceeded) as exc:
        llm_cost.reserve(MSGS_LONG, "deepseek-chat")
    assert "cap_usd" in str(exc.value)


def test_reserve_allows_room_left(tmp_path: Path) -> None:
    cfg = _config(tmp_path, "[budget]\nmode = \"cap\"\ncap_usd = 100\nper_action_usd = 5\n")
    llm_cost.configure(config=cfg)
    assert llm_cost.reserve(MSGS, "deepseek-chat") > 0


def test_budget_exceeded_is_a_blocked_lvs_error() -> None:
    """预算超了是**要人拍板**（Blocked，退出码 3），不是输入错。"""
    assert issubclass(llm_cost.BudgetExceeded, (BlockedError, LvsError, RuntimeError))
    assert llm_cost.BudgetExceeded.exit_code == EXIT_BLOCKED


# ---- 对账（记账 + 事件）------------------------------------------------------


def test_record_accumulates_and_writes_an_event(tmp_path: Path) -> None:
    ws = Workspace(task="t", root=tmp_path).ensure()
    llm_cost.configure(ws=ws, config=None)
    usd = llm_cost.record("deepseek-chat", {"prompt_tokens": 1000, "completion_tokens": 500},
                          estimated_usd=0.001, stage="shots")
    assert usd == pytest.approx((1000 * 0.27 + 500 * 1.10) / 1_000_000)
    events = llm_cost.read_events(ws)
    assert len(events) == 1
    assert events[0]["event"] == "llm_cost"
    assert events[0]["stage"] == "shots"
    assert events[0]["prompt_tokens"] == 1000
    assert events[0]["usd"] == pytest.approx(usd, abs=1e-6)
    assert events[0]["estimated_usd"] == 0.001
    assert llm_cost.state().calls == 1


def test_record_without_ws_is_a_noop(tmp_path: Path) -> None:
    llm_cost.reset()
    llm_cost.record("m", {"prompt_tokens": 10, "completion_tokens": 1})
    assert llm_cost.state().spent_usd == 0.0


def test_totals_group_by_stage() -> None:
    rows = [
        {"stage": "shots", "prompt_tokens": 10, "completion_tokens": 2, "usd": 0.01},
        {"stage": "shots", "prompt_tokens": 20, "completion_tokens": 4, "usd": 0.02},
        {"stage": "publish", "prompt_tokens": 5, "completion_tokens": 1, "usd": 0.005},
    ]
    agg = llm_cost.totals(rows)
    assert (agg["calls"], agg["prompt_tokens"], agg["completion_tokens"]) == (3, 35, 7)
    assert agg["usd"] == pytest.approx(0.035)
    assert agg["by_stage"]["shots"]["calls"] == 2
    assert agg["by_stage"]["publish"]["usd"] == pytest.approx(0.005)


def test_warn_mode_emits_over_event_once(tmp_path: Path) -> None:
    cfg = _config(tmp_path, "[budget]\nmode = \"warn\"\ncap_usd = 0.0001\n")
    ws = Workspace(task="t", root=tmp_path).ensure()
    llm_cost.configure(ws=ws, config=cfg)
    llm_cost.record("deepseek-chat", {"prompt_tokens": 10_000, "completion_tokens": 10_000},
                    stage="shots")
    llm_cost.record("deepseek-chat", {"prompt_tokens": 10_000, "completion_tokens": 10_000},
                    stage="shots")
    over = llm_cost.read_events(ws, llm_cost.OVER_EVENT)
    assert len(over) == 1, "只该告警一次（不是每次调用都刷屏）"
    assert over[0]["cap_usd"] == 0.0001


def test_observe_mode_never_emits_over_event(tmp_path: Path) -> None:
    cfg = _config(tmp_path, "[budget]\ncap_usd = 0.0001\n")   # mode 缺省 = observe
    ws = Workspace(task="t", root=tmp_path).ensure()
    llm_cost.configure(ws=ws, config=cfg)
    llm_cost.record("deepseek-chat", {"prompt_tokens": 10_000, "completion_tokens": 10_000})
    assert llm_cost.read_events(ws, llm_cost.OVER_EVENT) == []
    assert llm_cost.state().over is False


def test_record_never_raises_when_over_cap(tmp_path: Path) -> None:
    """钱已经花了 —— 对账这一步抛错只会把响应丢掉。cap 的拦截在 reserve。"""
    cfg = _config(tmp_path, "[budget]\nmode = \"cap\"\ncap_usd = 0.0001\n")
    ws = Workspace(task="t", root=tmp_path).ensure()
    llm_cost.configure(ws=ws, config=cfg)
    assert llm_cost.record("deepseek-chat",
                           {"prompt_tokens": 9_999, "completion_tokens": 9_999}) > 0


def test_cache_hit_counter() -> None:
    llm_cost.note_cached()
    llm_cost.note_cached()
    assert llm_cost.state().cache_hits == 2


def test_read_events_skips_bad_lines(tmp_path: Path) -> None:
    ws = Workspace(task="t", root=tmp_path).ensure()
    logs = ws.path("logs")
    (logs / "run-20260101.jsonl").write_text(
        "{not json\n"
        + json.dumps({"event": "stage_end", "stage": "shots"}) + "\n"
        + json.dumps({"event": "llm_cost", "stage": "shots", "usd": 0.5,
                      "prompt_tokens": 1, "completion_tokens": 1}) + "\n",
        encoding="utf-8",
    )
    events = llm_cost.read_events(ws)
    assert len(events) == 1 and events[0]["usd"] == 0.5
    assert llm_cost.cache_hits_of(ws) == 0


def test_cache_hits_of_counts_hit_events(tmp_path: Path) -> None:
    ws = Workspace(task="t", root=tmp_path).ensure()
    (ws.path("logs") / "run-20260101.jsonl").write_text(
        json.dumps({"event": "llm_cache_hit", "stage": "shots"}) + "\n", encoding="utf-8")
    assert llm_cost.cache_hits_of(ws) == 1


# ---- 与 LLMClient 的接线 ------------------------------------------------------


def test_cap_blocks_before_any_request(tmp_path: Path) -> None:
    """★ cap 硬拦：**发请求之前**就拒绝，一次都不花。"""
    cfg = _config(tmp_path, "[budget]\nmode = \"cap\"\nper_action_usd = 0.0001\n")
    ws = Workspace(task="t", root=tmp_path).ensure()
    llm.configure(ws=ws, stage="shots", config=cfg)
    fake = _transport([_Resp(200, "ok", _reply("A"))])
    llm.set_transport(fake)
    with pytest.raises(llm_cost.BudgetExceeded):
        _client().chat(MSGS_LONG)
    assert fake.calls == [], "被拦下就不该发请求"


def test_successful_call_records_usage(tmp_path: Path) -> None:
    """★ 估 → 对：响应里的真实 usage 落进 runlog，`lvs cost` 才读得到。"""
    ws = Workspace(task="t", root=tmp_path).ensure()
    llm.configure(ws=ws, stage="shots", config=None)
    llm.set_transport(_transport([
        _Resp(200, "ok", _reply("A", {"prompt_tokens": 30, "completion_tokens": 7})),
    ]))
    _client().chat(MSGS)
    events = llm_cost.read_events(ws)
    assert len(events) == 1
    assert events[0]["stage"] == "shots"
    assert (events[0]["prompt_tokens"], events[0]["completion_tokens"]) == (30, 7)


def test_call_without_configure_records_nothing(tmp_path: Path) -> None:
    llm.set_transport(_transport([_Resp(200, "ok", _reply("A", {"prompt_tokens": 5}))]))
    assert _client().chat(MSGS) == "A"
    assert llm_cost.state().calls == 0


# ---- `lvs cost` 命令 ---------------------------------------------------------


def _seed_task(tmp_path: Path, name: str = "CX") -> None:
    ws = Workspace(task=name, root=tmp_path).ensure()
    (ws.path("logs") / "run-20261005.jsonl").write_text(
        "\n".join(json.dumps(row) for row in (
            {"event": "llm_cost", "stage": "shots", "usd": 0.02,
             "prompt_tokens": 100, "completion_tokens": 50},
            {"event": "llm_cost", "stage": "shots", "usd": 0.01,
             "prompt_tokens": 20, "completion_tokens": 5},
            {"event": "llm_cost", "stage": "publish", "usd": 0.005,
             "prompt_tokens": 10, "completion_tokens": 3},
            {"event": "llm_cache_hit", "stage": "shots"},
        )) + "\n",
        encoding="utf-8",
    )


def test_collect_reads_the_task(tmp_path: Path, monkeypatch) -> None:
    _seed_task(tmp_path)
    monkeypatch.setattr(llm_cost, "PROJECT_ROOT", tmp_path)
    rows = llm_cost.collect(None, "CX", False)
    assert len(rows) == 1
    row = rows[0]
    assert row["task"] == "CX"
    assert row["calls"] == 3
    assert row["cache_hits"] == 1
    assert row["usd"] == pytest.approx(0.035)
    assert sorted(row["by_stage"]) == ["publish", "shots"]


def test_collect_missing_task_is_empty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(llm_cost, "PROJECT_ROOT", tmp_path)
    assert llm_cost.collect(None, "NOPE", False) == []


def test_format_text_mentions_task_and_budget(tmp_path: Path, monkeypatch) -> None:
    _seed_task(tmp_path)
    monkeypatch.setattr(llm_cost, "PROJECT_ROOT", tmp_path)
    text = llm_cost.format_text(llm_cost.collect(None, "CX", False))
    assert "CX" in text and "预算：" in text and "$0.0350" in text


def test_format_text_when_empty() -> None:
    assert "先跑" in llm_cost.format_text([])


def test_run_command_json(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_task(tmp_path)
    monkeypatch.setattr(llm_cost, "PROJECT_ROOT", tmp_path)
    code = llm_cost.run_command(None, SimpleNamespace(task="CX", json=True, all=False))
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_usd"] == pytest.approx(0.035)
    assert payload["tasks"][0]["task"] == "CX"


def test_run_command_text(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_task(tmp_path)
    monkeypatch.setattr(llm_cost, "PROJECT_ROOT", tmp_path)
    assert llm_cost.run_command(None, SimpleNamespace(task="CX", json=False, all=True)) == 0
    assert "shots:2/$0.0300" in capsys.readouterr().out
