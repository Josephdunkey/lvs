"""`cardhtml._shoot` 的浏览器子进程必须有 timeout（2026-10-05 审查 P01）。

为什么必须实测：无头浏览器在 profile 被占用 / 崩溃 / 卡在首次向导时会
**永不退出**。原实现 `subprocess.run(cmd, ...)` 既没有 timeout、也没留下
浏览器 stderr —— `lvs assets` 于是无输出、无日志、无退出的挂死
（最坏情况：一集 300+ 镜跑了一夜没动）。

两层判据（都要）：
1. **真的**起一个永不退出的子进程，看它是否在 timeout 附近被强杀并抛可读错误；
2. 浏览器"退出码 0 但没产出截图"时，stderr 要出现在最终报错里（否则挂死依然无从查）。

★ 用 `timeout=` 参数把默认 30 s 调小来测 —— 测的是**机制**，不是那个数字。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pytest

from lvs import cardhtml


class ShootTimeoutTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.tmp = Path(td)

    def test_timeout_becomes_a_readable_carderror(self) -> None:
        """超时不许裸奔出 `_shoot`，而且**必须连子孙进程一起杀**。

        后者是本轮实测出来的：只 kill 直接子进程时，浏览器的 helper 进程还攥着
        stdout 句柄，`communicate()` 会一直等（实测 1.5 s 的 timeout 等了 29.4 s）。
        """
        out = self.tmp / "card.png"
        fake_proc = mock.Mock()
        fake_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="edge", timeout=1.0)
        killed: list = []
        with mock.patch.object(cardhtml.subprocess, "Popen", return_value=fake_proc), \
             mock.patch.object(cardhtml, "_kill_process_tree", killed.append):
            with self.assertRaises(cardhtml.CardError) as ctx:
                cardhtml._shoot("edge.exe", "<html></html>", out, timeout=1.0)
        message = str(ctx.exception)
        self.assertIn("没有退出", message)
        self.assertIn("LVS_BROWSER", message)
        self.assertEqual(killed, [fake_proc], "超时必须调用 _kill_process_tree（否则仍是永久挂死）")

    def _fake_hanging_browser(self) -> str:
        """写一个"永不退出"的假浏览器。Windows 用 .cmd（CreateProcess 直接能起）。"""
        if sys.platform != "win32":
            self.skipTest("假浏览器是 .cmd，只有 Windows 有意义（本项目只在 Windows 跑）")
        if " " in str(self.tmp):
            self.skipTest(f"临时目录含空格，起不了 .cmd：{self.tmp}")
        exe = self.tmp / "fake_browser.cmd"
        # ping 充睡眠：比 `timeout /t` 稳（后者重定向时直接报错退出）
        exe.write_text("@echo off\r\nping -n 30 127.0.0.1 >nul\r\n", encoding="utf-8")
        return str(exe)

    @pytest.mark.slow
    def test_real_hang_is_killed_in_time(self) -> None:
        exe = self._fake_hanging_browser()
        out = self.tmp / "card.png"
        started = time.time()
        with self.assertRaises(cardhtml.CardError) as ctx:
            cardhtml._shoot(exe, "<html></html>", out, timeout=1.5)
        took = time.time() - started
        self.assertIn("没有退出", str(ctx.exception))
        self.assertLess(took, 20.0, f"timeout 没生效：等了 {took:.1f}s（假浏览器要睡 29s）")

    @pytest.mark.slow
    def test_missing_screenshot_reports_browser_stderr(self) -> None:
        """退出码 0、没产出截图 → 报错里要带上浏览器自己的话。"""
        if sys.platform != "win32":
            self.skipTest("同上")
        if " " in str(self.tmp):
            self.skipTest(f"临时目录含空格：{self.tmp}")
        exe = self.tmp / "quiet_browser.cmd"
        exe.write_text(
            "@echo off\r\necho fake-browser-noise 1>&2\r\nexit /b 0\r\n",
            encoding="utf-8")
        with mock.patch.object(cardhtml, "browser", lambda: exe):
            with self.assertRaises(cardhtml.CardError) as ctx:
                cardhtml.render(
                    cardhtml.cards.Card(kind=cardhtml.cards.KIND_LIST, items=("一",)),
                    self.tmp / "card2.png")
        self.assertIn("fake-browser-noise", str(ctx.exception))
