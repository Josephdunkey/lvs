"""LLM 成本记账 + `[budget]` 预算闸门（P1 / B5）+ `lvs cost`。

## 为什么要它

"跑一次 shots 多少钱"此前只能靠感觉。这个模块把每次调用的 token 与**估算**费用
写成 runlog 事件（`event="llm_cost"`），于是：

* `lvs cost` 能报出本次/累计 token 与估算费用，并按阶段拆分；
* `[budget] mode = cap` 时，超预算**硬拦**（抛 `BudgetExceeded`，退出码 3）。

## 估 → 留 → 对（简化自 OpenMontage 的 estimate→reserve→reconcile）

1. **估**：调之前按字符数粗估 prompt tokens（`len(text)/4`），算出预估价；
2. **留**：`cap` 模式下预估价就超预算 → 直接拒绝（不花这笔钱）；
3. **对**：拿到响应后按真实 `usage` 计费、落事件、更新累计。

## 价格是**估算**，不是账单

`PRICES` 是 USD / 1M tokens 的硬编码表（价格常变，改这里就好）；未知模型走
`DEFAULT_PRICE`（中位价，**宁可高估** —— 高估只会早点拦，低估会让 cap 失效）。
可用 `[budget].price_in_per_1m` / `price_out_per_1m` 覆盖。

## 配置（缺省保守：只记不拦）

```toml
[budget]
mode = "observe"          # observe（默认，只记）/ warn（记账并告警）/ cap（超了抛错）
cap_usd = 0.0             # 总预算；0 = 不限
per_action_usd = 0.0      # 单次调用阈值；0 = 不限
```
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lvs import llm_provider
from lvs import runlog
from lvs.config import PROJECT_ROOT, Config, ConfigError
from lvs.errors import BlockedError, EXIT_BLOCKED

MODE_OBSERVE = "observe"
MODE_WARN = "warn"
MODE_CAP = "cap"
MODES: tuple[str, ...] = (MODE_OBSERVE, MODE_WARN, MODE_CAP)
DEFAULT_MODE = MODE_OBSERVE

#: 模型 → (输入 USD / 1M, 输出 USD / 1M)。**估算用**，别当账单。
PRICES: dict[str, tuple[float, float]] = {
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "deepseek-v3": (0.27, 1.10),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "qwen-max": (1.60, 6.40),
    "qwen-plus": (0.40, 1.20),
}
#: 未知模型的价格（中位价，宁可高估）。
DEFAULT_PRICE: tuple[float, float] = (0.5, 1.5)

#: 粗估：多少字符 ≈ 1 token（中英混排的经验值，只为"先估"用）。
CHARS_PER_TOKEN = 4

#: runlog 事件名。
EVENT = "llm_cost"
OVER_EVENT = "llm_budget_over"


class BudgetExceeded(BlockedError, RuntimeError):
    """预算 cap 硬拦 —— 需要人拍板（加预算 / 改配置），不是程序的错。"""

    exit_code = EXIT_BLOCKED


def _num(value: Any, default: float = 0.0) -> float:
    """把配置里的任意值安全地读成 float。"""
    try:
        if value is None:
            return default
        if isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class Budget:
    """`[budget]` 段的解析结果。全默认 = observe（只记不拦）。"""

    mode: str = DEFAULT_MODE
    cap_usd: float = 0.0
    per_action_usd: float = 0.0
    price_in: float = 0.0   # 0 = 用内置价格表
    price_out: float = 0.0

    @classmethod
    def from_config(cls, config: Any = None) -> "Budget":
        if config is None:
            return cls()
        try:
            raw_mode = str(config.get("budget.mode", DEFAULT_MODE) or DEFAULT_MODE).strip().lower()
        except Exception:  # noqa: BLE001 - 读配置失败按默认处理
            raw_mode = DEFAULT_MODE
        mode = raw_mode if raw_mode in MODES else DEFAULT_MODE
        try:
            cap = _num(config.get("budget.cap_usd"))
            per_action = _num(config.get("budget.per_action_usd"))
            price_in = _num(config.get("budget.price_in_per_1m"))
            price_out = _num(config.get("budget.price_out_per_1m"))
        except Exception:  # noqa: BLE001
            cap = per_action = price_in = price_out = 0.0
        return cls(mode=mode, cap_usd=cap, per_action_usd=per_action,
                   price_in=price_in, price_out=price_out)

    @property
    def enforcing(self) -> bool:
        """是否真的会拦。"""
        return self.mode == MODE_CAP

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "cap_usd": self.cap_usd,
            "per_action_usd": self.per_action_usd,
            "enforcing": self.enforcing,
        }

    def describe(self) -> str:
        bits = [
            {"observe": "observe（只记不拦）", "warn": "warn（超了告警）"}.get(
                self.mode, f"{self.mode}（超了抛错，硬拦）"
            )
        ]
        bits.append(f"cap=${self.cap_usd:.2f}" if self.cap_usd else "cap=不限")
        bits.append(f"单次=${self.per_action_usd:.2f}" if self.per_action_usd else "单次=不限")
        return " / ".join(bits)


@dataclass
class CostState:
    """本进程的记账上下文。"""

    ws: Any = None
    config: Any = None
    stage: str = ""
    budget: Budget = field(default_factory=Budget)
    spent_usd: float = 0.0
    #: 本地腿"本可以花掉的钱"（按远程对照价估），只用于展示"省了多少"。
    saved_usd: float = 0.0
    calls: int = 0
    cache_hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    over: bool = False


_state = CostState()


# ---- 上下文 -----------------------------------------------------------------


def configure(ws: Any = None, config: Any = None, stage: str = "") -> None:
    """接上任务目录、配置与阶段名（**不调用 = 不记账**，老行为）。"""
    if ws is not None:
        _state.ws = ws
    if config is not None:
        _state.config = config
        _state.budget = Budget.from_config(config)
    if stage:
        _state.stage = str(stage)


def reset() -> None:
    global _state
    _state = CostState()


def state() -> CostState:
    return _state


def budget() -> Budget:
    return _state.budget


# ---- 价格与估算 -------------------------------------------------------------


def price_for(model: str, bud: Budget | None = None) -> tuple[float, float, str]:
    """(输入价, 输出价, 来源说明)。"""
    b = bud or _state.budget
    if b.price_in or b.price_out:
        return (b.price_in, b.price_out, "config")
    name = str(model or "").strip().lower()
    if name in PRICES:
        return (*PRICES[name], "table")
    for key, price in PRICES.items():   # 前缀匹配（如 `deepseek-chat-0324`）
        if name.startswith(key):
            return (*price, "table")
    return (*DEFAULT_PRICE, "default")


def usd_for(model: str, prompt_tokens: int, completion_tokens: int,
            bud: Budget | None = None) -> float:
    """按 token 数估算费用（USD）。"""
    p_in, p_out, _ = price_for(model, bud)
    return (int(prompt_tokens) * p_in + int(completion_tokens) * p_out) / 1_000_000.0


def estimate_tokens(messages: list[dict[str, Any]] | None) -> int:
    """调之前的粗估（字符数 / 4）。估不准没关系 —— 它只用来"先拦一道"。"""
    chars = 0
    for m in messages or []:
        if isinstance(m, dict):
            chars += len(str(m.get("content") or ""))
    return max(1, chars // CHARS_PER_TOKEN)


def estimate_usd(messages: list[dict[str, Any]] | None, model: str,
                 bud: Budget | None = None) -> float:
    return usd_for(model, estimate_tokens(messages), 0, bud)


# ---- 估 / 留 / 对 -----------------------------------------------------------


def reserve(messages: list[dict[str, Any]] | None, model: str,
            provider: str = llm_provider.PROVIDER_REMOTE) -> float:
    """调用**之前**：算预估价；`cap` 模式下超阈值就抛 `BudgetExceeded`。

    返回预估价（USD），供 `record` 写进事件做对比。

    本地腿（ollama）不花钱 → 直接返回 0 且**不进闸门**：拦一条免费的调用没有意义。
    """
    if provider == llm_provider.PROVIDER_LOCAL:
        return 0.0
    est = estimate_usd(messages, model)
    if not _state.budget.enforcing:
        return est
    b = _state.budget
    if b.per_action_usd and est > b.per_action_usd:
        raise BudgetExceeded(
            f"单次预估 ${est:.4f} 超过 [budget].per_action_usd=${b.per_action_usd:.4f}"
            f"（模型 {model}，prompt≈{estimate_tokens(messages)} tok）。\n"
            "  改：调小输入 / 换便宜模型 / 抬高 per_action_usd / 把 mode 改回 observe。"
        )
    if b.cap_usd and _state.spent_usd + est > b.cap_usd:
        raise BudgetExceeded(
            f"本次预估 ${est:.4f} 会让累计 ${_state.spent_usd:.4f} 超过 "
            f"[budget].cap_usd=${b.cap_usd:.4f}（模型 {model}）。\n"
            "  改：抬高 cap_usd / 把 mode 改回 observe 或 warn。"
        )
    return est


def note_cached() -> None:
    """缓存命中：不花钱，但要计一次命中。"""
    _state.cache_hits += 1


def record(model: str, usage: dict[str, Any] | None, *, estimated_usd: float = 0.0,
           stage: str = "", provider: str = llm_provider.PROVIDER_REMOTE,
           saved_model: str = "") -> float:
    """调用**之后**对账：按真实 usage 计费、落事件、更新累计，返回本次费用。

    ★ 这里**不抛**预算错：钱已经花了，抛错只会把响应丢掉。
      cap 的拦截在 `reserve`（下一次调用之前）。

    `provider="local"` 时本次费用按 **0** 记（本地推理没有 API 账单），并按
    `saved_model`（远程对照模型）的价格算出"**省了多少**"，供 `lvs cost` 展示。
    """
    if _state.ws is None:
        return 0.0
    usage = usage if isinstance(usage, dict) else {}
    prompt_tokens = int(_num(usage.get("prompt_tokens")))
    completion_tokens = int(_num(usage.get("completion_tokens")))
    is_local = provider == llm_provider.PROVIDER_LOCAL
    usd = 0.0 if is_local else usd_for(model, prompt_tokens, completion_tokens)
    saved = (
        usd_for(saved_model, prompt_tokens, completion_tokens)
        if is_local and saved_model else 0.0
    )
    _state.calls += 1
    _state.spent_usd += usd
    _state.saved_usd += saved
    _state.prompt_tokens += prompt_tokens
    _state.completion_tokens += completion_tokens

    _event(EVENT, {
        "model": str(model),
        "provider": str(provider),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "usd": round(usd, 6),
        "saved_usd": round(saved, 6),
        "estimated_usd": round(float(estimated_usd), 6),
        "cumulative_usd": round(_state.spent_usd, 6),
    }, stage=stage or _state.stage)
    _check_over()
    return usd


def _check_over() -> None:
    b = _state.budget
    if not b.cap_usd or _state.spent_usd <= b.cap_usd or _state.over:
        return
    if b.mode == MODE_OBSERVE:
        return
    _state.over = True
    print(
        f"[预算] 累计估算 ${_state.spent_usd:.4f} 已超过 [budget].cap_usd=${b.cap_usd:.4f}"
        f"（mode={b.mode}）。"
    )
    _event(OVER_EVENT, {
        "spent_usd": round(_state.spent_usd, 6),
        "cap_usd": b.cap_usd,
        "mode": b.mode,
    })


def _event(kind: str, fields: dict[str, Any], *, stage: str = "") -> None:
    ws = _state.ws
    if ws is None:
        return
    try:
        if _state.config is not None and not runlog.enabled(_state.config):
            return
    except Exception:  # noqa: BLE001
        pass
    runlog.event(ws, stage or "llm", kind, **fields)


# ---- 读回（`lvs cost`） ------------------------------------------------------


def read_events(ws: Any, event: str = EVENT) -> list[dict[str, Any]]:
    """读回某类事件（默认 `llm_cost`；跨天：`logs/run-*.jsonl`）。坏行跳过。

    `event=OVER_EVENT` 可单独取预算告警 —— `totals` 只喂 `llm_cost`，别把
    告警当调用次数统计进去。
    """
    logs = ws.path("logs")
    if not logs.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(logs.glob("run-*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("event") == event:
                out.append(row)
    return out


def totals(events: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总成 {calls, prompt_tokens, completion_tokens, usd, saved_usd,
    local_calls, remote_calls, by_stage, by_provider}。

    `by_provider` / `saved_usd` / `local_calls` 是 P2（provider 路由）新增：
    本地调用记 0 费用，另按远程对照价累计"省了多少"。
    """
    out: dict[str, Any] = {
        "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "usd": 0.0,
        "saved_usd": 0.0, "local_calls": 0, "remote_calls": 0,
        "by_stage": {}, "by_provider": {},
    }
    for row in events:
        usd = _num(row.get("usd"))
        pt = int(_num(row.get("prompt_tokens")))
        ct = int(_num(row.get("completion_tokens")))
        stage = str(row.get("stage") or "?")
        provider = str(row.get("provider") or llm_provider.PROVIDER_REMOTE)
        saved = _num(row.get("saved_usd"))
        out["calls"] += 1
        out["prompt_tokens"] += pt
        out["completion_tokens"] += ct
        out["usd"] += usd
        out["saved_usd"] += saved
        if provider == llm_provider.PROVIDER_LOCAL:
            out["local_calls"] += 1
        else:
            out["remote_calls"] += 1
        bucket = out["by_stage"].setdefault(stage, {"calls": 0, "prompt_tokens": 0,
                                                    "completion_tokens": 0, "usd": 0.0})
        bucket["calls"] += 1
        bucket["prompt_tokens"] += pt
        bucket["completion_tokens"] += ct
        bucket["usd"] += usd
        pbucket = out["by_provider"].setdefault(
            provider, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                       "usd": 0.0, "saved_usd": 0.0})
        pbucket["calls"] += 1
        pbucket["prompt_tokens"] += pt
        pbucket["completion_tokens"] += ct
        pbucket["usd"] += usd
        pbucket["saved_usd"] += saved
    out["usd"] = round(out["usd"], 6)
    out["saved_usd"] = round(out["saved_usd"], 6)
    for bucket in out["by_stage"].values():
        bucket["usd"] = round(bucket["usd"], 6)
    for pbucket in out["by_provider"].values():
        pbucket["usd"] = round(pbucket["usd"], 6)
        pbucket["saved_usd"] = round(pbucket["saved_usd"], 6)
    return out


def cache_hits_of(ws: Any) -> int:
    """该任务累计的缓存命中次数（读 runlog）。"""
    logs = ws.path("logs")
    if not logs.is_dir():
        return 0
    hits = 0
    for path in sorted(logs.glob("run-*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if '"llm_cache_hit"' in line:
                hits += 1
    return hits


def _select_tasks(task: str | None, show_all: bool) -> list[str]:
    from lvs import summary

    if task:
        name = str(task).strip()
        return [name] if (PROJECT_ROOT / ".work" / name).is_dir() else []
    dirs = summary.task_dirs()
    return [p.name for p in summary.select_tasks(dirs, prefix="UGE", show_all=show_all)]


def _load_config(config_path: Path | None) -> Config:
    if config_path and Path(config_path).is_file():
        try:
            return Config.load(config_path)
        except ConfigError:
            return Config.empty()
    return Config.empty()


def collect(config_path: Path | None, task: str | None, show_all: bool) -> list[dict[str, Any]]:
    """每个任务一份：调用数 / token / 估算费用 / 按阶段拆分。"""
    from lvs.workspace import Workspace

    config = _load_config(config_path)
    rows: list[dict[str, Any]] = []
    for name in _select_tasks(task, show_all):
        ws = Workspace.read(name, root=PROJECT_ROOT)
        events = read_events(ws)
        agg = totals(events)
        agg["task"] = name
        agg["cache_hits"] = cache_hits_of(ws)
        agg["events"] = len(events)
        _b = Budget.from_config(config)
        agg["budget"] = _b.to_dict()
        agg["budget_text"] = _b.describe()
        rows.append(agg)
    return rows


def format_text(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ("（没有可统计的任务）先跑 `lvs shots`（LLM 调用会落 logs/run-*.jsonl），"
                "或指定 `--task <任务名>`")
    lines = ["任务      调用  命中   提示tok   生成tok   估算USD  阶段明细"]
    for r in rows:
        stages = " ".join(
            f"{name}:{b['calls']}/" + f"${b['usd']:.4f}"
            for name, b in sorted(r["by_stage"].items())
        ) or "-"
        lines.append(
            f"{r['task']:<8} {r['calls']:>4} {r['cache_hits']:>5} "
            f"{r['prompt_tokens']:>9} {r['completion_tokens']:>9} "
            f"{r['usd']:>9.4f}  {stages}"
        )
    total = round(sum(r["usd"] for r in rows), 6)
    lines.append(f"合计：{sum(r['calls'] for r in rows)} 次调用 · "
                 f"缓存命中 {sum(r['cache_hits'] for r in rows)} · 估算 ${total:.4f}")
    if rows:
        local = sum(int(r.get("local_calls") or 0) for r in rows)
        remote = sum(int(r.get("remote_calls") or 0) for r in rows)
        saved = round(sum(_num(r.get("saved_usd")) for r in rows), 6)
        lines.append(f"路由：本地 {local} 次（省估算 ${saved:.4f}）/ 远程 {remote} 次")
        lines.append("预算：" + str(rows[0].get("budget_text") or ""))
        lines.append("说明：费用是**估算**（按内置价格表或 [budget] 覆盖价），不是账单；"
                     "缓存命中不花钱；本地调用（ollama）记 0 费用。")
    return "\n".join(lines)


def run_command(config_path: Path | None, args: Any) -> int:  # noqa: ANN401 - 由 cli 传入
    """`lvs cost` 的入口。**不需要配置文件**（没有就按默认 observe 展示）。"""
    rows = collect(config_path, getattr(args, "task", None), bool(getattr(args, "all", False)))
    if getattr(args, "json", False):
        payload: dict[str, Any] = {"tasks": rows}
        payload["total_usd"] = round(sum(r["usd"] for r in rows), 6)
        payload["total_saved_usd"] = round(
            sum(_num(r.get("saved_usd")) for r in rows), 6)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(format_text(rows))
    return 0
