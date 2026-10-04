"""来源策略在 assets 阶段的行为（票 41）。

与 `test_assets.py`（讲"按 beat 出卡"）分开：这里只讲**一支还是多支**、
**库没命中怎么办**、以及 `lvs assets` 有没有把策略真的落成逐镜 source。
"""

from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import types
import unittest
from pathlib import Path

from lvs import assets as assets_mod
from lvs import imagegen, sources
from lvs.config import Config
from lvs.workspace import Workspace


def _cfg(library_dirs: list[str] | None = None) -> Config:
    return Config(
        {"library": {"dirs": library_dirs or [], "min_score": 1}, "pexels": {"api_key": ""}},
        None,
    )


class SourceModeBranchesTest(unittest.TestCase):
    """策略决定「这一镜该去哪几支找」。"""

    def test_auto_keeps_the_documented_order(self) -> None:
        shot = {"id": 1, "source": "pexels"}
        self.assertEqual(assets_mod.branches_for(shot), ("library", "pexels"))
        self.assertEqual(assets_mod.branches_for(shot, allow_library=False), ("pexels",))

    def test_forced_mode_pins_the_chain(self) -> None:
        for mode in (sources.MODE_PEXELS, sources.MODE_LOCAL):
            shot = {"id": 1, "source": mode}
            self.assertEqual(assets_mod.branches_for(shot, mode=mode), (mode,))

    def test_forced_mode_closes_the_library_precheck(self) -> None:
        """选了 pexels 就不许端出库里的文件 —— 否则画面哪来的没法解释。"""
        shot = {"id": 1, "source": "pexels"}
        self.assertNotIn("library", assets_mod.branches_for(shot, mode=sources.MODE_PEXELS))

    def test_library_mode_carries_the_local_fallback(self) -> None:
        shot = {"id": 1, "source": sources.MODE_LIBRARY}
        self.assertEqual(
            assets_mod.branches_for(shot, mode=sources.MODE_LIBRARY),
            (sources.MODE_LIBRARY, sources.MODE_LOCAL),
        )

    def test_graphic_and_pinned_ignore_every_mode(self) -> None:
        graphic_shot = {"id": 1, "source": "graphic", "kind": "graphic"}
        pinned = {"id": 2, "source": sources.MODE_LIBRARY, "library_asset": "D:/lib/a.png"}
        for mode in sources.MODES:
            self.assertEqual(assets_mod.branches_for(graphic_shot, mode=mode), ("graphic",))
            self.assertEqual(assets_mod.branches_for(pinned, mode=mode), ("library",))

    def test_existing_asset_follows_the_mode(self) -> None:
        """旧来源留在别的分支里的文件，换策略后不该再被认领。"""
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(task="t", root=Path(td)).ensure()
            old = ws.path("assets", "library", "shot-001.png")
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_bytes(b"x")

            shot = {"id": 1, "source": "local"}
            self.assertIsNotNone(assets_mod.existing_asset(ws, shot), "auto 下旧库命中仍算数")
            self.assertIsNone(
                assets_mod.existing_asset(ws, shot, mode=sources.MODE_LOCAL),
                "强制 local 时不该认库里的旧文件",
            )


class LibraryFallbackTest(unittest.TestCase):
    """库没命中就自动回退本地生图（票 41 的决定）。"""

    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.root = Path(td)
        self.ws = Workspace(task="t", root=self.root).ensure()
        self.made: list[str] = []

    def _stub_comfy(self):  # noqa: ANN202
        def fake_generate(prompt, dst, **kw):  # noqa: ANN001
            self.made.append(str(dst))
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            Path(dst).write_bytes(b"png")
            return {"seed": kw.get("seed")}

        real = imagegen.generate
        imagegen.generate = fake_generate  # type: ignore[assignment]
        self.addCleanup(setattr, imagegen, "generate", real)
        return object()      # 只当"客户端可用"的哨兵

    def _resolve(self, shot: dict, *, mode: str, index=None, comfy=None):  # noqa: ANN001
        return assets_mod.resolve_shot(
            shot, ws=self.ws, config=_cfg(), index=index, allow_library=True,
            min_score=1, pexels=None, comfy=comfy, mode=mode,
        )

    def test_falls_back_to_local_when_the_library_misses(self) -> None:
        comfy = self._stub_comfy()
        shot = {"id": 1, "source": sources.MODE_LIBRARY, "keywords": ["城墙"], "prompt": "a wall"}
        res = self._resolve(shot, mode=sources.MODE_LIBRARY, index={"entries": []}, comfy=comfy)
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.resolved_by, "local", "实际走了生图，resolved_by 必须说实话")
        self.assertTrue(res.info.get("fallback"), "要能识别出这是回退，好在报告里点出来")
        self.assertEqual(res.path, self.ws.path("assets", "local", "shot-001.png"))
        self.assertEqual(shot["source"], sources.MODE_LIBRARY, "source 仍是意向，不受实际路径影响")
        self.assertTrue(
            assets_mod.existing_asset(self.ws, shot, mode=sources.MODE_LIBRARY),
            "重跑必须能命中回退产物，否则每次都白生成一遍",
        )

    def test_fallback_needs_comfy_and_says_so(self) -> None:
        shot = {"id": 1, "source": sources.MODE_LIBRARY, "keywords": ["城墙"]}
        res = self._resolve(shot, mode=sources.MODE_LIBRARY, index={"entries": []}, comfy=None)
        self.assertFalse(res.ok)
        self.assertIn("ComfyUI", res.reason)

    def test_auto_mode_does_not_fall_back(self) -> None:
        """auto 下被人手改成 library 的镜，找不到就该报错，别偷偷生一张。"""
        comfy = self._stub_comfy()
        shot = {"id": 1, "source": sources.MODE_LIBRARY, "keywords": ["城墙"]}
        res = self._resolve(shot, mode=sources.MODE_AUTO, index={"entries": []}, comfy=comfy)
        self.assertFalse(res.ok)
        self.assertIn("素材库未命中", res.reason)
        self.assertEqual(self.made, [], "不该悄悄生成")

    def test_switching_back_to_auto_keeps_the_fallback_reachable(self) -> None:
        """库策略回退过的镜，切回「自动」后仍要认领得到回退产物（票 41 审查补）。

        背景：`apply_mode(auto)` 不重写逐镜 `source`，所以这些镜的 `source` 还是
        `library`。若只按 `source` 判定，切回自动会把它们全判死 —— 而图就躺在
        `assets/local/` 里。判据是 D32 留下的 `resolved_by == "local"`。
        """
        comfy = self._stub_comfy()
        shot = {"id": 1, "source": sources.MODE_LIBRARY, "keywords": ["城墙"], "prompt": "a wall"}
        res = self._resolve(shot, mode=sources.MODE_LIBRARY, index={"entries": []}, comfy=comfy)
        self.assertTrue(res.ok, res.reason)
        shot["resolved_by"] = res.resolved_by          # assets 写回 shots.json 的那一步

        self.assertIn(
            sources.MODE_LOCAL,
            assets_mod.branches_for(shot, mode=sources.MODE_AUTO),
            "auto 下也要认 local 支，否则回退产物不可达",
        )
        self.assertTrue(assets_mod.existing_asset(self.ws, shot, mode=sources.MODE_AUTO))

    def test_auto_regenerates_a_lost_fallback(self) -> None:
        """回退产物没了的话，auto 下也该重新回退生图，而不是把这一镜判死。"""
        comfy = self._stub_comfy()
        shot = {
            "id": 1, "source": sources.MODE_LIBRARY, "keywords": ["城墙"],
            "prompt": "a wall", "resolved_by": sources.MODE_LOCAL,
        }
        res = self._resolve(shot, mode=sources.MODE_AUTO, index={"entries": []}, comfy=comfy)
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.resolved_by, "local")
        self.assertTrue(res.info.get("fallback"), "要能认出这次是回退，好在报告里点出来")

    def test_the_library_hit_still_wins(self) -> None:
        """库里有就用手工的 —— 别把回退当默认行为。"""
        comfy = self._stub_comfy()
        real = self.root / "城墙.jpg"
        real.write_bytes(b"jpg")
        index = {"entries": [{"path": str(real)}]}
        shot = {"id": 1, "source": sources.MODE_LIBRARY, "keywords": ["城墙"]}
        res = self._resolve(shot, mode=sources.MODE_LIBRARY, index=index, comfy=comfy)
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.resolved_by, "library")
        self.assertFalse(res.info.get("fallback"))
        self.assertEqual(self.made, [])


class _FakeComfy:
    def __init__(self, base):  # noqa: ANN001
        self.base = base

    def health(self) -> bool:
        return True


class SourceModeRunTest(unittest.TestCase):
    """`lvs assets` 要真的把策略落成逐镜 source，并且回退要看得见。"""

    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.root = Path(td)
        self.ws = Workspace(task="t", root=self.root).ensure()

    def _args(self, **kw):  # noqa: ANN202
        base = {"force": False, "only": None, "no_library": False, "source": None}
        base.update(kw)
        return types.SimpleNamespace(**base)

    def _run(self, config: Config | None = None, **kw) -> tuple[int, str]:  # noqa: ANN003
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = assets_mod.run_command(config or _cfg(), self.ws, self._args(**kw))
        return code, buf.getvalue()

    def test_source_flag_is_persisted(self) -> None:
        """`--source pexels` 要落进 shots.json；没配 key 时**逐镜失败**、不硬拦。

        ★ 语义订正（2026-10-03，第十一轮审查）：原先这里断言 `code == 2`（整阶段硬拦）。
        但"没配 Pexels key"只影响 **source=pexels 的那些镜**，其余来源本该照常出图 ——
        硬拦违背本项目自己的"逐镜失败隔离"原则。实测代价：`source_mode=auto` + 空 key 时，
        13 镜里 8 镜本该出图却一张都不出，而用户完全不知道是那 5 镜的锅。
        现在：这些镜标记失败 → 退出码 **1**（有失败件），其余镜照常。
        """
        self.ws.write_shots({"source_mode": "auto", "shots": [{"id": 1, "source": "pexels"}]})
        code, out = self._run(source="pexels")      # 没配 key → 该镜失败，但不硬拦
        self.assertEqual(code, 1, "该是「有失败件」，不是整阶段硬拦")
        self.assertIn("pexels.api_key", out)
        self.assertEqual(sources.mode_of(self.ws.load_shots()), "pexels")

    def test_existing_mode_is_honoured_without_the_flag(self) -> None:
        """不传 --source 时用 shots.json 里记住的策略；且**真的转去本地生图那一支**。

        这条测试要环境无关，所以把生图桩成必定失败 —— 重点是"它去了哪一支"，
        不是"它成没成"。依赖本机 ComfyUI 开着才能过的话，这测试就是假的。
        """
        self.ws.write_shots({"source_mode": "local", "shots": [{"id": 1, "source": "pexels"}]})

        def boom(*a, **kw):  # noqa: ANN002, ANN003
            raise imagegen.ComfyError("stub：故意失败")

        real_gen, real_client = imagegen.generate, imagegen.ComfyClient
        imagegen.generate = boom              # type: ignore[assignment]
        imagegen.ComfyClient = _FakeComfy     # type: ignore[assignment]
        self.addCleanup(setattr, imagegen, "generate", real_gen)
        self.addCleanup(setattr, imagegen, "ComfyClient", real_client)

        _, out = self._run()                  # 不传 --source，用 shots.json 里记住的
        self.assertNotIn("pexels.api_key", out, "策略已是 local，不该还去要 Pexels key")
        shot = self.ws.load_shots()["shots"][0]
        self.assertEqual(shot["source"], "local", "策略要落成逐镜 source")
        self.assertEqual(shot["status"], "failed")
        self.assertIn("stub", shot["error"], "失败原因来自生图那一支，证明走的不是 Pexels")

    def test_library_mode_without_library_is_refused(self) -> None:
        self.ws.write_shots({"source_mode": "library", "shots": [{"id": 1, "source": "pexels"}]})
        code, out = self._run(no_library=True)
        self.assertEqual(code, 2)
        self.assertIn("矛盾", out)

    def test_library_mode_reports_the_fallback(self) -> None:
        """端到端一半：策略=库、库为空 → 回退生图，且报告里要写明白。"""
        lib = self.root / "lib"
        lib.mkdir()
        self.ws.write_shots({
            "source_mode": "library",
            "shots": [{"id": 1, "source": "pexels", "keywords": ["城墙"], "prompt": "a wall"}],
        })

        def fake_generate(prompt, dst, **kw):  # noqa: ANN001
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            Path(dst).write_bytes(b"png")
            return {"seed": 1}

        real_gen, real_client = imagegen.generate, imagegen.ComfyClient
        imagegen.generate = fake_generate        # type: ignore[assignment]
        imagegen.ComfyClient = _FakeComfy        # type: ignore[assignment]
        self.addCleanup(setattr, imagegen, "generate", real_gen)
        self.addCleanup(setattr, imagegen, "ComfyClient", real_client)

        code, out = self._run(_cfg([str(lib)]))
        self.assertEqual(code, 0)
        self.assertIn("本地素材库", out)
        self.assertIn("已回退本地生图：1 镜", out)
        shot = self.ws.load_shots()["shots"][0]
        self.assertEqual(
            (shot["source"], shot["resolved_by"], shot["status"]),
            ("library", "local", "done"),
        )


class ReferencePlanTest(unittest.TestCase):
    """参考图档（L2）的**选模板与选图判据**。

    ## 三档一致性的落点（`docs/出图流程-v2-编排方案.md` §四）

    L1 文字锚定 `{NAME}` → `lock.json.anchor`（已实现，注入在 `_shot_prompt`）
    L2 参考图 img2img（**本次接线**）—— 判据全在这两个函数里
    L3 LoRA（未做，要云训）

    ## 为什么判据要拆成两个函数

    `_reference_for`（有没有图可喂）与 `_workflow_for`（用哪个模板）必须**分开**，
    且调用顺序是"先取图、再选模板"。反过来就会出现
    "选了参考图模板却没有图可喂"—— 那时 `generate()` 报的错离根因很远。
    """

    def _lock(self, state: str = "REF", refs=("D:/x/ref.png",)):
        class _E:
            def __init__(self):
                self.state = state
                self.refs = list(refs)

            @property
            def bindable(self):
                # 只按 state 判（真实实现还要文件存在，这里不碰磁盘）
                return self.state == "REF" and bool(self.refs)

            @property
            def primary(self):
                return self.refs[0] if self.refs else ""

        class _L:
            characters = {"NAOKO": _E()}

        return _L()

    def test_no_ref_workflow_configured_means_text_only(self) -> None:
        cfg = Config({"comfyui": {"workflow": "zimage_turbo.json"}, "cast": {}}, None)
        ref, why = assets_mod._reference_for({"visual": "{NAOKO} 走过"}, cfg, self._lock())
        self.assertIsNone(ref)
        self.assertIn("ref_workflow", why)
        self.assertEqual(assets_mod._workflow_for(cfg, ref_available=False), "zimage_turbo.json")

    def test_approved_character_yields_the_reference(self) -> None:
        cfg = Config({"cast": {"ref_workflow": "zimage_scene_ref.json"}}, None)
        ref, why = assets_mod._reference_for({"visual": "{NAOKO} 走过草地"}, cfg, self._lock())
        self.assertIsNotNone(ref, why)
        self.assertEqual(assets_mod._workflow_for(cfg, ref_available=True), "zimage_scene_ref.json")

    def test_unapproved_character_is_refused_on_purpose(self) -> None:
        """★ 未批准的角色**不给**参考图。

        拿一张"还没定下来的脸"去锚定，等于把未审核的脸固化成全集标准 ——
        比不给更糟，因为它是**一致地错**。理由要能说清、并给出下一步命令。
        """
        cfg = Config({"cast": {"ref_workflow": "zimage_scene_ref.json"}}, None)
        ref, why = assets_mod._reference_for(
            {"visual": "{NAOKO} 走过"}, cfg, self._lock(state="IMG", refs=())
        )
        self.assertIsNone(ref)
        self.assertIn("approve", why)

    def test_shot_without_slot_never_uses_the_reference(self) -> None:
        """空镜不需要参考图 —— 别把它也拖进 img2img（那会白白改变画面构成）。"""
        cfg = Config({"cast": {"ref_workflow": "zimage_scene_ref.json"}}, None)
        ref, why = assets_mod._reference_for({"visual": "一片安静的草地"}, cfg, self._lock())
        self.assertIsNone(ref)
        self.assertIn("无人物槽位", why)

    def test_missing_lock_does_not_crash(self) -> None:
        cfg = Config({"cast": {"ref_workflow": "zimage_scene_ref.json"}}, None)
        ref, why = assets_mod._reference_for({"visual": "{NAOKO} 走过"}, cfg, None)
        self.assertIsNone(ref)
        self.assertIn("定妆库", why)

    def test_missing_template_fails_loudly_with_the_right_key(self) -> None:
        """★ 配了不存在的模板 → 当场报错，并指出**正确的配置键**。

        修之前这条错要等到生图时才炸，且提示写的是 `[comfyui].workflow` ——
        键名是错的，用户会去改一个跟问题无关的配置。
        """
        cfg = Config({"cast": {"ref_workflow": "根本没有这个模板.json"}}, None)
        with self.assertRaises(assets_mod.AssetError) as ctx:
            assets_mod._workflow_for(cfg, ref_available=True)
        msg = str(ctx.exception)
        self.assertIn("[cast].ref_workflow", msg)
        self.assertIn("不存在", msg)

    def test_ref_available_false_skips_the_template_check(self) -> None:
        """没有图可喂时不查模板 —— 否则空集会因为一个用不上的配置项而失败。"""
        cfg = Config({
            "comfyui": {"workflow": "zimage_turbo.json"},
            "cast": {"ref_workflow": "根本没有这个模板.json"},
        }, None)
        self.assertEqual(assets_mod._workflow_for(cfg, ref_available=False), "zimage_turbo.json")

    def test_denoise_is_taken_from_cast_config(self) -> None:
        """`[cast].ref_denoise` 要真的落到模板占位符上（那是唯一的"松紧"旋钮）。"""
        cfg = Config({"cast": {"ref_denoise": 0.72}}, None)
        self.assertEqual(assets_mod._comfy_extra(cfg).get("DENOISE"), 0.72)

    def test_denoise_absent_means_no_override(self) -> None:
        """没配就**别**塞值 —— 让模板里的字面量生效（否则等于悄悄改了默认）。"""
        self.assertNotIn("DENOISE", assets_mod._comfy_extra(Config({"cast": {}}, None)))


if __name__ == "__main__":
    unittest.main()
