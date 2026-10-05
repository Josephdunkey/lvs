"""`lvs/llm_provider.py` + provider 路由的单元测试 —— **全程离线，一次都不联网**。

三层覆盖：

1. **路由决策**：总开关（`LVS_LLM_PROVIDER` / `[providers].mode`）> 单点覆盖
   (`[providers.routes]`) > 默认表 > `remote`（没声明的一律不切）；
2. **接线**：`LLMClient.from_config(config, site=...)` 真的把 base_url/model 换成
   ollama / DeepSeek，并带上 `provider`（记账用）；
3. **端到端**（假传输层抓 URL）：BGM 风格 → `localhost:11434`；投稿润色 → `11434`；
   拆镜 `_llm_enrich` → 远程；且本地那次在 runlog 里 `usd == 0`、`saved_usd > 0`。

本地模型真调**只在** `docs`/报告里做过一次（验证可用）；测试不依赖 ollama 在线。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lvs import bgm, llm, llm_cache, llm_cost, llm_provider, publish, shots
from lvs.config import Config
from lvs.workspace import Workspace

LVSDIR = Path(__file__).resolve().parent.parent / "lvs"


# ---- 公共替身 ----------------------------------------------------------------


class _Resp:
    def __init__(self, status_code: int, body: str, payload=None) -> None:
        self.status_code = status_code
        self.text = body
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _reply(text: str, usage: dict | None = None) -> dict:
    body: dict = {"choices": [{"message": {"content": text}}]}
    if usage is not None:
        body["usage"] = usage
    return body


def _transport(replies: list):
    """假传输层：依次吐出 replies，并记录 `url` 等调用参数。"""
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
def _clean(monkeypatch):
    monkeypatch.delenv(llm_provider.ENV_MODE, raising=False)
    llm.set_transport(None)
    llm_cache.reset()
    llm_cost.reset()
    yield
    llm.set_transport(None)
    llm_cache.reset()
    llm_cost.reset()


def _remote_cfg(**extra) -> Config:
    """一份"配了 DeepSeek"的配置（默认路由下这仍是常见形态）。"""
    data: dict = {
        "app": {
            "openai_api_key": "sk-test",
            "openai_base_url": "https://api.deepseek.com/v1",
            "openai_model_name": "deepseek-chat",
        }
    }
    data.update(extra)
    return Config(data, None)


def _urls(fake) -> list[str]:
    return [c["url"] for c in fake.calls]


# ---- 1. 路由决策 --------------------------------------------------------------


def test_default_table_routes_low_risk_calls_to_local() -> None:
    """BGM 风格 / 投稿文案 / 素材打标 = 低风险 → 默认本地。"""
    for site in (llm_provider.SITE_BGM_STYLE, llm_provider.SITE_PUBLISH_META,
                 llm_provider.SITE_ASSETS_TAGGING):
        assert llm_provider.route_for(site) == llm_provider.PROVIDER_LOCAL, site


def test_default_table_keeps_sensitive_and_unknown_calls_remote() -> None:
    """拆镜两处 + **任何没登记的名字** → 远程（保守默认，不偷偷切换）。"""
    for site in (llm_provider.SITE_SHOTS_BEATS, llm_provider.SITE_SHOTS_PROMPTS,
                 "", "some.new.site"):
        assert llm_provider.route_for(site) == llm_provider.PROVIDER_REMOTE, site


def test_mode_remote_rolls_everything_back_to_remote() -> None:
    """★ 一键回滚：`mode = "remote"` = P2 之前的现状（连 BGM 也走远程）。"""
    cfg = Config({"providers": {"mode": "remote"}}, None)
    assert llm_provider.route_for(llm_provider.SITE_BGM_STYLE, cfg) == "remote"
    assert llm_provider.route_for(llm_provider.SITE_PUBLISH_META, cfg) == "remote"
    assert set(llm_provider.route_table(cfg).values()) == {"remote"}


def test_mode_local_sends_even_the_shots_calls_local() -> None:
    cfg = Config({"providers": {"mode": "local"}}, None)
    assert llm_provider.route_for(llm_provider.SITE_SHOTS_BEATS, cfg) == "local"


def test_env_var_beats_the_config_mode(monkeypatch) -> None:
    cfg = Config({"providers": {"mode": "local"}}, None)
    monkeypatch.setenv(llm_provider.ENV_MODE, "remote")
    assert llm_provider.mode(cfg) == "remote"
    assert llm_provider.route_for(llm_provider.SITE_BGM_STYLE, cfg) == "remote"


def test_bad_mode_and_bad_override_fall_back_to_auto(monkeypatch) -> None:
    monkeypatch.setenv(llm_provider.ENV_MODE, "gpt5")     # 非法 → 当 auto
    assert llm_provider.mode(_remote_cfg()) == llm_provider.MODE_AUTO
    cfg = Config({"providers": {"routes": {llm_provider.SITE_BGM_STYLE: "maybe"}}}, None)
    assert llm_provider.route_for(llm_provider.SITE_BGM_STYLE, cfg) == "local"


def test_routes_table_overrides_a_single_site() -> None:
    """单点覆盖：只把 BGM 挪回远程，投稿文案仍走本地。"""
    cfg = Config({"providers": {"routes": {"bgm.style": "remote"}}}, None)
    assert llm_provider.route_for(llm_provider.SITE_BGM_STYLE, cfg) == "remote"
    assert llm_provider.route_for(llm_provider.SITE_PUBLISH_META, cfg) == "local"


def test_route_table_covers_every_known_site() -> None:
    table = llm_provider.route_table()
    assert table[llm_provider.SITE_SHOTS_PROMPTS] == "remote"
    assert table[llm_provider.SITE_ASSETS_TAGGING] == "local"


# ---- 2. 两条腿的连接参数 ------------------------------------------------------


def test_local_provider_defaults_point_at_ollama() -> None:
    prov = llm_provider.local_provider()
    assert prov.base_url == "http://localhost:11434/v1"
    assert prov.model == "qwen2.5:7b-instruct-q4_K_M"
    assert prov.api_key == "ollama"          # ollama 不校验，但 header 要有值
    assert prov.is_local is True
    assert "api_key" not in prov.as_dict(), "对外只给 name/base_url/model"


def test_local_provider_reads_overrides() -> None:
    cfg = Config({"providers": {"local": {"base_url": "http://127.0.0.1:9999/v1/",
                                          "model": "tiny", "api_key": "k"}}}, None)
    prov = llm_provider.local_provider(cfg)
    assert (prov.base_url, prov.model, prov.api_key) == ("http://127.0.0.1:9999/v1", "tiny", "k")


def test_remote_provider_falls_back_to_the_app_section() -> None:
    """★ `[providers.remote]` 留空 = 沿用 `[app]`（密钥不搬家）。"""
    prov = llm_provider.remote_provider(_remote_cfg())
    assert prov.base_url == "https://api.deepseek.com/v1"
    assert prov.model == "deepseek-chat"
    assert prov.api_key == "sk-test"


def test_remote_provider_defaults_without_any_config() -> None:
    prov = llm_provider.remote_provider(Config({}, None))
    assert prov.base_url == "https://api.openai.com/v1"
    assert prov.model == "gpt-4o-mini"


# ---- 3. LLMClient.from_config 的接线 ------------------------------------------


def test_from_config_local_site_needs_no_key_and_hits_ollama() -> None:
    """★ 本地腿不需要远程 key，地址/模型都指 ollama，并带上 provider=local。"""
    client = llm.LLMClient.from_config(Config({}, None),
                                       site=llm_provider.SITE_BGM_STYLE)
    assert client.provider == "local"
    assert client.base_url == "http://localhost:11434/v1"
    assert client.model == "qwen2.5:7b-instruct-q4_K_M"
    # 本地腿记"省了多少"要一个远程对照模型（默认 gpt-4o-mini）
    assert client.remote_model == "gpt-4o-mini"


def test_from_config_remote_site_uses_deepseek_and_requires_key() -> None:
    client = llm.LLMClient.from_config(_remote_cfg(), site=llm_provider.SITE_SHOTS_BEATS)
    assert (client.provider, client.base_url, client.model) == (
        "remote", "https://api.deepseek.com/v1", "deepseek-chat")
    assert client.remote_model == "", "远程腿不需要对照模型"
    with pytest.raises(llm.LLMError) as ei:
        llm.LLMClient.from_config(Config({}, None), site=llm_provider.SITE_SHOTS_BEATS)
    assert "openai_api_key" in str(ei.value)


def test_from_config_without_site_stays_remote() -> None:
    """★ 不声明 site = 远程 = 与 P2 之前一字不差（老调用方零改动）。"""
    client = llm.LLMClient.from_config(_remote_cfg())
    assert (client.provider, client.base_url, client.model) == (
        "remote", "https://api.deepseek.com/v1", "deepseek-chat")


def test_provider_for_helper_matches_resolve() -> None:
    cfg = _remote_cfg()
    assert llm.provider_for("bgm.style", cfg).name == "local"
    assert llm.provider_for("shots.beats", cfg).name == "remote"


def test_available_is_still_gated_by_the_remote_key() -> None:
    """★ 没 key = 没配 LLM（照旧走规则），路由**不**改变这个判断。"""
    assert llm.available(Config({}, None)) is False
    assert llm.available(Config({}, None), site="bgm.style") is False
    assert llm.available(_remote_cfg(), site="bgm.style") is True


def test_available_is_true_without_key_only_in_explicit_local_mode() -> None:
    cfg = Config({"providers": {"mode": "local"}}, None)
    assert llm.available(cfg) is True


def test_every_production_call_site_declares_a_site() -> None:
    """★ 生产调用点必须显式声明归属：漏了就会悄悄退回远程（回归防线）。"""
    offenders: list[str] = []
    for path in sorted(LVSDIR.glob("*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "LLMClient.from_config(" in line and "def from_config" not in line \
                    and "site=" not in line:
                offenders.append(f"{path.name}:{i}: {line.strip()}")
    assert offenders == [], f"这些调用点没声明 site：{offenders}"


# ---- 4. 端到端：假传输层抓 URL -------------------------------------------------


def test_bgm_style_call_goes_to_ollama() -> None:
    """★ 构造一次 BGM 风格调用 → 断言真的打到 11434（而不是 DeepSeek）。"""
    fake = _transport([_Resp(200, "ok", _reply(json.dumps({
        "genre": "noir jazz", "instruments": "double bass", "mood": "sultry",
        "bpm": 88, "prompt": "slow noir jazz, no vocals",
    })))])
    llm.set_transport(fake)
    plan = bgm.build_prompt("他念了一整夜的经，输给了一首诗。", _remote_cfg(), duration=60.0)
    assert plan.source == "llm"
    assert _urls(fake) == ["http://localhost:11434/v1/chat/completions"]


def test_publish_polish_goes_to_ollama() -> None:
    meta = {
        "book": {"title": "雨月物语"},
        "cover": {"lines": ["甲", "乙"]},
        "bilibili": {"title_candidates": ["旧标题"], "recommended_title": "旧标题",
                     "description": "旧简介", "tags": ["旧标签"]},
        "douyin": {"hashtags": ["#旧标签"]},
    }
    fake = _transport([_Resp(200, "ok", _reply(json.dumps(
        {"titles": ["新标题"], "description": "新简介", "tags": ["新标签"]},
        ensure_ascii=False)))])
    llm.set_transport(fake)
    out, note = publish.polish_with_llm(meta, _remote_cfg())
    assert "已用 LLM 润色" in note
    assert out["bilibili"]["recommended_title"] == "新标题"
    assert _urls(fake) == ["http://localhost:11434/v1/chat/completions"]


def test_shots_enrich_call_goes_remote(tmp_path: Path) -> None:
    """★ 构造一次拆镜调用（`_llm_enrich`）→ 断言走远程，没被本地路由吃掉。"""
    cfg = _remote_cfg()
    fake = _transport([_Resp(200, "ok", _reply(json.dumps({"shots": [
        {"id": 1, "beat": 0, "kind": "scene", "scene": "a wall",
         "prompt": "a wall", "keywords": ["wall"], "source": "local"},
    ]}, ensure_ascii=False)))])
    llm.set_transport(fake)
    client = llm.LLMClient.from_config(cfg, site=llm_provider.SITE_SHOTS_BEATS)
    ws = Workspace(task="t", root=tmp_path).ensure()
    shot_list = [{"id": 1, "segment": 1, "narration": "旁白一。", "visual": "墙",
                  "kind": "scene", "segment_heading": "H"}]
    shots._llm_enrich(client, shot_list, ws, {1: [{"text": "墙", "kind": None}]})
    assert _urls(fake) == ["https://api.deepseek.com/v1/chat/completions"]
    assert shot_list[0]["prompt"].startswith("a wall")   # 后面还会按风格补后缀


# ---- 5. 记账：本地记 0，并算出"省了多少" --------------------------------------


def test_local_call_costs_zero_and_records_the_saving(tmp_path: Path) -> None:
    ws = Workspace(task="t", root=tmp_path).ensure()
    cfg = _remote_cfg()
    llm.configure(ws=ws, stage="bgm", config=cfg)
    llm.set_transport(_transport([_Resp(200, "ok", _reply(
        "{}", {"prompt_tokens": 1000, "completion_tokens": 200}))]))
    llm.LLMClient.from_config(cfg, site=llm_provider.SITE_BGM_STYLE).chat(
        [{"role": "user", "content": "hi"}])
    event = llm_cost.read_events(ws)[0]
    assert event["provider"] == "local"
    assert event["usd"] == 0.0
    assert event["saved_usd"] > 0.0        # 同样的 token 走 deepseek-chat 要花钱
    agg = llm_cost.totals(llm_cost.read_events(ws))
    assert (agg["local_calls"], agg["remote_calls"]) == (1, 0)
    assert agg["usd"] == 0.0 and agg["by_provider"]["local"]["saved_usd"] > 0


def test_remote_call_cost_is_unchanged(tmp_path: Path) -> None:
    ws = Workspace(task="t", root=tmp_path).ensure()
    cfg = _remote_cfg()
    llm.configure(ws=ws, stage="shots", config=cfg)
    llm.set_transport(_transport([_Resp(200, "ok", _reply(
        "{}", {"prompt_tokens": 1000, "completion_tokens": 200}))]))
    llm.LLMClient.from_config(cfg, site=llm_provider.SITE_SHOTS_BEATS).chat(
        [{"role": "user", "content": "hi"}])
    event = llm_cost.read_events(ws)[0]
    assert event["provider"] == "remote"
    assert event["usd"] == pytest.approx(llm_cost.usd_for("deepseek-chat", 1000, 200))
    assert event["saved_usd"] == 0.0


def test_local_reserve_never_trips_the_budget_cap(tmp_path: Path) -> None:
    """本地腿免费 → 不预算、不拦（拦一条免费的调用没有意义）。"""
    ws = Workspace(task="t", root=tmp_path).ensure()
    cap_cfg = Config({"budget": {"mode": "cap", "cap_usd": 0.000001}}, None)
    llm_cost.configure(ws=ws, config=cap_cfg)
    msgs = [{"role": "user", "content": "很长" * 5000}]
    assert llm_cost.reserve(msgs, "qwen2.5:7b-instruct-q4_K_M", provider="local") == 0.0
    with pytest.raises(llm_cost.BudgetExceeded):
        llm_cost.reserve(msgs, "deepseek-chat", provider="remote")


def test_totals_splits_by_provider() -> None:
    agg = llm_cost.totals([
        {"stage": "bgm", "provider": "local", "prompt_tokens": 10,
         "completion_tokens": 2, "usd": 0.0, "saved_usd": 0.01},
        {"stage": "shots", "provider": "remote", "prompt_tokens": 20,
         "completion_tokens": 4, "usd": 0.02},
    ])
    assert agg["usd"] == pytest.approx(0.02)
    assert agg["saved_usd"] == pytest.approx(0.01)
    assert (agg["local_calls"], agg["remote_calls"]) == (1, 1)
    assert agg["by_provider"]["remote"]["calls"] == 1


def test_cost_text_and_json_report_the_routing() -> None:
    rows = [llm_cost.totals([
        {"stage": "bgm", "provider": "local", "prompt_tokens": 10,
         "completion_tokens": 2, "usd": 0.0, "saved_usd": 0.0123},
    ])]
    rows[0]["task"] = "CX"
    rows[0]["cache_hits"] = 0
    rows[0]["budget_text"] = "observe（只记不拦）"
    text = llm_cost.format_text(rows)
    assert "路由：本地 1 次（省估算 $0.0123）/ 远程 0 次" in text


def test_cache_keys_differ_between_the_two_legs() -> None:
    """缓存键含 base_url —— 本地与远程各存一份，不会互相串味。"""
    payload = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    local = llm_provider.local_provider().base_url
    remote = llm_provider.remote_provider().base_url
    assert llm_cache.key_for(local, payload) != llm_cache.key_for(remote, payload)