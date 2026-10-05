"""LLM provider 路由层（P2）：低风险调用走本地 ollama，敏感环节留远程。

## 为什么

拆镜的每条 LLM 调用都按 DeepSeek 的价钱算钱，但其中只有两类真的需要大模型的
判断力：**beat 归位 / 分类**与**逐镜提示词**（错一镜，整片画面跑偏）。其余
（BGM 风格提示词、投稿标题/简介/标签、素材打标）属于"结构化搬运"，本地 7B
够用，而且**全都带规则兜底**，失败只掉一点质量。

## 默认路由表（硬编码默认；`[providers.routes]` 可覆盖）

| 接入点 `site`      | 默认     | 理由                                   |
|--------------------|----------|----------------------------------------|
| `bgm.style`        | `local`  | `lvs/bgm.py` 有 7 条规则兜底            |
| `publish.meta`     | `local`  | 投稿文案，人还会再改一遍                |
| `assets.tagging`   | `local`  | 素材筛选/打标，规则化任务（见下"预留"）  |
| `shots.beats`      | `remote` | 拆镜判断，错一段毁一段                  |
| `shots.prompts`    | `remote` | 画面提示词，直接决定出图质量            |
| 其它/未登记        | `remote` | **保守默认**：没显式声明的一律不切       |

> "预留"：`assets.tagging` 目前**没有**真实调用点（素材筛选仍是纯规则）。
> 名字先占位，将来素材打标接入 LLM 时用它 = 自动走本地。

## 总开关（一键回滚）

`LVS_LLM_PROVIDER` 环境变量（**优先**）或 `[providers].mode`：

* `auto`（默认）：按上面的路由表；
* `remote`：**全部远程** —— 回到 P2 之前的现状，一字不差；
* `local`：**全部本地** —— 连拆镜也走 ollama。省到底、质量会掉；也是
  "没有远程 key 也想用 LLM"（离线跑）的逃生阀。

## 配置

```toml
[providers]
mode = "auto"                    # auto | local | remote

[providers.local]
base_url = "http://localhost:11434/v1"
model = "qwen2.5:7b-instruct-q4_K_M"
api_key = ""                     # ollama 不校验；留空自动用占位值 "ollama"

[providers.remote]
# 三个都留空 = 沿用 [app].openai_base_url / openai_model_name / openai_api_key
base_url = ""
model = ""
api_key = ""                     # 密钥仍以 [app].openai_api_key 为准，不搬家

[providers.routes]               # 单点覆盖（可选；键名要带引号）
# "shots.prompts" = "local"
```

"LLM 到底配没配"仍由**远程 key** 决定（`llm.available()`）：没 key = 没配 LLM，
行为与 P1 一字不差。只有 `mode = "local"` 才允许无 key 直接走 ollama。

## GPU 约束（本地腿的前置条件）

ollama 若把 7B 放进显存，会和 ComfyUI(:8188) / Qwen3-TTS(:8100) 抢这 8GB。
本机把 ollama 配成 **CPU 推理**：`OLLAMA_LLM_LIBRARY=cpu`（`OLLAMA_VULKAN=0`），
重启 `ollama serve` 后用 `ollama ps` 确认 `PROCESSOR` 是 `100% CPU`。
路由层**不**控制这个 —— 那是 ollama 服务端的事（见 README「背景音乐 BGM」附近）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# ---- 常量 --------------------------------------------------------------------

PROVIDER_LOCAL = "local"
PROVIDER_REMOTE = "remote"
PROVIDERS: tuple[str, ...] = (PROVIDER_LOCAL, PROVIDER_REMOTE)

MODE_AUTO = "auto"
MODE_LOCAL = "local"
MODE_REMOTE = "remote"
MODES: tuple[str, ...] = (MODE_AUTO, MODE_LOCAL, MODE_REMOTE)
DEFAULT_MODE = MODE_AUTO

#: 总开关的环境变量名（**优先于** `[providers].mode`）。
ENV_MODE = "LVS_LLM_PROVIDER"

#: 接入点名字。调用方把它们传给 `llm.LLMClient.from_config(config, site=...)`。
SITE_BGM_STYLE = "bgm.style"
SITE_PUBLISH_META = "publish.meta"
SITE_ASSETS_TAGGING = "assets.tagging"
SITE_SHOTS_BEATS = "shots.beats"
SITE_SHOTS_PROMPTS = "shots.prompts"

#: 本地 ollama（OpenAI 兼容端点）。默认值与本机现状一致。
DEFAULT_LOCAL_BASE_URL = "http://localhost:11434/v1"
DEFAULT_LOCAL_MODEL = "qwen2.5:7b-instruct-q4_K_M"
#: ollama 不校验 key，但 OpenAI 兼容层要求 Authorization 头里有个值。
DEFAULT_LOCAL_API_KEY = "ollama"

#: 远程默认值（与 `LLMClient.from_config` 一直以来的默认一字不差）。
DEFAULT_REMOTE_BASE_URL = "https://api.openai.com/v1"
DEFAULT_REMOTE_MODEL = "gpt-4o-mini"

#: 接入点 → 默认 tier。**改这里 = 改默认路由**；单点覆盖走 `[providers.routes]`。
DEFAULT_ROUTES: dict[str, str] = {
    SITE_BGM_STYLE: PROVIDER_LOCAL,
    SITE_PUBLISH_META: PROVIDER_LOCAL,
    SITE_ASSETS_TAGGING: PROVIDER_LOCAL,
    SITE_SHOTS_BEATS: PROVIDER_REMOTE,
    # `shots.prompts` 与 `shots.beats` 目前共用同一个 client（`shots.run_command`
    # 建一次、两处调用），所以两处都声明 remote；将来细分时不用改语义。
    SITE_SHOTS_PROMPTS: PROVIDER_REMOTE,
}


@dataclass(frozen=True)
class Provider:
    """一条腿（本地 / 远程）的连接参数。"""

    name: str
    base_url: str
    model: str
    api_key: str = ""

    @property
    def is_local(self) -> bool:
        return self.name == PROVIDER_LOCAL

    def as_dict(self) -> dict[str, str]:
        """只给 `name` / `base_url` / `model` —— **绝不外带 key**。"""
        return {"name": self.name, "base_url": self.base_url, "model": self.model}


# ---- 读配置 ------------------------------------------------------------------


def _text(config: Any, key: str, default: str = "") -> str:
    """读一个字符串配置项；读不到 / 空串 → default。配置坏了不炸穿路由。"""
    if config is None:
        return default
    try:
        value = config.get(key, default)
    except Exception:  # noqa: BLE001 - 配置替身/坏配置都按默认处理
        return default
    if value is None:
        return default
    return str(value).strip() or default


def _routes_table(config: Any) -> dict[str, Any]:
    """`[providers.routes]` 表。**按表读**而不是点号路径 —— 键名里带 `.`。"""
    if config is None:
        return {}
    try:
        raw = config.get("providers.routes")
    except Exception:  # noqa: BLE001
        return {}
    return raw if isinstance(raw, dict) else {}


def mode(config: Any = None) -> str:
    """总开关：环境变量 > `[providers].mode` > `auto`；非法值当 `auto`。"""
    raw = (os.environ.get(ENV_MODE) or "").strip().lower()
    if not raw:
        raw = _text(config, "providers.mode").lower()
    return raw if raw in MODES else DEFAULT_MODE


def route_for(site: str = "", config: Any = None) -> str:
    """这条调用该走谁：返回 `local` / `remote`。

    优先级：总开关（`remote`/`local` 时一锤定音）> `[providers.routes]`
    单点覆盖 > `DEFAULT_ROUTES` > `remote`（没声明的一律不切）。
    """
    current = mode(config)
    if current == MODE_LOCAL:
        return PROVIDER_LOCAL
    if current == MODE_REMOTE:
        return PROVIDER_REMOTE
    override = str(_routes_table(config).get(site, "") or "").strip().lower()
    if override in PROVIDERS:
        return override
    return DEFAULT_ROUTES.get(str(site or ""), PROVIDER_REMOTE)


def local_provider(config: Any = None) -> Provider:
    """本地腿（ollama）。"""
    return Provider(
        name=PROVIDER_LOCAL,
        base_url=_text(config, "providers.local.base_url",
                       DEFAULT_LOCAL_BASE_URL).rstrip("/"),
        model=_text(config, "providers.local.model", DEFAULT_LOCAL_MODEL),
        api_key=_text(config, "providers.local.api_key", DEFAULT_LOCAL_API_KEY),
    )


def remote_provider(config: Any = None) -> Provider:
    """远程腿。留空 = 沿用 `[app]` 段（密钥不搬家）。"""
    return Provider(
        name=PROVIDER_REMOTE,
        base_url=_text(
            config, "providers.remote.base_url",
            _text(config, "app.openai_base_url", DEFAULT_REMOTE_BASE_URL),
        ).rstrip("/"),
        model=_text(
            config, "providers.remote.model",
            _text(config, "app.openai_model_name", DEFAULT_REMOTE_MODEL),
        ),
        api_key=_text(
            config, "providers.remote.api_key", _text(config, "app.openai_api_key")
        ),
    )


def resolve(site: str = "", config: Any = None) -> Provider:
    """`route_for` + 对应腿的连接参数。"""
    if route_for(site, config) == PROVIDER_LOCAL:
        return local_provider(config)
    return remote_provider(config)


def route_table(config: Any = None) -> dict[str, str]:
    """已知接入点 → 当前生效的 tier（测试 / 将来的 `lvs providers` 用）。"""
    sites = set(DEFAULT_ROUTES) | {str(k) for k in _routes_table(config)}
    return {site: route_for(site, config) for site in sorted(sites)}