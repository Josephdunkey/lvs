"""画面风格注册表（`lvs/styles.py` + `lvs styles`）的测试。

这一组测试守的是**通用性**：风格必须能"加而不改代码"，且新加的风格不能踩老坑。
所以除了功能，还钉死两条**内容红线**（对全部内置预设生效）：

1. 不写艺术家姓名 —— cfg=1.0 的模型会把「某某的风格」字面画成「某某本人」
2. 不写否定词 —— 没有负向引导，否定句里的名词会被原样画出来

这两条以前只写在文档里，现在有测试兜着：**下一个加预设的人会被拦下来**。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lvs import prompting
from lvs import styles as st


# ---- 内置预设 --------------------------------------------------------------


def test_builtin_presets_are_plentiful():
    """预设要有得挑 —— 只有 3 个的时候，换题材就得改代码。"""
    assert len(st.BUILTIN) >= 25, f"内置预设只有 {len(st.BUILTIN)} 个，太少了"


def test_every_builtin_has_desc_and_tags():
    """`lvs styles` 要靠 desc/tags 让人选得下去 —— 没有说明的选项等于没有选项。"""
    for name, style in st.BUILTIN.items():
        assert style.name == name
        assert style.desc.strip(), f"{name} 缺 desc"
        assert style.tags, f"{name} 缺 tags"
        assert style.suffix.strip(), f"{name} 缺 suffix"


def test_builtin_names_are_ascii_slugs():
    """名字要能直接写在命令行与 config 里。"""
    for name in st.BUILTIN:
        assert name == name.strip()
        assert " " not in name
        assert name.isascii(), f"{name} 不是 ASCII（写进 config 容易出编码问题）"


def test_no_builtin_suffix_names_an_artist():
    """★ 红线 1：cfg=1.0 的模型会把「某某的风格」画成「某某本人」。"""
    bad = [n for n, s in st.BUILTIN.items() if "style of" in s.suffix.lower()]
    assert bad == [], f"这些预设写了艺术家导向的措辞：{bad}"


def test_no_builtin_suffix_uses_negation():
    """★ 红线 2：否定句里的名词会被原样画出来（与 `lvs check` 的 C021 同源判据）。"""
    bad: list[str] = []
    for name, style in st.BUILTIN.items():
        hits = prompting.find_negations(style.suffix)
        if hits:
            bad.append(f"{name} → {hits[0][0]}:{hits[0][1]!r}")
    assert bad == [], "这些预设含否定式：" + "；".join(bad)


def test_no_builtin_suffix_asks_for_panel_composition():
    """`dynamic panel composition` 会被读成漫画分格纸，往格子里填人（实测 1 人 → 2 人）。"""
    bad = [n for n, s in st.BUILTIN.items() if "panel composition" in s.suffix.lower()]
    assert bad == []


def test_builtin_suffixes_declare_one_frame():
    """要单幅就必须明写 —— 否则模型可能理解成联画 / 分镜。"""
    bad = [n for n, s in st.BUILTIN.items() if "full-frame" not in s.suffix and "full frame" not in s.suffix]
    assert bad == [], f"这些预设没写单幅：{bad}"


def test_monochrome_flag_matches_suffix_content():
    """标了单色的，提示词里必须真有 `grayscale` / `monochrome` / 两种色（否则 qc 判据与画面不符）。"""
    for name, style in st.BUILTIN.items():
        if not style.monochrome:
            continue
        low = style.suffix.lower()
        assert any(k in low for k in ("grayscale", "monochrome", "black and white", "two-tone")), (
            f"{name} 标为单色，但 suffix 里没有任何「只出黑白」的正面陈述"
        )


def test_to_dict_is_json_safe():
    import json

    d = st.BUILTIN["jp-youth-manga-bw"].to_dict()
    json.dumps(d)  # 不抛 = 可序列化给 Agent
    assert d["tags"] == ["黑白版画"]


# ---- 注册表合并 ------------------------------------------------------------


class _Cfg:
    def __init__(self, data=None):
        self._d = data or {}

    def get(self, key, default=None):
        return self._d.get(key, default)


def _write_style_file(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_registry_defaults_to_builtin_without_config():
    reg = st.build_registry(None)
    assert set(st.BUILTIN).issubset(set(reg.styles))
    assert reg.default == st.DEFAULT_NAME
    assert reg.get(None).name == st.DEFAULT_NAME


def test_project_style_file_is_picked_up(tmp_path: Path):
    """项目级风格（跟素材走）要能被读到，且**盖过内置同名项**。"""
    lib = tmp_path / "书" / "10-语料" / "知识视频素材库"
    (lib / "00-设定").mkdir(parents=True)
    _write_style_file(
        st.project_style_file(lib),
        'default = "ink-mine"\n\n[styles.ink-mine]\nsuffix = "my own ink style"\n',
    )
    reg = st.build_registry(_Cfg({"paths.lib": str(lib)}))
    assert reg.get(None).name == "ink-mine"
    assert reg.default == "ink-mine"
    assert reg.source["ink-mine"].startswith("项目")
    # 内置的仍然在（是**叠加**不是**替换**）
    assert "jp-youth-manga-bw" in reg.styles


def test_project_style_file_overrides_builtin_with_same_name(tmp_path: Path):
    lib = tmp_path / "lib"
    (lib / "00-设定").mkdir(parents=True)
    _write_style_file(
        st.project_style_file(lib),
        '[styles.jp-youth-manga-bw]\nsuffix = "overridden for this book"\n',
    )
    reg = st.build_registry(_Cfg({"paths.lib": str(lib)}))
    assert reg.get("jp-youth-manga-bw").suffix == "overridden for this book"


def test_inline_config_styles_are_merged():
    reg = st.build_registry(_Cfg({"styles": {"house": {"suffix": "my house look", "desc": "本机"}}}))
    assert reg.get("house").suffix == "my house look"


def test_shots_style_wins_as_default():
    reg = st.build_registry(_Cfg({"shots.style": "cinematic-film-noir"}))
    assert reg.default == "cinematic-film-noir"
    assert reg.get(None).name == "cinematic-film-noir"


def test_explicit_style_file_has_highest_priority(tmp_path: Path):
    lib = tmp_path / "lib"
    (lib / "00-设定").mkdir(parents=True)
    _write_style_file(st.project_style_file(lib), '[styles.shared]\nsuffix = "from project"\n')
    other = _write_style_file(tmp_path / "adhoc.toml", '[styles.shared]\nsuffix = "from cli file"\n')
    reg = st.build_registry(_Cfg({"paths.lib": str(lib)}), style_file=other)
    assert reg.get("shared").suffix == "from cli file"


# ---- 错误处理 --------------------------------------------------------------


def test_unknown_style_lists_options_and_suggests():
    reg = st.build_registry(None)
    with pytest.raises(st.StyleError) as ei:
        reg.get("jp-youth-manga")
    msg = str(ei.value)
    assert "jp-youth-manga-bw" in msg, "打字打一半应该给出近似项"
    assert "lvs styles" in msg, "报错要告诉人怎么看到全部选项"
    assert "风格预设.toml" in msg, "要告诉人怎么加自己的风格"


def test_broken_style_file_does_not_crash(tmp_path: Path):
    """风格文件写坏了只报 notes，**不打断出图** —— 但也不许静默。"""
    lib = tmp_path / "lib"
    (lib / "00-设定").mkdir(parents=True)
    _write_style_file(st.project_style_file(lib), "this is not = valid [[ toml")
    reg = st.build_registry(_Cfg({"paths.lib": str(lib)}))
    assert reg.notes, "坏文件必须留下痕迹"
    assert "jp-youth-manga-bw" in reg.styles, "坏文件不该影响能用的内置预设"


def test_style_file_without_styles_table_is_noted(tmp_path: Path):
    lib = tmp_path / "lib"
    (lib / "00-设定").mkdir(parents=True)
    _write_style_file(st.project_style_file(lib), 'default = "x"\n')
    reg = st.build_registry(_Cfg({"paths.lib": str(lib)}))
    assert any("styles" in n for n in reg.notes)


def test_style_file_accepts_shorthand_string_form(tmp_path: Path):
    """两种写法都要认：`x = "suffix"` 与 `[styles.x] suffix = "…"`。"""
    lib = tmp_path / "lib"
    (lib / "00-设定").mkdir(parents=True)
    _write_style_file(
        st.project_style_file(lib),
        '[styles.short]\nsuffix = "A"\n[styles.long]\nsuffix = "B"\ndesc = "说明"\n',
    )
    reg = st.build_registry(_Cfg({"paths.lib": str(lib)}))
    assert reg.get("short").suffix == "A"
    assert reg.get("long").desc == "说明"


def test_monochrome_helper_degrades_to_false():
    """读不到风格时按"不要求单色"处理 —— 不能因为风格表坏了就拦下整批图。"""
    reg = st.build_registry(None)
    assert reg.monochrome("no-such-style") is False
    assert reg.monochrome("chinese-ink-wash") is True
    assert reg.monochrome("historical-documentary") is False


# ---- 目录 / 序列化 ---------------------------------------------------------


def test_by_tag_groups_every_style():
    reg = st.build_registry(None)
    seen = {s.name for group in reg.by_tag().values() for s in group}
    assert seen == set(reg.styles)


def test_catalog_json_fields_for_agents():
    """Agent 选风格要看 desc 和 monochrome —— 这两个字段不能在重构里丢掉。"""
    reg = st.build_registry(None)
    data = st.catalog_json(reg, current="jp-youth-manga-bw")
    assert data["count"] == len(reg.styles)
    assert data["current"] == "jp-youth-manga-bw"
    one = next(s for s in data["styles"] if s["name"] == "jp-youth-manga-bw")
    assert one["desc"] and one["monochrome"] is True and "source" in one


def test_render_catalog_tells_where_the_project_style_file_is(tmp_path: Path):
    """配了 lib 就要指引到项目风格文件 —— 且**存在与否要如实说**。

    原来不管文件在不在都打印路径，人会以为"这个文件在生效"，
    而其实一条自定义风格都没读到。所以两个分支都要测。
    """
    lib = tmp_path / "lib"
    (lib / "00-设定").mkdir(parents=True)
    reg = st.build_registry(None)

    # 分支 1：文件还不存在 → 明说"还没有"，并给出 `lvs init` 会生成在哪
    out = st.render_catalog(reg, lib=lib)
    assert "还没有风格文件" in out
    assert "00-设定" in out
    assert "lvs shots --style" in out

    # 分支 2：文件存在 → 打印它的路径
    st.scaffold_style_file(lib)
    out2 = st.render_catalog(reg, lib=lib)
    assert "风格预设.toml" in out2
    assert "还没有风格文件" not in out2


def test_render_catalog_without_lib_explains_the_benefit(tmp_path: Path):
    out = st.render_catalog(st.build_registry(None), lib=None)
    assert "[paths].lib" in out


def test_scaffold_style_file_is_idempotent(tmp_path: Path):
    lib = tmp_path / "lib"
    p1 = st.scaffold_style_file(lib)
    assert p1.is_file()
    p1.write_text(p1.read_text(encoding="utf-8") + "\n# 手工改过\n", encoding="utf-8")
    p2 = st.scaffold_style_file(lib)
    assert "# 手工改过" in p2.read_text(encoding="utf-8"), "已存在的骨架不许被覆盖"


# ---- 风格内容 lint（项目自定义风格的最后一道闸） ---------------------------
#
# 为什么要有它：这三条红线**只写在文档里时没人会看**。
# 2026-10-03 我自己写的 30 个内置预设全踩了 —— 直到把断言补成测试才发现，
# 之后又发现**项目自己的风格文件不在测试覆盖里**，所以把判据接到了 `lvs styles`。


def _style(suffix: str, **kw) -> st.Style:
    return st.Style(name=kw.pop("name", "demo"), suffix=suffix, **kw)


def test_lint_style_flags_negation():
    problems = st.lint_style(_style("flat vector art, no gradients, single full-frame composition"))
    assert any("否定词" in p for p in problems)


def test_lint_style_negation_is_stricter_than_c021():
    """`no shading` 这类词 C021 的名词表管不到 —— 风格 lint 必须自己判。

    这条是踩出来的：`minimal-line-art` 曾写 `no shading and no fill`，
    而 `prompting.find_negations` 对它**无感**。
    """
    text = "line art, no shading, no fill, single full-frame composition"
    assert prompting.find_negations(text) == [], "前提：C021 对这条无感"
    assert any("否定词" in p for p in st.lint_style(_style(text)))


def test_lint_style_flags_artist_name():
    problems = st.lint_style(_style("ink drawing in the style of Van Gogh, single full-frame composition"))
    assert any("艺术家属名" in p for p in problems)


def test_lint_style_flags_panel_words():
    problems = st.lint_style(_style("manga with dynamic panel composition, single full-frame"))
    assert any("多格词" in p for p in problems)


def test_lint_style_flags_missing_single_frame():
    problems = st.lint_style(_style("cinematic still, warm light"))
    assert any("没声明单幅" in p for p in problems)


def test_lint_style_flags_monochrome_without_positive_statement():
    problems = st.lint_style(_style("ink drawing on paper", monochrome=True))
    assert any("黑白陈述" in p for p in problems)


def test_lint_style_passes_a_clean_style():
    assert st.lint_style(_style(
        "monochrome ink drawing, grayscale only, single full-frame composition, one uninterrupted picture"
    , monochrome=True)) == []


def test_every_builtin_passes_the_style_lint():
    """内置预设必须全过 —— 这条断言把"写预设"的成本变成了"写对"的成本。"""
    bad = {n: st.lint_style(s) for n, s in st.BUILTIN.items() if st.lint_style(s)}
    assert bad == {}, f"这些内置预设没通过：{bad}"


def test_lint_registry_only_reports_non_builtin(tmp_path: Path):
    """内置的由测试守；`lvs styles` 只报**项目自定义**的问题（否则噪音淹掉重点）。"""
    lib = tmp_path / "lib"
    (lib / "00-设定").mkdir(parents=True)
    (lib / "00-设定" / st.STYLE_FILENAME).write_text(
        'default = "mine"\n\n[styles.mine]\nsuffix = "flat art with no gradients"\n',
        encoding="utf-8",
    )
    reg = st.build_registry(_Cfg({"paths.lib": str(lib)}))
    issues = st.lint_registry(reg)
    assert "mine" in issues
    assert "jp-youth-manga-bw" not in issues


def test_catalog_json_carries_issues(tmp_path: Path):
    """Agent 也要能拿到问题清单 —— 它选风格时该避开没通过体检的。"""
    lib = tmp_path / "lib"
    (lib / "00-设定").mkdir(parents=True)
    (lib / "00-设定" / st.STYLE_FILENAME).write_text(
        '[styles.bad]\nsuffix = "art with no shadows"\n', encoding="utf-8"
    )
    reg = st.build_registry(_Cfg({"paths.lib": str(lib)}))
    data = st.catalog_json(reg)
    assert "bad" in data["issues"]


def test_render_catalog_shows_issues(tmp_path: Path):
    lib = tmp_path / "lib"
    (lib / "00-设定").mkdir(parents=True)
    (lib / "00-设定" / st.STYLE_FILENAME).write_text(
        '[styles.bad]\nmonochrome = true\nsuffix = "art with no shadows"\n', encoding="utf-8"
    )
    reg = st.build_registry(_Cfg({"paths.lib": str(lib)}))
    out = st.render_catalog(reg, lib=lib)
    assert "内容体检" in out and "bad" in out
