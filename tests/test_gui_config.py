"""配置编辑（票据 37）测试。

核心约束：改 `config.toml` 不能把注释抹掉 —— 用户文件里每行都带说明。
用 `tomlkit`（保留注释/格式）做回写，这里重点验这一点。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lvs.gui import configio

SAMPLE = '''# 顶层说明
[app]
llm_provider = "openai"
openai_api_key = "sk-1234567890abcdef"   # ← 拆镜阶段必填：你的 DeepSeek key
openai_base_url = "https://api.deepseek.com/v1"

[shots]
# 画面模式
visual_mode = "graphic"

[library]
# 本地素材库（可后续指定）
dirs = []
min_score = 1                              # 关键词命中阈值

[comfyui]
base_url = "http://127.0.0.1:8188"
'''


class RoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(self._cleanup, td)
        self.path = Path(td) / "config.toml"
        self.path.write_text(SAMPLE, encoding="utf-8")

    def _cleanup(self, td: str) -> None:
        import shutil
        shutil.rmtree(td, ignore_errors=True)

    def test_no_changes_leaves_file_untouched(self) -> None:
        before = self.path.read_bytes()
        configio.apply(self.path, {})
        self.assertEqual(self.path.read_bytes(), before)

    def test_set_dirs_preserves_comments(self) -> None:
        configio.apply(self.path, {"library.dirs": ["D:\\素材库", "E:\\已下载"]})
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# ← 拆镜阶段必填", text)       # 别处注释还在
        self.assertIn("# 关键词命中阈值", text)        # 同行注释还在
        self.assertIn("# 本地素材库", text)            # 段首注释还在
        self.assertIn('dirs = ["D:\\\\素材库", "E:\\\\已下载"]', text)

    def test_read_back_shows_new_value(self) -> None:
        configio.apply(self.path, {"library.dirs": ["D:\\素材库"]})
        state = configio.read_editable(self.path)
        self.assertEqual(state["library.dirs"]["value"], ["D:\\素材库"])


class ValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(self._cleanup, td)
        self.path = Path(td) / "config.toml"
        self.path.write_text(SAMPLE, encoding="utf-8")

    def _cleanup(self, td: str) -> None:
        import shutil
        shutil.rmtree(td, ignore_errors=True)

    def test_rejects_unknown_key(self) -> None:
        with self.assertRaises(ValueError):
            configio.apply(self.path, {"rm_rf": "x"})

    def test_rejects_non_editable_key(self) -> None:
        with self.assertRaises(ValueError):
            configio.apply(self.path, {"build.subtitle_style": "x"})

    def test_rejects_bad_choice(self) -> None:
        with self.assertRaises(ValueError):
            configio.apply(self.path, {"shots.visual_mode": "wat"})

    def test_rejects_bad_int(self) -> None:
        with self.assertRaises(ValueError):
            configio.apply(self.path, {"library.min_score": "abc"})

    def test_accepts_valid_choice_and_int(self) -> None:
        configio.apply(self.path, {"shots.visual_mode": "photo", "library.min_score": 3})
        state = configio.read_editable(self.path)
        self.assertEqual(state["shots.visual_mode"]["value"], "photo")
        self.assertEqual(state["library.min_score"]["value"], 3)


class MaskingTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(self._cleanup, td)
        self.path = Path(td) / "config.toml"
        self.path.write_text(SAMPLE, encoding="utf-8")

    def _cleanup(self, td: str) -> None:
        import shutil
        shutil.rmtree(td, ignore_errors=True)

    def test_secret_is_masked_but_present(self) -> None:
        state = configio.read_editable(self.path)
        self.assertEqual(state["app.openai_api_key"]["value"], "sk-1…cdef")
        self.assertTrue(state["app.openai_api_key"]["secret"])

    def test_empty_secret_reads_empty(self) -> None:
        configio.apply(self.path, {"pexels.api_key": ""})
        state = configio.read_editable(self.path)
        self.assertEqual(state["pexels.api_key"]["value"], "")

    def test_apply_new_secret_round_trips(self) -> None:
        configio.apply(self.path, {"app.openai_api_key": "sk-NEWKEY000"})
        state = configio.read_editable(self.path)
        self.assertEqual(state["app.openai_api_key"]["value"], "sk-N…Y000")


class MaskSingleOwnerTest(unittest.TestCase):
    """遮蔽判定与打码只有一处实现（票 38 修复）。"""

    def test_is_secret_key(self) -> None:
        self.assertTrue(configio.is_secret_key("app.openai_api_key"))
        self.assertTrue(configio.is_secret_key("pexels.api_key"))
        self.assertFalse(configio.is_secret_key("library.dirs"))

    def test_mask_shapes(self) -> None:
        self.assertEqual(configio.mask("sk-1234567890abcdef"), "sk-1…cdef")
        self.assertEqual(configio.mask("short"), "已设置")
        self.assertEqual(configio.mask(""), "")


if __name__ == "__main__":
    unittest.main()
