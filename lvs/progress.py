"""轻量进度条（零依赖）。

长视频是"逐镜"做事（270 镜生图/配音），没有进度就完全是黑盒。设计原则：

- **TTY**：单行原地刷新（`\r`），显示 `[████░░░░] 45% 122/270 ETA 12:30`
- **非 TTY**（重定向到日志 / 后台跑）：退化为**每个整数百分比打一行**，避免日志被 `\r` 塞满
- **可禁用**：`total<=0` 或显式 `enabled=False` 时不输出

用法：

    with Progress(len(shots), prefix="素材") as p:
        for shot in shots:
            ...
            p.advance()
"""

from __future__ import annotations

import statistics
import sys
import time
from collections import deque
from typing import Any, TextIO

FILLED = "█"
EMPTY = "░"

# ETA 用**最近 K 项**的平均耗时，不用全程平均（票 25）。
# 全程平均会把"前面那批瞬时完成项"一起算进去：small 任务里 46 张图文卡片（瞬时）
# 后面跟着 64 张生图（各 20s），旧算法会长时间显示 `ETA 00:03`，而实际还要二十分钟。
# K 取 15：实测在这个形状下约 15 项内收敛到真值量级。
ETA_WINDOW = 15


def render_bar(done: int, total: int, width: int = 24) -> str:
    """把完成度画成 `[████░░░░]`。total<=0 时视作 1，避免除零。"""
    total = max(1, int(total))
    filled = int(round(width * max(0, min(done, total)) / total))
    return FILLED * filled + EMPTY * (width - filled)


def fmt_eta(seconds: float) -> str:
    """秒 → `MM:SS` / `H:MM:SS`；未知返回 `--:--`。"""
    if seconds != seconds or seconds < 0:  # NaN 或负
        return "--:--"
    s = int(round(seconds))
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60:02d}:{s % 60:02d}"


class Progress:
    """一个进度条。用 `with` 打开会自动收尾（补满 100%）。"""
    def __init__(
        self,
        total: int,
        prefix: str = "",
        *,
        stream: TextIO | None = None,
        width: int = 24,
        enabled: bool = True,
    ) -> None:
        self.total = max(0, int(total))
        self.prefix = prefix
        self.width = width
        self.stream: Any = stream if stream is not None else sys.stderr
        self.enabled = bool(enabled) and self.total > 0
        self.done = 0
        self.started = time.time()
        self._last_pct = -1
        self._samples: deque[float] = deque(maxlen=ETA_WINDOW)   # 最近几项各用了多久
        self._last_mark = self.started

    # ---- 判定 ---------------------------------------------------------------

    @property
    def is_tty(self) -> bool:
        return bool(getattr(self.stream, "isatty", lambda: False)())

    # ---- 生命周期 -----------------------------------------------------------

    def __enter__(self) -> "Progress":
        self._emit(self._line(0), first=True)
        return self

    def __exit__(self, *exc: object) -> bool:
        self.finish()
        return False

    # ---- 推进 ---------------------------------------------------------------

    def advance(self, n: int = 1) -> None:
        self.set(self.done + n)

    def set(self, done: int) -> None:
        new = max(0, min(self.total, int(done)))
        step = new - self.done
        if step == 1:
            now = time.time()
            self._samples.append(now - self._last_mark)
            self._last_mark = now
        elif step > 1:
            # 一次跳多项（`finish()` / `set(x)`）的耗时不能算进"一项用了多久"，
            # 那会把窗口污染成一个假的速率；只把计时基准往前挪。
            self._last_mark = time.time()
        self.done = new
        self._emit(self._line(self.percent), first=False)

    def eta_seconds(self) -> float | None:
        """按最近 `ETA_WINDOW` 项的速率估剩余时间；估不出来返回 None。"""
        if not self._samples or self.done <= 0 or self.done >= self.total:
            return None
        return statistics.fmean(self._samples) * (self.total - self.done)

    @property
    def percent(self) -> int:
        return int(100 * self.done / self.total) if self.total else 100

    def finish(self) -> None:
        self.done = self.total
        self._emit(self._line(100), first=False, final=True)

    # ---- 渲染 ---------------------------------------------------------------

    def _line(self, pct: int) -> str:
        bits: list[str] = []
        if self.prefix:
            bits.append(self.prefix)
        bits.append("[" + render_bar(self.done, self.total, self.width) + "]")
        bits.append(f"{pct:3d}%")
        bits.append(f"{self.done}/{self.total}")
        if self.done < self.total:
            # 起点（done=0）没法估时，但仍显示 `ETA --:--` 占位，好让字段位置固定（票 22）
            eta = self.eta_seconds()
            bits.append("ETA " + ("--:--" if eta is None else fmt_eta(eta)))
        return " ".join(bits)

    def _emit(self, line: str, *, first: bool, final: bool = False) -> None:
        if not self.enabled:
            return
        pct = self.percent
        if self.is_tty:
            self.stream.write("\r" + line)
            self.stream.flush()
            if final:
                self.stream.write("\n")
                self.stream.flush()
            return
        # 非 TTY：只在中点变化时打行，避免刷屏
        if not first and not final and pct == self._last_pct:
            return
        self.stream.write(line + "\n")
        self.stream.flush()
        self._last_pct = pct


def track(items, prefix: str = "", *, stream: TextIO | None = None, enabled: bool = True):
    """边遍历边推进进度条：`for shot in track(shots, "素材"): ...`。

    比手写 `with Progress(...)` 更适合带 `continue` 的循环 —— 推进发生在每次取下一项时，
    循环体怎么跳转都不用额外照顾。
    """
    items = list(items)
    with Progress(len(items), prefix, stream=stream, enabled=enabled) as p:
        for item in items:
            yield item
            p.advance()
