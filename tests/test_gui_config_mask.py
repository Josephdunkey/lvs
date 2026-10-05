"""S01：页面回显的**掩码**绝不能被当成新密钥写回（不可逆覆盖）。

触发概率极高：用户只是去设置页改一下 `library.dirs`，保存时那个被掩码填满的
密钥输入框会一起提交 —— 于是 `openai_api_key` 被 `sk-1…cdef` 覆盖，
而原值**不在任何地方留副本**（不可恢复）。

三层判据：`looks_masked` 判得准、`apply` 拒得住、`read_editable` 如实标 `masked`。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from lvs.gui import configio

SAMPLE = """[library]
dirs = ["D:/素材库"]
min_score = 1

[app]
openai_api_key = "sk-1234567890abcdef"

[pexels]
api_key = ""
"""


class MaskedSecretGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.path = Path(td) / "config.toml"
        self.path.write_text(SAMPLE, encoding="utf-8")

    def test_looks_masked_shapes(self) -> None:
        self.assertTrue(configio.looks_masked("sk-1…cdef"))
        self.assertTrue(configio.looks_masked("已设置"))
        self.assertFalse(configio.looks_masked("sk-1234567890abcdef"))
        self.assertFalse(configio.looks_masked(""))
        self.assertFalse(configio.looks_masked(None))

    def test_write_of_masked_value_is_refused(self) -> None:
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(configio.ConfigIOError) as ctx:
            configio.apply(self.path, {"app.openai_api_key": "sk-1…cdef"})
        self.assertIn("掩码", str(ctx.exception))
        self.assertEqual(self.path.read_text(encoding="utf-8"), before,
                         "文件被改了就不能叫「拒绝」")

    def test_round_trip_of_the_page_echo_is_refused(self) -> None:
        """★ 真实触发路径：把 `/api/config` 回显的 value 原样 POST 回去。"""
        echo = configio.read_editable(self.path)["app.openai_api_key"]["value"]
        self.assertTrue(echo, "回显的应该是掩码（非空）")
        with self.assertRaises(configio.ConfigIOError):
            configio.apply(self.path, {"app.openai_api_key": echo})
        state = configio.read_editable(self.path)
        self.assertEqual(state["app.openai_api_key"]["value"], "sk-1…cdef", "真 key 被覆盖了")

    def test_masked_flag_is_reported(self) -> None:
        state = configio.read_editable(self.path)
        self.assertTrue(state["app.openai_api_key"]["masked"])
        self.assertFalse(state["pexels.api_key"]["masked"], "空密钥不算掩码")
        self.assertFalse(state["library.dirs"]["masked"], "非密钥字段一律 False")

    def test_empty_still_means_clear(self) -> None:
        """显式传空串仍然是「清掉密钥」—— 别把这条正常用法一起堵死。"""
        configio.apply(self.path, {"pexels.api_key": ""})
        self.assertEqual(configio.read_editable(self.path)["pexels.api_key"]["value"], "")

    def test_real_key_still_writable(self) -> None:
        configio.apply(self.path, {"app.openai_api_key": "sk-BRANDNEW9999"})
        self.assertEqual(configio.read_editable(self.path)["app.openai_api_key"]["value"],
                         "sk-B…9999")
