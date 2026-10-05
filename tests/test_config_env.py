"""密钥的**环境变量通道**（2026-10-05 审查 S07 / 清单 #4）。

为什么必须实测：`Config.load` 会沿 `[base] config = ...` 继承链走好几层，
"环境变量优先"如果不是在**合并之后**叠加，就会被底配置悄悄覆盖回去 ——
那时 `doctor` 说"已配置"，而实际用的是文件里那把旧 key。

手法：`mock.patch.dict(os.environ, ...)`（退出时自动还原，测试之间不串味）。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lvs import doctor
from lvs.config import Config

SAMPLE = """[app]
openai_api_key = "sk-FILE0000000000"
openai_base_url = "http://127.0.0.1:8000/v1"
openai_model_name = "gpt-4o-mini"

[pexels]
api_key = "pexels-FILE"
"""


class EnvOverrideTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.tmp = Path(td)
        self.path = self.tmp / "config.toml"
        self.path.write_text(SAMPLE, encoding="utf-8")

    def test_env_wins_over_file(self) -> None:
        with mock.patch.dict(os.environ, {"LVS_OPENAI_API_KEY": "sk-FROMENV"}):
            config = Config.load(self.path)
        self.assertEqual(config.get("app.openai_api_key"), "sk-FROMENV")
        self.assertIn("app.openai_api_key", config.env_keys)

    def test_file_value_used_when_env_absent(self) -> None:
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("LVS_OPENAI_API_KEY", None)
            config = Config.load(self.path)
        self.assertEqual(config.get("app.openai_api_key"), "sk-FILE0000000000")
        self.assertEqual(config.env_keys, frozenset())

    def test_empty_env_is_ignored(self) -> None:
        """空串**不算**设置 —— 否则一个手滑的 `set LVS_OPENAI_API_KEY=` 会把 key 抹掉。"""
        with mock.patch.dict(os.environ, {"LVS_OPENAI_API_KEY": ""}):
            config = Config.load(self.path)
        self.assertEqual(config.get("app.openai_api_key"), "sk-FILE0000000000")

    def test_env_survives_base_inheritance(self) -> None:
        """★ 最要紧的一条：配置走 `[base]` 继承链合并时，环境变量必须**仍然**赢。"""
        (self.tmp / "base.toml").write_text(
            '[app]\nopenai_api_key = "sk-BASE"\n', encoding="utf-8")
        child = self.tmp / "child.toml"
        child.write_text(
            '[base]\nconfig = "base.toml"\n\n[app]\nopenai_model_name = "m"\n',
            encoding="utf-8")
        with mock.patch.dict(os.environ, {"LVS_PEXELS_API_KEY": "pexels-FROMENV"}):
            config = Config.load(child)
        self.assertEqual(config.get("pexels.api_key"), "pexels-FROMENV")
        self.assertIn("pexels.api_key", config.env_keys)

    def test_env_does_not_leak_into_other_keys(self) -> None:
        with mock.patch.dict(os.environ, {"LVS_OPENAI_API_KEY": "sk-FROMENV"}):
            config = Config.load(self.path)
        self.assertEqual(config.get("app.openai_model_name"), "gpt-4o-mini")
        self.assertEqual(config.get("pexels.api_key"), "pexels-FILE")

    def test_doctor_says_env_sourced_without_printing_the_value(self) -> None:
        with mock.patch.dict(os.environ, {"LVS_OPENAI_API_KEY": "sk-FROMENV"}):
            check = doctor.check_llm(Config.load(self.path))
        self.assertEqual(check.status, doctor.PASS)
        self.assertIn("环境变量", check.detail)
        self.assertNotIn("sk-FROMENV", check.detail, "体检输出会进日志/截图，键名可以说，值不行")

    def test_doctor_file_sourced_says_nothing_about_env(self) -> None:
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("LVS_OPENAI_API_KEY", None)
            check = doctor.check_llm(Config.load(self.path))
        self.assertEqual(check.status, doctor.PASS)
        self.assertNotIn("环境变量", check.detail)
