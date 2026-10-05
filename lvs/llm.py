"""LLM 客户端（OpenAI 兼容 `/chat/completions`）。

用于拆镜阶段（票据 07/08）：生成画面提示词、检索关键词、判定素材来源。

沿用 `config.toml` 的 `[app]` 段（与 MoneyPrinterTurbo 约定一致）：

    [app]
    llm_provider = "openai"
    openai_api_key = "..."                  # 必填
    openai_base_url = "https://api.deepseek.com/v1"
    openai_model_name = "deepseek-chat"

设计要点：
- **失败可排查**：坏响应/坏 JSON 落盘到 `.work/<task>/logs/`，错误消息里给出文件路径
- **重试一次**：`chat_json` 对首次失败重试 1 次（票据 08）
- **可降级**：`available()` 为假时调用方走启发式，不硬崩（这样没有 key 也能跑通全流程）
- **可省钱（P1）**：内容寻址缓存 + 成本记账，见 `lvs/llm_cache.py` / `lvs/llm_cost.py`。
  调用方先 `configure(ws, stage, config)` 接上；**不接 = 与从前一字不差**（不读不写缓存、
  不记账）。逃生阀：`--no-cache` / 环境变量 `LVS_LLM_CACHE=0`。
- **可再省钱（P2）**：provider 路由 —— 低风险调用（BGM 风格 / 投稿文案 / 素材打标）
  走本地 ollama，拆镜这类敏感调用留远程。默认路由表与总开关在 `lvs/llm_provider.py`；
  调用方用 `from_config(config, site=...)` 声明归属，**不声明 = 远程 = 与从前一字不差**。
"""

from __future__ import annotations

from lvs import llm_provider
from lvs.errors import LvsError, EXIT_FAILED

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

try:  # 允许在无 requests 的环境导入本模块（真正调用时才需要）
    import requests
except ModuleNotFoundError:  # pragma: no cover
    requests = None  # type: ignore[assignment]


class LLMError(LvsError, RuntimeError):
    """LLM 调用或解析失败。

    `raw` 是**触发失败的那次原始响应**（`shots.py:585` 用它落盘排查）——
    声明成字段而不是动态 `setattr`，将来改名静态检查能发现。
    """

    exit_code = EXIT_FAILED

    def __init__(self, message: str = "", raw: str | None = None) -> None:
        super().__init__(message)
        self.raw: str | None = raw


#: 传输层注入点（测试 / 离线用）。默认 `None` = 走 `requests.post`，生产行为不变。
_transport: Any = None


def set_transport(transport: Any) -> None:
    """注入自定义 HTTP 传输层（签名同 `requests.post`）；传 `None` 恢复默认。

    这是 `LLMClient.chat` **唯一**的可替换点：测试用它换掉联网，不必 monkeypatch
    全局 `requests`，也不会污染其它用例。生产路径默认 `None`，行为一字不变。
    """
    global _transport
    _transport = transport


def _post(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> Any:
    """真正发请求的地方 —— 冷路径与错误语义都集中在这里。"""
    transport = _transport
    if transport is None:
        if requests is None:
            raise LLMError("未安装 `requests`，无法调用 LLM。请 pip install requests。")
        transport = requests.post
    return transport(url, json=payload, headers=headers, timeout=timeout)


def available(config, site: str = "") -> bool:  # noqa: ANN001 - Config
    """是否具备可用的 LLM 配置（+ 有可用传输层）。

    "有传输层" = 装了 `requests`，或测试用 `set_transport()` 注入了假传输层
    （这样离线也能覆盖这段行为，不必真联网）。

    `site` 是这条调用的归属（见 `lvs/llm_provider.py` 的路由表），但它**不改变
    "配没配"**：没有远程 key 就是没配 LLM，一切照旧走规则 / 启发式（与 P1 一字
    不差）。唯一例外是显式 `[providers].mode = "local"`（全本地，含离线无 key）。
    """
    if _transport is None and requests is None:
        return False
    if llm_provider.mode(config) == llm_provider.MODE_LOCAL:
        return True
    return bool(config.has("app.openai_api_key"))


def provider_for(site: str = "", config=None) -> llm_provider.Provider:  # noqa: ANN001
    """这条调用（`site`）该用的腿：base_url + model + key（本地 / 远程）。"""
    return llm_provider.resolve(site, config)


def configure(ws=None, stage: str = "", config=None) -> None:  # noqa: ANN001 - Workspace / Config
    """给本次进程接上 P1 的**缓存**与**成本记账**（不调用 = 老行为）。

    为什么做成"模块级上下文"而不是改 `chat` 的签名：调用点有 4 处
    （shots 两处、publish、bgm），改签名会波及所有测试与调用方；上下文只影响
    愿意接上的调用方，且 `llm_cache.enabled()` 要求显式接上才算数。
    """
    from lvs import llm_cache, llm_cost

    llm_cache.configure(ws=ws, stage=stage, config=config)
    llm_cost.configure(ws=ws, config=config, stage=stage)


class LLMClient:
    """极简 OpenAI 兼容客户端。只做本项目需要的事。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 120,
        *,
        provider: str = llm_provider.PROVIDER_REMOTE,
        remote_model: str = "",
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        #: `local` / `remote` —— 只影响记账（本地记 0）与可读性，不影响协议。
        self.provider = provider
        #: 本地腿的"远程对照模型"：同样的 token 走远程要多少钱（算省了多少）。
        self.remote_model = remote_model

    @classmethod
    def from_config(cls, config, site: str = "") -> "LLMClient":  # noqa: ANN001 - Config
        """按 `site` 的路由结果建 client（默认远程 = 与 P2 之前一字不差）。

        `site` 取 `llm_provider.SITE_*`；`[providers.remote]` 留空时仍读 `[app]`。
        """
        if requests is None and _transport is None:
            raise LLMError("未安装 `requests`，无法调用 LLM。请 pip install requests。")
        prov = llm_provider.resolve(site, config)
        if not prov.is_local and not prov.api_key:
            raise LLMError(
                "缺少 LLM key：请在 config.toml 的 [app] 段填写 `openai_api_key`（拆镜阶段必需）。"
            )
        return cls(
            base_url=prov.base_url,
            api_key=prov.api_key,
            model=prov.model,
            provider=prov.name,
            remote_model=llm_provider.remote_provider(config).model if prov.is_local else "",
        )

    # ---- 底层调用 ---------------------------------------------------------

    def complete(
        self, messages: list[dict[str, str]], temperature: float = 0.3, **kw: Any
    ) -> tuple[str, dict[str, Any]]:
        """一次调用，返回 `(响应原文, usage)` —— **唯一**真正发请求的地方。

        与 P1 之前相比只有两处增量，且都可关：

        * 缓存命中 → **不发请求**（返回原文与空 usage；不计费）；
        * 发请求前后各记一笔账（`estimate → reserve → reconcile`）。

        顺手把 `usage` 带出来（原先被 `chat` 丢掉），成本记账才有依据。
        """
        from lvs import llm_cache, llm_cost

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            **kw,
        }
        key = llm_cache.key_for(self.base_url, payload)
        if llm_cache.enabled():
            cached = llm_cache.get(key)
            if cached is not None:
                llm_cache.note_hit(key, self.model)
                llm_cost.note_cached()
                return cached, {}
            llm_cache.note_miss(key, self.model)
        # 先估：cap 模式下**发请求之前**就可能在这里被拦下（省下这笔钱）
        # 本地腿免费：`reserve` 直接返回 0，也不进预算闸门（拦它没有意义）
        estimated = llm_cost.reserve(messages, self.model, provider=self.provider)

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = _post(url, payload, headers, self.timeout)
        except Exception as exc:  # 网络类异常统一转成 LLMError
            raise LLMError(f"LLM 请求失败（{url}）：{exc}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"LLM 返回 HTTP {resp.status_code}：{resp.text[:400]}")
        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM 响应结构异常：{resp.text[:400]}") from exc
        # 后对：按真实 usage 记账（钱已经花了，这里**不抛**预算错）
        usage = data.get("usage") if isinstance(data, dict) else None
        llm_cost.record(
            self.model, usage, estimated_usd=estimated,
            provider=self.provider, saved_model=self.remote_model,
        )
        if llm_cache.enabled():
            llm_cache.put(key, text)
        return text, usage

    def chat(self, messages: list[dict[str, str]], temperature: float = 0.3, **kw: Any) -> str:
        """一次调用，只要响应原文（**签名与行为与 P1 之前一字不变**）。"""
        return self.complete(messages, temperature, **kw)[0]


# ---- JSON 抽取（LLM 常用 ```json 围栏包裹） --------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """从模型输出里尽力抽出 JSON 对象/数组。失败抛 `LLMError`。"""
    if text is None:
        raise LLMError("LLM 返回为空")
    candidates: list[str] = []
    m = _FENCE.search(text)
    if m:
        candidates.append(m.group(1))
    stripped = text.strip()
    candidates.append(stripped)
    # 再兜底：截取第一个 { 或 [ 到最后一个 } 或 ]
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        i, j = stripped.find(open_ch), stripped.rfind(close_ch)
        if i != -1 and j > i:
            candidates.append(stripped[i : j + 1])

    for cand in candidates:
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
    raise LLMError("无法从模型输出中解析出 JSON")


def dump_raw(ws, stage: str, text: str | None, error: str = "") -> Path:
    """把原始响应落盘到 `.work/<task>/logs/`，返回路径（供错误消息引用）。"""
    logs = ws.path("logs")
    logs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = logs / f"llm-{stage}-{stamp}.txt"
    body = (text or "<空响应>") + (f"\n\n--- 错误 ---\n{error}\n" if error else "")
    path.write_text(body, encoding="utf-8")
    return path


def chat_json(client: LLMClient, messages: list[dict[str, str]], *, retries: int = 1, **kw: Any) -> Any:
    """调用 LLM 并要求返回 JSON；失败重试 `retries` 次后抛错（由调用方落盘）。"""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        text = None
        try:
            text = client.chat(messages, **kw)
            return extract_json(text)
        except LLMError as exc:
            # 把上面拿到的原文带上，便于调用方落盘（`shots.py:585` 读 `.raw`）
            last_err = LLMError(str(exc), raw=text)
    raise last_err if last_err else LLMError("LLM 调用失败")
