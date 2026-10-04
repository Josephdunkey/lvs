"""进度条单元测试（零依赖）。"""

from __future__ import annotations

import io
import unittest

from lvs import progress
from lvs.progress import Progress, fmt_eta, render_bar


class RenderBarTest(unittest.TestCase):
    def test_endpoints(self) -> None:
        self.assertEqual(render_bar(0, 10, 10), "░" * 10)
        self.assertEqual(render_bar(10, 10, 10), "█" * 10)

    def test_halfway(self) -> None:
        self.assertEqual(render_bar(5, 10, 10), "█" * 5 + "░" * 5)

    def test_zero_total_does_not_crash(self) -> None:
        self.assertEqual(len(render_bar(0, 0, 8)), 8)

    def test_over_done_is_clamped(self) -> None:
        self.assertEqual(render_bar(99, 10, 10), "█" * 10)


class FmtEtaTest(unittest.TestCase):
    def test_zero(self) -> None:
        self.assertEqual(fmt_eta(0), "00:00")

    def test_minutes(self) -> None:
        self.assertEqual(fmt_eta(95), "01:35")

    def test_hours(self) -> None:
        self.assertEqual(fmt_eta(3661), "1:01:01")

    def test_unknown(self) -> None:
        self.assertEqual(fmt_eta(float("nan")), "--:--")
        self.assertEqual(fmt_eta(-1), "--:--")


class ProgressTest(unittest.TestCase):
    def _run(self, total: int, steps: int) -> str:
        s = io.StringIO()
        with Progress(total, prefix="素材", stream=s) as p:
            for _ in range(steps):
                p.advance()
        return s.getvalue()

    def test_non_tty_prints_lines_not_carriage_returns(self) -> None:
        out = self._run(4, 4)
        self.assertNotIn("\r", out)  # 非 TTY 不刷 \r
        self.assertIn("素材", out)
        self.assertIn("100%", out)
        self.assertIn("4/4", out)

    def test_non_tty_is_throttled(self) -> None:
        # 100 步只应打大约 0/1/2…/100 行，而不是 100 行原样
        out = self._run(1000, 1000)
        lines = [l for l in out.splitlines() if l.strip()]
        self.assertLess(len(lines), 200)
        self.assertTrue(lines[-1].rstrip().endswith("1000/1000"))

    def test_disabled_with_zero_total_is_silent(self) -> None:
        s = io.StringIO()
        with Progress(0, stream=s) as p:
            p.advance()
        self.assertEqual(s.getvalue(), "")

    def test_explicitly_disabled_is_silent(self) -> None:
        s = io.StringIO()
        with Progress(10, stream=s, enabled=False) as p:
            p.advance(5)
        self.assertEqual(s.getvalue(), "")

    def test_eta_field_present_at_start(self) -> None:
        # 起点还没有任何完成项：ETA 字段应显示 `--:--`，而不是整段消失（票 22）
        s = io.StringIO()
        with Progress(10, prefix="素材", stream=s):
            pass  # 只在进入时打一行 0/10
        self.assertIn("ETA --:--", s.getvalue())

    def test_tty_uses_carriage_return(self) -> None:
        class _Tty(io.StringIO):
            def isatty(self) -> bool:
                return True

        s = _Tty()
        with Progress(2, stream=s) as p:
            p.advance()
            p.advance()
        self.assertIn("\r", s.getvalue())
        self.assertTrue(s.getvalue().endswith("\n"))


class _StubTime:
    """让进度条的时钟可控（否则"收敛"这种事没法断言）。"""

    def __init__(self) -> None:
        self.t = 10_000.0

    def time(self) -> float:
        return self.t


class RecentRateEtaTest(unittest.TestCase):
    """ETA 用**最近几项**的速率，不用全程平均（票 25）。

    原来的算法 `elapsed / done × 剩余` 把"前面那些瞬时完成项"也算进平均里，
    于是 `small` 任务里 46 张图文卡片（瞬时）+ 64 张生图（各 20s）会让进度条
    长时间停在 `ETA 00:03`，而实际还要二十分钟。
    """

    def setUp(self) -> None:
        self.stub = _StubTime()
        real = progress.time
        progress.time = self.stub            # type: ignore[assignment]
        self.addCleanup(setattr, progress, "time", real)

    def _advance(self, p: progress.Progress, seconds: float) -> None:
        p.advance()
        self.stub.t += seconds

    def test_converges_to_the_recent_rate(self) -> None:
        total, fast, slow, dur = 518, 18, 500, 30.0
        with progress.Progress(total, prefix="素材", stream=io.StringIO()) as p:
            for _ in range(fast):
                self._advance(p, 0.001)          # 前缀：一批瞬时完成项
            for i in range(1, 61):
                self._advance(p, dur)
                if i <= progress.ETA_WINDOW:
                    continue                      # 窗口刚填满时还含 1 个瞬时样本，正常
                eta = p.eta_seconds()
                expected = (total - p.done) * dur
                self.assertIsNotNone(eta)
                self.assertLess(
                    abs(eta - expected) / expected, 0.02,
                    f"第 {i} 个慢项时 ETA 还没收敛到「剩余×{dur}s」（{eta:.0f} vs {expected:.0f}）",
                )

    def test_whole_run_average_would_be_wrong_here(self) -> None:
        """对照：旧算法在这个形状下会**严重低估** —— 这是本票存在的理由。

        200 个瞬时 + 20 个 30s：旧算法把瞬时项也算进平均，于是给出一个比真值小一个
        数量级的 ETA（"还有 8 分钟"其实是"还有 2.5 小时"）。
        """
        total, fast, slow, dur = 518, 200, 100, 30.0
        with progress.Progress(total, stream=io.StringIO()) as p:
            for _ in range(fast):
                self._advance(p, 0.001)
            for _ in range(20):
                self._advance(p, dur)
            eta = p.eta_seconds()
            old_style = (self.stub.t - p.started) / p.done * (total - p.done)
        self.assertIsNotNone(eta)
        self.assertGreater(eta, old_style * 5, f"近期速率 {eta:.0f}s 该远大于旧算法的 {old_style:.0f}s")
        self.assertAlmostEqual(eta, (total - fast - 20) * dur, delta=total)

    def test_unknown_before_first_item(self) -> None:
        with progress.Progress(10, stream=io.StringIO()) as p:
            self.assertIsNone(p.eta_seconds(), "一个都没完成时估不出来")

    def test_unknown_when_finished(self) -> None:
        with progress.Progress(1, stream=io.StringIO()) as p:
            p.advance()
            self.assertIsNone(p.eta_seconds(), "做完了就没有 ETA")

    def test_multi_step_jump_does_not_poison_the_window(self) -> None:
        """一次跳多项（`finish()` / `set(x)`）不该被当成"一项用了这么久"。"""
        with progress.Progress(100, stream=io.StringIO()) as p:
            self.stub.t += 9999                   # 前面那 50 项花了很久
            p.set(50)                             # 但这是一次跳，不能当一个样本
            p.advance()                           # 紧接着完成一项（几乎不耗时）
            self.assertAlmostEqual(p.eta_seconds(), 0.0, places=3)


if __name__ == "__main__":
    unittest.main()
