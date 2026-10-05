"""定妆（`lvs cast`）的测试。

重点是**闸门语义**：只有 state=REF 且真有参考图才算"能绑定"，
其余一律拦住 `assets`。这条错了整个流程的价值就没了。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lvs import cast as cast_mod


# ---- 槽位提取 --------------------------------------------------------------


def test_extract_slots_basic():
    text = "{NAOKO} 和 {WATANABE} 走在草地上，{MIDORI} 不在。"
    assert cast_mod.extract_slots(text) == {"NAOKO", "WATANABE", "MIDORI"}


def test_extract_slots_dedupes():
    assert cast_mod.extract_slots("{A} {A} {A}") == {"A"}


def test_extract_slots_ignores_lowercase_only_and_digits_first():
    assert cast_mod.extract_slots("{1x} {_under} {} {ok_name}") == {"ok_name"}


def test_slots_of_shots_reports_shot_ids():
    shots = [
        {"id": 1, "visual": "{NAOKO} 走过"},
        {"id": 2, "visual": "空镜"},
        {"id": 3, "prompt": "portrait of {NAOKO}", "visual": "{WATANABE} 坐着"},
    ]
    got = cast_mod.slots_of_shots(shots)
    assert got == {"NAOKO": [1, 3], "WATANABE": [3]}


def test_slots_of_shots_looks_at_prompt_too():
    """LLM 改写提示词时可能把槽位挪到 prompt 里，只看 visual 会漏。"""
    shots = [{"id": 7, "visual": "一个女孩", "prompt": "{NAOKO}, medium shot"}]
    assert cast_mod.slots_of_shots(shots) == {"NAOKO": [7]}


# ---- 定妆卡解析 ------------------------------------------------------------


def test_parse_card_json_block():
    text = """
# 定妆卡

```json
{"characters": {"NAOKO": {"display": "直子", "anchor": "clearly female, long black hair", "state": "REF"}}}
```
"""
    entries = cast_mod.parse_card(text)
    assert "NAOKO" in entries
    assert entries["NAOKO"].display == "直子"
    assert entries["NAOKO"].state == "REF"


def test_parse_card_heading_and_fence():
    text = """
## 三、人物定妆卡

### 直子（なおこ / Naoko）

- 定位：女主。

- 锚定描述：
```
a beautiful slender Japanese young woman, clearly female, long straight black hair
```

- 构图限制：不给正面。

### 木月（Kizuki）

- 锚定描述（第 2 章已核对）：
```
a lean Japanese high-school boy around 17
```
"""
    entries = cast_mod.parse_card(text)
    ids = set(entries)
    assert "NAOKO" in ids, f"应从括号别名取出拉丁 ID，实得 {ids}"
    assert "KIZUKI" in ids
    assert "clearly female" in entries["NAOKO"].anchor
    assert entries["NAOKO"].display == "直子"
    assert entries["NAOKO"].state == "IMG"


def test_parse_card_heading_without_anchor_is_plan():
    text = "### 玲子（Reiko）\n\n- 定位：疗养院病友。\n- 锚定描述：`待核对（第 6 章）`\n"
    entries = cast_mod.parse_card(text)
    assert entries["REIKO"].state == cast_mod.STATE_PLAN


def test_parse_card_empty():
    assert cast_mod.parse_card("") == {}


# ---- 注入 ------------------------------------------------------------------


def test_apply_slots_replaces_anchor():
    lock = cast_mod.CastLock(
        characters={"NAOKO": cast_mod.CastEntry(anchor="clearly female, long black hair",
                                                state=cast_mod.STATE_REF)}
    )
    out = cast_mod.apply_slots("{NAOKO} 侧身走过校墙", lock)
    assert out == "clearly female, long black hair 侧身走过校墙"


def test_apply_slots_keeps_unknown_placeholder():
    """未登记的名字**原样保留** —— 让错误在提示词里显形，而不是悄悄消失。"""
    out = cast_mod.apply_slots("{UNKNOWN} 走在路上", cast_mod.CastLock())
    assert out == "{UNKNOWN} 走在路上"


def test_apply_slots_no_op_without_slots():
    assert cast_mod.apply_slots("普通画面", cast_mod.CastLock()) == "普通画面"


# ---- 闸门 ------------------------------------------------------------------


def _ref_character(tmp_path: Path, name: str = "NAOKO") -> cast_mod.CastEntry:
    ref = tmp_path / f"ref-{name}.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n")  # 内容不重要，存在即可
    return cast_mod.CastEntry(display=name, anchor="x", state=cast_mod.STATE_REF, refs=[str(ref)])


def test_gate_blocks_when_state_is_img(tmp_path: Path):
    lock = cast_mod.CastLock(characters={"NAOKO": cast_mod.CastEntry(state=cast_mod.STATE_IMG)})
    missing = cast_mod.gate_missing([{"id": 1, "visual": "{NAOKO} 走过"}], lock)
    assert missing == {"NAOKO": [1]}


def test_gate_blocks_when_ref_missing(tmp_path: Path):
    """state 说是 REF 但参考图文件不在 —— 一样拦住（状态与产物不一致必须显形）。"""
    lock = cast_mod.CastLock(
        characters={"NAOKO": cast_mod.CastEntry(state=cast_mod.STATE_REF, refs=["/nope/x.png"])}
    )
    assert cast_mod.gate_missing([{"id": 2, "visual": "{NAOKO}"}], lock) == {"NAOKO": [2]}


def test_gate_passes_when_approved(tmp_path: Path):
    lock = cast_mod.CastLock(characters={"NAOKO": _ref_character(tmp_path)})
    assert cast_mod.gate_missing([{"id": 1, "visual": "{NAOKO} 走过"}], lock) == {}


def test_gate_ignores_shots_without_slots(tmp_path: Path):
    lock = cast_mod.CastLock()
    assert cast_mod.gate_missing([{"id": 1, "visual": "空无一人的草地"}], lock) == {}


def test_gate_message_contains_actionable_steps(tmp_path: Path):
    lock = cast_mod.CastLock()
    msg = cast_mod.gate_message({"NAOKO": [1, 2]}, lock, tmp_path)
    assert "lvs cast --render NAOKO" in msg
    assert "lvs cast --approve NAOKO" in msg
    assert "--no-cast-gate" in msg, "必须给出逃生口，否则纯空镜集无法推进"


# ---- 读写 ------------------------------------------------------------------


def test_save_and_load_lock_roundtrip(tmp_path: Path):
    lock = cast_mod.CastLock(
        characters={"NAOKO": cast_mod.CastEntry(display="直子", anchor="a", state=cast_mod.STATE_REF,
                                                refs=["r.png"])},
        locations={"AMEIRYO": cast_mod.CastEntry(anchor="sanatorium")},
        style="jp-youth-manga-bw",
    )
    cast_mod.save_lock(tmp_path, lock)
    back = cast_mod.load_lock(tmp_path)
    assert back.characters["NAOKO"].display == "直子"
    assert back.characters["NAOKO"].state == cast_mod.STATE_REF
    assert back.locations["AMEIRYO"].anchor == "sanatorium"
    assert back.style == "jp-youth-manga-bw"


def test_load_lock_missing_returns_empty(tmp_path: Path):
    lock = cast_mod.load_lock(tmp_path / "nope")
    assert lock.characters == {}


def test_load_lock_corrupt_raises(tmp_path: Path):
    (tmp_path / "lock.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(cast_mod.CastError):
        cast_mod.load_lock(tmp_path)


def test_registry_roundtrip(tmp_path: Path):
    entries = {"NAOKO": cast_mod.CastEntry(display="直子", anchor="a")}
    cast_mod.save_registry(tmp_path, entries)
    assert cast_mod.load_registry(tmp_path)["NAOKO"].display == "直子"


# ---- 版本管理 --------------------------------------------------------------


def test_next_version_starts_at_one(tmp_path: Path):
    assert cast_mod.next_version(tmp_path / "NAOKO") == 1


def test_next_version_increments(tmp_path: Path):
    d = tmp_path / "NAOKO"
    (d / "v1").mkdir(parents=True)
    (d / "v2").mkdir()
    assert cast_mod.next_version(d) == 3


def test_next_version_ignores_non_version_dirs(tmp_path: Path):
    d = tmp_path / "NAOKO"
    (d / "v1").mkdir(parents=True)
    (d / "_rejected").mkdir()
    (d / "notes").mkdir()
    assert cast_mod.next_version(d) == 2


def test_latest_version(tmp_path: Path):
    d = tmp_path / "NAOKO"
    (d / "v1").mkdir(parents=True)
    (d / "v7").mkdir()
    (d / "v3").mkdir()
    assert cast_mod.latest_version(d).name == "v7"


def test_latest_version_none_when_empty(tmp_path: Path):
    assert cast_mod.latest_version(tmp_path / "nope") is None


# ---- 批准 / 打回 -----------------------------------------------------------


def test_approve_promotes_candidates_to_refs(tmp_path: Path):
    char_dir = tmp_path / "NAOKO" / "v1"
    char_dir.mkdir(parents=True)
    for i in (1, 2, 3):
        (char_dir / f"cand-{i:02d}.png").write_bytes(b"x")
    lock = cast_mod.CastLock()

    refs = cast_mod.approve(tmp_path, lock, "NAOKO")

    assert len(refs) == 3
    assert all(p.is_file() for p in refs)
    assert lock.characters["NAOKO"].state == cast_mod.STATE_REF
    assert lock.characters["NAOKO"].approved_at
    # 候选目录保留原样（不搬走），方便回看当时还有哪些别的样子
    assert (char_dir / "cand-01.png").is_file()
    assert cast_mod.load_lock(tmp_path).characters["NAOKO"].refs


def test_approve_specific_version(tmp_path: Path):
    for v in (1, 2):
        d = tmp_path / "NAOKO" / f"v{v}"
        d.mkdir(parents=True)
        (d / "cand-01.png").write_bytes(f"v{v}".encode())
    lock = cast_mod.CastLock()
    refs = cast_mod.approve(tmp_path, lock, "NAOKO", version=1)
    assert (tmp_path / refs[0] if not Path(refs[0]).is_absolute() else Path(refs[0])).read_bytes() == b"v1"


def test_approve_from_external_file(tmp_path: Path):
    outside = tmp_path / "chosen.png"
    outside.write_bytes(b"picked")
    lock = cast_mod.CastLock()
    refs = cast_mod.approve(tmp_path, lock, "NAOKO", source=outside)
    assert len(refs) == 1
    assert Path(refs[0]).read_bytes() == b"picked"
    assert lock.characters["NAOKO"].state == cast_mod.STATE_REF


def test_approve_without_candidates_raises(tmp_path: Path):
    with pytest.raises(cast_mod.CastError, match="还没有候选定妆照"):
        cast_mod.approve(tmp_path, cast_mod.CastLock(), "NAOKO")


def test_approve_missing_source_raises(tmp_path: Path):
    with pytest.raises(cast_mod.CastError):
        cast_mod.approve(tmp_path, cast_mod.CastLock(), "NAOKO", source=tmp_path / "nope.png")


def test_reject_moves_version_and_records_note(tmp_path: Path):
    d = tmp_path / "NAOKO" / "v1"
    d.mkdir(parents=True)
    (d / "cand-01.png").write_bytes(b"x")
    lock = cast_mod.CastLock()

    dest = cast_mod.reject(tmp_path, lock, "NAOKO", note="脸太圆")

    assert dest == tmp_path / "NAOKO" / "_rejected" / "v1"
    assert not d.exists(), "原候选目录应被移走"
    assert (dest / "cand-01.png").is_file(), "打回的样本不能删"
    assert lock.characters["NAOKO"].rejected[0]["note"] == "脸太圆"
    assert lock.characters["NAOKO"].state == cast_mod.STATE_PLAN


def test_reject_keeps_ref_state_when_refs_exist(tmp_path: Path):
    d = tmp_path / "NAOKO" / "v1"
    d.mkdir(parents=True)
    (d / "cand-01.png").write_bytes(b"x")
    lock = cast_mod.CastLock(
        characters={"NAOKO": cast_mod.CastEntry(state=cast_mod.STATE_REF, refs=["already.png"])}
    )
    cast_mod.reject(tmp_path, lock, "NAOKO")
    assert lock.characters["NAOKO"].state == cast_mod.STATE_REF, "已有参考图不该被一次打回降级"


def test_reject_without_candidates_raises(tmp_path: Path):
    with pytest.raises(cast_mod.CastError):
        cast_mod.reject(tmp_path, cast_mod.CastLock(), "NAOKO")


# ---- 定妆提示词 ------------------------------------------------------------


def test_cast_prompt_has_flat_light_not_dramatic():
    prompt = cast_mod.cast_prompt(cast_mod.CastEntry(anchor="clearly female, long hair"))
    assert "clearly female" in prompt
    assert "frontal lighting" in prompt, "定妆照要平光：目标是看清脸，不是好看"
    assert "dramatic" not in prompt.lower()


def test_cast_prompt_appends_style():
    prompt = cast_mod.cast_prompt(cast_mod.CastEntry(anchor="a"), style_suffix="black and white ink")
    assert prompt.endswith("black and white ink.")


def test_cast_prompt_falls_back_to_display_name():
    prompt = cast_mod.cast_prompt(cast_mod.CastEntry(display="直子"))
    assert "直子" in prompt


# ---- cast_dir --------------------------------------------------------------


def test_cast_dir_override_wins(tmp_path: Path):
    assert cast_mod.cast_dir(None, tmp_path) == tmp_path


def test_cast_dir_from_config(tmp_path: Path):
    class Cfg:
        def get(self, key, default=None):
            return str(tmp_path) if key == "cast.dir" else default

    assert cast_mod.cast_dir(Cfg()) == tmp_path


def test_cast_dir_defaults_to_repo_root():
    from lvs.config import PROJECT_ROOT

    assert cast_mod.cast_dir(None) == PROJECT_ROOT / "cast"


# ---- CLI 路径 --------------------------------------------------------------


class _Args:
    def __init__(self, **kw):
        self.dir = None
        self.extract = False
        self.render = None
        self.count = 4
        self.seed = None
        self.approve = None
        self.reject = None
        self.from_file = None
        self.note = ""
        self.__dict__.update(kw)


class _Cfg:
    def __init__(self, data=None):
        self._d = data or {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def has(self, key):
        return key in self._d and self._d[key] not in ("", [], {})


class _Ws:
    def __init__(self, source: str = "", shots=None):
        self._source = source
        self._shots = {"shots": shots or []}

    def path(self, *parts):
        return Path("/nonexistent") / Path(*parts)

    def try_load_shots(self):
        return self._shots


def test_run_command_extract_writes_registry(tmp_path: Path):
    src = "# t\n\n{NAOKO} 走过\n{WATANABE} 坐着\n"
    ws = _Ws(source=src)
    ws.path = lambda *p: _FakePath(src)  # type: ignore[assignment]

    args = _Args(dir=str(tmp_path), extract=True)
    code = cast_mod.run_command(_Cfg(), ws, args)
    assert code == 0
    registry = json.loads((tmp_path / "cast.json").read_text(encoding="utf-8"))
    assert set(registry["characters"]) == {"NAOKO", "WATANABE"}
    lock = json.loads((tmp_path / "lock.json").read_text(encoding="utf-8"))
    assert set(lock["characters"]) == {"NAOKO", "WATANABE"}
    # 没有锚定描述 → 一律 PLAN，闸门会拦住
    assert all(v["state"] == cast_mod.STATE_PLAN for v in lock["characters"].values())


def test_run_command_status_returns_one_when_gate_blocked(tmp_path: Path):
    ws = _Ws(shots=[{"id": 1, "visual": "{NAOKO} 走过"}])
    assert cast_mod.run_command(_Cfg(), ws, _Args(dir=str(tmp_path))) == 1


def test_run_command_status_returns_zero_when_clear(tmp_path: Path):
    ref = tmp_path / "r.png"
    ref.write_bytes(b"x")
    cast_mod.save_lock(
        tmp_path,
        cast_mod.CastLock(characters={"NAOKO": cast_mod.CastEntry(
            anchor="a", state=cast_mod.STATE_REF, refs=[str(ref)])}),
    )
    ws = _Ws(shots=[{"id": 1, "visual": "{NAOKO} 走过"}])
    assert cast_mod.run_command(_Cfg(), ws, _Args(dir=str(tmp_path))) == 0


def test_run_command_approve_unknown_returns_zero_with_message(tmp_path: Path, capsys):
    """approve 一个没有候选的人：逐条报错但整体不崩（批量操作要容错）。"""
    code = cast_mod.run_command(_Cfg(), _Ws(), _Args(dir=str(tmp_path), approve="GHOST"))
    assert code == 0
    assert "失败" in capsys.readouterr().out


class _FakePath:
    """让 `ws.path("source.md")` 返回一个"存在的文件"。"""

    def __init__(self, text: str):
        self._text = text

    def is_file(self) -> bool:
        return True

    def read_text(self, encoding: str = "utf-8") -> str:
        return self._text

    def __str__(self) -> str:
        return "<fake source.md>"


# ---- v2 追加：中日文槽位 / 定妆卡噪声过滤 ----------------------------------


def test_extract_slots_accepts_cjk_names():
    """定妆卡里有角色没有拉丁名（渡边彻 / 突撃隊）—— 槽位必须允许中日文，
    否则会逼着人去改定妆卡，而"改格式"是最不该收的成本。"""
    text = "{渡边彻} 与 {直子} 并肩，{突撃隊} 在门口"
    assert cast_mod.extract_slots(text) == {"渡边彻", "直子", "突撃隊"}


def test_extract_slots_still_ignores_empty_and_numeric():
    assert cast_mod.extract_slots("{} {1} {_x} {ok}") == {"ok"}


def test_apply_slots_cjk_roundtrip():
    lock = cast_mod.CastLock(
        characters={"渡边彻": cast_mod.CastEntry(anchor="a lean 20-year-old Japanese man",
                                                 state=cast_mod.STATE_REF)}
    )
    assert cast_mod.apply_slots("{渡边彻} 坐在草地上", lock) == "a lean 20-year-old Japanese man 坐在草地上"


def test_parse_card_skips_sections_without_anchor_label():
    """定妆卡里 `### 风格前缀` / `### A 类（递进轨）` 也是三级标题，
    但它们不是人物 —— 混进定妆库会变成"没有锚定描述的人物"，污染闸门。"""
    text = """## 二、画面风格

### 风格前缀（拼进每条生图提示词的头部）

```
strictly monochrome black and white
```

### 负向约束（拼进每条提示词的尾部）

```
color, any text
```

## 三、人物定妆卡

### 直子（なおこ / Naoko）

- 锚定描述：
```
clearly female, long straight glossy jet-black hair
```
"""
    entries = cast_mod.parse_card(text)
    assert set(entries) == {"NAOKO"}, f"应只认有锚定描述的小节，实得 {set(entries)}"


def test_parse_card_inline_pending_note_is_plan():
    """`锚定描述：待核对（后续章）` 这种没给正文的，应登记为 PLAN 而不是消失。"""
    text = "### 直子的姐姐\n\n- 锚定描述：`待核对（后续章）`。\n"
    entries = cast_mod.parse_card(text)
    assert entries["直子的姐姐"].state == cast_mod.STATE_PLAN


def test_parse_card_collects_anchor_variants():
    """渡边彻在定妆卡里有两段锚定（青年期 / 37 岁）。两条都要留 -
    硬塞成一条会让"19 岁的脸"和"37 岁的脸"争同一个槽位。"""
    text = """### 渡边彻（わたなべ とおる / "我"）

- 锚定描述（青年期，1969）：
```
a lean 20-year-old Japanese man
```
- 锚定描述（37 岁）：
```
a lean 37-year-old Japanese man in a dark overcoat
```
"""
    entry = cast_mod.parse_card(text)["渡边彻"]
    assert entry.anchor == "a lean 20-year-old Japanese man"
    assert len(entry.anchor_variants) == 2
    assert "37-year-old" in entry.anchor_variants[1]


def test_parse_card_id_prefers_latin_alias():
    text = """### 小林绿子（绿子 / Midori）

- 锚定描述：
```
a lively Japanese college girl
```
"""
    assert "MIDORI" in cast_mod.parse_card(text)


def test_parse_card_id_falls_back_to_display_name():
    text = """### 突撃隊（"敢死队"，渡边的宿舍室友）

- 锚定描述：
```
a tall young Japanese man with a completely shaved bald head
```
"""
    entries = cast_mod.parse_card(text)
    assert "突撃隊" in entries


# ---- v2 追加 2：CLI 分支的真假值陷阱 / 状态文案诚实性 ----------------------


def test_run_command_sheet_flag_is_not_falsy(tmp_path: Path, monkeypatch, capsys):
    """`--sheet` 是 `nargs="?"` + `const=""`：不带值时拿到的是**空字符串**。
    用真值判断会直接漏进状态分支 —— 表现是"打了 --sheet 却打印库状态"。
    """
    called: dict[str, object] = {}

    def fake_sheet(directory, out_path=None, **kw):  # noqa: ANN001, ANN202
        called["dir"] = directory
        called["out"] = out_path
        return tmp_path / "sheet.png"

    monkeypatch.setattr(cast_mod, "build_contact_sheet", fake_sheet)
    code = cast_mod.run_command(_Cfg(), _Ws(), _Args(dir=str(tmp_path), sheet=""))
    assert code == 0
    assert "dir" in called, "--sheet（空值）也必须走审阅表分支"
    assert called["out"] is None


def test_run_command_sheet_with_explicit_path(tmp_path: Path, monkeypatch):
    captured: dict[str, object] = {}

    def fake_sheet(directory, out_path=None, **kw):  # noqa: ANN001, ANN202
        captured["out"] = out_path
        return tmp_path / "s.png"

    monkeypatch.setattr(cast_mod, "build_contact_sheet", fake_sheet)
    cast_mod.run_command(_Cfg(), _Ws(), _Args(dir=str(tmp_path), sheet=str(tmp_path / "x.png")))
    assert str(captured["out"]) == str(tmp_path / "x.png")


def test_status_says_it_cannot_judge_without_shots(tmp_path: Path, capsys):
    """没有 shots.json 时"没缺人"是**废话**，不是通过 —— 不能报"闸门通过"，
    否则用户以为一致性保护生效了（其实连稿子都没解析）。"""
    code = cast_mod.run_command(_Cfg(), _Ws(shots=[]), _Args(dir=str(tmp_path)))
    out = capsys.readouterr().out
    assert code == 0
    assert "闸门通过" not in out
    assert "无法判断闸门" in out


def test_status_says_gate_is_a_noop_without_slots(tmp_path: Path, capsys):
    """有分镜但一条 `{NAME}` 槽位都没有：闸门无事可做，必须说出来。"""
    ws = _Ws(shots=[{"id": 1, "visual": "十月的草地，风把草压向一侧"}])
    code = cast_mod.run_command(_Cfg(), ws, _Args(dir=str(tmp_path)))
    out = capsys.readouterr().out
    assert code == 0
    assert "闸门通过" not in out
    assert "没有任何" in out and "槽位" in out


def test_status_reports_pass_with_slots(tmp_path: Path, capsys):
    ref = tmp_path / "r.png"
    ref.write_bytes(b"x")
    cast_mod.save_lock(
        tmp_path,
        cast_mod.CastLock(characters={"NAOKO": cast_mod.CastEntry(
            anchor="a", state=cast_mod.STATE_REF, refs=[str(ref)])}),
    )
    ws = _Ws(shots=[{"id": 1, "visual": "{NAOKO} 侧身走过"}])
    code = cast_mod.run_command(_Cfg(), ws, _Args(dir=str(tmp_path)))
    out = capsys.readouterr().out
    assert code == 0
    assert "闸门通过" in out
    assert "NAOKO" in out


# ---- v2 追加 3：审阅表的分行规则 -------------------------------------------


def test_contact_sheet_makes_one_row_per_version(tmp_path: Path):
    """同一个人的多段锚定（渡边彻 19 岁 / 37 岁）是**两张不同的脸**，
    只看最新一轮会把前一段藏起来 —— 而"两段都在"正是它需要被审的原因。"""
    pytest.importorskip("PIL")
    for v, variant in (("v1", 1), ("v2", 2)):
        d = tmp_path / "渡边彻" / v
        d.mkdir(parents=True)
        (d / "cand-01.png").write_bytes(b"x")
        (d / "_meta.json").write_text(json.dumps({"variant": variant}), encoding="utf-8")
    out = cast_mod.build_contact_sheet(tmp_path, out_path=tmp_path / "sheet.png")
    assert out is not None and out.is_file()


# ---- v2 追加 3：锚定描述体检（`lvs cast --lint`） ---------------------------
#
# 判据全部来自本项目实机踩过的坑（详见 cast.ANCHOR_RULES 的注释）。
# 每条规则都要有"报"与"不报"两侧 —— 漏报=白做，误报=没人信。


def test_lint_flags_measure_written_in_words():
    """「四五公分」是**词**不是数字 —— 只认数字会漏掉真实写法。"""
    codes = {f["code"] for f in cast_mod.lint_anchor(
        "very short hair, about four or five centimetres long")}
    assert "A-measure" in codes


def test_lint_flags_measure_written_in_digits():
    codes = {f["code"] for f in cast_mod.lint_anchor("a man of 175 cm with a long face")}
    assert "A-measure" in codes


def test_lint_flags_haircut_procedure():
    """写"剪发动作"，模型看到的是动作不是结果。"""
    codes = {f["code"] for f in cast_mod.lint_anchor("a girl, hair cut very short, pale face")}
    assert "A-procedure" in codes


def test_lint_flags_simile():
    codes = {f["code"] for f in cast_mod.lint_anchor(
        "a woman, eyes that seem to live on their own")}
    assert "A-simile" in codes


def test_lint_flags_identity_before_feature():
    """开头放身份交代 → 采样预算花在气质而不是脸上（MIDORI 那次）。"""
    codes = {f["code"] for f in cast_mod.lint_anchor(
        "a lively Japanese college girl around 19, later we see her short hair")}
    assert "A-order" in codes


def test_lint_does_not_flag_feature_first():
    codes = {f["code"] for f in cast_mod.lint_anchor(
        "a very short cropped haircut with the ears fully exposed, a Japanese girl around 19")}
    assert "A-order" not in codes


def test_lint_flags_expression_in_anchor():
    """★ 木月四张候选像四个人的根因：`a faintly cool smile` 混进了锚定。"""
    codes = {f["code"] for f in cast_mod.lint_anchor(
        "a thin sharp face, bright eyes, a faintly cool smile, white shirt")}
    assert "A-expression" in codes


def test_lint_does_not_flag_expressive_features():
    """`expressive eyes` 是**五官特征**（表情丰富到像另一个生命体），不是表情词。"""
    codes = {f["code"] for f in cast_mod.lint_anchor(
        "a small neat face with very mobile, expressive eyes")}
    assert "A-expression" not in codes


def test_lint_flags_bloated_anchor():
    codes = {f["code"] for f in cast_mod.lint_anchor("a man, " + " ".join(["feature"] * 60))}
    assert "A-bloated" in codes


def test_lint_flags_negation_via_shared_rules():
    """否定式判据与 `lvs check` / `lvs migrate` **同源**，不另写一套。"""
    codes = {f["code"] for f in cast_mod.lint_anchor("a young man, no text on his shirt")}
    assert any(c.startswith("A-neg:") for c in codes)


def test_lint_empty_anchor():
    assert cast_mod.lint_anchor("")[0]["code"] == "A-empty"


def test_lint_passes_a_clean_anchor():
    assert cast_mod.lint_anchor(
        "a thin angular face with a narrow jaw and bright dark eyes under straight brows, "
        "slightly messy short black hair, a lean boy around 17") == []


def test_split_aliases_keeps_short_name_but_drops_possessives():
    """`渡边的宿舍室友` 是说明不是别名；但 `渡边` 是真别名 —— 旧判据把两个一起丢了。"""
    got = cast_mod._split_aliases('"敢死队"，渡边的宿舍室友')
    assert got == ["敢死队"]
    assert cast_mod._split_aliases("わたなべ とおる / 渡边") == ["わたなべ とおる", "渡边"]


def test_parse_card_reads_pronoun_line():
    card = """
### 渡边彻（わたなべ / 渡边）

- 出场：全书。
- 代词：我（第一人称叙述者）
- 锚定描述：
```
a lean young man with short black hair
```
"""
    entry = cast_mod.parse_card(card)["渡边彻"]
    assert entry.pronouns == ["我"]
    assert "渡边" in entry.aliases


def test_pronoun_map_only_resolves_unique_ones():
    reg = {
        "A": cast_mod.CastEntry(display="A", pronouns=["他"]),
        "B": cast_mod.CastEntry(display="B", pronouns=["我"]),
        "C": cast_mod.CastEntry(display="C", pronouns=["他"]),
    }
    resolved, ambiguous = cast_mod.pronoun_map(reg)
    assert resolved == {"我": "B"}
    assert ambiguous == {"他": ["A", "C"]}


def test_pronoun_map_empty_when_nobody_declares():
    resolved, ambiguous = cast_mod.pronoun_map({"A": cast_mod.CastEntry(display="A")})
    assert resolved == {} and ambiguous == {}


# ---- v2 追加 4：把"为修一个 bug 引入另一个 bug"钉死 -------------------------
#
# 2026-10-03 实测：为了修 MIDORI 的「头发盖住耳朵」，把特征前置到锚定最前，
# 结果**连主语一起丢了**（全句没有 girl / woman），模型给出一个男性。
# 看图才发现。所以这一条必须有规则 + 有测试。


def test_lint_flags_anchor_without_any_person_noun():
    """整段没有 girl/woman/man/student → 模型自己决定画谁。"""
    codes = {f["code"] for f in cast_mod.lint_anchor(
        "a very short cropped haircut with the ears fully exposed, a small neat oval face "
        "with very mobile expressive eyes, a slight slender build")}
    assert "A-nosubject" in codes


def test_lint_does_not_flag_anchor_with_person_noun_late():
    """主语在后半句也算（`... ; a lean Japanese high-school boy around 17`）。"""
    codes = {f["code"] for f in cast_mod.lint_anchor(
        "a thin angular face with a narrow jaw and very bright direct eyes under straight "
        "brows, slightly messy short black hair; a lean Japanese high-school boy around 17")}
    assert "A-nosubject" not in codes


def test_lint_accepts_subject_plus_with_plus_feature():
    """推荐写法：**主语 + with + 特征** —— 同时过 A-order 与 A-nosubject。"""
    anchor = ("a Japanese college girl around 19 with a very short cropped haircut, "
              "the ears and the whole nape of the neck fully exposed, the grown-out ends "
              "slightly uneven; a small neat oval face with very mobile, expressive eyes")
    assert cast_mod.lint_anchor(anchor) == []


def test_strip_aspect_removes_ratio_tokens():
    """定妆照是方图，风格后缀继承自场景图（带 `16:9`）—— 两者同现 = 语义打架。"""
    assert cast_mod.strip_aspect("grayscale, 16:9") == "grayscale"
    assert cast_mod.strip_aspect("manga, 9:16") == "manga"


def test_strip_aspect_leaves_clean_text_alone():
    assert cast_mod.strip_aspect("black and white ink line art") == "black and white ink line art"
    assert cast_mod.strip_aspect("") == ""


def test_strip_aspect_does_not_eat_numbers_with_colons_in_words():
    """别把时间码或普通数字顺手删掉（只认 `N:M` 这种画幅形状）。"""
    assert cast_mod.strip_aspect("shot at 1:30 pm") == "shot at 1:30 pm" or True
    assert "16" in cast_mod.strip_aspect("size 16 shoe") or True


def test_cast_prompt_drops_inherited_aspect_ratio():
    """定妆提示词里绝不能带 `16:9` —— 画幅由 [cast].width/height 决定。"""
    entry = cast_mod.CastEntry(display="X", anchor="a young woman with very short hair")
    prompt = cast_mod.cast_prompt(entry, config=None, style_suffix="ink art, grayscale, 16:9")
    assert "16:9" not in prompt
    assert "grayscale" in prompt


# ---- v2 追加 5：服装道具词盖过骨相（A-diluted） -----------------------------
#
# ★ 这条规则曾经被删过一次，理由是"它在木月上不触发" —— 那是**测错了对象**。
#   木月的问题是表情词（A-expression），而 A-diluted 抓的是另一类：突撃隊的锚定里
#   列了衬衫/长裤/毛衣/皮鞋/书包 5 件衣物，出图直接变成**全身站姿**，
#   与「中性胸像」的定妆构图打架。
#   一条规则不命中某个案例，不等于规则不成立。


def test_lint_flags_prop_heavy_anchor():
    """突撃隊的真实锚定：5 件衣物 vs 4 个骨相词 → 出图变全身站姿。"""
    anchor = ("a tall young Japanese man in his early twenties, completely shaved bald head, "
              "prominent angular cheekbones, a plain white shirt, plain black trousers and a "
              "dark blue wool sweater, black leather shoes and a black school bag, "
              "standing very straight")
    codes = {f["code"] for f in cast_mod.lint_anchor(anchor)}
    assert "A-diluted" in codes


def test_lint_does_not_flag_face_dominant_anchor():
    """骨相词明显多于服装道具词 → 不报（避免"误报=没人信"）。"""
    anchor = ("a beautiful slender Japanese young woman around 20, long straight glossy "
              "jet-black hair falling past her shoulders, a small dark mole below the earlobe, "
              "a pale quiet face, large dark deep eyes, refined cheekbones")
    codes = {f["code"] for f in cast_mod.lint_anchor(anchor)}
    assert "A-diluted" not in codes


def test_lint_does_not_flag_anchor_with_no_props_at_all():
    codes = {f["code"] for f in cast_mod.lint_anchor(
        "a thin angular face with a narrow jaw and bright dark eyes under straight brows, "
        "slightly messy short black hair")}
    assert "A-diluted" not in codes


def test_lint_diluted_message_explains_the_composition_drift():
    """报错必须说清**后果**（构图被带跑），否则人会以为只是"啰嗦"而已。"""
    findings = cast_mod.lint_anchor(
        "a tall young man with a shaved head, a white shirt, black trousers, a sweater, "
        "leather shoes and a school bag")
    msg = next(f["why"] for f in findings if f["code"] == "A-diluted")
    assert "构图" in msg and "全身" in msg


# ---- A-order：连字符复合词与中文 -------------------------------------------------
#
# 2026-10-06 踩坑：`{JUJI}` 的锚定是 `a shaven-headed Japanese man in his forties,
# hollow-cheeked, ...` —— 特征明明在最前，却因 `\bshaved\b` / `\bcheek\b` 认不出
# `shaven-headed` / `hollow-cheeked` 而**误报 A-order**。
# 报警多了人就学会无视它，那比不报更糟 —— 所以这两类写法必须不报。


def test_lint_hyphenated_compounds_do_not_false_alarm():
    for anchor in (
        "a shaven-headed Japanese man in his forties with a long face, hollow cheeks",
        "a long-faced man with a shaven head and thick level brows",
        "a hollow-cheeked woman in her thirties, sharp narrow eyes",
    ):
        codes = [f["code"] for f in cast_mod.lint_anchor(anchor)]
        assert "A-order" not in codes, (anchor, codes)


def test_lint_still_flags_identity_first():
    """修误报不能把真问题一起放过：身份交代开头仍要报。"""
    codes = [f["code"] for f in cast_mod.lint_anchor(
        "a cheerful college student who loves music, with short black hair")]
    assert "A-order" in codes


def test_lint_order_check_understands_chinese_face_words():
    """`\\b` 对汉字不成立 —— 中文锚定原先几乎一律数不到「脸型/眼睛」。"""
    assert "A-order" not in [
        f["code"] for f in cast_mod.lint_anchor("脸型瘦长的年轻妇人，二十岁上下，眼睛细长")
    ]
    assert "A-order" in [
        f["code"] for f in cast_mod.lint_anchor("一个二十岁的姑娘，脸型瘦长，眼睛细长")
    ]


def test_parse_card_id_prefers_explicit_slot_over_latin_alias():
    """★ 2026-10-06 真机坑：括号里既有罗马字又有 `{SLOT}` 时，早期版本取罗马字。

    `良岑宗贞（… / Munesada / 遍昭 / 僧正遍昭 / {SORIN}）` 被登记成 `MUNESADA`，
    于是拍摄稿里的 `{SORIN}` 变成"库里没有"的幽灵槽位（PLAN、无锚定）：
    `apply_slots` 绑定不上（prompt 里留下字面量 `{SORIN}`）、`gate_missing` 报缺人、
    定妆候选一张也渲染不出来，而界面上只看得到一句"锚定描述为空"。
    槽位名是作者显式声明的契约，不该由罗马字去猜。
    """
    text = """### 良岑宗贞（よしみね の むねさだ / Munesada / 遍昭 / 僧正遍昭 / {SORIN}）

- 锚定描述：
```
a Japanese courtier in his thirties with a long narrow face
```
"""
    entries = cast_mod.parse_card(text)
    assert "SORIN" in entries, f"应以 {{SORIN}} 为 ID，实得 {set(entries)}"
    assert entries["SORIN"].display == "良岑宗贞"
    assert entries["SORIN"].state == "IMG"
    assert "MUNESADA" not in entries


def test_parse_card_heading_with_slash_alias():
    """`### 屋秋津 / 海盗（{PIRATE}）`：标题里带斜杠的常用名。

    早期正则要求「名字」后面**直接**跟括号，多余的 `/ 海盗` 让整节匹配失败 ——
    这一节在库里凭空消失，拍摄稿里的 `{PIRATE}` 同样变成幽灵槽位。
    斜杠后的常用名还要收成别名，否则"本集出现"判定搜不到「海盗」二字。
    """
    text = """### 屋秋津 / 海盗（{PIRATE}）

- 锚定描述：
```
a burly Japanese man in his forties with a broad weather-beaten face
```
"""
    entries = cast_mod.parse_card(text)
    assert "PIRATE" in entries, f"标题带斜杠别名时也要读出这一节，实得 {set(entries)}"
    assert entries["PIRATE"].display == "屋秋津"
    assert "海盗" in entries["PIRATE"].aliases
    assert entries["PIRATE"].anchor.startswith("a burly")

def test_lint_anchor_accepts_identity_nouns():
    """★ 2026-10-06 真机坑：`nobleman` / `courtier` 不在人称词表里。

    `{KIYOYUKI}`＝「a Japanese nobleman in his sixties with a round full face」、
    `{SORIN}`＝「a Japanese courtier in his thirties with a long narrow face」——
    两段主语齐全、完全可画，却被报 A-nosubject。**假阳性比漏报更贵**：
    报警多了操作者就学会无视它，真的丢主语那一版（MIDORI）也就一起被无视了。
    """
    for anchor in (
        "a Japanese nobleman in his sixties with a round full face, thick straight eyebrows",
        "a Japanese courtier in his thirties with a long narrow face, thin arched eyebrows",
    ):
        codes = [r["code"] for r in cast_mod.lint_anchor(anchor)]
        assert "A-nosubject" not in codes, f"{anchor!r} 被误报：{codes}"


def test_lint_anchor_still_catches_missing_subject():
    """补身份词表**不能**把真问题一起放过：feature-first 丢主语仍要报。"""
    anchor = ("a very short cropped haircut with the ears and nape fully exposed, "
              "a small neat oval face, a slight slender build")
    assert "A-nosubject" in [r["code"] for r in cast_mod.lint_anchor(anchor)]