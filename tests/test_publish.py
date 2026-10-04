"""投稿物料（`lvs/publish.py`）—— 封面叠字与"可直接粘贴"的稿件。

## 这组测试守的是什么

1. **封面字来自稿子，不是编的**：`【封面文案】` 的 `行1/行2/行3` 必须被原样取出，
   顺序按编号而不是按出现顺序（作者常把"画面建议"写在后面）。
2. **真的把字画上去了**：无底图（渐变底）也要能出图，且叠字区域必须与不叠字
   的底图**不一样** —— 否则"叠字"可能只是写了文件却没画。
3. **缺东西只降级不阻断**：没有 `final.mp4` 只是少报时长 + 一条警告，物料照出。
4. **幂等**：同样的输入跑两次，字节一致（不含时间戳的 PNG / md）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lvs import publish
from lvs.config import Config
from lvs.workspace import Workspace

_META = {
    "title_card": "主用（金句钩子）：先让你见到她，再让你失去她｜《雨月物语》夜宿荒宅",
    "cover": [
        "行3：《雨月物语》夜宿荒宅",
        "行1：他七年没回家",
        "行2：那晚睡的床底下是什么",
        "画面建议：浮世绘木刻质感，靛蓝夜与朱红为主色。",
        "叠字前先做 zoom-out 测试。",
    ],
}


def _ws(tmp_path: Path) -> Workspace:
    return Workspace(task="t", root=tmp_path).ensure()


def _write_parse(ws: Workspace, meta: dict | None = None) -> None:
    ws.path("parse.json").write_text(
        json.dumps({"source": "稿.md", "title": "雨月物语", "meta": meta or _META,
                    "cold_open": {"text": ["九月初九，一个书生摆好酒菜。", "他等了一整天。"]},
                    "segments": [{"narration": "约好来的义兄被锁在一百里外。"}]},
                   ensure_ascii=False),
        encoding="utf-8",
    )


def _config(tmp_path: Path, **publish_keys) -> Config:
    data = {
        "publish": {
            "book_title": "雨月物语·春雨物语",
            "author": "上田秋成",
            "translator": "王新禧",
            "publisher": "浙江出版集团数字传媒",
            "series": "《雨月物语·春雨物语》19 期",
            "episode": 2,
            "tags": ["雨月物语", "上田秋成"],
        }
    }
    data["publish"].update(publish_keys)
    return Config(data, tmp_path / "config.toml")


# ---- 取值 -------------------------------------------------------------------


def test_cover_lines_follow_the_numbers_not_the_order():
    lines = publish.cover_lines(_META)
    assert lines == ["他七年没回家", "那晚睡的床底下是什么", "《雨月物语》夜宿荒宅"]


def test_cover_lines_override_wins():
    assert publish.cover_lines(_META, "甲|乙") == ["甲", "乙"]


def test_cover_lines_fall_back_to_title_card():
    assert publish.cover_lines({"title_card": "标题甲"}) == ["标题甲"]


def test_title_candidates_strip_the_editorial_prefix():
    got = publish.title_candidates({"title": "雨月物语"}, _META)
    assert got[0] == "先让你见到她，再让你失去她｜《雨月物语》夜宿荒宅"
    assert all(len(t) <= publish.TITLE_MAX for t in got[:1])


def test_description_has_credit_line_and_image_note(tmp_path: Path):
    desc = publish.description({"cold_open": {"text": ["甲。乙。"]}}, _config(tmp_path), ["行一"])
    assert "原著：《雨月物语·春雨物语》 上田秋成 著 / 王新禧 译" in desc
    assert "AI 自绘" in desc
    assert "本期是《雨月物语·春雨物语》19 期第 2 期。" in desc


def test_tags_are_capped_and_deduped(tmp_path: Path):
    got = publish.tags({"title": "雨月物语"}, _config(tmp_path, tags=["甲"] * 12), [])
    assert len(got) <= publish.TAG_MAX
    assert len(set(got)) == len(got)


# ---- 封面渲染 ---------------------------------------------------------------


def test_render_cover_draws_text_on_a_gradient(tmp_path: Path):
    out = publish.render_cover(None, ["第一行", "第二行"], publish.BILIBILI_SIZE,
                               tmp_path / "c.png")
    from PIL import Image

    with Image.open(out) as im:
        assert im.size == publish.BILIBILI_SIZE
        bright = sum(1 for px in im.convert("L").getdata() if px > 200)
    assert bright > 400, "封面上没找到足够的高亮像素 —— 字可能根本没画上去"


def test_render_cover_refuses_empty_lines(tmp_path: Path):
    with pytest.raises(publish.PublishError):
        publish.render_cover(None, [], publish.BILIBILI_SIZE, tmp_path / "c.png")


def test_render_cover_uses_the_base_image(tmp_path: Path):
    from PIL import Image

    base = tmp_path / "base.png"
    Image.new("RGB", (400, 900), (120, 30, 30)).save(base)   # 竖图 → 必须裁成 16:9
    out = publish.render_cover(base, ["甲"], publish.BILIBILI_SIZE, tmp_path / "c.png")
    with Image.open(out) as im:
        assert im.size == publish.BILIBILI_SIZE


# ---- 组装 / 端到端 ----------------------------------------------------------


def test_build_meta_warns_when_final_missing(tmp_path: Path):
    ws = _ws(tmp_path)
    _write_parse(ws)
    meta = publish.build_meta(ws, _config(tmp_path))
    assert meta["video"]["seconds"] is None
    assert any("final.mp4" in w for w in meta["warnings"])
    assert meta["cover"]["lines"]


def test_build_meta_needs_parse_json(tmp_path: Path):
    with pytest.raises(publish.PublishError):
        publish.build_meta(_ws(tmp_path), _config(tmp_path))


def test_run_command_writes_products_and_manifest(tmp_path: Path, capsys):
    ws = _ws(tmp_path)
    _write_parse(ws)

    class Args:
        lines = None
        base = None
        llm = False
        out = None

    assert publish.run_command(_config(tmp_path), ws, Args()) == 0
    for name in ("meta.json", "bilibili.md", "douyin.md", "cover-bilibili.png", "cover-douyin.png"):
        assert ws.path("publish", name).is_file(), name
    meta = json.loads(ws.path("publish", "meta.json").read_text(encoding="utf-8"))
    assert meta["bilibili"]["recommended_title"]
    assert "publish" in ws.manifest.get("stages", {})
    md = ws.path("publish", "bilibili.md").read_text(encoding="utf-8")
    assert "他七年没回家" in md
    # 底图那一行本身已自带“底图”字样，模板再加前缀会变成“底图：指定的底图：…”
    assert "底图：指定的底图" not in md and "底图：自动挑" not in md
    assert any(x.startswith("- ") and ("底图" in x or "没有可用画面" in x) for x in md.splitlines())


def test_run_command_is_idempotent(tmp_path: Path):
    ws = _ws(tmp_path)
    _write_parse(ws)

    class Args:
        lines = None
        base = None
        llm = False
        out = None

    publish.run_command(_config(tmp_path), ws, Args())
    first = (ws.path("publish", "cover-bilibili.png").read_bytes(),
             ws.path("publish", "bilibili.md").read_bytes())
    publish.run_command(_config(tmp_path), ws, Args())
    second = (ws.path("publish", "cover-bilibili.png").read_bytes(),
              ws.path("publish", "bilibili.md").read_bytes())
    assert first == second


def test_run_command_bad_input_returns_2(tmp_path: Path):
    class Args:
        lines = None
        base = None
        llm = False
        out = None

    assert publish.run_command(_config(tmp_path), _ws(tmp_path), Args()) == 2


def test_cli_exposes_publish_with_json():
    from lvs import cli

    parser = cli.build_parser()
    subs: dict = {}
    for action in parser._actions:  # noqa: SLF001
        if getattr(action, "choices", None) and isinstance(action.choices, dict):
            subs.update(action.choices)
    assert "publish" in subs
    flags = {s for a in subs["publish"]._actions for s in a.option_strings}  # noqa: SLF001
    assert {"--json", "--lines", "--base", "--out", "--llm"} <= flags


def test_stage_table_lists_publish_as_a_driver():
    from lvs import stage as stage_mod

    assert "publish" in stage_mod.BY_NAME
    assert stage_mod.BY_NAME["publish"].kind == "driver"
    assert "publish" not in stage_mod.ORDER     # 不是流水线阶段，不参与 `lvs run`


def test_title_candidates_drop_the_internal_script_title():
    """拍摄稿的文件标题（`003-x 拍摄稿 · …`）不该进候选 —— 那是给制作人看的。"""
    got = publish.title_candidates(
        {"title": "003-雨月物语-夜宿荒宅 拍摄稿 · 《雨月物语》第三期「夜宿荒宅」"},
        {"title_card": "主用：钩子甲｜《雨月物语》夜宿荒宅", "cover": []},
    )
    assert got and all("拍摄稿" not in t for t in got)


def test_title_candidates_keep_the_internal_title_only_as_a_last_resort():
    """稿子只写了内部标题时，退回原文 —— 宁可难看，也不交空标题。"""
    got = publish.title_candidates({"title": "003-x 拍摄稿"}, {})
    assert got == ["003-x 拍摄稿"]


def test_tags_never_leak_the_internal_script_title(tmp_path: Path):
    got = publish.tags(
        {"title": "003-雨月物语-夜宿荒宅 拍摄稿 · 《雨月物语》第三期「夜宿荒宅」"},
        _config(tmp_path, tags=[], book_title=""),
        ["他七年没回家", "那晚睡的床底下是什么", "《雨月物语》夜宿荒宅"],
    )
    assert "雨月物语" in got      # 《X》里的书名是干净标签
    assert "夜宿荒宅" in got      # 篇名（左端“《雨月物语》”被摘掉后剩下的）
    assert all("拍摄稿" not in t and " " not in t for t in got)
    assert all("》" not in t for t in got)   # 不许留下“雨月物语》夜宿荒宅”这种半截书名号


def test_douyin_captions_never_emit_a_tags_only_candidate(tmp_path: Path):
    got = publish.douyin_captions(["甲", "乙"], ["书", "怪谈"], _config(tmp_path, intro=""))
    assert len(got) == 3
    assert got[2] == "甲 #书 #怪谈"   # 没配 intro → 用最短的一行封面字兜底
    assert all(c.replace("#书 #怪谈", "").strip() for c in got)
