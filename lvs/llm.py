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
"""

from __future__ import annotations
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


def available(config) -> bool:  # noqa: ANN001 - Config
    """是否具备可用的 LLM 配置（key 已填 + 有可用传输层）。

    "有传输层" = 装了 `requests`，或测试用 `set_transport()` 注入了假传输层
    （这样离线也能覆盖这段行为，不必真联网）。
    """
    has_transport = _transport is not None or requests is not None
    return has_transport and bool(config.has("app.openai_api_key"))


class LLMClient:
    """极简 OpenAI 兼容客户端。只做本项目需要的事。"""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 120) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @classmethod
    def from_config(cls, config) -> "LLMClient":  # noqa: ANN001 - Config
        if requests is None and _transport is None:
            raise LLMError("未安装 `requests`，无法调用 LLM。请 pip install requests。")
        if not config.has("app.openai_api_key"):
            raise LLMError(
                "缺少 LLM key：请在 config.toml 的 [app] 段填写 `openai_api_key`（拆镜阶段必需）。"
            )
        return cls(
            base_url=config.get("app.openai_base_url", "https://api.openai.com/v1"),
            api_key=str(config.get("app.openai_api_key")),
            model=config.get("app.openai_model_name", "gpt-4o-mini"),
        )

    # ---- 底层调用 ---------------------------------------------------------

    def chat(self, messages: list[dict[str, str]], temperature: float = 0.3, **kw: Any) -> str:
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            **kw,
        }
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
            return data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM 响应结构异常：{resp.text[:400]}") from exc


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
