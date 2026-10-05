"""跨站写防护（2026-10-05 审查 S03 / 清单 #6）。

背景：GUI 的取舍是"只绑 127.0.0.1 + 不做鉴权"，但这个取舍**不成立** ——
浏览器的跨站规则里有一类"简单请求"**不发预检**。实测：GUI 开着时从任意
网页 POST 写端点**全都返 200**（能建任务、覆盖拍摄稿、重置来源策略、
停掉正在跑的生图 job）。

四条判据一起才算数（缺一条就是"要么没防住、要么把正常用法堵了"）：
1. 带**外站** `Origin` 的写请求 → 403，且**盘上什么都没变**；
2. 本机来源（`127.0.0.1` / `localhost`）→ 照常 200；
3. **非浏览器**客户端（没有 Origin / Referer）→ 照常放行（curl / 脚本要用）；
4. GET 一律不受影响 —— 读不该被这条规则波及。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

EVIL = "http://evil.example"


def _task(root: Path, name: str) -> Path:
    d = root / ".work" / name
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"task": name, "stages": {}}),
                                     encoding="utf-8")
    (d / "shots.json").write_text(json.dumps({
        "title": "演示", "source_mode": "local",
        "shots": [{"id": 1, "source": "local", "kind": "scene", "narration": "一。"}],
    }, ensure_ascii=False), encoding="utf-8")
    return d


class CrossSiteWriteGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.root = Path(td)
        self.cfg = self.root / "config.toml"
        self.cfg.write_text(
            "[library]\ndirs = []\nmin_score = 1\n\n[pexels]\napi_key = \"\"\n",
            encoding="utf-8")
        self.task = _task(self.root, "t")
        from lvs.gui.app import create_app

        self.app = create_app(self.root, config_path=self.cfg)
        self.app.testing = True
        self.client = self.app.test_client()

    # ---- 1) 外站来源必须被拒 -------------------------------------------
    def test_cross_site_config_write_is_refused(self) -> None:
        before = self.cfg.read_text(encoding="utf-8")
        r = self.client.post("/api/config", json={"library.dirs": ["D:\\素材库"]},
                             headers={"Origin": EVIL})
        self.assertEqual(r.status_code, 403, r.get_json())
        self.assertIn("跨站", r.get_json()["error"])
        self.assertEqual(self.cfg.read_text(encoding="utf-8"), before,
                         "被拒绝的请求不许在盘上留下任何改动")

    def test_cross_site_source_mode_write_is_refused(self) -> None:
        r = self.client.post("/api/task/t/source-mode", json={"mode": "auto"},
                             headers={"Origin": EVIL})
        self.assertEqual(r.status_code, 403)
        data = json.loads((self.task / "shots.json").read_text(encoding="utf-8"))
        self.assertEqual(data["source_mode"], "local", "来源策略被跨站改掉了")

    def test_cross_site_stop_job_is_refused(self) -> None:
        r = self.client.post("/api/job/stop", headers={"Origin": EVIL})
        self.assertEqual(r.status_code, 403)

    def test_referer_only_is_refused(self) -> None:
        """表单提交不一定带 Origin，所以没有 Origin 时要退到 Referer。"""
        r = self.client.post("/api/job/stop", headers={"Referer": EVIL + "/page"})
        self.assertEqual(r.status_code, 403)

    def test_null_origin_is_refused(self) -> None:
        """`Origin: null`（沙箱 iframe / file://）解析不出主机名 —— 一并拒掉。"""
        r = self.client.post("/api/job/stop", headers={"Origin": "null"})
        self.assertEqual(r.status_code, 403)

    # ---- 2) 本机来源不许被误伤 -----------------------------------------
    def test_same_origin_is_allowed(self) -> None:
        r = self.client.post("/api/task/t/source-mode", json={"mode": "auto"},
                             headers={"Origin": "http://127.0.0.1:8730"})
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_localhost_origin_is_allowed(self) -> None:
        r = self.client.post("/api/job/stop", headers={"Origin": "http://localhost:8730"})
        self.assertNotEqual(r.status_code, 403)

    # ---- 3) 非浏览器客户端照常放行 -------------------------------------
    def test_client_without_origin_is_allowed(self) -> None:
        r = self.client.post("/api/job/stop")
        self.assertNotEqual(r.status_code, 403)

    # ---- 4) 读不受影响 --------------------------------------------------
    def test_get_is_never_blocked(self) -> None:
        self.assertEqual(self.client.get("/api/config", headers={"Origin": EVIL}).status_code, 200)
        self.assertEqual(self.client.get("/api/tasks", headers={"Origin": EVIL}).status_code, 200)


class SourceModeMissingBodyTest(unittest.TestCase):
    """空 body **不是**"回到 auto"，是"你没说要用哪个"。

    实测（S03）：跨站表单 POST 一个空 body，`sources.normalize(None)` 会给出
    "auto" —— 于是"什么都没传"成了一个**有效的写操作**，能把用户的 local 重置掉。
    """

    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.root = Path(td)
        self.task = _task(self.root, "t")
        from lvs.gui.app import create_app

        self.app = create_app(self.root, config_path=None)
        self.app.testing = True
        self.client = self.app.test_client()

    def test_missing_mode_is_400_and_nothing_is_written(self) -> None:
        r = self.client.post("/api/task/t/source-mode", json={})
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertIn("mode", r.get_json()["error"])
        data = json.loads((self.task / "shots.json").read_text(encoding="utf-8"))
        self.assertEqual(data["source_mode"], "local")

    def test_explicit_auto_still_works(self) -> None:
        r = self.client.post("/api/task/t/source-mode", json={"mode": "auto"})
        self.assertEqual(r.status_code, 200, r.get_json())
        data = json.loads((self.task / "shots.json").read_text(encoding="utf-8"))
        self.assertEqual(data["source_mode"], "auto")
