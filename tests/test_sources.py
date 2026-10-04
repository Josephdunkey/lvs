"""素材来源策略（票 41）。

两件事要分清：
- `source_mode` 是**任务级意向**（auto/pexels/local/library）；
- `source` 是**逐镜现实**（这一镜最后是从哪儿来的）。

策略只作用在「实拍（scene）镜」上：图文/图表 beat 仍由本地排版渲染（D20/D24），
手工钉死的镜（`source_pinned` / D14）也不许被策略掀掉。
"""

from __future__ import annotations

import unittest

from lvs import sources


def _shot(sid: int, source: str = "pexels", kind: str = "scene", **extra):
    shot = {"id": sid, "source": source, "kind": kind}
    shot.update(extra)
    return shot


def _data(shots: list[dict], mode: str = "auto") -> dict:
    return {"source_mode": mode, "shots": shots}


class NormalizeTest(unittest.TestCase):
    def test_accepts_the_four_modes(self) -> None:
        for mode in ("auto", "pexels", "local", "library"):
            self.assertEqual(sources.normalize(mode), mode)

    def test_is_case_and_space_tolerant(self) -> None:
        self.assertEqual(sources.normalize("  LOCAL "), "local")

    def test_empty_is_auto(self) -> None:
        self.assertEqual(sources.normalize(None), "auto")
        self.assertEqual(sources.normalize(""), "auto")

    def test_rejects_garbage(self) -> None:
        with self.assertRaises(sources.SourceModeError):
            sources.normalize("magic")


class ModeOfTest(unittest.TestCase):
    def test_defaults_to_auto(self) -> None:
        self.assertEqual(sources.mode_of({}), "auto")
        self.assertEqual(sources.mode_of(None), "auto")

    def test_reads_the_field(self) -> None:
        self.assertEqual(sources.mode_of({"source_mode": "library"}), "library")


class SceneTest(unittest.TestCase):
    def test_graphic_is_not_a_scene(self) -> None:
        self.assertFalse(sources.is_scene(_shot(1, source="graphic", kind="graphic")))
        self.assertTrue(sources.is_scene(_shot(2, source="pexels")))

    def test_scene_shots_filters(self) -> None:
        data = _data([_shot(1), _shot(2, source="graphic", kind="graphic"), _shot(3, source="local")])
        self.assertEqual([s["id"] for s in sources.scene_shots(data)], [1, 3])

    def test_pinned_shot_is_not_retargetable(self) -> None:
        """手工钉死的镜不受策略影响（D14）。"""
        self.assertFalse(sources.retargetable(_shot(1, source_pinned=True)))
        self.assertTrue(sources.retargetable(_shot(2)))


class ResolveTest(unittest.TestCase):
    """优先级只有一处实现（票 41 复审：原先三处各写一套且行为不一致）。"""

    def test_cli_wins(self) -> None:
        self.assertEqual(sources.resolve("local", "pexels", "library"), "local")

    def test_explicit_auto_from_cli_is_honoured(self) -> None:
        """界面把选择器切回「自动」时，必须真的能取消强制。"""
        self.assertEqual(sources.resolve("auto", "local"), "auto")

    def test_recorded_wins_over_default(self) -> None:
        self.assertEqual(sources.resolve(None, "library", "pexels"), "library")

    def test_recorded_auto_falls_through_to_default(self) -> None:
        """shots 每次都写 source_mode，写进去的 auto 只是默认值、不是选择。"""
        self.assertEqual(sources.resolve(None, "auto", "local"), "local")

    def test_all_empty_is_auto(self) -> None:
        self.assertEqual(sources.resolve(), "auto")

    def test_garbage_raises(self) -> None:
        with self.assertRaises(sources.SourceModeError):
            sources.resolve("magic")


class ApplyModeTest(unittest.TestCase):
    def test_auto_records_but_changes_nothing(self) -> None:
        data = _data([_shot(1, "pexels"), _shot(2, "local")])
        self.assertEqual(sources.apply_mode(data, "auto"), 0)
        self.assertEqual(data["source_mode"], "auto")
        self.assertEqual([s["source"] for s in data["shots"]], ["pexels", "local"])

    def test_forces_every_scene_shot(self) -> None:
        data = _data([_shot(1, "pexels"), _shot(2, "library"), _shot(3, "pexels")])
        self.assertEqual(sources.apply_mode(data, "local"), 3)
        self.assertEqual([s["source"] for s in data["shots"]], ["local"] * 3)
        self.assertEqual(data["source_mode"], "local")

    def test_counts_only_the_shots_it_actually_moved(self) -> None:
        """返回值是「改了几镜」，不是「一共几镜」—— 用来报进度，不能虚报。"""
        data = _data([_shot(1, "pexels"), _shot(2, "local"), _shot(3, "pexels")])
        self.assertEqual(sources.apply_mode(data, "local"), 2)

    def test_leaves_graphic_alone(self) -> None:
        """图文 beat 必须留在卡片渲染器上，否则数字/时间轴会被画成伪汉字。"""
        data = _data([_shot(1, "pexels"), _shot(2, "graphic", kind="graphic")])
        sources.apply_mode(data, "pexels")
        self.assertEqual(data["shots"][1]["source"], "graphic")

    def test_leaves_user_pinned_alone(self) -> None:
        data = _data([_shot(1, "pexels"), _shot(2, "library", source_pinned=True)])
        sources.apply_mode(data, "local")
        self.assertEqual(data["shots"][1]["source"], "library")
        self.assertEqual(data["shots"][0]["source"], "local")

    def test_clears_stale_library_pin_when_source_changes(self) -> None:
        """换来源后，上一来源自动钉上的库命中不能继续粘着（否则分支 ① 会赢）。"""
        data = _data([_shot(1, "pexels", library_asset="D:/lib/a.png")])
        sources.apply_mode(data, "local")
        self.assertNotIn("library_asset", data["shots"][0])

    def test_keeps_pin_when_source_is_unchanged(self) -> None:
        """已经在目标来源上就不动它 —— 重跑要幂等，不能每跑一次就丢一次命中。"""
        data = _data([_shot(1, "library", library_asset="D:/lib/a.png")], mode="library")
        self.assertEqual(sources.apply_mode(data, "library"), 0)
        self.assertEqual(data["shots"][0]["library_asset"], "D:/lib/a.png")

    def test_a_thumb_pin_survives_every_mode(self) -> None:
        """界面「自己选一张图」钉死的镜，换任何来源都不许被掀掉（票 41 复审修的真 bug）。

        现场形态是 `library_asset` + `source=library` + `source_pinned=True`；
        少打 `source_pinned` 就会走 `_STALE_FIELDS` 把它连图一起清掉。
        """
        data = _data([_shot(7, "pexels"), _shot(8, "library", source_pinned=True,
                                                  library_asset="D:/proj/.work/t/picked/shot-008.png")])
        for mode in ("local", "pexels", "library"):
            data["shots"][1].setdefault("library_asset", "D:/proj/.work/t/picked/shot-008.png")
            sources.apply_mode(data, mode)
            self.assertEqual(data["shots"][1]["source"], "library", f"{mode} 模式下钉死的镜不许被改")
            self.assertEqual(data["shots"][1]["library_asset"],
                             "D:/proj/.work/t/picked/shot-008.png", "更不许把图删了")

    def test_library_hit_is_still_retargetable(self) -> None:
        """翻库命中的镜也会带 `library_asset`，但它**不是**人挑的 —— 策略必须能换掉它，
        否则库模式跑一次之后所有镜都粘死在库上，再也切不到生图。"""
        data = _data([_shot(1, "pexels", library_asset="D:/素材库/城墙.jpg")])
        self.assertEqual(sources.apply_mode(data, "local"), 1)
        self.assertEqual(data["shots"][0]["source"], "local")
        self.assertNotIn("library_asset", data["shots"][0])

    def test_clears_stale_resolution_when_source_changes(self) -> None:
        data = _data([_shot(1, "pexels", asset_path=".work/t/assets/pexels/shot-001.mp4",
                            resolved_by="pexels", status="done")])
        sources.apply_mode(data, "local")
        shot = data["shots"][0]
        for field in ("asset_path", "resolved_by", "status"):
            self.assertNotIn(field, shot, f"{field} 属于旧来源，必须清掉")

    def test_second_apply_is_a_noop(self) -> None:
        data = _data([_shot(1, "pexels")])
        self.assertEqual(sources.apply_mode(data, "local"), 1)
        self.assertEqual(sources.apply_mode(data, "local"), 0)

    def test_unknown_mode_raises(self) -> None:
        with self.assertRaises(sources.SourceModeError):
            sources.apply_mode(_data([_shot(1)]), "magic")


class RetargetTest(unittest.TestCase):
    """保守翻转 —— 就是原来 `route_pexels_to_local` 的行为，不能变。"""

    def test_only_flips_the_named_branch(self) -> None:
        data = _data([_shot(1, "pexels"), _shot(2, "local"), _shot(3, "graphic", kind="graphic")])
        self.assertEqual(sources.retarget(data, "pexels", "local"), 1)
        self.assertEqual([s["source"] for s in data["shots"]], ["local", "local", "graphic"])

    def test_skips_pinned(self) -> None:
        data = _data([_shot(1, "pexels", library_asset="D:/lib/a.png")])
        self.assertEqual(sources.retarget(data, "pexels", "local"), 0)


if __name__ == "__main__":
    unittest.main()
