"""产物契约（`schemas/*.json` 四份）的守卫测试 —— 含**与 `handoff` 互为镜像**。

## 为什么要有这一组（提案 §3 B2 / P2 验收②）

契约外置成 JSON Schema 之后，"一份分镜表必须长什么样"有了**两个独立表达**：

| 表达 | 给谁 | 说什么 |
|---|---|---|
| `schemas/shots.schema.json` | 机器 / 外部工具 / CI | "这份产物合不合规" |
| `lvs/handoff.validate_shots` | 人 / 下游阶段 | "哪个上游产的、缺什么、重跑哪条命令" |

两个表达**各自都能被判据钉住**；而"它们会不会各自悄悄漂移"只有机器能保证。
本文件钉三件事：

1. 四份契约都登记了、本身是合法 JSON Schema（写坏了在**这里**红，不是等用的时候）；
2. **真实产物过得了** —— 拿 `.work/` 里现成的任务扫（契约不是写给假想数据的）；
3. **互为镜像** —— 人为破坏一个字段，schema 与 handoff **同时变红**；任一边单飞就红。

第 3 条是核心判据，刻意做成参数化的：**一条破坏 = 一个用例名**，
坏了的时候看用例名就知道是哪条判据漂了。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lvs import config as config_mod
from lvs import handoff, schemas

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".work"

#: 一份"最小但完整"的合法分镜（下面所有破坏都以它为基线）。
GOOD = {"id": 1, "source": "pexels", "narration": "他念了一夜的经。", "visual": "夜色中的寺院"}

REAL_CONFIGS = ("config.toml", "config.example.toml", "config.ugetsu.toml", "config.雨月物语.toml")


def _doc(shots: object) -> dict:
    """把（可能是坏掉的）分镜列表包成一份产物文档。"""
    return {"source_mode": "pexels", "shots": shots}


# ---- 1. 四份契约本身 ----------------------------------------------------------


def test_four_contracts_are_registered():
    assert schemas.available() == ("config", "parse", "qc", "shots")


@pytest.mark.parametrize("kind", ["shots", "parse", "config", "qc"])
def test_every_contract_is_a_valid_json_schema(kind):
    """契约自己写坏了要在**这里**红 —— 而不是等到校验产物时才炸。"""
    assert schemas.validator(kind) is not None      # 内部跑 Draft202012Validator.check_schema


def test_unknown_contract_name_says_what_is_known():
    with pytest.raises(schemas.SchemaError) as exc:
        schemas.schema_path("nope")
    assert "config" in str(exc.value) and "shots" in str(exc.value)


def test_schema_dir_can_be_redirected_and_missing_dir_is_explained(monkeypatch, tmp_path):
    """契约目录找不到时的报错必须**给出逃生阀**，不能是个 `FileNotFoundError`。"""
    monkeypatch.setenv(schemas.ENV_SCHEMA_DIR, str(tmp_path / "nope"))
    schemas.clear_cache()
    try:
        with pytest.raises(schemas.SchemaError) as exc:
            schemas.load("shots")
        assert schemas.ENV_SCHEMA_DIR in str(exc.value)
    finally:
        schemas.clear_cache()


# ---- 2. 真源真产物过得了（验收①）----------------------------------------------


@pytest.mark.parametrize("kind,name", [("shots", "shots.json"), ("parse", "parse.json")])
def test_real_work_artifacts_pass_the_contract(kind, name):
    """★ 拿 `.work/` 里**现成任务**的真实产物扫一遍（没有就 skip —— CI 上是正常的）。"""
    files = sorted(WORK.glob(f"*/{name}")) if WORK.is_dir() else []
    if not files:
        pytest.skip("没有 .work/ 真实产物可扫")
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert schemas.validate(kind, data) == [], f"{path} 过不了 {kind} 契约"


def test_real_configs_pass_the_contract():
    found = [ROOT / name for name in REAL_CONFIGS if (ROOT / name).is_file()]
    if not found:
        pytest.skip("仓库里没有配置文件可扫")
    for path in found:
        data = config_mod.Config.load(path).as_dict()
        assert schemas.validate("config", data) == [], f"{path} 过不了 config 契约"


def test_config_contract_knows_every_documented_section():
    """`known_keys` 是 `lvs config check` 的"未知键警告"的真源，段要认得全。"""
    table = schemas.known_keys("config")
    for section in ("app", "paths", "tts", "shots", "build", "bgm", "gpu", "comfyui",
                    "cast", "qc", "pipeline", "budget", "providers", "publish"):
        assert section in table, f"config 契约里没有 `{section}` 段"
    assert {"local", "remote"} <= set(table["providers"])
    # ★ `[styles.<名字>]` / `providers.routes` 是**动态键**（键名由用户定），
    # `known_keys` 刻意不收它们 —— 收了就会把合法的自由表报成"未知键"。
    assert "styles" in table[""], "styles 段本身要在契约里声明过（否则会被报成未知段）"
    assert "styles" not in table, "styles 的子键是用户定的，不该进已知键表"
    assert "providers.routes" not in table, "providers.routes 同理"


# ---- 3. ★ 互为镜像（验收②）---------------------------------------------------


def test_source_enum_is_the_same_set_as_handoff():
    enum = schemas.load("shots")["$defs"]["shot"]["properties"]["source"]["enum"]
    assert set(enum) == set(handoff.VALID_SOURCES), (
        "schema 的 source 枚举与 handoff.VALID_SOURCES 漂移了 —— "
        "两边必须同时改（handoff 那边还有 test_architecture 对着真源 `sources.MODES` 守）"
    )


def test_schema_requires_at_least_what_handoff_requires():
    required = set(schemas.load("shots")["$defs"]["shot"]["required"])
    assert set(handoff.REQUIRED_FIELDS) <= required, (
        f"handoff 要求 {sorted(handoff.REQUIRED_FIELDS)}，schema 只要求 {sorted(required)}"
    )


#: 人为破坏 → **两边都必须报**。（判据：`schema` 与 `handoff` 同时变红。）
BREAKAGES: list[tuple[str, object]] = [
    ("source 不在合法集合里", [dict(GOOD, source="bogus")]),
    ("narration 是空白串", [dict(GOOD, narration="   ")]),
    ("visual 是空串", [dict(GOOD, visual="")]),
    ("缺 id", [{k: v for k, v in GOOD.items() if k != "id"}]),
    ("分镜表不是列表", "不是列表"),
    ("某一项不是对象", ["不是对象"]),
]


@pytest.mark.parametrize("label,shots", BREAKAGES, ids=[c[0] for c in BREAKAGES])
def test_both_sides_go_red_on_the_same_breakage(label, shots):
    """★ 验收②：**人为破坏一个字段 → schema 与 handoff 两边同时变红**。

    任一边单飞（一边红一边绿）就说明两份契约已经漂了 —— 那正是"契约外置"
    最容易退化成的样子：两处各说各的，谁也拦不住谁。
    """
    schema_problems = schemas.validate("shots", _doc(shots))
    handoff_problems = handoff.validate_shots(shots)
    assert schema_problems, f"[{label}] schema 没红：{schema_problems}"
    assert handoff_problems, f"[{label}] handoff 没红：{handoff_problems}"


def test_handoff_catches_what_json_schema_cannot_express():
    """重复 `id`：handoff 抓得到，JSON Schema **表达不了**"按某个键唯一"。

    这里记录的是**故意的能力差**，不是缺陷 —— 也正因如此两份契约都要留：
    schema 负责"给外部工具一个能读的判据"，handoff 负责"更强的判据 + 人话 + 该重跑哪条命令"。
    """
    dupe = [dict(GOOD), dict(GOOD)]
    assert handoff.validate_shots(dupe), "handoff 该抓到重复 id"
    assert schemas.validate("shots", _doc(dupe)) == [], "schema 表达不了唯一性（已知边界）"


def test_good_shot_passes_both_sides():
    """反向也要钉住：合法的表两边都放行（防"为了变红而变红"的假判据）。"""
    assert schemas.validate("shots", _doc([GOOD])) == []
    assert handoff.validate_shots([GOOD]) == []
