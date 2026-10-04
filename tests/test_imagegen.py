"""`lvs/imagegen.py` 的行为测试（票据 19）。

只测**不联网**的部分：`run_command` 的参数解析、seed 递增、产物命名、错误路径。
真正的出图由 `scripts/smoke_imagegen.py` 在真机上验证（需要 CUDA + 模型）。
"""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path

from lvs import imagegen
from lvs.config import Config


def _args(**kw) -> argparse.Namespace:
    base = dict(
        prompt="一只猫", from_shot=None, out=None, seed=None, count=1,
        width=None, height=None,
        workflow=None, model=None, task=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


class _FakeClient:
    """假装 ComfyUI 在线。"""

    def __init__(self, base, timeout: int = 600) -> None:
        self.base = base

    def health(self) -> bool:
        return True


class RunCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._orig_root = imagegen.PROJECT_ROOT
        self._orig_client = imagegen.ComfyClient
        self._orig_generate = imagegen.generate
        imagegen.PROJECT_ROOT = self.root
        imagegen.ComfyClient = _FakeClient  # type: ignore[assignment]
        self.calls: list[tuple[str, Path, int]] = []

        def fake_generate(prompt, out, **kw):
            self.calls.append((prompt, Path(out), int(kw["seed"])))
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"PNG")
            return {"workflow": kw["workflow"], "model": kw["model"], "seed": int(kw["seed"]),
                    "prompt_id": "pid", "width": 1, "height": 1, "bytes": 3}

        imagegen.generate = fake_generate  # type: ignore[assignment]

    def tearDown(self):
        imagegen.PROJECT_ROOT = self._orig_root
        imagegen.ComfyClient = self._orig_client  # type: ignore[assignment]
        imagegen.generate = self._orig_generate  # type: ignore[assignment]
        self.tmp.cleanup()

    def _cfg(self, **extra) -> Config:
        data = {"comfyui": {"base_url": "http://x", "seed": 100}, "gpu": {"skip_check": True}}
        data["comfyui"].update(extra)
        return Config(data, None)

    def test_single_image_default_output(self):
        rc = imagegen.run_command(self._cfg(), _args())
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls), 1)
        prompt, out, seed = self.calls[0]
        self.assertEqual(prompt, "一只猫")
        self.assertEqual(seed, 100)  # 来自 config [comfyui].seed
        self.assertEqual(out.parent.name, "_imagegen")
        self.assertEqual(out.suffix, ".png")

    def test_count_increments_seed_and_suffixes(self):
        rc = imagegen.run_command(self._cfg(), _args(count=3, seed=7))
        self.assertEqual(rc, 0)
        self.assertEqual([c[2] for c in self.calls], [7, 8, 9])
        names = [c[1].name for c in self.calls]
        self.assertEqual(names, ["seed-7-1.png", "seed-7-2.png", "seed-7-3.png"])

    def test_out_path_with_count_suffixes(self):
        target = self.root / "my.png"
        imagegen.run_command(self._cfg(), _args(count=2, out=str(target)))
        self.assertEqual([c[1].name for c in self.calls], ["my-1.png", "my-2.png"])

    def test_no_prompt_is_usage_error(self):
        rc = imagegen.run_command(self._cfg(), _args(prompt=None))
        self.assertEqual(rc, 2)
        self.assertEqual(self.calls, [])

    def test_from_shot_reads_shots_json(self):
        ws_dir = self.root / ".work" / "taskA"
        ws_dir.mkdir(parents=True)
        (ws_dir / "shots.json").write_text(
            '[{"id": 1, "prompt": "p1"}, {"id": 2, "prompt": "分镜二的提示词"}]',
            encoding="utf-8",
        )
        rc = imagegen.run_command(self._cfg(), _args(prompt=None, task="taskA", from_shot=2))
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls[0][0], "分镜二的提示词")
        self.assertEqual(self.calls[0][1].name, "shot-002.png")

    def test_from_shot_unknown_id(self):
        ws_dir = self.root / ".work" / "taskA"
        ws_dir.mkdir(parents=True)
        (ws_dir / "shots.json").write_text('[{"id": 1, "prompt": "p1"}]', encoding="utf-8")
        rc = imagegen.run_command(self._cfg(), _args(prompt=None, task="taskA", from_shot=99))
        self.assertEqual(rc, 2)
        self.assertEqual(self.calls, [])

    def test_size_overrides_reach_generate(self):
        captured: dict = {}
        orig = imagegen.generate

        def spy(prompt, out, **kw):
            captured.update(kw.get("extra") or {})
            return orig(prompt, out, **kw)

        imagegen.generate = spy  # type: ignore[assignment]
        try:
            imagegen.run_command(self._cfg(clip="c.safetensors", vae="v.safetensors"),
                                 _args(width=896, height=512))
        finally:
            imagegen.generate = orig  # type: ignore[assignment]
        self.assertEqual(captured.get("WIDTH"), 896)
        self.assertEqual(captured.get("HEIGHT"), 512)
        self.assertEqual(captured.get("CLIP"), "c.safetensors")
        self.assertEqual(captured.get("VAE"), "v.safetensors")
        # 采样器/步数属模型专用，固定在模板里，不由 CLI 透传（D11）
        self.assertNotIn("STEPS", captured)
        self.assertNotIn("CFG", captured)

    def test_comfyui_down_returns_one(self):
        class DeadClient(_FakeClient):
            def health(self) -> bool:
                return False

        imagegen.ComfyClient = DeadClient  # type: ignore[assignment]
        rc = imagegen.run_command(self._cfg(), _args())
        self.assertEqual(rc, 1)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
