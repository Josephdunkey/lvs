"""连续失败熔断（`lvs/breaker.py`）的测试。

熔断的价值全在**边界**：什么时候该停、什么时候必须不停。
最容易写错的两侧：
- 触发太早 → 偶发的单件失败（某张图抽坏）就中断了整集，比不熔断更糟
- 触发太晚 → 服务挂了还硬跑几百镜，白烧几十分钟
"""

from __future__ import annotations

from lvs import breaker


def test_trips_exactly_at_the_limit():
    b = breaker.Breaker(limit=3)
    assert b.record(False, index=1) is False
    assert b.record(False, index=2) is False
    assert b.record(False, index=3) is True
    assert b.tripped
    assert b.tripped_at == 3


def test_success_resets_the_streak():
    """★ 关键的一侧：中间成功一次就清零 —— 否则"偶发失败"会累积成假熔断。"""
    b = breaker.Breaker(limit=3)
    b.record(False, index=1)
    b.record(False, index=2)
    assert b.record(True) is False
    assert b.consecutive == 0
    assert b.record(False, index=3) is False
    assert not b.tripped


def test_interleaved_failures_never_trip():
    """每失败一次就成功一次 → 永远不熔断（这是对的：不是系统性问题）。"""
    b = breaker.Breaker(limit=3)
    for i in range(1, 30):
        b.record(False, index=i)
        b.record(True)
    assert not b.tripped


def test_total_failures_counts_everything():
    b = breaker.Breaker(limit=10)
    b.record(False)
    b.record(True)
    b.record(False)
    assert b.total_failures == 2


def test_limit_zero_disables_the_breaker():
    """配 0 = 关闭熔断（想跑完看全貌时用）。"""
    b = breaker.Breaker(limit=0)
    for i in range(1, 100):
        assert b.record(False, index=i) is False
    assert not b.tripped
    assert b.total_failures == 99


def test_negative_limit_disables_too():
    b = breaker.Breaker(limit=-1)
    for i in range(1, 20):
        assert b.record(False, index=i) is False


def test_stays_tripped_after_tripping():
    b = breaker.Breaker(limit=2)
    b.record(False, index=1)
    assert b.record(False, index=2) is True
    assert b.record(False, index=3) is True
    assert b.tripped_at == 2, "触发点应记在第一次触发处"


def test_reset_clears_state():
    b = breaker.Breaker(limit=2)
    b.record(False, index=1)
    b.record(False, index=2)
    assert b.tripped
    b.reset()
    assert not b.tripped and b.consecutive == 0 and b.tripped_at is None


def test_message_says_why_and_what_next():
    b = breaker.Breaker(limit=2)
    b.record(False, index=1)
    b.record(False, index=2)
    msg = b.message(stage="素材（assets）", remaining=100, command="lvs assets --task t1")
    assert "熔断" in msg
    assert "素材（assets）" in msg
    assert "lvs assets --task t1" in msg
    assert "关闭熔断" in msg


# ---- 阈值读取 --------------------------------------------------------------


class _Cfg:
    def __init__(self, value):
        self._v = value

    def get(self, key, default=None):
        return self._v if key == breaker.CONFIG_KEY else default


def test_limit_from_none_uses_default():
    assert breaker.limit_from(None) == breaker.DEFAULT_LIMIT


def test_limit_from_reads_config():
    assert breaker.limit_from(_Cfg(3)) == 3
    assert breaker.limit_from(_Cfg("5")) == 5


def test_limit_from_bad_value_falls_back():
    assert breaker.limit_from(_Cfg("not-a-number")) == breaker.DEFAULT_LIMIT
    assert breaker.limit_from(_Cfg(None)) == breaker.DEFAULT_LIMIT


def test_limit_from_config_that_raises_is_safe():
    class _Bad:
        def get(self, key, default=None):
            raise RuntimeError("boom")

    assert breaker.limit_from(_Bad()) == breaker.DEFAULT_LIMIT
