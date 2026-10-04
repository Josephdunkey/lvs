"""阶段间契约（`lvs/handoff.py`）的测试。

这一层防的是多 agent 流水线里最贵的失败形状 ——
**错的东西被当成对的继续用**（MAST 报告里「交互错位」占 36.94%）。
所以测试重点不只是"该报的报了"，还要"**不该报的别报**"：
分镜表里合法的多样形态（graphic 镜、start 为 None 的静音镜）不能被误判成坏数据。
"""

from __future__ import annotations

from lvs import handoff


def _shot(**kw) -> dict:
    base = {"id": 1, "source": "local", "narration": "一句旁白。", "visual": "一片草地"}
    base.update(kw)
    return base


# ---- 该报的 ----------------------------------------------------------------


def test_flags_bad_source():
    problems = handoff.validate_shots([_shot(source="bogus")])
    assert any("source" in p for p in problems)


def test_flags_missing_narration():
    assert any("narration" in p for p in handoff.validate_shots([_shot(narration="")]))


def test_flags_missing_visual():
    assert any("visual" in p for p in handoff.validate_shots([_shot(visual="   ")]))


def test_flags_missing_id():
    s = _shot()
    del s["id"]
    assert any("id" in p for p in handoff.validate_shots([s]))


def test_flags_duplicate_id():
    """重复 id 会让后一项覆盖前一项的素材/音轨 —— 静默错位。"""
    problems = handoff.validate_shots([_shot(id=1), _shot(id=1, narration="另一句")])
    assert any("重复" in p for p in problems)


def test_flags_non_dict_item():
    assert handoff.validate_shots(["not a dict"])


def test_flags_non_list_payload():
    assert handoff.validate_shots({"shots": []})


def test_problem_message_names_the_producer():
    """★ 报错必须点名**是哪个上游产的**，否则拿到报错的人无从下手。"""
    problems = handoff.validate_shots([_shot(source="bogus")], producer="lvs shots")
    assert any("lvs shots" in p for p in problems)


# ---- 不该报的（误报会让整个校验器失去信任） --------------------------------


def test_accepts_clean_table():
    assert handoff.validate_shots([_shot(), _shot(id=2)]) == []


def test_accepts_graphic_source():
    assert handoff.validate_shots([_shot(source="graphic")]) == []


def test_accepts_pexels_and_library():
    assert handoff.validate_shots([_shot(source="pexels"), _shot(id=2, source="library")]) == []


def test_accepts_shot_without_timeline():
    """静音镜（`start`/`end` 为 None）是合法的中间态，不该被判坏。"""
    assert handoff.validate_shots([_shot(start=None, end=None)]) == []


def test_accepts_empty_table():
    """空表不是"坏数据"，是"还没拆镜"—— 由调用方按各自语义处理。"""
    assert handoff.validate_shots([]) == []


# ---- 渲染与一步到位的门面 --------------------------------------------------


def test_check_shots_or_message_empty_when_ok():
    assert handoff.check_shots_or_message([_shot()], stage="配音") == ""


def test_check_shots_or_message_has_three_parts_when_bad():
    msg = handoff.check_shots_or_message(
        [_shot(source="bogus")], stage="配音（voice）", command="lvs shots --task t1",
    )
    assert "配音（voice）" in msg
    assert "契约" in msg
    assert "lvs shots --task t1" in msg


def test_render_problems_truncates_long_lists():
    shots = [_shot(id=i, source="bogus") for i in range(1, 40)]
    msg = handoff.render_problems(
        handoff.validate_shots(shots), stage="x", producer="lvs shots"
    )
    assert "省略" in msg


# ---- 按消费者声明的必需字段（避免"一套卡所有人"造成的误报） -------------------
#
# ★ 这是实施时真踩出来的：`assets` 只取图、根本不用 `narration`，
# 用完整契约去卡它 = **误报** —— 而无理由卡住流水线会让人不再信任校验器。
# 校验器的价值全在**报得准**。


def test_assets_does_not_require_narration():
    """素材阶段只取图，`narration` 缺了不影响它。"""
    shot = {"id": 1, "source": "local"}          # 无 narration / visual
    assert handoff.check_shots_or_message(shot and [shot], stage="素材", consumer="assets") == ""


def test_voice_requires_narration():
    """配音要合成旁白，缺了会合出空音轨 —— 这才是该拦的。"""
    shot = {"id": 1, "source": "local"}
    msg = handoff.check_shots_or_message([shot], stage="配音", consumer="voice")
    assert msg and "narration" in msg


def test_assets_requires_source():
    """缺 `source` 会**静默走错分支**（该生图的去翻库）—— 该拦。"""
    shot = {"id": 1, "narration": "x"}
    msg = handoff.check_shots_or_message([shot], stage="素材", consumer="assets")
    assert msg and "source" in msg


def test_every_consumer_requires_id():
    """`id` 对谁都必需：缺了会变 0 / 重复 → 逐镜对位错乱（素材/音轨/字幕全错）。"""
    for consumer in handoff.CONSUMER_REQUIREMENTS:
        assert "id" in handoff.requirements_for(consumer), consumer
    # 未登记的消费者按**完整契约**（从严）
    assert handoff.requirements_for("nope") == handoff.REQUIRED_FIELDS


def test_unknown_consumer_falls_back_to_full_contract():
    """没登记的消费者从严 —— 宁可多要求，也不要放过一个该拦的。"""
    shot = {"id": 1, "source": "local"}          # 缺 narration/visual
    msg = handoff.check_shots_or_message([shot], stage="x", consumer="未知阶段")
    assert msg, "未登记的消费者应按完整契约拦下"


def test_qc_only_needs_id():
    """巡检的其余字段都是 `.get()` 优雅降级 —— 真正会静默出错的是 `id`。"""
    assert handoff.requirements_for("qc") == ("id",)
