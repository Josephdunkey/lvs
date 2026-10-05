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
# ---- 封面改法守卫（2026-10-05 用户打回"图不好看 / 字不够大"后补的判据） ------------
#
# 这组测试守的是"封面到底像不像封面"，而不是"函数没抛异常"：
#   · 字要**大**（旧版单行字高只有屏高 2.8%，信息流里缩成 ≈8 px，等于没字）；
#   · 底图的平涂色块要**裁掉**（实测 base_003 左侧 41.5% 宽是纯色块）；
#   · 深墨字要**比奶白描边铺得多**（反过来就是"空心描边字"，就是被打回的那批）；
#   · 收尾标点不许掉到行首（避头尾）。


def _text_rows(out: Path, size: tuple[int, int]) -> list[int]:
    """有封面字像素的行号（精确配色匹配 —— PIL 画的字一定留下精确像素值）。"""
    from PIL import Image

    colors = (publish._COVER_INK, publish._COVER_PAPER)
    with Image.open(out) as im:
        px = list(im.convert("RGB").getdata())
    w, h = size
    return [y for y in range(h)
            if sum(1 for p in px[y * w:(y + 1) * w] if p in colors) >= 8]


def _line_heights(rows: list[int]) -> list[int]:
    """把"有字的行号"切成一段段连续区间 —— 每段就是一行字的**字面高度**。"""
    groups: list[int] = []
    start = prev = None
    for y in rows:
        if start is None:
            start = prev = y
        elif y == prev + 1:
            prev = y
        else:
            groups.append(prev - start + 1)
            start = prev = y
    if start is not None:
        groups.append(prev - start + 1)
    return groups


def test_cover_text_is_big_enough(tmp_path: Path):
    """单行字高 ≥ 屏高 8%（旧版实测 2.82%，B 站信息流里 ≈ 8 px，等于没字）。"""
    out = publish.render_cover(None, ["他七年没回家", "那晚睡的床底下是什么"],
                               publish.DOUYIN_SIZE, tmp_path / "c.png", layout="top")
    heights = _line_heights(_text_rows(out, publish.DOUYIN_SIZE))
    assert heights, "封面上一个字都没找到 —— 叠字没生效"
    tallest, screen_h = max(heights), publish.DOUYIN_SIZE[1]
    assert tallest >= screen_h * 0.08, (
        f"封面字太小：单行 {tallest} px = 屏高 {tallest / screen_h:.2%}（要求 ≥ 8%）")


def test_cover_ink_dominates_the_paper_stroke(tmp_path: Path):
    """深墨字要盖过奶白描边（实测 ink/paper ≈ 1.30）—— 反过来就是空心描边字。"""
    from PIL import Image

    out = publish.render_cover(None, ["他七年没回家", "那晚睡的床底下是什么"],
                               publish.BILIBILI_SIZE, tmp_path / "c.png")
    with Image.open(out) as im:
        px = list(im.convert("RGB").getdata())
    ink = sum(1 for p in px if p == publish._COVER_INK)
    paper = sum(1 for p in px if p == publish._COVER_PAPER)
    assert ink and paper, f"没找到封面字（ink={ink}, paper={paper}）"
    assert ink / paper >= 1.0, (
        f"描边吃掉了字骨：ink={ink}, paper={paper}, 比值 {ink / paper:.2f}")


def test_trim_flat_bands_cuts_the_paint_block():
    """左边 40% 平涂色块 → 必须裁掉；裁完左边第一列就得是有内容的。"""
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (800, 800), (200, 210, 220))
    draw = ImageDraw.Draw(im)
    for y in range(800):               # 右侧 60% 有内容（逐行变色 → 列向有方差）
        draw.line([(320, y), (799, y)], fill=(30 + y % 200, 40, 50 + (y * 3) % 200))
    trimmed = publish._trim_flat_bands(im)
    assert trimmed.size[0] < 800, "左侧那块平涂色块没被裁掉"
    gray = trimmed.convert("L")
    col = [gray.getpixel((0, y)) for y in range(gray.size[1])]
    mean = sum(col) / len(col)
    std = (sum((v - mean) ** 2 for v in col) / len(col)) ** 0.5
    assert std >= 9.0, f"裁完左边第一列还是平的（std={std:.1f}）"


def test_trim_flat_bands_keeps_a_fully_flat_image():
    """整张都平涂 → 判定为误裁，原图返回（保护"纯色底图"这种正当用法）。"""
    from PIL import Image

    im = Image.new("RGB", (600, 600), (10, 10, 10))
    assert publish._trim_flat_bands(im).size == (600, 600)


def test_cover_never_starts_a_line_with_closing_punct():
    """避头尾：`》` 不许掉到行首（实测痛点，见 publish._NO_LINE_START）。"""
    from PIL import Image, ImageDraw

    assert publish._fix_orphan_punct(["《雨月物语", "》夜宿荒宅"]) == ["《雨月物语》", "夜宿荒宅"]
    font = publish._cover_font(120)
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    long_line = "《雨月物语》夜宿荒宅里的那盏灯"
    rows = publish._fix_orphan_punct(
        publish.graphic.wrap(draw, long_line, font, 600) or [long_line])
    assert rows
    assert all(row[0] not in publish._NO_LINE_START for row in rows), rows


def test_cover_font_uses_the_bold_chain():
    """封面取字要走黑体链（曾误走 NotoSerifSC-VF —— 衬线细字 → 空心描边字）。"""
    assert publish._font_path(None) == publish._cover_font_path()
def test_wrap_balanced_kills_the_single_char_last_line():
    """末行只剩一个字 → 按字数均分重切（实测痛点：`他七年没回家` → `他七年没回`/`家`）。"""
    from PIL import Image, ImageDraw

    font = publish._cover_font(120)
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    char_w = draw.textlength("他", font=font)

    rows = publish._wrap_balanced(draw, "他七年没回家", font, char_w * 5.2)
    assert len(rows) == 2, rows
    assert min(len(r) for r in rows) >= 2, rows
    assert "".join(rows) == "他七年没回家"

    # 本来就折得均匀的，不许乱动（4+3 不是孤字）
    kept = publish._wrap_balanced(draw, "他七年没回家乡", font, char_w * 4.1)
    assert "".join(kept) == "他七年没回家乡"
    assert min(len(r) for r in kept) >= 2, kept
def test_episode_comes_from_the_script_title_not_the_shared_config(tmp_path: Path):
    """期号跟**本期**走，config 里那个是手改的（实测坑：给 005 改成 5 后，重出 003 变成"第 5 期"）。"""
    ws = _ws(tmp_path)
    _write_parse(ws)
    data = json.loads(ws.path("parse.json").read_text(encoding="utf-8"))
    data["title"] = "003-雨月物语-夜宿荒宅 拍摄稿 · 《雨月物语》第三期「夜宿荒宅」"
    ws.path("parse.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    meta = publish.build_meta(ws, _config(tmp_path, episode=5))
    assert meta["book"]["episode"] == 3
    assert meta["book"]["episode_source"] == "拍摄稿标题"
    assert "第 3 期" in meta["bilibili"]["description"]
    assert any("第 3 期" in w for w in meta["warnings"])   # config 与拍摄稿打架要报出来


def test_episode_falls_back_to_config_without_a_hint(tmp_path: Path):
    ws = _ws(tmp_path)
    _write_parse(ws)                                   # 标题里没有「第 N 期」
    meta = publish.build_meta(ws, _config(tmp_path, episode=2))
    assert meta["book"]["episode"] == 2
    assert meta["book"]["episode_source"] == "config"
    assert not meta["warnings"] or all("期" not in w for w in meta["warnings"])


def test_cn_number_reads_chinese_episode_numbers():
    assert publish._cn_number("三") == 3
    assert publish._cn_number("十") == 10
    assert publish._cn_number("十三") == 13
    assert publish._cn_number("二十三") == 23
    assert publish._cn_number("12") == 12
    assert publish._cn_number("廿") is None

def _crossed_canvas(size: tuple[int, int], color: tuple[int, int, int] = (255, 255, 255)) -> Any:
    """一张"不会触发平涂裁切"的白底图（横竖各一条黑线，逐列/逐行都不平涂）。"""
    from PIL import Image, ImageDraw

    w, h = size
    im = Image.new("RGB", size, color)
    d = ImageDraw.Draw(im)
    d.line([(0, h // 2), (w, h // 2)], fill=(0, 0, 0), width=2)
    d.line([(w // 2, 0), (w // 2, h)], fill=(0, 0, 0), width=2)
    return im


def test_cover_base_is_untouched_outside_the_text_block(tmp_path: Path):
    """★ 压暗层已整体删除（用户 2026-10-05 第四轮："文字那块有个黑影"）。

    封面只许**画字**，不许改动底图的任何其他像素 —— 所以"变化像素"的纵向包围盒
    必须落在字块的纵向范围里（±40 px 给抗锯齿留余量）。
    旧版那层压暗覆盖屏高的 11 %–87 %，一层回来这条用例立刻红。
    """
    from PIL import Image, ImageChops

    base_path = tmp_path / "base.png"
    _crossed_canvas(publish.BILIBILI_SIZE).save(base_path)
    out = publish.render_cover(base_path, ["他七年没回家", "那晚睡的床底下是什么"],
                               publish.BILIBILI_SIZE, tmp_path / "c.png")
    with Image.open(base_path) as a, Image.open(out) as b:
        bbox = ImageChops.difference(a.convert("RGB"), b.convert("RGB")).convert("L").getbbox()
    assert bbox is not None, "底图一个像素都没变 —— 说明字根本没画上去"
    rows = _text_rows(out, publish.BILIBILI_SIZE)
    assert rows, "封面上一个字都没找到（叠字没生效）"
    lo, hi = min(rows) - 40, max(rows) + 40
    assert bbox[1] >= lo and bbox[3] <= hi, (
        f"字块之外的画面被改动了：变化范围 y {bbox[1]}–{bbox[3]}，字块 y {min(rows)}–{max(rows)}")


def _line_starts(out: Path, size: tuple[int, int]) -> list[int]:
    """每一行字的**最左**墨/边像素 x 坐标（按连续行分组）。"""
    from PIL import Image

    with Image.open(out) as im:
        px = im.convert("RGB").load()
    w, _ = size
    rows = _text_rows(out, size)
    groups: list[list[int]] = []
    for y in rows:
        if groups and y != groups[-1][-1] + 1:
            groups.append([])
        if not groups:
            groups.append([])
        groups[-1].append(y)
    starts = []
    for g in groups:
        xs = [x for y in g for x in range(w) if px[x, y] in (publish._COVER_INK, publish._COVER_PAPER)]
        if xs:
            starts.append(min(xs))
    return starts


def test_cover_vertical_lines_are_left_aligned(tmp_path: Path):
    """★ 竖版两行以上必须左对齐（用户 2026-10-05 第四轮："尽量搞成左对齐"）。

    居中排版时「他七年没回家」（6 字）与「那晚睡的床底下是什么」（10 字）的行首
    会差出一大截（实测居中时相差 100+ px）；左对齐则各行行首基本同一 x。
    """
    base_path = tmp_path / "base.png"
    _crossed_canvas(publish.DOUYIN_SIZE).save(base_path)
    out = publish.render_cover(base_path, ["他七年没回家", "那晚睡的床底下是什么"],
                               publish.DOUYIN_SIZE, tmp_path / "c.png", layout="top")
    starts = _line_starts(out, publish.DOUYIN_SIZE)
    assert len(starts) >= 2, f"至少要两行才谈得上对齐，实测 {len(starts)} 行：{starts}"
    assert max(starts) - min(starts) <= 6, f"竖版行首没对齐：{starts}"


def test_douyin_captions_stay_distinct_with_three_lines(tmp_path: Path):
    """三行封面字（本系列的实际口径）时必须给出 3 条**互不相同**的候选。

    两行时 `"｜".join` 与 `首｜尾` 同形，去重后只剩 2 条 —— 这条用例把两种
    行数都钉住，免得下次又只测一种。
    """
    lines = ["他撞见一场夜宴", "席上全是死人", "《雨月物语》血溅宫闱"]
    got = publish.douyin_captions(lines, ["书", "怪谈"], _config(tmp_path, intro=""))
    assert len(got) == 3, got
    assert len(set(got)) == 3, got
    two = publish.douyin_captions(["甲", "乙"], ["书", "怪谈"], _config(tmp_path, intro=""))
    assert len(two) == 3, two
    assert len(set(two)) == 3, two