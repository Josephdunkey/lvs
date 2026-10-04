"""多项目泛化（`[paths].lib` / 中文锚定 / 英文代词 / 风格决定单色）的测试。

这个文件守的是一个主张：**`lvs` 不止服务《挪威的森林》**。

具体钉死四件事：

| 主张 | 一旦不成立会怎样 |
|---|---|
| `[paths].lib` 一配，定妆库/定妆卡/风格文件都自动派生 | 接第二本书要手填 5 个路径，填错**不报错**（最坏的一类问题） |
| 定妆卡自动发现，**多份时不猜** | 静默挑错一份 → 整个定妆库被污染 |
| 中文锚定描述也能体检 | 换一个中文写设定的项目，lint 全是误报 → 没人再信它 |
| 英文代词用单词边界 | `he` 命中 `the` → 把英文稿改成乱码 |
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lvs import cast as cast_mod
from lvs import migrate as migrate_mod
from lvs import qc as qc_mod
from lvs import styles as styles_mod


class _Cfg:
    def __init__(self, data=None):
        self._d = data or {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def has(self, key):
        return key in self._d and self._d[key] not in ("", [], {})


# ---- [paths].lib 派生 --------------------------------------------------------


def test_cast_dir_derives_from_lib(tmp_path: Path):
    """配了 lib，定妆库落在**那本书**的素材库里（跨集共享，跟着书走）。"""
    lib = tmp_path / "书" / "10-语料" / "知识视频素材库"
    cfg = _Cfg({"paths.lib": str(lib)})
    assert cast_mod.cast_dir(cfg) == lib / "_cast"


def test_cast_dir_explicit_wins_over_lib(tmp_path: Path):
    explicit = tmp_path / "somewhere-else"
    cfg = _Cfg({"paths.lib": str(tmp_path / "lib"), "cast.dir": str(explicit)})
    assert cast_mod.cast_dir(cfg) == explicit


def test_cast_dir_falls_back_to_repo_root_without_lib():
    from lvs.config import PROJECT_ROOT

    assert cast_mod.cast_dir(_Cfg()) == PROJECT_ROOT / "cast"


def test_lib_cast_dirname_matches_scaffold(tmp_path: Path):
    """★ 两端必须同名：`lvs init` 建什么，`cast_dir` 就读什么。
    不一致的后果是"凭空多出一个空定妆库"，而且**不报错**。"""
    from lvs import project as proj_mod

    lib = tmp_path / "lib"
    proj_mod.scaffold_lib(lib)
    assert cast_mod.cast_dir(_Cfg({"paths.lib": str(lib)})) == lib / "_cast"
    assert (lib / "_cast").is_dir()


# ---- 定妆卡自动发现 ----------------------------------------------------------


def test_find_card_auto_discovers_single_card(tmp_path: Path):
    lib = tmp_path / "lib"
    (lib / "00-设定").mkdir(parents=True)
    card = lib / "00-设定" / "00-风格与人物定妆卡.md"
    card.write_text("### 甲\n", encoding="utf-8")
    assert cast_mod.find_card(_Cfg({"paths.lib": str(lib)})) == card


def test_find_card_refuses_to_guess_when_multiple(tmp_path: Path, capsys):
    lib = tmp_path / "lib"
    (lib / "00-设定").mkdir(parents=True)
    (lib / "00-设定" / "00-风格与人物定妆卡.md").write_text("a", encoding="utf-8")
    (lib / "00-设定" / "01-配角定妆卡.md").write_text("b", encoding="utf-8")
    assert cast_mod.find_card(_Cfg({"paths.lib": str(lib)})) is None
    out = capsys.readouterr().out
    assert "不猜" in out and "[cast].card" in out, "必须告诉人怎么指定"


def test_find_card_explicit_path_wins(tmp_path: Path):
    lib = tmp_path / "lib"
    (lib / "00-设定").mkdir(parents=True)
    (lib / "00-设定" / "00-风格与人物定妆卡.md").write_text("a", encoding="utf-8")
    other = tmp_path / "my.md"
    other.write_text("b", encoding="utf-8")
    assert cast_mod.find_card(_Cfg({"paths.lib": str(lib), "cast.card": str(other)})) == other


def test_find_card_returns_none_without_lib():
    assert cast_mod.find_card(_Cfg()) is None


# ---- lint 的语言覆盖 ---------------------------------------------------------


def test_lint_handles_chinese_anchor():
    """中文写的锚定也要能体检 —— 否则换个语言的项目，lint 全是误报。"""
    cn = "一个十九岁的中国女大学生，极短的短发，耳朵与后颈完全露出，鹅蛋脸，眼睛很活"
    codes = {f["code"] for f in cast_mod.lint_anchor(cn)}
    assert "A-nosubject" not in codes, "有「女大学生」，不该报缺主语"
    assert "A-order" in codes, "第一小句是身份交代，应提示把特征前置"


def test_lint_flags_chinese_anchor_without_subject():
    codes = {f["code"] for f in cast_mod.lint_anchor("极短的短发，耳朵与后颈完全露出，鹅蛋脸，眼睛很活")}
    assert "A-nosubject" in codes


def test_lint_flags_chinese_prop_heavy_anchor():
    codes = {f["code"] for f in cast_mod.lint_anchor(
        "一个年轻男人，光头，颧骨很高，白衬衫、黑长裤、深蓝毛衣、皮鞋、书包")}
    assert "A-diluted" in codes


def test_lint_flags_chinese_expression():
    codes = {f["code"] for f in cast_mod.lint_anchor("一个瘦削的男人，短发，眼神很亮，嘴角挂着冷笑")}
    assert "A-expression" in codes


def test_lint_still_clean_on_english_anchor():
    anchor = ("a thin angular face with a narrow jaw and bright dark eyes under straight "
              "brows, slightly messy short black hair, a lean boy around 17")
    assert cast_mod.lint_anchor(anchor) == []


# ---- 英文代词 ---------------------------------------------------------------


def _reg():
    return {"X": cast_mod.CastEntry(display="X", anchor="a")}


@pytest.mark.parametrize("text", ["the man is here", "her coat was wet", "when the wind rose"])
def test_ascii_pronoun_does_not_hit_substrings(text: str):
    """★ `he` 是 `the` / `her` / `when` 的子串。没有单词边界就会把英文稿改成乱码
    （`the man` → `t{X} man`）。"""
    out, *_ = migrate_mod.slot_line(text, _reg(), {"he": "X"})
    assert out == text
    assert "{X}" not in out


def test_ascii_pronoun_matches_whole_word_and_ignores_case():
    out, _n, p, _f = migrate_mod.slot_line("when He left", _reg(), {"he": "X"})
    assert "{X}" in out
    assert p == 1


def test_chinese_pronoun_rules_still_apply():
    out, *_ = migrate_mod.slot_line("其他人都走了", _reg(), {"他": "X"})
    assert out == "其他人都走了"


def test_chinese_pronoun_at_line_start_still_works():
    out, _n, p, _f = migrate_mod.slot_line("她钻出松林", _reg(), {"她": "X"})
    assert out == "{X} 钻出松林" and p == 1


# ---- 风格决定单色（qc 的彩度检查） -------------------------------------------


def test_qc_monochrome_follows_the_style():
    """「必须纯黑白」是**风格属性**，不是流水线常数 —— 换油画题材后彩度超标是对的。"""
    cfg = _Cfg({})
    mono = qc_mod._monochrome_for(cfg, [{"id": 1, "style": "jp-youth-manga-bw"}])
    color = qc_mod._monochrome_for(cfg, [{"id": 1, "style": "van-gogh-impasto"}])
    assert mono is True
    assert color is False


def test_qc_monochrome_prefers_the_shot_level_style():
    """镜上记的 `style` 最准（逐镜留痕），优先于 config 的默认。"""
    cfg = _Cfg({"shots.style": "van-gogh-impasto"})
    assert qc_mod._monochrome_for(cfg, [{"id": 1, "style": "chinese-ink-wash"}]) is True


def test_qc_monochrome_degrades_gracefully_on_unknown_style():
    """风格表坏了/名字不认识 → 按"不要求单色"处理。
    宁可少报一条彩度告警，也不要因为工具坏了就拦下整批图。"""
    assert qc_mod._monochrome_for(_Cfg({}), [{"id": 1, "style": "no-such"}]) is False


def test_inspect_image_skips_chroma_when_not_monochrome(tmp_path: Path):
    """非单色时不报 Q005。构造一张"彩色"图来验。"""
    pytest.importorskip("PIL")
    from PIL import Image

    p = tmp_path / "color.png"
    Image.new("RGB", (8, 8), (200, 40, 40)).save(p)

    _, issues_mono = qc_mod.inspect_image(p, 1, monochrome=True)
    assert any(i.code == "Q005" for i in issues_mono)
    _, issues_color = qc_mod.inspect_image(p, 1, monochrome=False)
    assert not any(i.code == "Q005" for i in issues_color)


# ---- 风格后缀的内容红线（比 C021 更严） --------------------------------------


def test_no_builtin_suffix_contains_any_negation_word():
    """★ 风格后缀必须是 **100% 正面陈述**。

    为什么比 `prompting.NEGATION_RULES`（C021）更严：C021 的名词表只覆盖
    people/text/face 那一类；风格里出现的是「no shading」「no gradients」
    「no anti-aliasing」—— 同样会被字面画出来，但 C021 管不到。
    实测踩过：`minimal-line-art` 写了 `no shading and no fill`。
    """
    pattern = re.compile(r"(?<![A-Za-z])(?:no|not|without|never|none|nothing)(?![A-Za-z])", re.I)
    bad = [f"{n}:{pattern.findall(s.suffix)}" for n, s in styles_mod.BUILTIN.items() if pattern.search(s.suffix)]
    assert bad == [], "这些风格后缀含否定词：" + "；".join(bad)


def test_monochrome_presets_state_black_and_white_positively():
    """标了单色就必须**正面**写出黑白 —— 靠否定（`not color`）是无效的。"""
    keys = ("grayscale", "monochrome", "black and white", "two-tone")
    bad = [n for n, s in styles_mod.BUILTIN.items()
           if s.monochrome and not any(k in s.suffix.lower() for k in keys)]
    assert bad == [], f"这些单色预设没写清楚黑白：{bad}"


def test_every_builtin_declares_a_single_frame():
    bad = [n for n, s in styles_mod.BUILTIN.items() if "full-frame" not in s.suffix]
    assert bad == [], f"这些预设没声明单幅（可能出联画/分格）：{bad}"
