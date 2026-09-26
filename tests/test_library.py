"""本地素材库索引与检索回归测试（票据 18 的索引/检索部分）。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from lvs.library import (
    best_match,
    build_index,
    cache_path,
    load_index,
    scan,
    score_entry,
    search,
    tokens,
)
from lvs.workspace import Workspace


def _make_library(root: Path) -> None:
    (root / "竹简").mkdir(parents=True)
    (root / "长平").mkdir(parents=True)
    (root / "竹简" / "bamboo-01.jpg").write_bytes(b"x")
    (root / "竹简" / "bamboo-02.png").write_bytes(b"x")
    (root / "竹简" / "bamboo-01.json").write_text(
        json.dumps({"tags": ["竹简", "bamboo slips", "ancient china"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "长平" / "changping-battle.mp4").write_bytes(b"x")
    (root / "readme.txt").write_text("not media", encoding="utf-8")


class TestTokens(unittest.TestCase):
    def test_splits_and_keeps_cjk(self) -> None:
        self.assertIn("bamboo", tokens("bamboo-01.jpg"))
        self.assertIn("竹简", tokens("竹简/bamboo"))
        self.assertNotIn("a", tokens("a b"))  # 单字符英文丢弃


class TestScan(unittest.TestCase):
    def test_scan_collects_media_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_library(root)
            entries = scan([root])
            names = {Path(e.path).name for e in entries}
            self.assertEqual(names, {"bamboo-01.jpg", "bamboo-02.png", "changping-battle.mp4"})
            self.assertNotIn("readme.txt", names)
            self.assertNotIn("bamboo-01.json", names)  # sidecar 不是素材本体

    def test_sidecar_and_folder_tags(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_library(root)
            img = next(e for e in scan([root]) if e.path.endswith("bamboo-01.jpg"))
            self.assertIn("竹简", img.tags)
            self.assertIn("bamboo slips", img.tags)

    def test_scan_never_modifies_originals(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_library(root)
            target = root / "竹简" / "bamboo-01.jpg"
            before = (target.stat().st_mtime_ns, target.stat().st_size)
            scan([root])
            after = (target.stat().st_mtime_ns, target.stat().st_size)
            self.assertEqual(before, after)

    def test_missing_dir_is_skipped(self) -> None:
        self.assertEqual(scan([Path("definitely-not-here-xyz")]), [])


class TestIndexCache(unittest.TestCase):
    def test_build_then_cache_then_reindex(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib = root / "lib"
            lib.mkdir()
            _make_library(lib)

            first = build_index([str(lib)], root=root)
            self.assertFalse(first["from_cache"])
            self.assertEqual(first["count"], 3)
            self.assertTrue(cache_path(root).is_file())

            second = build_index([str(lib)], root=root)
            self.assertTrue(second["from_cache"], "签名一致时应复用缓存")

            forced = build_index([str(lib)], root=root, reindex=True)
            self.assertFalse(forced["from_cache"], "--reindex 必须强制重建")

    def test_load_index_returns_none_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(load_index(Path(td)))


class TestSearch(unittest.TestCase):
    def _index(self, root: Path) -> dict:
        lib = root / "lib"
        lib.mkdir()
        _make_library(lib)
        return build_index([str(lib)], root=root)

    def test_rank_and_min_score(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            idx = self._index(Path(td))
            hits = search(idx, ["bamboo", "竹简"], min_score=1)
            self.assertTrue(hits)
            self.assertTrue(all(h[0]["type"] == "image" for h in hits))
            self.assertEqual(hits[0][1], max(h[1] for h in hits))
            # 阈值抬高到不可能达到的分数 → 不命中
            self.assertEqual(search(idx, ["bamboo"], min_score=99), [])

    def test_no_false_positive_for_unrelated_keyword(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            idx = self._index(Path(td))
            self.assertEqual(search(idx, ["zzz-nonexistent"], min_score=1), [])

    def test_best_match(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            idx = self._index(Path(td))
            m = best_match(idx, ["长平", "battle"])
            self.assertIsNotNone(m)
            self.assertTrue(m["path"].endswith("changping-battle.mp4"))
            self.assertIsNone(best_match(idx, ["zzz"]))

    def test_score_entry_zero_when_no_keywords(self) -> None:
        entry = {"path": "/a/b.jpg", "tags": ["bamboo"]}
        self.assertEqual(score_entry(entry, []), 0)


class _StubConfig:
    def __init__(self, data: dict) -> None:
        self._data = data

    def get(self, dotted: str, default=None):
        cur = self._data
        for part in dotted.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur


class TestLibraryCommand(unittest.TestCase):
    def test_unconfigured_library_skips_quietly(self) -> None:
        from lvs.library import run_command

        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(task="t", root=Path(td)).ensure()
            code = run_command(_StubConfig({"library": {"dirs": []}}), ws,
                               SimpleNamespace(reindex=False))
            self.assertEqual(code, 0)
            self.assertFalse(cache_path(Path(td)).exists())

    def test_configured_library_indexes(self) -> None:
        from lvs.library import run_command

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib = root / "lib"
            lib.mkdir()
            _make_library(lib)
            ws = Workspace(task="t", root=root).ensure()
            cfg = _StubConfig({"library": {"dirs": [str(lib)], "recursive": True, "min_score": 1}})
            code = run_command(cfg, ws, SimpleNamespace(reindex=False))
            self.assertEqual(code, 0)
            idx = load_index(root)
            self.assertEqual(idx["count"], 3)


if __name__ == "__main__":
    unittest.main()
