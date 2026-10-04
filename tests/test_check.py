"""拍摄稿校验（`lvs check`）的测试。

校验器的价值全在"**报得准**"：漏报等于白做，误报会让人不再信任它。
所以这里既测"该报的报了"，也测"不该报的别报"。
"""

from __future__ import annotations

from pathlib import Path

from lvs import check as check_mod


GOOD_MANUSCRIPT = """# 001-测试集 拍摄稿

## 一、传达层

**【标题】**
> 「希望你能记住我」——第一章就把结局写完了

**【冷开场】**（0:00–0:30，从最戏剧的一刻切入）
> 这片草地上，有一口井。

**【封面文案】**
> 行1：她求的不是爱

---

## 二、正文讲稿

### 【① 开场】0:00–0:30

这片草地上，有一口井。

谁也不知道它在哪儿。

[画面位] [场景] 十月的芒草坡，风过草浪

### 【② 相遇】0:30–1:20

{NAOKO} 侧身走过校墙，背景只有枯枝。

[画面位] [场景] {NAOKO} 侧身走过校墙，背景只有枯枝与斑驳的墙面

---

## 三、画面位清单

| 时间 | 画面 | 素材建议 |
|---|---|---|
| 0:00 | 草地与井 | 生图：黑白漫画 |
| 0:30 | 校墙边侧影 | 生图：黑白漫画 |
"""


def test_good_manuscript_passes():
    result = check_mod.check_manuscript(GOOD_MANUSCRIPT)
    assert result.errors == [], [f.render() for f in result.errors]
    assert result.stats["segments"] == 2
    assert result.stats["visual_marks"] == 2
    assert result.stats["mark_table_rows"] == 2


def test_missing_structure_is_reported():
    text = "随便写点东西，没有分节标题，也没有画面位。"
    result = check_mod.check_manuscript(text)
    codes = {f.code for f in result.errors}
    assert "C001" in codes, "没有 ## 分节标题必须报 error"
    assert "C002" in codes, "缺冷开场必须报 error"
    assert "C003" in codes, "缺正文/画面位清单必须报 error"


def test_negation_in_visual_mark_is_error():
    text = GOOD_MANUSCRIPT.replace(
        "[画面位] [场景] 十月的芒草坡，风过草浪",
        "[画面位] [场景] 空荡的草地，no people in frame",
    )
    result = check_mod.check_manuscript(text)
    codes = {f.code for f in result.errors}
    assert "C021" in codes, "否定式描述必须报 error（模型会把否定句里的名词画出来）"


def test_chinese_negation_is_error():
    text = GOOD_MANUSCRIPT.replace(
        "[画面位] [场景] 十月的芒草坡，风过草浪",
        "[画面位] [场景] 空荡的草地，画面里没有人，也没有文字",
    )
    result = check_mod.check_manuscript(text)
    assert "C021" in {f.code for f in result.errors}


def test_artist_name_is_error():
    text = GOOD_MANUSCRIPT.replace(
        "[画面位] [场景] 十月的芒草坡，风过草浪",
        "[画面位] [场景] 草坡，in the style of Van Gogh",
    )
    result = check_mod.check_manuscript(text)
    assert "C022" in {f.code for f in result.errors}


def test_bold_narration_is_error():
    text = GOOD_MANUSCRIPT.replace("谁也不知道它在哪儿。", "**谁也不知道它在哪儿。**")
    result = check_mod.check_manuscript(text)
    assert "C040" in {f.code for f in result.errors}, "加粗脚手架会被 TTS 念出来"


def test_quoted_text_is_warn():
    text = GOOD_MANUSCRIPT.replace(
        "[画面位] [场景] 十月的芒草坡，风过草浪",
        '[画面位] [场景] 黑板上高亮"斩首四万"',
    )
    result = check_mod.check_manuscript(text)
    codes = {f.code for f in result.warns}
    assert "C023" in codes, "引号原文容易被画成伪汉字"
    assert "C024" in codes, "剪辑动词（高亮）应告警"


def test_missing_visual_tag_is_info():
    text = GOOD_MANUSCRIPT.replace(
        "[画面位] [场景] 十月的芒草坡，风过草浪",
        "[画面位] 十月的芒草坡，风过草浪",
    )
    result = check_mod.check_manuscript(text)
    codes = {f.code for f in result.findings}
    assert "C025" in codes, "没写 [场景]/[图表] 标注应提示"


def test_unknown_cast_slot_is_error_when_registry_given():
    result = check_mod.check_manuscript(GOOD_MANUSCRIPT, known_cast={"WATANABE"})
    assert "C026" in {f.code for f in result.errors}, "槽位不在定妆库里必须报 error"


def test_known_cast_slot_passes():
    result = check_mod.check_manuscript(GOOD_MANUSCRIPT, known_cast={"NAOKO"})
    assert "C026" not in {f.code for f in result.errors}


def test_no_cast_check_when_registry_absent():
    result = check_mod.check_manuscript(GOOD_MANUSCRIPT, known_cast=None)
    assert "C026" not in {f.code for f in result.findings}


def test_segment_without_timecode_is_error():
    text = GOOD_MANUSCRIPT.replace("### 【① 开场】0:00–0:30", "### 【① 开场】")
    result = check_mod.check_manuscript(text)
    assert "C010" in {f.code for f in result.errors}


def test_long_narration_line_is_warn():
    long_line = "这是一句非常长的旁白" * 8  # > 60 字
    text = GOOD_MANUSCRIPT.replace("谁也不知道它在哪儿。", long_line)
    result = check_mod.check_manuscript(text)
    assert "C041" in {f.code for f in result.warns}


def test_quote_block_body_not_scanned_as_narration():
    """`> 〔引原文〕` 块的正文（无 `>` 前缀）不是旁白 —— 长引文不得触发 C041。

    块语义与 parse 2026-10-04 修复同口径：check 此前只跳 `>` 开头的行，
    块正文落进「其余视为旁白」→ 003 拍摄稿 5 处 67–92 字引文全部误报。
    守护两头：长引文行本身仍报 C041（上一个测试），块里的不报。
    """
    long_quote = "这是一段很长的引文。" * 8  # > 60 字
    text = GOOD_MANUSCRIPT.replace(
        "谁也不知道它在哪儿。",
        f"〔原文〕短引文。\n\n> 〔引原文〕\n{long_quote}\n\n谁也不知道它在哪儿。",
    )
    result = check_mod.check_manuscript(text)
    assert "C041" not in {f.code for f in result.warns}, \
        "引文块正文不是旁白，不得按旁白行长报 C041"


def test_long_tail_without_marks_is_warn():
    # 插在 `## 三、画面位清单` 之前 —— 放到文末表格章之后就落进 table 分节，不再是正文段了
    filler = "\n\n".join("这是一句普通的旁白。" for _ in range(30))
    new_seg = f"### 【③ 长尾】1:20–2:30\n\n{filler}\n\n---\n\n"
    text = GOOD_MANUSCRIPT.replace("---\n\n## 三、画面位清单", new_seg + "## 三、画面位清单")
    result = check_mod.check_manuscript(text)
    assert "C011" in {f.code for f in result.warns}, "单段 >200 字却没有画面位应告警"


def test_unknown_bracket_marker_is_warn():
    text = GOOD_MANUSCRIPT.replace("谁也不知道它在哪儿。", "[某个未知标记]\n\n谁也不知道它在哪儿。")
    result = check_mod.check_manuscript(text)
    assert "C030" in {f.code for f in result.warns}


def test_empty_visual_mark_is_warn():
    text = GOOD_MANUSCRIPT.replace(
        "[画面位] [场景] 十月的芒草坡，风过草浪", "[画面位]"
    )
    result = check_mod.check_manuscript(text)
    assert "C020" in {f.code for f in result.warns}


def test_findings_sorted_errors_first():
    text = GOOD_MANUSCRIPT.replace(
        "[画面位] [场景] 十月的芒草坡，风过草浪", "[画面位] 空荡的草地，no people"
    ).replace("谁也不知道它在哪儿。", "x" * 80)
    result = check_mod.check_manuscript(text)
    levels = [f.level for f in result.sorted_findings()]
    assert levels == sorted(levels, key=lambda lv: {"error": 0, "warn": 1, "info": 2}[lv])


def test_collect_known_cast_reads_lock(tmp_path: Path):
    cast_dir = tmp_path / "cast"
    cast_dir.mkdir()
    (cast_dir / "lock.json").write_text(
        '{"characters": {"NAOKO": {"state": "REF"}, "MIDORI": {"state": "IMG"}}}',
        encoding="utf-8",
    )
    assert check_mod.collect_known_cast(tmp_path) == {"NAOKO", "MIDORI"}


def test_collect_known_cast_missing_returns_none(tmp_path: Path):
    assert check_mod.collect_known_cast(tmp_path) is None


def test_collect_known_cast_uses_config_lib_derivation(tmp_path: Path):
    """★ 多项目化后定妆库落在 `<素材库>/_cast`，不是仓库根 `cast/`。

    上一版 `collect_known_cast` 只扫 `cast/lock.json`，导致 `lvs check` 的 C026
    （人物槽位是否在册）在真实项目上**永远读不到库、静默失效**。
    这里钉死：传了 config 就要走 `cast_dir(config)` 的派生路径。
    """
    from lvs import cast as cast_mod

    lib = tmp_path / "素材库"
    cast_dir = lib / "_cast"
    cast_dir.mkdir(parents=True)
    (cast_dir / "lock.json").write_text(
        '{"characters": {"NAOKO": {"state": "REF"}, "MIDORI": {"state": "IMG"}}}',
        encoding="utf-8",
    )

    class _Cfg:
        def __init__(self, lib_path: Path):
            self._lib = str(lib_path)

        def get(self, key, default=None):
            if key == "paths.lib":
                return self._lib
            if key == "cast.dir":
                return ""
            return default

    cfg = _Cfg(lib)
    assert cast_mod.cast_dir(cfg) == cast_dir
    assert check_mod.collect_known_cast(config=cfg) == {"NAOKO", "MIDORI"}


def test_run_command_reports_and_returns_code(tmp_path: Path, monkeypatch, capsys):
    good = tmp_path / "good.md"
    good.write_text(GOOD_MANUSCRIPT, encoding="utf-8")
    monkeypatch.setattr(check_mod, "collect_known_cast", lambda root=None, config=None: {"NAOKO"})
    assert check_mod.run_command(None, None, _Args(manuscript=str(good))) == 0
    out = capsys.readouterr().out
    assert "全部通过" in out or "warn" in out

    bad = tmp_path / "bad.md"
    bad.write_text("什么都没有", encoding="utf-8")
    assert check_mod.run_command(None, None, _Args(manuscript=str(bad))) == 1


def test_run_command_missing_file(tmp_path: Path, capsys):
    assert check_mod.run_command(None, None, _Args(manuscript=str(tmp_path / "nope.md"))) == 2
    assert "不存在" in capsys.readouterr().out


def test_run_command_requires_path(capsys):
    assert check_mod.run_command(None, None, _Args(manuscript=None)) == 2


class _Args:
    def __init__(self, manuscript=None):
        self.manuscript = manuscript
