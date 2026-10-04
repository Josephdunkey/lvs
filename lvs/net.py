"""网络小工具：**访问本机服务时绕开系统代理**。

## 为什么必须有这个模块（这是实测踩出来的真 bug）

本项目大量调用**本机**服务：ComfyUI（默认 `127.0.0.1:8188`）、本地 TTS
（默认 `127.0.0.1:8100`）。而标准库 `urllib.request` 在设置了代理环境变量
（`HTTP_PROXY` / `http_proxy`）时，**会把 `127.0.0.1` 的请求也发给代理** ——
Python 的 `urllib` 不像 `requests`/`urllib3` 那样内置"localhost 自动绕过代理"。

实测（本机环境 `HTTP_PROXY=http://127.0.0.1:10159` 时）：

```
urllib.request.urlopen("http://127.0.0.1:8188/system_stats")
  → HTTPError（被代理拦下）

requests.get("http://127.0.0.1:8188/system_stats")
  → 200（urllib3 自带 localhost 绕过）
```

**后果**：用户明明开着 ComfyUI，`lvs assets` 却报"ComfyUI 不可达"；
更糟的是，用户完全不知道是代理在捣鬼，会去反复重启 ComfyUI。

在国内这是**很容易踩到**的：Clash / v2ray / 各种 VPN 客户端普遍会设置
系统代理（Windows 上还会写进注册表，`urllib` 连注册表也读）。

## 做法

对**本机地址**（`localhost` / `127.0.0.1` / `[::1]`）用一个**禁用代理的
`ProxyHandler({})`** 的 opener；其余地址照常（该走代理还是走代理，
不会影响用户访问外网 LLM / Pexels）。

`requests` 那一路（LLM / Pexels / 本地 TTS 的合成请求）**不用改** ——
`urllib3` 默认就绕过 localhost。这里只修 `urllib` 那一路。
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request

__all__ = ["is_local", "opener_for", "urlopen"]

#: 视为"本机"的主机名。`urlsplit().hostname` 已小写化。
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

#: 禁用代理的 opener（本机用）。构造一次即可复用 —— `OpenerDirector` 是线程安全的读操作。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def is_local(url: str) -> bool:
    """这个 URL 是否指向本机。判不准（解析失败）时返回 `False`（保守：不改行为）。"""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOCAL_HOSTS


def opener_for(url: str) -> urllib.request.OpenerDirector | None:
    """本机 URL 返回"禁用代理"的 opener；其余返回 `None`（用默认的 `urlopen`）。

    `None` 的语义是"别插手"——外部地址坚持走系统代理，那是用户的正常网络配置。
    """
    return _NO_PROXY_OPENER if is_local(url) else None


def urlopen(req: str | urllib.request.Request, timeout: float = 30):
    """`urllib.request.urlopen` 的本机安全版。

    ★ **本机的请求一律绕开代理**；外部地址行为与标准库完全一致。

    `req` 可以是 URL 字符串或 `Request` 对象（两者在调用点都有用到）。
    """
    url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
    op = opener_for(url)
    if op is None:
        return urllib.request.urlopen(req, timeout=timeout)
    return op.open(req, timeout=timeout)
