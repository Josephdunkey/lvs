"""连续失败熔断（circuit breaker）—— 别在服务已经挂了之后还硬跑几百镜。

## 为什么需要它

本项目各阶段已经做了**逐镜失败隔离**（一镜失败只标记它、其余继续），这是对的。
但那只解决了"单件坏"，没解决"**整条系统性坏**"。

实测过的形状：ComfyUI 没起来 / 显存被占 / TTS 服务掉线。
这时每一镜都会失败，而流水线会**一镜一镜地失败到底** ——
654 镜的集子就是 654 次连接超时（每次几秒到几十秒），
白烧几十分钟甚至几小时，最后才说"全部失败"。

这正是 2026 年多 agent 编排实践里的 **Circuit Breaker** 模式
（CLOSED → OPEN → HALF_OPEN）：**连续失败到阈值就该停**，
因为"连续"这个信号说明问题不在单件、而在环境。

## 语义边界（很重要）

- 它**只跳过失败**，**不跳过人审门禁** —— 门禁拦住是"等人决定"，不是"出错"。
- 它是"**这一批先别跑了**"，不是"永久禁用"。修好环境重跑，熔断器是全新的。
- 阈值可配；配 `0` 或负数 = 关闭熔断（想跑完看全貌时用）。

## 与 `--keep-going` 的关系

`--keep-going` 说"单个阶段失败也往下走"；熔断说"同一阶段内连续失败太多就别硬撑"。
两者互补：熔断先把它拦住，`--keep-going` 才有意义（否则它只是陪着一路失败）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 默认阈值。取 8 的理由：单件偶发失败（某张图抽坏了、某镜素材下载超时）
#: 极少连续超过 3–4 次；而"服务挂了"会立刻连续几十次。
#: 8 给了偶发抖动足够的余量，又能在一个值得注意的时间窗口内止损。
DEFAULT_LIMIT = 8

CONFIG_KEY = "pipeline.max_consecutive_failures"


@dataclass
class Breaker:
    """连续失败计数器。`record()` 返回"是否应当停"。"""

    limit: int = DEFAULT_LIMIT
    consecutive: int = 0
    tripped: bool = False
    tripped_at: int | None = None       # 连续失败的第几项触发
    total_failures: int = 0
    enabled: bool = field(init=False, default=True)

    def __post_init__(self) -> None:
        try:
            self.limit = int(self.limit)
        except (TypeError, ValueError):
            self.limit = DEFAULT_LIMIT
        self.enabled = self.limit > 0

    def record(self, ok: bool, *, index: int | None = None) -> bool:
        """记一次结果。返回 True 表示**已触发熔断、应当停止**。

        `index` 是这项在处理序列中的位置（1-based），只用于报错信息。
        """
        if ok:
            self.consecutive = 0
            return False
        self.consecutive += 1
        self.total_failures += 1
        if self.enabled and self.consecutive >= self.limit:
            self.tripped = True
            if self.tripped_at is None:
                self.tripped_at = index
            return True
        return False

    def reset(self) -> None:
        self.consecutive = 0
        self.tripped = False
        self.tripped_at = None

    def message(self, *, stage: str, remaining: int, command: str = "") -> str:
        """触发后给人看的话：说清"为什么停、这不是单件问题、下一步做什么"。"""
        lines: list[str] = [
            "",
            f"⛔ 已熔断：{stage} 连续失败 {self.consecutive} 次"
            + (f"（从第 {self.tripped_at} 项起）" if self.tripped_at else ""),
            "",
            "  连续失败说明问题**不在单件，而在环境** —— 常见原因：",
            "    · ComfyUI / TTS 服务没起来或掉了",
            "    · 显存被别的进程占满（OOM）",
            "    · 网络/密钥不可用",
            "",
            "  继续跑只会白烧时间与算力，先停下。",
        ]
        if remaining > 0:
            lines.append(f"  本次已停在第 {remaining} 项之前，剩余分镜未处理。")
        if command:
            lines.append(f"  排查后重跑同一条命令即可从断点继续：`{command}`")
        lines.append(
            "  确认要跑完看全貌（不推荐）：在 config.toml 的 `[pipeline]` 段写 "
            "`max_consecutive_failures = 0` 关闭熔断。"
        )
        return "\n".join(lines)


def limit_from(config, *, default: int = DEFAULT_LIMIT) -> int:  # noqa: ANN001
    """从 config 读阈值（duck-typed：只要有 `.get` 即可，不 import Config）。"""
    if config is None:
        return default
    try:
        raw = config.get(CONFIG_KEY, default)
    except Exception:  # noqa: BLE001 - 读配置失败不该阻断阶段
        return default
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default
