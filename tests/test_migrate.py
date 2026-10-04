"""拍摄稿 v2 迁移（`lvs migrate`）的测试。

迁移是**有损风险**的操作（改的是作者的原稿），所以两条纪律要在测试里钉死：
1. 绝不覆盖原稿
2. 判据与 `lvs check` 同源（不然两个工具会互相打架）
"""

from __future__ import annotations

from pathlib import Path

from lvs import cast as cast_mod
from lvs import migrate as migrate_mod
from lvs import prompting


# ---- 补 kind 标注 ----------------------------------------------------------


def test_tag_line_adds_scene():
    body, changed = migrate_mod.tag_line("十月的芒草坡，风过草浪")
    assert changed
    assert body.startswith("[场景] ")


def test_tag_line_adds_graphic():
    body, changed = migrate_mod.tag_line("分栏对照表：左边数字，右边文字")
    assert changed
    assert body.startswith("[图表] ")


def test_tag_line_is_idempotent():
    """已经是 `[场景]` 的不能再加一层 —— 迁移要能安全地重复跑。"""
    body, changed = migrate_mod.tag_line("[场景] 十月的芒草坡")
    assert not changed
    assert body == "[场景] 十月的芒草坡"


def test_tag_line_respects_existing_tag_variants():
    for tag in ("[实拍]", "[画面]", "[图表卡]", "[图示]"):
        _, changed = migrate_mod.tag_line(f"{tag} 某个画面")
        assert not changed, f"{tag} 应被认作已有标注"


# ---- 补人物槽位 ------------------------------------------------------------


def _registry() -> dict[str, cast_mod.CastEntry]:
    return {
        "NAOKO": cast_mod.CastEntry(display="直子", aliases=["なおこ", "Naoko"]),
        "MIDORI": cast_mod.CastEntry(display="小林绿子", aliases=["绿子"]),
    }


def test_slot_line_replaces_name():
    body, n, _p, _f = migrate_mod.slot_line("直子的侧脸绷紧", _registry())
    assert n == 1
    assert body == "{NAOKO} 侧脸绷紧"


def test_slot_line_eats_the_particle():
    """`直子的侧脸` → `{NAOKO} 侧脸`：锚定描述是英文，后面挂个中文「的」很别扭。"""
    body, _n, _p, _f = migrate_mod.slot_line("直子的手", _registry())
    assert "的" not in body.split("{NAOKO}")[1][:2]


def test_slot_line_matches_alias():
    body, n, _p, _f = migrate_mod.slot_line("绿子把太阳镜摘下来", _registry())
    assert n == 1
    assert "{MIDORI}" in body


def test_slot_line_only_first_occurrence():
    """同一条画面位里反复提同一个人很常见，只换第一处 —— 换两遍会让提示词里
    塞两段一模一样的锚定描述。"""
    body, n, _p, _f = migrate_mod.slot_line("直子的侧脸，直子的手", _registry())
    assert n == 1
    assert body.count("{NAOKO}") == 1
    assert "直子的手" in body


def test_slot_line_prefers_longer_name():
    """`小林绿子` 与别名 `绿子` 同时能匹配时，优先换更长的那个（否则会切出「小林{绿子}」）。"""
    reg = {"MIDORI": cast_mod.CastEntry(display="小林绿子", aliases=["绿子"])}
    body, n, _p, _f = migrate_mod.slot_line("小林绿子在教室里", reg)
    assert n == 1
    assert body.startswith("{MIDORI}")
    assert "小林" not in body


def test_slot_line_no_match_is_noop():
    body, n, _p, _f = migrate_mod.slot_line("空荡的草地", _registry())
    assert n == 0
    assert body == "空荡的草地"


# ---- 整篇迁移 --------------------------------------------------------------


SAMPLE = """# 测试稿

## 一、传达层

**【冷开场】**（0:00–0:30）
> 这片草地上，有一口井。

## 二、正文讲稿

### 【① 开场】0:00–0:30

她走过来了。

[画面位] 直子的侧脸绷紧的肩线，背景草浪仍在炸散

[画面位] 空无一人的十月草地，杂草覆径

### 【② 对照】0:30–1:20

数据说明。

[画面位] 分栏对照：左边年份，右边城池数

## 三、画面位清单

| 时间 | 画面 | 素材建议 |
|---|---|---|
| 0:00 | 草地 | 生图 |
"""


def test_migrate_text_tags_all_marks():
    new_text, report = migrate_mod.migrate_text(SAMPLE, _registry())
    assert report.total_marks == 3
    assert report.tagged == 3
    lines = [ln for ln in new_text.splitlines() if ln.startswith("[画面位]")]
    assert all("[场景]" in ln or "[图表]" in ln for ln in lines)


def test_migrate_text_slots_and_report():
    new_text, report = migrate_mod.migrate_text(SAMPLE, _registry())
    assert report.slotted == 1
    assert "{NAOKO}" in new_text
    assert any(c.why == "补 1 处人物槽位" for c in report.changes)


def test_migrate_text_lists_negations_for_human():
    """否定式**不能**自动改 —— 改法要看上下文。迁移必须把它列成工作清单。"""
    _, report = migrate_mod.migrate_text(SAMPLE, _registry())
    assert len(report.worklist) == 1
    assert "空无一人" in report.worklist[0].text


def test_migrate_text_does_not_touch_narration():
    new_text, _ = migrate_mod.migrate_text(SAMPLE, _registry())
    assert "她走过来了。" in new_text
    assert "> 这片草地上，有一口井。" in new_text


def test_migrate_text_preserves_trailing_newline():
    new_text, _ = migrate_mod.migrate_text(SAMPLE, _registry())
    assert new_text.endswith("\n")


def test_migrate_report_markdown_mentions_worklist():
    _, report = migrate_mod.migrate_text(SAMPLE, _registry())
    md = report.to_markdown("测试稿.md")
    assert "待人工判断" in md
    assert "空无一人" in md


def test_migrate_rules_same_source_as_check():
    """迁移的工作清单与 `lvs check` 的 C021 必须同源 —— 两个工具报的东西不一样，
    用户就不知道该信哪个（这比没有工具更糟）。"""
    text = "空无一人的草地，旗面空白无字，不给正面"
    hits = prompting.find_negations(text)
    assert len(hits) >= 3
    # check 用的就是这一份规则；这里断言的是"它确实能命中这三类"
    codes = {c for c, _, _ in hits}
    assert "C021-empty" in codes
    assert "C021-noText" in codes
    assert "C021-noGive" in codes


# ---- CLI -------------------------------------------------------------------


class _Args:
    def __init__(self, **kw):
        self.manuscript = None
        self.out = None
        self.cast_dir = None
        self.__dict__.update(kw)


class _Cfg:
    def __init__(self, data=None):
        self._d = data or {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def has(self, key):
        return key in self._d and self._d[key] not in ("", [], {})


def test_run_command_never_overwrites_original(tmp_path: Path, capsys):
    src = tmp_path / "001-测试稿.md"
    src.write_text(SAMPLE, encoding="utf-8")
    original = src.read_text(encoding="utf-8")

    code = migrate_mod.run_command(_Cfg(), None, _Args(manuscript=str(src)))

    assert code == 0
    assert src.read_text(encoding="utf-8") == original, "原稿必须一字不动"
    out = tmp_path / migrate_mod.DEFAULT_OUTDIRNAME / "001-测试稿.md"
    assert out.is_file()
    assert "{NAOKO}" not in out.read_text(encoding="utf-8")  # 没有 registry → 不补槽位


def test_run_command_writes_report(tmp_path: Path):
    src = tmp_path / "a.md"
    src.write_text(SAMPLE, encoding="utf-8")
    migrate_mod.run_command(_Cfg(), None, _Args(manuscript=str(src)))
    report = tmp_path / migrate_mod.DEFAULT_OUTDIRNAME / "a-迁移报告.md"
    assert report.is_file()
    assert "待人工判断" in report.read_text(encoding="utf-8")


def test_run_command_respects_out_dir(tmp_path: Path):
    src = tmp_path / "a.md"
    src.write_text(SAMPLE, encoding="utf-8")
    target = tmp_path / "elsewhere"
    migrate_mod.run_command(_Cfg(), None, _Args(manuscript=str(src), out=str(target)))
    assert (target / "a.md").is_file()


def test_run_command_missing_file(tmp_path: Path, capsys):
    assert migrate_mod.run_command(_Cfg(), None, _Args(manuscript=str(tmp_path / "no.md"))) == 2


def test_run_command_missing_arg(capsys):
    assert migrate_mod.run_command(_Cfg(), None, _Args()) == 2


# ---- v2 追加：代词槽位（`代词：` 声明 + 边界判据） --------------------------


def test_read_pronoun_line_parses_pairs():
    text = "# 标题\n\n> 代词：她={NAOKO}，他={渡边彻}\n\n---\n"
    assert migrate_mod.read_pronoun_line(text) == {"她": "NAOKO", "他": "渡边彻"}


def test_read_pronoun_line_tolerates_plain_and_fullwidth_equals():
    assert migrate_mod.read_pronoun_line("代词：她=NAOKO") == {"她": "NAOKO"}
    assert migrate_mod.read_pronoun_line("代词：她＝NAOKO") == {"她": "NAOKO"}


def test_read_pronoun_line_takes_the_first_match_only():
    text = "代词：她=NAOKO\n代词：她=MIDORI\n"
    assert migrate_mod.read_pronoun_line(text) == {"她": "NAOKO"}


def test_read_pronoun_line_absent_is_empty():
    assert migrate_mod.read_pronoun_line("# 标题\n正文\n") == {}


def _reg():
    return {
        "渡边彻": cast_mod.CastEntry(display="渡边彻", aliases=["渡边"], anchor="a",
                                     pronouns=["他"]),
        "NAOKO": cast_mod.CastEntry(display="直子", anchor="b"),
    }


def test_slot_line_pronoun_at_line_start():
    """★ 句首的代词前面什么都没有 —— `"" in "其无任..."` 恒为真，
    少了短路判断就会把句首代词全部挡掉（不报错，只是悄悄少注入一个锚定）。"""
    out, _n, p, _ = migrate_mod.slot_line("她钻出松林", _reg(), {"她": "NAOKO"})
    assert out == "{NAOKO} 钻出松林"
    assert p == 1


def test_slot_line_ignores_compound_words():
    pron = {"他": "渡边彻", "我": "渡边彻", "她": "NAOKO"}
    for text in ("其他人都走了", "他们并肩走", "自我放逐", "她们都笑了"):
        out, _n, p, _ = migrate_mod.slot_line(text, _reg(), pron)
        assert out == text, f"{text} 不该被改"
        assert p == 0


def test_slot_line_swallows_trailing_de():
    """`直子的` → `{NAOKO} `：锚定描述是英文，后面挂个中文「的」很别扭。"""
    out, _n, p, _ = migrate_mod.slot_line("直子的左手握住他的手", _reg(), {"他": "渡边彻"})
    assert out == "{NAOKO} 左手握住{渡边彻} 手"
    assert p == 1


def test_slot_line_does_not_reinject_same_person():
    """同一个人的锚定不注入两遍（姓名换过一次，代词就跳过）。"""
    out, n, p, _ = migrate_mod.slot_line("渡边走在前面，他回头", _reg(), {"他": "渡边彻"})
    assert out.count("{渡边彻}") == 1
    assert n == 1 and p == 0


def test_slot_line_short_alias_does_not_corrupt_full_slot():
    """`渡边` 是 `渡边彻` 的子串。没有槽位守卫就会产出 `{{渡边彻}彻}`。"""
    out, n, _p, _ = migrate_mod.slot_line("渡边彻走过", _reg(), {})
    assert out == "{渡边彻} 走过"
    assert "{渡边彻}彻" not in out
    assert n == 1


def test_slot_line_keeps_existing_slot_and_fills_the_other():
    out, n, _p, _ = migrate_mod.slot_line("{NAOKO} 与渡边并肩", _reg(), {})
    assert out == "{NAOKO} 与{渡边彻} 并肩"
    assert n == 1


def test_slot_line_flags_comparison_mention():
    """「与渡边彻同龄」和「与渡边彻并肩」句式相同 —— 机器照常槽位化，但必须列出来给人复核。"""
    out, n, _p, flags = migrate_mod.slot_line("与渡边彻同龄的十九岁少年", _reg(), {})
    assert n == 1 and "{渡边彻}" in out
    assert flags and "比较提及" in flags[0]


def test_slot_line_no_flag_for_plain_mention():
    _out, _n, _p, flags = migrate_mod.slot_line("渡边走在前面", _reg(), {})
    assert flags == []


def test_migrate_text_records_pronoun_slots_and_review():
    text = "[画面位] 她钻出松林\n[画面位] 与渡边彻同龄的少年\n"
    new, report = migrate_mod.migrate_text(text, _reg(), {"她": "NAOKO"})
    assert "{NAOKO} 钻出松林" in new
    assert report.pronoun_slotted == 1
    assert len(report.review) == 1
    md = report.to_markdown("t")
    assert "比较提及" in md
    assert "补代词槽位 **1** 条" in md
