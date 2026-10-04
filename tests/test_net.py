"""本机服务调用必须**绕开系统代理**（`lvs/net.py`）。

## 为什么必须测这个

实测踩到的真 bug：本机环境设了 `HTTP_PROXY=http://127.0.0.1:10159`，
而标准库 `urllib.request` **会把 `127.0.0.1` 的请求也发给代理**（不像
`requests`/`urllib3` 那样内置 localhost 绕过）：

```
urllib.request.urlopen("http://127.0.0.1:8188/system_stats")  → HTTPError（被代理拦）
requests.get("http://127.0.0.1:8188/system_stats")            → 200
```

**后果**：用户开着 ComfyUI，`lvs assets` 却报"ComfyUI 不可达" ——
而用户完全不知道是代理在捣鬼，会去反复重启 ComfyUI。

国内这**很容易踩到**：Clash / v2ray / VPN 客户端普遍设系统代理
（Windows 上连注册表也会被 `urllib` 读到）。

## 判据要双向

1. **本机 → 必须绕开代理**（否则服务在跑却判不可达）
2. **外网 → 不许插手**（否则会把用户的正常翻墙/代理配置搞坏）
"""

from __future__ import annotations

import http.server
import socketserver
import threading
import time
import urllib.request

import pytest

from lvs import doctor, guard, net


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - 标准库约定
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"model_loaded": true}')

    def log_message(self, *a):  # noqa: ANN002 - 静音
        pass


@pytest.fixture
def local_server():
    """起一个真的本机 HTTP 服务（比 mock 更能说明问题）。"""
    srv = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def poisoned_proxy(monkeypatch):
    """模拟"用户开了代理"：设一个**连不通**的代理。

    连不通是刻意的 —— 若代码没绕开代理，请求必然失败，测试就能变红。

    ★★ 必须同时清掉 `urllib.request._opener`（2026-10-03 实锤的顺序敏感）：
    `urlopen` **首次调用会把带当时代理设置的全局 opener 缓存住**。全量跑时
    `test_guard` 的坏 URL 探测先跑（那时环境还没下毒）→ 缓存了"干净" opener
    → 本测试再下毒，裸 `urlopen` 用的还是缓存 → "DID NOT RAISE"。
    单独跑本文件一切正常，全量跑必挂 —— 典型的"测试互相污染"。
    monkeypatch 会在用例结束后还原原值。
    """
    for var in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")   # 9 = discard，必然连不上
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    # 清缓存：让本次用例真正"在当前（有毒的）环境下"构建 opener
    import urllib.request as _ur

    monkeypatch.setattr(_ur, "_opener", None, raising=False)


# ---- is_local ---------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8188/system_stats",
    "http://localhost:8100/health",
    "http://[::1]:8100/health",
    "http://0.0.0.0:8188/",
])
def test_recognizes_local_urls(url: str):
    assert net.is_local(url)


@pytest.mark.parametrize("url", [
    "https://api.deepseek.com/v1/chat/completions",
    "https://api.pexels.com/videos/search",
    "http://192.168.1.10:8188/system_stats",   # 局域网 ≠ 本机（该走用户的网络配置）
])
def test_non_local_urls_are_not_local(url: str):
    assert not net.is_local(url)


def test_malformed_url_is_conservative():
    """解析不了的 URL 返回 False（保守：不改行为）。"""
    assert not net.is_local("这不是一个 url")


# ---- opener_for：外网不许插手 -----------------------------------------------


def test_external_url_gets_no_special_opener():
    """★ 外网必须返回 None —— 用户的代理配置是**他的正常网络环境**，不许我们改。"""
    assert net.opener_for("https://api.deepseek.com/v1") is None


def test_local_url_gets_a_no_proxy_opener():
    assert net.opener_for("http://127.0.0.1:8188/") is not None


# ---- 真实场景：有代理时也要能连上本机服务 -----------------------------------


@pytest.mark.parametrize("probe", [
    lambda url: net.urlopen(url, timeout=3),
    lambda url: guard._alive(url),               # noqa: SLF001
    lambda url: doctor._probe_http(url),
])
def test_local_probe_works_even_with_a_broken_proxy(local_server, poisoned_proxy, probe):
    """★ 核心回归：**代理开着也必须在跑的服务上返回成功**。

    修之前：`guard._alive` / `doctor._probe_http` 一律 False，
    用户会看到"ComfyUI 不可达"而 ComfyUI 明明开着。
    """
    result = probe(f"{local_server}/health")
    if hasattr(result, "status"):        # net.urlopen 返回 response
        assert 200 <= result.status < 500
    else:
        assert result is True, "有代理时本机服务被判为不可达 —— 绕代理失效了"


def test_plain_urllib_would_fail_without_the_fix(local_server, poisoned_proxy):
    """反证：**裸 `urllib` 在同一环境下确实会失败**。

    这条同时钉住"为什么需要 `net` 模块" —— 如果哪天有人把调用点改回裸 `urlopen`，
    上面的正测试会红；如果哪天 Python 改了默认行为，这条会红并提醒我们。
    """
    with pytest.raises(Exception):  # noqa: B017 - 具体类型随代理实现而变
        urllib.request.urlopen(f"{local_server}/health", timeout=3)


# ---- 调用点必须真的走 net（防"改了模块没人用"） ------------------------------


def test_no_bare_urlopen_left_in_the_codebase():
    """★ 架构判据：`lvs/` 里除 `net.py` 外**不许**有裸 `urllib.request.urlopen`。

    防的正是本项目最大的坑型 —— "定义了却从不接线"。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "lvs"
    offenders: list[str] = []
    for path in sorted(root.glob("*.py")):
        if path.name == "net.py":
            continue                      # 它就是干这个的，允许
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "urllib.request.urlopen" in line and not line.lstrip().startswith("#"):
                offenders.append(f"{path.name}:{i}")
    assert offenders == [], (
        f"这些地方还在用裸 urlopen（会被系统代理拦掉）：{offenders}\n"
        f"  请改用 `lvs.net.urlopen`。"
    )
