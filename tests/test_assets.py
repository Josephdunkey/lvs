"""assets 阶段的卡片行为测试（票据 34/35）。

只测「按 beat 出卡」这一件事，不真去调浏览器 —— `graphic.render` 被替换成计数器。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from lvs import assets as assets_mod
from lvs import cards, graphic
from lvs.config import Config
from lvs.workspace import Workspace

BEAT_A = "时间轴：前403 → 前313 → 前284 → 前256 → 名分价格曲线缓缓下滑"
BEAT_B = "三十六个邑，三万口人，六十万斤黄金"


def _cfg() -> Config:
    return Config({"library": {"dirs": [], "min_score": 1}, "pexels": {"api_key": ""}}, None)


def _graphic_shot(sid: int, beat: str, narration: str = "") -> dict:
    return {
        "id": sid, "kind": "graphic", "source": "graphic",
        "visual": beat, "narration": narration, "status": "pending",
    }


class CardBenchTest(unittest.TestCase):
    def test_builds_one_card_per_beat(self) -> None:
        shots = [
            _graphic_shot(1, BEAT_A, "先退回去。"),
            _graphic_shot(2, BEAT_A, "再往前。"),
            _graphic_shot(3, BEAT_B),
            {"id": 4, "kind": "scene", "visual": "城墙"},
        ]
        bench = assets_mod.CardBench.build(shots)
        self.assertEqual(len(bench.content), 2)
        self.assertEqual(bench.content[BEAT_B].items, ("三十六个邑", "三万口人", "六十万斤黄金"))
        self.assertEqual(bench.content[BEAT_A].kind, cards.KIND_TIMELINE)


class GraphicBranchTest(unittest.TestCase):
    """同一条 beat 铺多个镜时，PNG 只栅格化一次。"""

    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.root = Path(td)

    def _run(self, shots: list[dict], bench) -> tuple[Workspace, int]:
        calls: list[str] = []
        real = graphic.render

        def counting(card, out):  # noqa: ANN001
            calls.append(str(out))
            return real(card, out)

        graphic.render = counting  # type: ignore[assignment]
        self.addCleanup(setattr, graphic, "render", real)

        ws = Workspace(task="t", root=self.root).ensure()
        for shot in shots:
            res = assets_mod.resolve_shot(
                shot, ws=ws, config=_cfg(), index=None, allow_library=False,
                min_score=1, pexels=None, comfy=None, bench=bench,
            )
            if res.ok:
                shot["asset_path"] = str(res.path)
        return ws, len(calls)

    @unittest.skipUnless(graphic.available(), "需要可用的渲染后端")
    def test_renders_once_per_beat_and_copies(self) -> None:
        shots = [_graphic_shot(i, BEAT_A) for i in range(1, 6)]
        bench = assets_mod.CardBench.build(shots)
        ws, renders = self._run(shots, bench)
        self.assertEqual(renders, 1, "同一条 beat 的 5 个镜只该栅格化 1 次")
        files = sorted((ws.path("assets", "graphic")).glob("shot-*.png"))
        self.assertEqual(len(files), 5, "每个镜仍要有自己的产物（重跑/清理都按镜来）")
        self.assertEqual(len({f.read_bytes() for f in files}), 1, "五份应该是同一张图")

    @unittest.skipUnless(graphic.available(), "需要可用的渲染后端")
    def test_distinct_beats_render_separately(self) -> None:
        shots = [_graphic_shot(1, BEAT_A), _graphic_shot(2, BEAT_B)]
        bench = assets_mod.CardBench.build(shots)
        _, renders = self._run(shots, bench)
        self.assertEqual(renders, 2)

    @unittest.skipUnless(graphic.available(), "需要可用的渲染后端")
    def test_without_bench_still_renders(self) -> None:
        """没传工作台（例如单镜调用）也不能坏。"""
        _, renders = self._run([_graphic_shot(1, BEAT_B)], bench=None)
        self.assertEqual(renders, 1)


class ContentOnCardTest(unittest.TestCase):
    """卡上不许出现脚本提示词（段落标题），也不许出现指令式 beat 原文。"""

    def test_instruction_beat_does_not_leak_onto_card(self) -> None:
        beat = "三段原文并排浮现"
        shots = [
            _graphic_shot(1, beat, "先把这一年的三笔记载，并排摆出来。"),
            _graphic_shot(2, beat, "第一笔：秦将军伐韩，取阳城、负黍，斩首四万。"),
            _graphic_shot(3, beat, "第二笔：伐赵，取二十馀县，斩首虏九万。"),
        ]
        bench = assets_mod.CardBench.build(shots)
        card = bench.content[beat]
        self.assertNotIn("三段原文", card.items)
        self.assertNotIn("并排", "".join(card.items))
        self.assertIn("斩首四万", "".join(card.items))


if __name__ == "__main__":
    unittest.main()


class PinnedAssetTest(unittest.TestCase):
    """票 40：`library_asset` 钉死的图要用上，而且**不能被自己删掉**。

    `_materialize(src, dst)` 是「先 dst.unlink() 再 os.link(src, dst)」；
    若钉死路径正好等于它要材化的目标路径（src == dst），就会先删再链 ——
    把用户选的图弄丢。GUI 因此把原图另存一个名字。这条测试就是锁住这一点。
    """

    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.root = Path(td)

    def test_pinned_file_is_materialized_and_survives(self) -> None:
        from PIL import Image

        from lvs import assets as assets_mod

        ws = Workspace(task="t", root=self.root).ensure()
        picked = ws.path("picked", "shot-001.png")
        picked.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (320, 180), (10, 10, 10)).save(picked, "PNG")

        shot = {"id": 1, "source": "library", "library_asset": str(picked)}
        res = assets_mod.resolve_shot(
            shot, ws=ws, config=_cfg(), index=None, allow_library=True,
            min_score=1, pexels=None, comfy=None,
        )
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.path, ws.path("assets", "library", "shot-001.png"))
        self.assertTrue(res.path.is_file())
        self.assertTrue(picked.is_file(), "钉死的原图不许被 _materialize 顺手删掉")
        self.assertTrue(assets_mod.existing_asset(ws, shot), "重跑时应命中已材化的产物")

    def test_pinned_branch_skips_tooling(self) -> None:
        """钉死的镜不该再要求 ComfyUI / Pexels —— 它根本不走那些分支。"""
        from lvs import assets as assets_mod

        shot = {"id": 1, "source": "library", "library_asset": "D:/somewhere/a.png"}
        self.assertEqual(assets_mod.branches_for(shot), ("library",))


class StaleAssetCacheTest(unittest.TestCase):
    """票据 46：素材缓存原先只按**镜号**命中，镜表一重排就静默复用上一版的图。

    实测：第一章重新钉图后 60 镜里 52 镜指向了错位的旧图，全程不报错。
    两道校验挡住它 —— ① 钉死镜比内容；② 其余镜比请求指纹。
    """

    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.root = Path(td)

    def _png(self, path: Path, color: tuple[int, int, int]) -> Path:
        from PIL import Image

        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 36), color).save(path, "PNG")
        return path

    def test_pinned_cache_rejected_when_source_changed(self) -> None:
        ws = Workspace(task="t", root=self.root).ensure()
        a = self._png(self.root / "a.png", (10, 10, 10))
        b = self._png(self.root / "b.png", (200, 200, 200))
        assets_mod._materialize(a, ws.path("assets", "library", "shot-001.png"))

        shot_a = {"id": 1, "source": "library", "library_asset": str(a)}
        self.assertIsNotNone(assets_mod.existing_asset(ws, shot_a))
        # 重新钉图：镜 1 改钉 b —— 旧缓存必须作废，否则画面全错位
        shot_b = {"id": 1, "source": "library", "library_asset": str(b)}
        self.assertIsNone(assets_mod.existing_asset(ws, shot_b))

    def test_request_key_change_rejected(self) -> None:
        ws = Workspace(task="t", root=self.root).ensure()
        a = self._png(self.root / "a.png", (10, 10, 10))
        assets_mod._materialize(a, ws.path("assets", "local", "shot-001.png"))
        shot = {"id": 1, "source": "local", "kind": "scene", "narration": "第一句"}
        assets_mod.write_src_record(ws, shot)
        self.assertIsNotNone(assets_mod.existing_asset(ws, shot))
        self.assertIsNone(
            assets_mod.existing_asset(ws, dict(shot, narration="换了一句完全不同的旁白"))
        )

    def test_legacy_cache_without_record_still_works(self) -> None:
        """老任务没有 `.key` 记录 → 放行，不能因为加校验就让既有产物全部重取。"""
        ws = Workspace(task="t", root=self.root).ensure()
        a = self._png(self.root / "a.png", (10, 10, 10))
        assets_mod._materialize(a, ws.path("assets", "local", "shot-001.png"))
        shot = {"id": 1, "source": "local", "kind": "scene", "narration": "第一句"}
        self.assertIsNotNone(assets_mod.existing_asset(ws, shot))


class ShotPromptSlotTest(unittest.TestCase):
    """`_shot_prompt` 的槽位补齐：**生效路径必须与 G2 判据一致**。

    `slots_of_shots`（G2 闸门）看 visual∪prompt 并集，而生图只读 prompt ——
    LLM 改写把 `{NAME}` 丢掉时，判据照样放行，图却没有锚定（002 首跑 126/399
    镜实测丢失）。所以 prompt 缺槽位时必须从 visual（拍摄稿原话）补回来。
    """

    def _lock(self):  # noqa: ANN202
        from lvs import cast as cast_mod

        return cast_mod.CastLock(
            characters={
                "NAOKO": cast_mod.CastEntry(
                    anchor="clearly female, long black hair",
                    state=cast_mod.STATE_REF,
                )
            }
        )

    def test_visual_slot_recovered_when_prompt_dropped(self) -> None:
        shot = {
            "prompt": "a woman walking past a wall, flat woodblock style",
            "visual": "{NAOKO} 侧身走过校墙",
        }
        out = assets_mod._shot_prompt(shot, self._lock())
        self.assertTrue(out.startswith("clearly female, long black hair 侧身走过校墙"), out)
        self.assertIn("a woman walking past a wall", out)

    def test_no_duplicate_when_prompt_keeps_slot(self) -> None:
        shot = {
            "prompt": "{NAOKO} 侧身走过校墙. flat woodblock style",
            "visual": "{NAOKO} 侧身走过校墙",
        }
        out = assets_mod._shot_prompt(shot, self._lock())
        self.assertEqual(out.count("clearly female, long black hair"), 1)
        self.assertEqual(out.count("侧身走过校墙"), 1)

    def test_prompt_empty_falls_back_to_visual(self) -> None:
        shot = {"prompt": "", "visual": "{NAOKO} 侧身走过校墙"}
        out = assets_mod._shot_prompt(shot, self._lock())
        self.assertEqual(out, "clearly female, long black hair 侧身走过校墙")

    def test_unknown_slot_still_recovered_verbatim(self) -> None:
        """未登记的名字也要原样前置 —— 让错误显形，而不是把整句吞掉。"""
        shot = {"prompt": "a quiet street", "visual": "{UNKNOWN} 走在路上"}
        out = assets_mod._shot_prompt(shot, self._lock())
        self.assertTrue(out.startswith("{UNKNOWN} 走在路上"), out)
