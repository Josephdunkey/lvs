"""LLM 内容寻址缓存（`lvs/llm_cache.py`，P1 / B10）。

## 这组守什么

1. **不改参数重跑 = 命中**（不真调 LLM，传输层调用数 0 次增长）；
2. **改一个字就重算**（提示词 / temperature / 模型 / base_url 任一变化 → 键变）；
3. **默认路径不变**：没 `configure(ws=...)` 的进程（既有全部单测与临时脚本）
   照旧每次都真调，**不读也不写**缓存；
4. **缓存是增强**：错的响应不缓存、写盘失败不连累主流程、命中留 runlog 痕迹。

全部离线：走 `llm.set_transport()` 注入假传输层，不联网。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lvs import llm, llm_cache, runlog
from lvs.llm import LLMClient, LLMError, chat_json
from lvs.workspace import Workspace

MSGS = [{"role": "user", "content": "把这段话拆成 3 个分镜"}]


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
    """每个用例前后都把注入点与上下文清回默认，杜绝串味。"""
    llm.set_transport(None)
    llm_cache.reset()
    yield
    llm.set_transport(None)
    llm_cache.reset()


def _client(**kw) -> LLMClient:
    kw.setdefault("base_url", "https://api.example.com/v1")
    kw.setdefault("api_key", "sk-test")
    kw.setdefault("model", "m1")
    return LLMClient(**kw)


def _wire(tmp_path: Path, replies: list, *, stage: str = "shots"):
    """接上缓存上下文 + 假传输层，返回 (ws, client, 假传输层)。"""
    ws = Workspace(task="t", root=tmp_path)
    llm_cache.configure(ws=ws, stage=stage, config=None)
    fake = _transport(replies)
    llm.set_transport(fake)
    return ws, _client(), fake


def _cache_files(ws: Workspace) -> list[Path]:
    d = ws.path("llm-cache")
    return sorted(d.glob("*.txt")) if d.is_dir() else []


# ---- 键：内容寻址的判据 -------------------------------------------------------


def test_key_is_stable_for_the_same_payload() -> None:
    payload = {"model": "m", "messages": MSGS, "temperature": 0.3}
    assert llm_cache.key_for("https://x/v1", payload) == llm_cache.key_for("https://x/v1", payload)


def test_key_ignores_dict_key_order() -> None:
    a = llm_cache.key_for("https://x/v1", {"model": "m", "temperature": 0.3})
    b = llm_cache.key_for("https://x/v1", {"temperature": 0.3, "model": "m"})
    assert a == b, "键必须只看内容，不看字典顺序（否则白花钱）"


@pytest.mark.parametrize(
    "changed",
    [
        {"model": "m2"},
        {"messages": [{"role": "user", "content": "改一个字"}]},
        {"temperature": 0.7},
        {"max_tokens": 100},          # 其它透传参数也要进键
    ],
)
def test_key_changes_with_every_parameter(changed: dict) -> None:
    base = {"model": "m", "messages": MSGS, "temperature": 0.3}
    assert llm_cache.key_for("https://x/v1", base) != llm_cache.key_for("https://x/v1", {**base, **changed})


def test_key_changes_with_base_url() -> None:
    payload = {"model": "m", "messages": MSGS, "temperature": 0.3}
    assert llm_cache.key_for("https://x/v1", payload) != llm_cache.key_for("https://y/v1", payload)


def test_trailing_slash_is_normalised() -> None:
    payload = {"model": "m", "messages": MSGS}
    assert llm_cache.key_for("https://x/v1/", payload) == llm_cache.key_for("https://x/v1", payload)


# ---- 命中 / 未命中（核心判据）-------------------------------------------------


def test_second_identical_call_hits_cache(tmp_path: Path) -> None:
    """★ 判据：不改参数重跑 → 命中，LLM 调用数 0 增长。"""
    ws, client, fake = _wire(tmp_path, [_Resp(200, "ok", _reply("答案"))])

    assert client.chat(MSGS) == "答案"
    assert client.chat(MSGS) == "答案"

    assert len(fake.calls) == 1, "第二次不该再发请求"
    assert llm_cache.stats() == {"hits": 1, "misses": 1, "writes": 1}
    assert len(_cache_files(ws)) == 1
    assert _cache_files(ws)[0].read_text(encoding="utf-8") == "答案"


def test_chat_json_hits_cache_and_resumes_from_disk(tmp_path: Path) -> None:
    """`chat_json` 走同一条路；且新进程（重放上下文）也能读回磁盘上的缓存。"""
    ws, client, fake = _wire(tmp_path, [_Resp(200, "ok", _reply('{"a": 1}'))])
    assert chat_json(client, MSGS) == {"a": 1}
    assert len(fake.calls) == 1

    llm.set_transport(_transport([OSError("不该再发请求")]))   # 第二次若真调会炸
    assert chat_json(client, MSGS) == {"a": 1}


def test_changing_one_word_of_the_prompt_misses(tmp_path: Path) -> None:
    """★ 判据：改一个提示词 → 必重算。"""
    ws, client, fake = _wire(tmp_path, [
        _Resp(200, "ok", _reply("A")),
        _Resp(200, "ok", _reply("B")),
    ])
    assert client.chat(MSGS) == "A"
    assert client.chat([{"role": "user", "content": "把这段话拆成 4 个分镜"}]) == "B"
    assert len(fake.calls) == 2
    assert len(_cache_files(ws)) == 2


def test_changing_temperature_misses(tmp_path: Path) -> None:
    ws, client, fake = _wire(tmp_path, [
        _Resp(200, "ok", _reply("A")),
        _Resp(200, "ok", _reply("B")),
    ])
    client.chat(MSGS, temperature=0.3)
    client.chat(MSGS, temperature=0.9)
    assert len(fake.calls) == 2


def test_hit_is_recorded_in_runlog(tmp_path: Path) -> None:
    """命中要留痕 —— 否则"这次省了钱"没人看得见。"""
    ws, client, _ = _wire(tmp_path, [_Resp(200, "ok", _reply("A"))])
    client.chat(MSGS)
    client.chat(MSGS)
    events = [r.get("event") for r in runlog.read(ws)]
    assert "llm_cache_hit" in events
    row = [r for r in runlog.read(ws) if r.get("event") == "llm_cache_hit"][0]
    assert row["stage"] == "shots"
    assert row["model"] == "m1"
    assert len(row["key"]) == llm_cache.KEY_CHARS


def test_runlog_disabled_silences_cache_events(tmp_path: Path) -> None:
    class _Cfg:
        def get(self, key, default=None):
            return False if key == runlog.CONFIG_KEY else default

    ws = Workspace(task="t", root=tmp_path)
    llm_cache.configure(ws=ws, stage="shots", config=_Cfg())
    llm.set_transport(_transport([_Resp(200, "ok", _reply("A"))]))
    client = _client()
    client.chat(MSGS)
    client.chat(MSGS)
    assert runlog.read(ws) == []


# ---- 逃生阀（不缓存的三条路）--------------------------------------------------


def test_without_configure_nothing_is_cached(tmp_path: Path) -> None:
    """★ 默认路径不变：没 `configure(ws=...)` 就不读不写（既有测试与脚本不受影响）。"""
    fake = _transport([_Resp(200, "ok", _reply("A")), _Resp(200, "ok", _reply("A"))])
    llm.set_transport(fake)
    client = _client()
    client.chat(MSGS)
    client.chat(MSGS)
    assert len(fake.calls) == 2
    assert not (tmp_path / ".work").exists()


def test_env_var_turns_cache_off(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(llm_cache.ENV_KEY, "0")
    ws, client, fake = _wire(tmp_path, [
        _Resp(200, "ok", _reply("A")), _Resp(200, "ok", _reply("A")),
    ])
    client.chat(MSGS)
    client.chat(MSGS)
    assert not llm_cache.enabled()
    assert len(fake.calls) == 2
    assert _cache_files(ws) == [], "关了就不该落盘"


def test_disable_switch_is_an_escape_hatch(tmp_path: Path) -> None:
    ws, client, fake = _wire(tmp_path, [
        _Resp(200, "ok", _reply("A")), _Resp(200, "ok", _reply("A")),
    ])
    llm_cache.disable()
    client.chat(MSGS)
    client.chat(MSGS)
    assert len(fake.calls) == 2
    assert _cache_files(ws) == []
    llm_cache.enable()
    assert llm_cache.enabled()


def test_reset_restores_unconfigured_state(tmp_path: Path) -> None:
    ws, client, _ = _wire(tmp_path, [_Resp(200, "ok", _reply("A"))])
    assert llm_cache.enabled()
    llm_cache.reset()
    assert not llm_cache.enabled()
    assert llm_cache.stats() == {"hits": 0, "misses": 0, "writes": 0}


# ---- 增强不该变成故障点 -------------------------------------------------------


def test_error_response_is_not_cached(tmp_path: Path) -> None:
    ws, client, fake = _wire(tmp_path, [
        _Resp(500, "boom"), _Resp(500, "boom"),
    ])
    with pytest.raises(LLMError):
        client.chat(MSGS)
    with pytest.raises(LLMError):
        client.chat(MSGS)
    assert _cache_files(ws) == [], "错响应不能当缓存值存下来"


def test_broken_response_shape_is_not_cached(tmp_path: Path) -> None:
    ws, client, _ = _wire(tmp_path, [_Resp(200, "{}", {"unexpected": 1})])
    with pytest.raises(LLMError):
        client.chat(MSGS)
    assert _cache_files(ws) == []


def test_unreadable_cache_entry_counts_as_miss(tmp_path: Path) -> None:
    """缓存目录里被塞了个目录（或权限坏）时，退化成未命中而不是抛错。"""
    ws, client, fake = _wire(tmp_path, [_Resp(200, "ok", _reply("A"))])
    key = llm_cache.key_for(client.base_url, {
        "model": client.model, "messages": MSGS, "temperature": 0.3})
    path = ws.path("llm-cache") / f"{key[:llm_cache.KEY_CHARS]}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()                       # 目录 → read_text 抛 OSError
    assert client.chat(MSGS) == "A"
    assert len(fake.calls) == 1


def test_put_failure_is_swallowed(tmp_path: Path, monkeypatch) -> None:
    """写缓存失败（只读盘 / 磁盘满）不许让这次调用失败。"""
    ws = Workspace(task="t", root=tmp_path)
    llm_cache.configure(ws=ws, stage="shots")

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(llm_cache.artifact, "atomic_write_text", boom)
    llm.set_transport(_transport([_Resp(200, "ok", _reply("A"))]))
    assert _client().chat(MSGS) == "A"
    assert llm_cache.stats()["writes"] == 0


def test_complete_returns_usage_and_chat_delegates(tmp_path: Path) -> None:
    """`complete` 把 usage 带出来（成本记账的依据）；`chat` 返回原文。"""
    ws, client, _ = _wire(tmp_path, [
        _Resp(200, "ok", _reply("A", {"prompt_tokens": 10, "completion_tokens": 2})),
    ])
    text, usage = client.complete(MSGS)
    assert (text, usage) == ("A", {"prompt_tokens": 10, "completion_tokens": 2})
    assert client.chat(MSGS) == "A"          # 第二次命中缓存（无 usage）


def test_cache_file_is_the_raw_text(tmp_path: Path) -> None:
    """值是**响应原文**（含围栏），这样 JSON 抽取照旧在调用方做。"""
    raw = "```json\n{\"a\": 1}\n```"
    ws, client, _ = _wire(tmp_path, [_Resp(200, "ok", _reply(raw))])
    client.chat(MSGS)
    files = _cache_files(ws)
    assert json.loads(json.dumps(files[0].read_text(encoding="utf-8"))) == raw


# ---- CLI 逃生阀（`--no-cache`）------------------------------------------------


def test_every_llm_command_parses_no_cache() -> None:
    """★ `--no-cache` 必须真能被解析到 —— 否则逃生阀只是文档。"""
    from lvs import cli

    parser = cli.build_parser()
    for cmd in ("shots", "publish", "resume", "cost"):
        assert parser.parse_args([cmd, "--no-cache"]).no_cache is True, cmd
        assert parser.parse_args([cmd]).no_cache is False, cmd


def test_main_disables_cache_before_running(tmp_path: Path, monkeypatch) -> None:
    """`lvs ... --no-cache` 在进命令之前就把缓存关了（早于任何 LLM 调用）。"""
    from lvs import cli

    llm_cache.configure(ws=Workspace(task="t", root=tmp_path), stage="shots")
    assert llm_cache.enabled() is True
    monkeypatch.setattr(cli, "find_config", lambda *a, **k: None)
    code = cli._main(["resume", "--no-cache", "--task", "NOPE"])
    assert code == 0
    assert llm_cache.enabled() is False
