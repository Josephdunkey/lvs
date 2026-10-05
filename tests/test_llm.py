"""`lvs/llm.py` 单元测试 —— 全程离线，一次网络请求都不发。

为什么单开一个文件：此前没有任何用例执行过 `llm.py` 的函数体（M01：覆盖率
26% = 只有 import 与 class 语句）。它是"模型输出 → 结构化数据"的**唯一**转换器，
错了不报错，只会静默少几镜 / 空提示词。

怎么做到不联网：用本次新增的注入点 `llm.set_transport(fake)` 顶替 HTTP 传输层，
不 monkeypatch `requests`、不碰全局状态；生产路径默认 `_transport is None`，
行为一字不变（见 `test_default_path_still_goes_through_requests`）。
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from lvs import llm
from lvs import publish
from lvs.llm import LLMClient, LLMError, chat_json, extract_json


# ---- 公共替身 ----------------------------------------------------------------

class _Cfg:
    """最小 Config 替身：只实现 `has` / `get`。"""

    def __init__(self, values: dict | None = None, has=None) -> None:
        self._values = values or {}
        self._has = has

    def has(self, key: str) -> bool:
        if self._has is not None:
            return bool(self._has(key))
        return key in self._values

    def get(self, key: str, default=None):
        return self._values.get(key, default)


class _Resp:
    def __init__(self, status_code: int, body: str, payload=None) -> None:
        self.status_code = status_code
        self.text = body
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _reply(text: str) -> dict:
    """OpenAI 兼容响应体。"""
    return {"choices": [{"message": {"content": text}}]}


def _transport(replies: list):
    """假传输层：依次吐出 replies（_Resp 或要抛的异常），并记录调用参数。"""
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
def _reset_transport():
    """每个用例前后都把注入点清回默认，杜绝串味。"""
    llm.set_transport(None)
    yield
    llm.set_transport(None)


def _client(**kw) -> LLMClient:
    kw.setdefault("base_url", "https://api.example.com/v1/")
    kw.setdefault("api_key", "sk-test")
    kw.setdefault("model", "m1")
    return LLMClient(**kw)


# ---- 注入点本身 ---------------------------------------------------------------

def test_default_transport_is_none() -> None:
    assert llm._transport is None, "默认必须是 None（生产走 requests.post）"


def test_default_path_still_goes_through_requests(monkeypatch) -> None:
    """注入是**新增**能力：没注入时仍走 `requests.post`，行为不变。"""
    seen = {}

    def post(url, **kw):
        seen.update({"url": url, **kw})
        return _Resp(200, "ok", _reply("hello"))

    monkeypatch.setattr(llm, "requests", types.SimpleNamespace(post=post))
    assert _client(timeout=7).chat([{"role": "user", "content": "hi"}]) == "hello"
    assert seen["url"] == "https://api.example.com/v1/chat/completions"
    assert seen["timeout"] == 7


def test_injected_transport_bypasses_requests(monkeypatch) -> None:
    """注入了假传输层后，真 `requests` 一次都不该被碰。"""

    def boom(*_a, **_k):  # pragma: no cover - 被调用即失败
        raise AssertionError("注入后不应再走 requests")

    monkeypatch.setattr(llm, "requests", types.SimpleNamespace(post=boom))
    llm.set_transport(_transport([_Resp(200, "ok", _reply("via-fake"))]))
    assert _client().chat([{"role": "user", "content": "x"}]) == "via-fake"


def test_set_transport_none_restores_requests(monkeypatch) -> None:
    fake = _transport([_Resp(200, "ok", _reply("fake"))])
    llm.set_transport(fake)
    llm.set_transport(None)
    monkeypatch.setattr(
        llm, "requests",
        types.SimpleNamespace(post=lambda *a, **k: _Resp(200, "ok", _reply("real"))),
    )
    assert _client().chat([]) == "real"
    assert fake.calls == [], "恢复默认后不该再走假传输层"


# ---- available() --------------------------------------------------------------

def test_available_is_false_without_a_key() -> None:
    assert llm.available(_Cfg()) is False


def test_available_is_true_with_key_and_requests() -> None:
    cfg = _Cfg({"app.openai_api_key": "sk-1"})
    assert llm.available(cfg) is True


def test_available_is_true_with_key_and_injected_transport(monkeypatch) -> None:
    """装了 key + 注入假传输层 = 可用，即使 `requests` 缺席也能离线验证。"""
    monkeypatch.setattr(llm, "requests", None)
    llm.set_transport(_transport([]))
    assert llm.available(_Cfg({"app.openai_api_key": "sk-1"})) is True


def test_available_is_false_without_key_even_with_transport() -> None:
    llm.set_transport(_transport([]))
    assert llm.available(_Cfg()) is False


# ---- LLMClient.chat：请求构造与 4 条错误路径 ----------------------------------

def test_chat_builds_url_payload_and_headers() -> None:
    fake = _transport([_Resp(200, "ok", _reply("hello"))])
    llm.set_transport(fake)
    out = _client().chat([{"role": "user", "content": "hi"}], temperature=0.25)
    assert out == "hello"
    call = fake.calls[0]
    assert call["url"] == "https://api.example.com/v1/chat/completions"
    assert call["json"]["model"] == "m1"
    assert call["json"]["temperature"] == 0.25
    assert call["json"]["messages"] == [{"role": "user", "content": "hi"}]
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert call["timeout"] == 120


def test_chat_base_url_trailing_slash_is_normalised() -> None:
    fake = _transport([_Resp(200, "ok", _reply("x"))])
    llm.set_transport(fake)
    _client(base_url="https://x/v1/").chat([])
    assert fake.calls[0]["url"] == "https://x/v1/chat/completions"


def test_chat_wraps_network_exception_into_llm_error() -> None:
    llm.set_transport(_transport([OSError("connection reset")]))
    with pytest.raises(LLMError) as ei:
        _client().chat([])
    assert "connection reset" in str(ei.value)


def test_chat_raises_on_http_4xx_and_keeps_body_snippet() -> None:
    llm.set_transport(_transport([_Resp(401, "invalid api key")]))
    with pytest.raises(LLMError) as ei:
        _client().chat([])
    assert "401" in str(ei.value) and "invalid api key" in str(ei.value)


def test_chat_raises_on_broken_response_shape() -> None:
    llm.set_transport(_transport([_Resp(200, "{}", {"unexpected": 1})]))
    with pytest.raises(LLMError) as ei:
        _client().chat([])
    assert "结构" in str(ei.value)


def test_chat_raises_when_body_is_not_json() -> None:
    llm.set_transport(_transport([_Resp(200, "<html>502</html>")]))
    with pytest.raises(LLMError):
        _client().chat([])


def test_llm_error_is_a_lvs_error_with_exit_code() -> None:
    from lvs.errors import EXIT_FAILED, LvsError

    exc = LLMError("x")
    assert isinstance(exc, LvsError) and exc.exit_code == EXIT_FAILED


def test_llm_error_carries_raw_text() -> None:
    assert LLMError("坏", raw="原文").raw == "原文"
    assert LLMError("坏").raw is None


# ---- extract_json：模型输出的 4 类形态 ---------------------------------------

def test_extract_json_plain_object() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_json_fence() -> None:
    assert extract_json('好的，结果如下：\n```json\n{"a": 1}\n```\n以上。') == {"a": 1}


def test_extract_json_fence_without_language_tag() -> None:
    assert extract_json("```\n[1, 2]\n```") == [1, 2]


def test_extract_json_prose_around_a_bare_array() -> None:
    text = 'Here is the list: [{"id": "s1"}, {"id": "s2"}] — hope it helps!'
    assert extract_json(text) == [{"id": "s1"}, {"id": "s2"}]


def test_extract_json_prefers_the_fence_over_an_earlier_brace() -> None:
    text = '注意 {这个不是 JSON}：\n```json\n{"ok": true}\n```'
    assert extract_json(text) == {"ok": True}


def test_extract_json_raises_on_none() -> None:
    with pytest.raises(LLMError):
        extract_json(None)  # type: ignore[arg-type]


def test_extract_json_raises_on_garbage() -> None:
    with pytest.raises(LLMError):
        extract_json("完全不包含任何结构化内容的回答。")


# ---- chat_json：重试语义（票据 08） --------------------------------------------

class _StubClient:
    """记录调用次数、按序吐回复的 chat 替身。"""

    def __init__(self, replies: list) -> None:
        self.replies = replies
        self.calls: list[list] = []

    def chat(self, messages, **kw):  # noqa: ANN001
        self.calls.append(list(messages))
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_chat_json_returns_parsed_payload_on_first_try() -> None:
    c = _StubClient(['```json\n{"a": 1}\n```'])
    assert chat_json(c, [{"role": "user", "content": "x"}]) == {"a": 1}
    assert len(c.calls) == 1


def test_chat_json_retries_once_after_a_failure() -> None:
    c = _StubClient([LLMError("第一次坏"), '{"ok": true}'])
    assert chat_json(c, []) == {"ok": True}
    assert len(c.calls) == 2, "票据 08 承诺失败重试一次"


def test_chat_json_gives_up_after_retries_exhausted() -> None:
    c = _StubClient([LLMError("坏1"), LLMError("坏2")])
    with pytest.raises(LLMError):
        chat_json(c, [])
    assert len(c.calls) == 2, "retries=1 表示最多 2 次尝试，不是无限重试"


def test_chat_json_honours_retries_zero() -> None:
    c = _StubClient([LLMError("坏")])
    with pytest.raises(LLMError):
        chat_json(c, [], retries=0)
    assert len(c.calls) == 1


def test_chat_json_attaches_raw_text_for_the_caller_to_dump() -> None:
    """`shots.py:584` 读 `exc.raw` 落盘 —— 这个契约必须被守住。"""
    c = _StubClient(["这不是 JSON，但有原文"] * 2)  # retries=1 → 调两次
    with pytest.raises(LLMError) as ei:
        chat_json(c, [])
    assert ei.value.raw == "这不是 JSON，但有原文"


# ---- from_config --------------------------------------------------------------

def test_from_config_requires_a_key() -> None:
    with pytest.raises(LLMError) as ei:
        LLMClient.from_config(_Cfg())
    assert "openai_api_key" in str(ei.value)


def test_from_config_reads_base_url_and_model_with_defaults() -> None:
    c = LLMClient.from_config(_Cfg({"app.openai_api_key": "sk-1"}))
    assert c.api_key == "sk-1"
    assert c.base_url == "https://api.openai.com/v1"
    assert c.model == "gpt-4o-mini"


def test_from_config_reads_explicit_values() -> None:
    c = LLMClient.from_config(_Cfg({
        "app.openai_api_key": "sk-2",
        "app.openai_base_url": "https://api.deepseek.com/v1",
        "app.openai_model_name": "deepseek-chat",
    }))
    assert (c.api_key, c.base_url, c.model) == (
        "sk-2", "https://api.deepseek.com/v1", "deepseek-chat")


def test_from_config_works_with_injected_transport_and_no_requests(monkeypatch) -> None:
    monkeypatch.setattr(llm, "requests", None)
    fake = _transport([_Resp(200, "ok", _reply("offline"))])
    llm.set_transport(fake)
    c = LLMClient.from_config(_Cfg({"app.openai_api_key": "sk-1"}))
    assert c.chat([]) == "offline"


def test_from_config_raises_without_requests_or_transport(monkeypatch) -> None:
    monkeypatch.setattr(llm, "requests", None)
    with pytest.raises(LLMError) as ei:
        LLMClient.from_config(_Cfg({"app.openai_api_key": "sk-1"}))
    assert "requests" in str(ei.value)


# ---- dump_raw -----------------------------------------------------------------

def _ws(tmp_path: Path):
    def path(*parts):
        p = tmp_path.joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p

    return types.SimpleNamespace(path=path)


def test_dump_raw_writes_the_response_and_the_error(tmp_path: Path) -> None:
    p = llm.dump_raw(_ws(tmp_path), "shots", "原始响应", "解析失败")
    body = p.read_text(encoding="utf-8")
    assert "原始响应" in body and "解析失败" in body
    assert p.parent.name == "logs" and p.name.startswith("llm-shots-")


def test_dump_raw_marks_an_empty_response(tmp_path: Path) -> None:
    p = llm.dump_raw(_ws(tmp_path), "shots", None)
    assert "空响应" in p.read_text(encoding="utf-8")


# ---- publish.polish_with_llm：`--llm` 投稿润色（此前整段 0 覆盖）-------------

def _meta() -> dict:
    return {
        "book": {"title": "雨月物语"},
        "cover": {"lines": ["他七年没回家", "那晚睡的床底下是什么"]},
        "bilibili": {
            "title_candidates": ["规则标题1", "规则标题2"],
            "recommended_title": "规则标题1",
            "description": "规则简介",
            "tags": ["规则标签"],
        },
        "douyin": {"hashtags": ["#规则标签"]},
    }


def test_polish_skips_without_a_key() -> None:
    meta = _meta()
    out, note = publish.polish_with_llm(meta, _Cfg())
    assert out is meta and "跳过润色" in note


def test_polish_applies_titles_description_and_tags() -> None:
    payload = {"titles": ["反差标题A", "反差标题B"], "description": "新简介",
               "tags": ["新标签1", "新标签2"]}
    llm.set_transport(_transport([_Resp(200, "ok", _reply(json.dumps(payload, ensure_ascii=False)))]))
    meta, note = publish.polish_with_llm(_meta(), _Cfg({"app.openai_api_key": "sk-1"}))
    assert "已用 LLM 润色" in note
    assert meta["bilibili"]["title_candidates"][:2] == ["反差标题A", "反差标题B"]
    assert meta["bilibili"]["recommended_title"] == "反差标题A"
    assert meta["bilibili"]["description"] == "新简介"
    assert meta["bilibili"]["tags"] == ["新标签1", "新标签2"]
    assert meta["douyin"]["hashtags"] == ["#新标签1", "#新标签2"]
    assert meta["bilibili"]["polished_by_llm"] is True


def test_polish_falls_back_to_rule_based_copy_on_failure() -> None:
    llm.set_transport(_transport([OSError("断网")]))
    before = _meta()
    meta, note = publish.polish_with_llm(before, _Cfg({"app.openai_api_key": "sk-1"}))
    assert meta is before, "润色失败必须原样退回已算好的物料"
    assert "LLM 润色失败" in note and "保留规则生成的文案" in note