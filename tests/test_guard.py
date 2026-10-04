"""GPU 串行守卫测试（票据 17）。

回归重点（踩过的坑）：守卫**不能**因为"ComfyUI 常驻导致空闲显存变低"就拦住生图
—— 那是正常状态（ComfyUI 自己管显存）。真冲突只有"本地 TTS 服务在跑"。
"""

from __future__ import annotations

import unittest

from lvs import guard
from lvs.config import Config


def _cfg(**kw) -> Config:
    data: dict = {"gpu": {}, "tts": {"backend": "edge"}, "comfyui": {"base_url": "http://127.0.0.1:8188"}}
    for section, values in kw.items():
        data.setdefault(section, {}).update(values)
    return Config(data, None)


class _Stub:
    """临时替换 guard 的探测函数。"""

    def __init__(self, *, alive: bool, free_mb: int | None = 7000):
        self.alive = alive
        self.free_mb = free_mb

    def __enter__(self):
        self._alive = guard._alive
        self._vram = guard.vram
        self._procs = guard.processes
        guard._alive = lambda *a, **k: self.alive  # type: ignore[assignment]
        guard.vram = (  # type: ignore[assignment]
            (lambda: guard.Vram("RTX 4060", 8188, 8188 - self.free_mb, self.free_mb))
            if self.free_mb is not None
            else (lambda: None)
        )
        guard.processes = lambda: ["1234, python.exe, 6200 MiB"]  # type: ignore[assignment]
        return self

    def __exit__(self, *exc):
        guard._alive = self._alive  # type: ignore[assignment]
        guard.vram = self._vram  # type: ignore[assignment]
        guard.processes = self._procs  # type: ignore[assignment]


class ImagegenStageTest(unittest.TestCase):
    def test_low_free_vram_is_not_a_conflict(self):
        """核心回归：ComfyUI 常驻把空闲显存压到 2.6G，生图**不许**被拦。"""
        with _Stub(alive=False, free_mb=2627):
            note = guard.check("imagegen", _cfg())
        self.assertIsNotNone(note)
        self.assertIn("2627", note)
        self.assertIn("不阻断", note)  # 只告警

    def test_edge_tts_never_conflicts(self):
        """tts.backend=edge 不吃本地显存，本地 TTS 服务不存在 → 无冲突。"""
        with _Stub(alive=True, free_mb=7000):
            note = guard.check("imagegen", _cfg())
        self.assertIn("显存", note)  # 没有抛错

    def test_local_tts_running_is_a_conflict(self):
        with _Stub(alive=True, free_mb=7000):
            cfg = _cfg(tts={"backend": "openai_speech", "base_url": "http://127.0.0.1:8000"})
            with self.assertRaises(guard.GPUConflict) as ctx:
                guard.check("imagegen", cfg)
        msg = str(ctx.exception)
        self.assertIn("本地 TTS", msg)
        self.assertIn("停", msg)
        self.assertIn("python.exe", msg)  # 列出了占用进程

    def test_local_tts_configured_but_not_running(self):
        with _Stub(alive=False, free_mb=7000):
            cfg = _cfg(tts={"backend": "openai_speech", "base_url": "http://127.0.0.1:8000"})
            note = guard.check("imagegen", cfg)
        self.assertIn("显存", note)


class TtsStageTest(unittest.TestCase):
    def test_edge_backend_skips_everything(self):
        with _Stub(alive=True, free_mb=100):
            note = guard.check("tts", _cfg(tts={"backend": "edge"}))
        self.assertIn("显存", note)  # 不因 ComfyUI 在跑就拦 edge 配音
        self.assertNotIn("ComfyUI", note)

    def test_openai_speech_blocks_when_comfyui_running(self):
        with _Stub(alive=True, free_mb=7000):
            cfg = _cfg(tts={"backend": "openai_speech", "base_url": "http://127.0.0.1:8000"})
            with self.assertRaises(guard.GPUConflict) as ctx:
                guard.check("tts", cfg)
        self.assertIn("ComfyUI", str(ctx.exception))

    def test_openai_speech_hard_fails_on_low_vram(self):
        with _Stub(alive=False, free_mb=1000):
            cfg = _cfg(tts={"backend": "openai_speech", "base_url": "http://127.0.0.1:8000"})
            with self.assertRaises(guard.GPUConflict) as ctx:
                guard.check("tts", cfg)
        self.assertIn("显存不足", str(ctx.exception))


class DegradeTest(unittest.TestCase):
    def test_skip_check_config(self):
        cfg = Config({"gpu": {"skip_check": True}}, None)
        self.assertIn("跳过", guard.check("imagegen", cfg) or "")

    def test_no_nvidia_smi_degrades_silently(self):
        with _Stub(alive=False, free_mb=None):
            note = guard.check("imagegen", _cfg())
        self.assertIn("nvidia-smi", note or "")

    def test_alive_helper_survives_bad_url(self):
        # 探测失败必须静默返回 False，不能抛
        self.assertFalse(guard._alive("http://127.0.0.1:1/nope", timeout=0.2))
        self.assertFalse(guard._alive("not-a-url", timeout=0.2))


if __name__ == "__main__":
    unittest.main()
