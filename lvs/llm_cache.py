"""LLM 内容寻址缓存（P1 / B10）—— 同一次请求只付一次钱。

## 为什么要有它

拆镜是**逐块**调 LLM 的，重跑一次 `lvs shots --force` 或改一镜提示词，
几百次请求里的绝大多数是**字节级重复**。缓存把"重复请求"变成"读一个文件"。

## 键怎么算

`sha256(base_url + 规范化(payload))`，其中 payload 是**将要发给服务端的那一份**
（`model` / `messages` / `temperature` / 以及调用方透传的其它参数）。
所以判据是自动成立的：

* 不改参数重跑 → 键相同 → 命中（LLM 调用数 0）
* 改一个字的提示词 / 改 temperature / 换模型 → 键不同 → 必重算

**不含 api_key**：同一份内容换一把 key 不该重算（钱一样花在内容上）。

## 值存什么

响应**原文**（`choices[0].message.content`），UTF-8 纯文本落
`.work/<task>/llm-cache/<key前16位>.txt`。

## 逃生阀（三层，任一层都能关）

1. `--no-cache`（CLI 全局开关）
2. 环境变量 `LVS_LLM_CACHE=0`（`false/no/off` 同义）
3. `configure(enabled=False)`

## 默认行为不变的保证

`enabled()` 要求**显式 `configure(ws=...)`** —— 没接上下文的进程（全部既有
单测、临时脚本）一律不读不写缓存，走的还是老路径。命中/未命中都会往
`logs/run-*.jsonl` 记一条事件（runlog 关着就不记）。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lvs import artifact, runlog

#: 环境变量名（值为 `0/false/no/off` 时**关闭**缓存）。
ENV_KEY = "LVS_LLM_CACHE"
_OFF_VALUES = ("0", "false", "no", "off", "")

#: 缓存目录名（任务目录下）。
CACHE_DIRNAME = "llm-cache"

#: 文件名用 key 的前多少位十六进制。16 位 = 64 bit，几千条量级下碰撞可忽略。
KEY_CHARS = 16

#: runlog 事件名。
HIT_EVENT = "llm_cache_hit"
MISS_EVENT = "llm_cache_miss"


@dataclass
class CacheState:
    """本进程的缓存上下文。字段全是标量/引用，便于测试断言。"""

    ws: Any = None
    stage: str = ""
    config: Any = None
    enabled: bool = True
    hits: int = 0
    misses: int = 0
    writes: int = 0


_state = CacheState()


# ---- 开关与上下文 -----------------------------------------------------------


def env_disabled() -> bool:
    """环境变量是否显式关闭了缓存。"""
    return str(os.environ.get(ENV_KEY, "1")).strip().lower() in _OFF_VALUES


def configure(
    ws: Any = None,
    stage: str | None = None,
    config: Any = None,
    enabled: bool | None = None,
) -> None:
    """给本次进程接上缓存。**不调用 = 老行为**（不缓存、不记账）。

    `ws` 为空时 `enabled()` 为假 —— 这是"默认路径不变"的实现方式，
    而不是靠调用方记得传 `--no-cache`。
    """
    if ws is not None:
        _state.ws = ws
    if stage is not None:
        _state.stage = str(stage)
    if config is not None:
        _state.config = config
    if enabled is not None:
        _state.enabled = bool(enabled)


def reset() -> None:
    """恢复到"未配置"状态（测试与进程收尾用）。"""
    global _state
    _state = CacheState()


def disable() -> None:
    _state.enabled = False


def enable() -> None:
    _state.enabled = True


def enabled() -> bool:
    """**有效**开关：显式开着 + 环境变量没关 + 已接上任务目录。"""
    if not _state.enabled or env_disabled():
        return False
    return _state.ws is not None


def stats() -> dict[str, int]:
    return {"hits": _state.hits, "misses": _state.misses, "writes": _state.writes}


# ---- 键与存储 ---------------------------------------------------------------


def canonical(base_url: str, payload: dict[str, Any]) -> str:
    """规范化成**唯一**字符串：键排序 + 紧凑分隔符 + 不转义中文。

    唯一性比可读性重要 —— 少一个空格差异就会多花一次钱。
    """
    return json.dumps(
        {"base_url": (base_url or "").rstrip("/"), "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def key_for(base_url: str, payload: dict[str, Any]) -> str:
    """内容寻址键（64 位十六进制）。"""
    return hashlib.sha256(canonical(base_url, payload).encode("utf-8")).hexdigest()


def dir_path() -> Path | None:
    """缓存目录（任务目录下）。没有 ws 时返回 None。"""
    ws = _state.ws
    if ws is None:
        return None
    try:
        return ws.path(CACHE_DIRNAME)
    except Exception:  # noqa: BLE001 - 缓存不该因为一个坏 ws 炸掉主流程
        return None


def path_for(key: str) -> Path | None:
    root = dir_path()
    return None if root is None else root / f"{key[:KEY_CHARS]}.txt"


def get(key: str) -> str | None:
    """命中返回响应原文；未命中返回 None（并记一次 miss 计数）。"""
    if not enabled():
        return None
    path = path_for(key)
    if path is None:
        return None
    try:
        if not path.is_file():
            _state.misses += 1
            return None
        return path.read_text(encoding="utf-8")
    except OSError:  # 读不了当未命中
        _state.misses += 1
        return None


def put(key: str, text: str | None) -> None:
    """写入响应原文。**任何失败都吞掉** —— 缓存是增强，不是故障点。"""
    if not enabled() or text is None:
        return
    path = path_for(key)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        artifact.atomic_write_text(path, text)
        _state.writes += 1
    except OSError:
        pass


# ---- 观测 -------------------------------------------------------------------


def log_event(kind: str, **fields: Any) -> None:
    """往 runlog 追加一条事件（runlog 关着 / 没有 ws 时静默跳过）。"""
    ws = _state.ws
    if ws is None:
        return
    try:
        if _state.config is not None and not runlog.enabled(_state.config):
            return
    except Exception:  # noqa: BLE001
        pass
    runlog.event(ws, _state.stage or "llm", kind, **fields)


def note_hit(key: str, model: str = "") -> None:
    _state.hits += 1
    log_event(HIT_EVENT, key=key[:KEY_CHARS], model=str(model))


def note_miss(key: str, model: str = "") -> None:
    log_event(MISS_EVENT, key=key[:KEY_CHARS], model=str(model))
