"""shots 阶段单元测试（票据 06/07/08）。不依赖网络与 LLM。"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
from pathlib import Path

import unittest

from lvs import prompting as prompts
from lvs import shots
from lvs import shots as shots_mod
from lvs.workspace import Workspace


class SplitSentencesTest(unittest.TestCase):
    def test_basic_periods(self):
        self.assertEqual(
            shots.split_sentences("公元前256年。周天子，最后一次结账。"),
            ["公元前256年。", "周天子，最后一次结账。"],
        )

    def test_no_text_loss(self):
        text = "第一句。第二句！第三句？最后一句"
        joined = "".join(shots.split_sentences(text))
        self.assertEqual(joined.replace(" ", ""), text.replace(" ", ""))

    def test_quotes_not_broken(self):
        # 引号内的句号不应作为断句点：整段引用必须落在同一个句子里
        text = "他说：「天下大势，分久必合。」众人默然。"
        got = shots.split_sentences(text)
        self.assertTrue(any("「" in s and "」" in s for s in got), got)
        self.assertEqual("".join(got), text)
        # 对照：无引号时正常断成两句
        self.assertEqual(len(shots.split_sentences("第一句。第二句。")), 2)

    def test_long_sentence_split(self):
        text = "甲" * 30 + "，" + "乙" * 30 + "，" + "丙" * 30 + "。"
        got = shots.split_sentences(text, max_chars=40)
        self.assertGreater(len(got), 1)
        self.assertEqual("".join(got), text)

    def test_short_fragment_merged(self):
        got = shots.split_sentences("这是一句完整的话。但。")
        self.assertEqual("".join(got), "这是一句完整的话。但。")


class HeuristicTest(unittest.TestCase):
    def test_source_local_for_abstract(self):
        self.assertEqual(shots.heuristic_source("竹简上三笔记载并排"), "local")

    def test_source_pexels_for_realistic(self):
        self.assertEqual(shots.heuristic_source("士兵列队走过城墙与河流"), "pexels")

    def test_keywords_and_prompt_nonempty(self):
        self.assertTrue(shots.heuristic_keywords("竹简"))
        self.assertIn(shots.STYLE_SUFFIX, shots.heuristic_prompt("竹简"))


class StyleTest(unittest.TestCase):
    """画面风格可配置（票 44）：整片风格是**具名预设**，不是写死的常量。"""

    def test_default_is_historical_documentary(self):
        self.assertEqual(shots.style_for(None).name, shots.STYLE_NAME)
        self.assertEqual(shots.style_for("").suffix, shots.STYLE_SUFFIX)

    def test_named_preset_changes_the_suffix(self):
        manga = shots.style_for("jp-youth-manga-bw")
        self.assertNotEqual(manga.suffix, shots.STYLE_SUFFIX)
        self.assertNotIn("cinematic", manga.suffix.lower())

    def test_unknown_name_raises_not_silently_defaults(self):
        """风格名拼错要报错 —— 否则整片会悄悄按默认风格出图，跑完才发现不对。"""
        with self.assertRaises(KeyError):
            shots.style_for("no-such-style")

    def test_heuristic_prompt_uses_the_given_style(self):
        manga = shots.style_for("jp-youth-manga-bw")
        p = shots.heuristic_prompt("竹简在月光下", style=manga)
        self.assertIn(manga.suffix, p)
        self.assertNotIn(shots.STYLE_SUFFIX, p)

    def test_heuristic_fill_uses_the_given_style(self):
        manga = shots.style_for("jp-youth-manga-bw")
        s = shots.build_skeleton(self._parse(), style_name=manga.name)
        shots._heuristic_fill(s, style=manga)
        self.assertIn(manga.suffix, s[0]["prompt"])
        self.assertEqual(s[0]["style"], "jp-youth-manga-bw")

    def test_apply_llm_item_uses_the_given_style(self):
        manga = shots.style_for("jp-youth-manga-bw")
        shot = {"id": 1, "visual": "城墙", "kind": None, "narration": "旁白。",
                "segment_heading": "h", "source": "", "prompt": "", "keywords": []}
        shots._apply_llm_item(
            shot, {"beat": 0, "kind": "scene", "prompt": "a cold moon"},
            [{"text": "城墙", "kind": None}], prompts.MODE_GRAPHIC, None, manga,
        )
        self.assertIn(manga.suffix, shot["prompt"])

    @staticmethod
    def _parse():
        return {
            "title": "T",
            "meta": {"title_card": "士兵列队走过城墙与河流"},
            "cold_open": {"start": 0, "end": 30, "text": ["第一句。"]},
            "segments": [],
        }


class SkeletonTest(unittest.TestCase):
    def _parse(self):
        return {
            "title": "T",
            "meta": {"title_card": "标题卡"},
            "cold_open": {"start": 0, "end": 30, "text": ["第一句。第二句。"]},
            "segments": [
                {
                    "index": 1,
                    "heading": "开局",
                    "start": 30.0,
                    "end": 60.0,
                    "narration": ["旁白一。", "旁白二。"],
                    "visual_marks": ["画面甲"],
                    "flow": [
                        {"kind": "visual", "text": "画面甲"},
                        {"kind": "narration", "text": "旁白一。"},
                        {"kind": "narration", "text": "旁白二。"},
                    ],
                }
            ],
        }

    def test_counts_and_ids(self):
        s = shots.build_skeleton(self._parse())
        self.assertEqual([x["id"] for x in s], [1, 2, 3, 4])
        self.assertEqual(s[0]["segment"], 0)          # 冷开场
        self.assertEqual(s[2]["segment"], 1)

    def test_visual_mapping(self):
        s = shots.build_skeleton(self._parse())
        # 冷开场用标题卡兜底
        self.assertEqual(s[0]["visual"], "标题卡")
        # 段内两句都映射到最近的画面位
        self.assertEqual(s[2]["visual"], "画面甲")
        self.assertEqual(s[3]["visual"], "画面甲")

    def test_narration_text_preserved(self):
        s = shots.build_skeleton(self._parse())
        self.assertEqual("".join(x["narration"] for x in s[2:]), "旁白一。旁白二。")

    def test_kind_and_visual_come_from_beats(self):
        parse = self._parse()
        parse["segments"][0]["flow"] = [
            {"kind": "narration", "text": "旁白一。"},
            {"kind": "narration", "text": "旁白二。"},
            {
                "kind": "visual", "text": "分栏呈现 → 玉玺推回桌面",
                "beats": [
                    {"text": "分栏呈现", "kind": "graphic"},
                    {"text": "玉玺推回桌面", "kind": "scene"},
                ],
            },
        ]
        parse["segments"][0]["visual_marks"] = ["分栏呈现 → 玉玺推回桌面"]
        seg_shots = [x for x in shots.build_skeleton(parse) if x["segment"] == 1]
        self.assertEqual([x["visual"] for x in seg_shots], ["分栏呈现", "玉玺推回桌面"])
        self.assertEqual([x["kind"] for x in seg_shots], ["graphic", "scene"])

    def test_validate_detects_bad_source(self):
        s = shots.build_skeleton(self._parse())
        shots._heuristic_fill(s)
        s[0]["source"] = "bogus"
        problems = shots._validate(s)
        self.assertTrue(any("source" in p for p in problems))


class BeatAssignmentTest(unittest.TestCase):
    """票据 27：段内句子要摊到该段画面位的各个 beat 上，而不是整段共用一条。"""

    def _seg(self, texts, beats):
        flow = [{"kind": "narration", "text": t} for t in texts]
        flow.append(
            {"kind": "visual", "text": " → ".join(b["text"] for b in beats), "beats": beats}
        )
        return {
            "index": 1, "heading": "H", "flow": flow, "narration": [],
            "visual_marks": [b["text"] for b in beats],
        }

    def test_beats_spread_across_sentences(self):
        beats = [{"text": k, "kind": None} for k in "ABC"]
        got = shots._sentences_with_visual(self._seg([f"句子{i}。" for i in range(6)], beats))
        self.assertEqual([g[1] for g in got], ["A", "A", "B", "B", "C", "C"])

    def test_single_beat_covers_all(self):
        got = shots._sentences_with_visual(
            self._seg([f"句子{i}。" for i in range(4)], [{"text": "唯一", "kind": None}])
        )
        self.assertEqual(len(got), 4)
        self.assertEqual({g[1] for g in got}, {"唯一"})

    def test_kind_travels_with_beat(self):
        beats = [
            {"text": "分栏呈现", "kind": "graphic"},
            {"text": "玉玺推回桌面", "kind": "scene"},
        ]
        got = shots._sentences_with_visual(self._seg(["甲。", "乙。"], beats))
        self.assertEqual([g[2] for g in got], ["graphic", "scene"])

    def test_legacy_segment_without_flow(self):
        seg = {"index": 1, "heading": "H", "narration": ["一句话。"], "visual_marks": ["甲", "乙"]}
        got = shots._sentences_with_visual(seg)
        self.assertEqual(len(got), 1)
        self.assertIn(got[0][1], {"甲", "乙"})

    def test_no_visual_at_all_falls_back_to_heading(self):
        seg = {"index": 1, "heading": "段标题", "narration": ["一句话。"], "visual_marks": []}
        got = shots._sentences_with_visual(seg)
        self.assertEqual(got[0][1], "段标题")


class HeuristicFillKindTest(unittest.TestCase):
    def test_graphic_beat_gets_no_prompt(self):
        s = [shots._new_shot(1, 1, "H", "旁白。", "七种反抗形态分栏呈现", "graphic")]
        shots._heuristic_fill(s)
        self.assertEqual(s[0]["source"], "graphic")
        self.assertEqual(s[0]["prompt"], "")
        self.assertEqual(s[0]["keywords"], [])

    def test_scene_prompt_is_sanitized(self):
        raw = '玉玺被推回桌面（核心画面）→ 苏代"所得民无几何人"砸屏'
        s = [shots._new_shot(1, 1, "H", "旁白。", raw, "scene")]
        shots._heuristic_fill(s)
        self.assertIn("玉玺被推回桌面", s[0]["prompt"])
        self.assertNotIn("核心画面", s[0]["prompt"])
        self.assertNotIn('"', s[0]["prompt"])
        self.assertIn(shots.STYLE_SUFFIX, s[0]["prompt"])

    def test_purely_editorial_beat_falls_back_to_heading(self):
        s = [shots._new_shot(1, 1, "王莽的账本", "旁白。", '"45万"砸屏', "scene")]
        shots._heuristic_fill(s)
        self.assertIn("王莽的账本", s[0]["prompt"])

    def test_classifies_when_kind_missing(self):
        s = [shots._new_shot(1, 1, "H", "旁白。", "时间轴：前403 → 前313", None)]
        shots._heuristic_fill(s)
        self.assertEqual(s[0]["kind"], "graphic")

    def test_photo_mode_forces_everything_to_scene(self):
        s = [shots._new_shot(1, 1, "H", "旁白。", "七种反抗形态分栏呈现", "graphic")]
        shots._heuristic_fill(s, mode="photo")
        self.assertEqual(s[0]["kind"], "scene")
        self.assertIn(shots.STYLE_SUFFIX, s[0]["prompt"])


if __name__ == "__main__":
    unittest.main()


class BeatKindConsistencyTest(unittest.TestCase):
    """票 32 现象 A：同一 beat 被 LLM 逐镜判成不同种类 → 按 beat 记忆首次判定。

    真机症状（`small` 110 镜）：beat「中间一道"科目转换器"」铺在 5 个镜上，
    4 镜判成 `local`、1 镜判成 `graphic` —— 同一个屏幕元素一会儿是照片、一会儿是字卡。
    """

    BEAT = "中间一道“科目转换器”"
    OTHER = "时间轴：前403 → 前313"

    def _beats(self):
        return [{"text": self.BEAT, "kind": None}, {"text": self.OTHER, "kind": None}]

    def _shot(self) -> dict:
        return {"id": 1, "narration": "旁白。", "visual": self.BEAT, "kind": "scene"}

    def test_first_verdict_is_remembered(self) -> None:
        memo: dict = {}
        beats = self._beats()
        first = self._shot()
        shots_mod._apply_llm_item(
            first, {"beat": 0, "kind": "graphic"}, beats, prompts.MODE_GRAPHIC, memo
        )
        self.assertEqual(first["kind"], "graphic")

        second = self._shot()               # 同一个 beat，LLM 这次说是实拍
        shots_mod._apply_llm_item(
            second, {"beat": 0, "kind": "scene", "prompt": "a wall"}, beats, prompts.MODE_GRAPHIC, memo
        )
        self.assertEqual(second["kind"], "graphic", "同一 beat 必须复用首次判定")
        self.assertEqual(second["source"], "graphic")

    def test_without_memo_behaviour_is_unchanged(self) -> None:
        beats = self._beats()
        shot = self._shot()
        shots_mod._apply_llm_item(shot, {"beat": 0, "kind": "graphic"}, beats, prompts.MODE_GRAPHIC)
        self.assertEqual(shot["kind"], "graphic")

    def test_memo_does_not_leak_across_beats(self) -> None:
        memo: dict = {}
        beats = self._beats()
        a = self._shot()
        shots_mod._apply_llm_item(a, {"beat": 0, "kind": "graphic"}, beats, prompts.MODE_GRAPHIC, memo)
        b = {"id": 2, "narration": "旁白。", "visual": self.OTHER, "kind": "scene"}
        shots_mod._apply_llm_item(
            b, {"beat": 1, "kind": "scene", "prompt": "timeline", "source": "local"},
            beats, prompts.MODE_GRAPHIC, memo,
        )
        self.assertEqual(b["kind"], "scene")
        self.assertEqual(a["kind"], "graphic")

    def test_photo_mode_still_ignores_the_verdict(self) -> None:
        """`photo` 模式下所有 beat 一律当场景 —— 记忆不能把它掰回卡片。"""
        memo: dict = {}
        shot = self._shot()
        shots_mod._apply_llm_item(
            shot, {"beat": 0, "kind": "graphic"}, self._beats(), prompts.MODE_PHOTO, memo
        )
        self.assertEqual(shot["kind"], "scene")


class LlmEnrichConsistencyTest(unittest.TestCase):
    """整条 `_llm_enrich` 走一遍：一个批次里同一 beat 判得不一样，写回后必须一样。"""

    def setUp(self) -> None:
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.ws = Workspace(task="t", root=Path(td)).ensure()
        self.beat = "中间一道“科目转换器”"

    def test_conflicting_verdicts_are_collapsed(self) -> None:
        shots = [
            {"id": i, "segment": 1, "narration": f"旁白{i}。", "visual": self.beat,
             "kind": "scene", "segment_heading": "H"}
            for i in (1, 2, 3, 4)
        ]
        real = shots_mod.llm_mod.chat_json

        def fake(client, messages, **kw):  # noqa: ANN001
            payload = json.loads(messages[1]["content"].split("分镜：\n", 1)[1])
            return {"shots": [
                {"id": s["id"], "beat": 0,
                 "kind": "scene" if s["id"] == 1 else "graphic",   # 故意自相矛盾
                 "scene": "a wall", "prompt": "a wall", "keywords": ["wall"], "source": "local"}
                for s in payload["shots"]
            ]}

        shots_mod.llm_mod.chat_json = fake            # type: ignore[assignment]
        self.addCleanup(setattr, shots_mod.llm_mod, "chat_json", real)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            shots_mod._llm_enrich(None, shots, self.ws, {1: [{"text": self.beat, "kind": None}]})
        kinds = {s["kind"] for s in shots}
        self.assertEqual(len(kinds), 1, f"同一 beat 出现了多种判定：{kinds}")


class ColdOpenVisualTest(unittest.TestCase):
    """冷开场的镜要用**自己的画面位**，而不是退化成标题文案。

    实测踩过：`fallback_visual = meta["title_card"]` 在作者没给冷开场画面位时是合理的兜底，
    但作者给了画面位也读不到（那时 parse 还不收），于是片头 10 镜的提示词全变成
    「主用（金句钩子）：他念了一整夜的经，输给了一首诗｜《白峰》」—— 叫模型把标题画出来。
    """

    def _parse(self, marks):
        return {
            "title": "T",
            "meta": {"title_card": "标题文案不该当画面"},
            "cold_open": {"start": 0, "end": 30, "text": ["甲句在此。乙句在此。丙句在此。丁句在此。"],
                          "visual_marks": marks},
            "segments": [],
        }

    def test_marks_are_used_in_order(self):
        shots_ = shots_mod.build_skeleton(self._parse([
            "土墩与三块石头",
            "灯笼与石阶",
        ]))
        self.assertEqual(len(shots_), 4)
        visuals = [s["visual"] for s in shots_]
        self.assertEqual(visuals[0], "土墩与三块石头")
        self.assertEqual(visuals[1], "土墩与三块石头")
        self.assertEqual(visuals[2], "灯笼与石阶")
        self.assertEqual(visuals[3], "灯笼与石阶")
        for v in visuals:
            self.assertNotIn("标题文案不该当画面", v)

    def test_no_marks_falls_back_to_title_card(self):
        """作者没给画面位时，兜底仍是标题卡 —— 老行为不许被改坏。"""
        shots_ = shots_mod.build_skeleton(self._parse([]))
        self.assertTrue(shots_)
        self.assertTrue(all(s["visual"] == "标题文案不该当画面" for s in shots_))

    def test_single_mark_covers_all_sentences(self):
        shots_ = shots_mod.build_skeleton(self._parse(["唯一的画面"]))
        self.assertEqual(len(shots_), 4)
        self.assertTrue(all(s["visual"] == "唯一的画面" for s in shots_))

    def test_more_marks_than_sentences_does_not_lose_them(self):
        """画面位比句子还多时**不许丢**：丢一条，画面位清单就对不上数了。"""
        shots_ = shots_mod.build_skeleton(self._parse([
            "甲景", "乙景",
            "[画面位] [场景] 丙景", "[画面位] [场景] 丁景",
            "[画面位] [场景] 戊景", "[画面位] [场景] 己景",
        ]))
        self.assertEqual(len(shots_), 4)
        self.assertTrue(all(s["visual"] for s in shots_))


class PromptNegationTest(unittest.TestCase):
    """提示词否定式巡检。

    背景（2026-10-03 实测，白峰 435 镜）：拍摄稿的 `[画面位]` 是禁否定式的（`lvs check` C021 守着），
    但提示词是 LLM 重写的，它把「檐下没有灯火」译成了 `no lamps beneath`、
    「身边一个人也没有」译成 `no figures present`。cfg=1.0 的蒸馏模型**没有负向引导**，
    这两句等于命令它画灯、画人 —— 「空镜长人」就是这么来的。
    """

    def test_detects_english_negations(self):
        shots_ = [
            {"id": 1, "prompt": "row of palace eaves at dusk, no lamps beneath, deep indigo sky"},
            {"id": 2, "prompt": "a vacant courtyard, no figures present, dim twilight"},
            {"id": 3, "prompt": "pine forest in total darkness, no light anywhere"},
            {"id": 4, "prompt": "a lone lantern on stone steps, wet moss"},
        ]
        got = shots_mod.prompt_negations(shots_)
        self.assertEqual(sorted(got), [1, 2, 3])
        self.assertIn("no", got[1])

    def test_ignores_chinese_fallback_prompts(self):
        """启发式兜底的中文提示词不在这里判 —— 它的否定式来源仍是拍摄稿，归 C021 管。"""
        shots_ = [{"id": 1, "prompt": "檐下没有灯火. japanese woodblock print from the Edo period"}]
        self.assertEqual(shots_mod.prompt_negations(shots_), {})

    def test_does_not_flag_substrings(self):
        """`noir` / `north` / `notice` 里都有 `no`，不能被当成否定词。"""
        shots_ = [
            {"id": 1, "prompt": "film noir alley, north-facing window, notice board"},
            {"id": 2, "prompt": "an honest record of nothingness"},  # nothingness 也放过
        ]
        self.assertEqual(shots_mod.prompt_negations(shots_), {})

    def test_empty_prompt_skipped(self):
        self.assertEqual(shots_mod.prompt_negations([{"id": 1, "prompt": ""}]), {})
        self.assertEqual(shots_mod.prompt_negations([{"id": 1}]), {})

    def test_repair_rejects_still_negative_rewrite(self):
        """改写结果若自己还带否定词，必须**保留原句**，不能当成修好了。"""
        class FakeClient:
            model = "fake"

        shots_ = [{"id": 7, "prompt": "eaves at dusk, no lamps beneath", "visual": "檐下"}]
        orig = shots_mod.prompt_negations(shots_)
        self.assertEqual(list(orig), [7])

        import lvs.shots as S
        calls = []

        def fake_chat(client, messages, retries=1, temperature=0.0):
            calls.append(messages)
            return [{"id": 7, "prompt": "eaves with no lamp glow at dusk"}]  # 还是否定式

        real = S.llm_mod.chat_json
        S.llm_mod.chat_json = fake_chat
        try:
            fixed = S._llm_fix_negations(FakeClient(), shots_, orig)
        finally:
            S.llm_mod.chat_json = real
        self.assertEqual(fixed, 0)
        self.assertEqual(shots_[0]["prompt"], "eaves at dusk, no lamps beneath")

    def test_repair_accepts_clean_rewrite(self):
        class FakeClient:
            model = "fake"

        import lvs.shots as S
        shots_ = [{"id": 7, "prompt": "eaves at dusk, no lamps beneath", "visual": "檐下"}]
        orig = shots_mod.prompt_negations(shots_)
        real = S.llm_mod.chat_json
        S.llm_mod.chat_json = lambda *a, **k: [
            {"id": 7, "prompt": "row of ice-cold unlit eaves at dusk, deep indigo sky"}
        ]
        try:
            fixed = S._llm_fix_negations(FakeClient(), shots_, orig)
        finally:
            S.llm_mod.chat_json = real
        self.assertEqual(fixed, 1)
        self.assertEqual(shots_mod.prompt_negations(shots_), {})
