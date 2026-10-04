"""第十一轮审查修掉的 4 个真 bug —— 每个都来自"真机走一遍"。

## 这 4 个 bug 的共同点

都不是单元测试能发现的（它们要求"整条链按用户的真实配置跑一遍"）：

1. `Config.load("config.toml")` 传字符串 → `AttributeError: 'str' object has no attribute 'is_file'`
2. 本机服务调用被系统代理拦下 → 明明开着 ComfyUI 却报"不可达"（见 `test_net.py`）
3. 全新项目第一次 `lvs run` 卡在 G0，**`parse` 根本没跑**，却让你"看产物"（列表是空的）
4. `source_mode=auto` + `pexels.api_key` 为空 → `lvs assets` **整阶段硬失败**，
   本该出图的镜一张都不出

修完之后每一条都补了回归，免得以后再退回去。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lvs import doctor
from lvs.config import Config, ConfigError


# ---- 1. Config.load 接受字符串路径 -------------------------------------------


def test_config_load_accepts_a_string_path(tmp_path: Path):
    """★ 调用方几乎都会顺手写字符串 —— 原先会崩成一句人话都没有的 AttributeError。"""
    cfg = tmp_path / "config.toml"
    cfg.write_text('[tts]\nbackend = "edge"\n', encoding="utf-8")

    loaded = Config.load(str(cfg))          # 字符串，不是 Path
    assert loaded.get("tts.backend") == "edge"
    assert loaded.path == cfg


def test_config_load_string_path_missing_file_gives_human_message(tmp_path: Path):
    """字符串路径指向不存在的文件时，要报**人话**（不是 AttributeError）。"""
    with pytest.raises(ConfigError) as exc:
        Config.load(str(tmp_path / "没有这个文件.toml"))
    assert "未找到配置文件" in str(exc.value)


def test_config_load_still_accepts_path(tmp_path: Path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[tts]\nbackend = "edge"\n', encoding="utf-8")
    assert Config.load(cfg).get("tts.backend") == "edge"


# ---- 3. `run` 必须先跑 parse，再过 G0 ---------------------------------------


def test_next_stage_never_reports_parse(tmp_path: Path):
    """`_next_stage` 只回答"第一道会被门禁拦住的阶段" —— parse 无门禁，不算。"""
    from lvs import run as run_mod
    from lvs.workspace import Workspace

    ws = Workspace(task="t", root=tmp_path).ensure()   # 真 workspace（空任务）
    got = run_mod._next_stage(ws, type("A", (), {"force": False})())
    assert got != "parse", f"`_next_stage` 不该把 parse 当成门禁阶段，实际：{got}"


def test_run_runs_parse_before_checking_gates(tmp_path: Path, monkeypatch):
    """★★ 核心回归：`run_command` 必须在**门禁之前**把 parse 跑掉。

    否则 G0 让你"看产物"，而 `parse.json` 压根不存在 —— 用户会以为工具坏了。
    """
    import inspect

    from lvs import run as run_mod

    src = inspect.getsource(run_mod.run_command)
    # 顺序断言：_ensure_parse 必须出现在 _gate_hold 之前
    i_parse = src.find("_ensure_parse")
    i_gate = src.find("_gate_hold")
    assert i_parse != -1 and i_gate != -1, "两个调用点都必须还在"
    assert i_parse < i_gate, (
        "`_ensure_parse` 必须在 `_gate_hold` 之前 —— "
        "否则全新项目第一次 `lvs run` 会卡在 G0 且没有可审的产物"
    )


def test_block_message_never_leaves_the_artifact_list_blank(tmp_path: Path):
    """★ 产物列表为空时必须给一句解释（不能留白）。"""
    from lvs import pipeline as pl
    from lvs.workspace import Workspace

    ws = Workspace(task="t", root=tmp_path).ensure()
    st = pl.evaluate(ws, pl.BY_ID["G0"])
    msg = pl.block_message(ws, st)

    assert "看产物：" in msg
    # "看产物：" 之后必须有非空内容（要么是文件、要么是那句解释）
    tail = msg.split("看产物：", 1)[1].split("下一步", 1)[0].strip()
    assert tail, "`看产物：` 后面不该是空的"


# ---- 4. assets：Pexels key 缺失要**逐镜隔离**，不是硬拦 -----------------------


class _ShotWs:
    """最小 workspace：assets 的逐镜循环只用到这几个方法。"""

    def __init__(self, root: Path, shots: list[dict]):
        self.task = "t"
        self.root = root
        self._shots = shots
        self.manifest = {"stages": {}}

    def path(self, *parts):
        p = Path(self.root).joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def load_shots(self, error=RuntimeError):  # noqa: ANN001
        return {"shots": self._shots, "count": len(self._shots)}

    def try_load_shots(self):
        return {"shots": self._shots}

    def write_shots(self, data):  # noqa: ANN001
        self._shots = data.get("shots", self._shots)

    def mark_stage_progress(self, *a, **k):  # noqa: ANN002, ANN003
        pass

    def mark_stage(self, *a, **k):  # noqa: ANN002, ANN003
        pass

    def ensure(self):
        return self


def test_pexels_missing_key_does_not_abort_the_whole_stage(tmp_path: Path, monkeypatch, capsys):
    """★★ 核心回归：Pexels key 为空时，**其余来源的镜要照常出图**。

    实测踩过：`source_mode=auto` + 空 key，13 镜里 8 镜本该出图，
    却因为 5 镜要 Pexels 而整批失败（退出码 2），用户不知道自己其实在跑本地生图。
    """
    from lvs import assets as assets_mod
    from lvs.config import Config

    root = tmp_path / ".work" / "t"
    root.mkdir(parents=True)
    shots = [
        {"id": 1, "source": "local", "narration": "a", "visual": "草地"},
        {"id": 2, "source": "local", "narration": "b", "visual": "桌子"},
        {"id": 3, "source": "pexels", "narration": "c", "visual": "城市"},
    ]
    ws = _ShotWs(root, shots)
    cfg = Config({
        "pexels": {"api_key": ""},
        "comfyui": {"backend": "placeholder", "width": 320, "height": 192},
        "library": {"dirs": []},
    }, None)

    args = type("A", (), {
        "force": False, "only": None, "no_library": True,
        "source": None, "no_cast_gate": True, "task": "t",
    })()

    code = assets_mod.run_command(cfg, ws, args)
    out = capsys.readouterr().out

    assert code == 1, f"该是「有失败件」（1），不是硬拦（2）：{code}"
    assert "Pexels" in out
    # 两个 local 镜必须真的做出了东西
    made = list(Path(root).glob("assets/local/*.png"))
    assert len(made) == 2, f"本该出 2 张图，实际 {len(made)} 张"


def test_pexels_message_gives_actionable_options(tmp_path: Path, capsys):
    """报错要给**出路**（填 key / 改 source_mode / 单改 shots.json）。"""
    from lvs import assets as assets_mod
    from lvs.config import Config

    root = tmp_path / ".work" / "t"
    root.mkdir(parents=True)
    ws = _ShotWs(root, [{"id": 1, "source": "pexels", "narration": "a", "visual": "城市"}])
    cfg = Config({
        "pexels": {"api_key": ""},
        "comfyui": {"backend": "placeholder"},
        "library": {"dirs": []},
    }, None)
    args = type("A", (), {
        "force": False, "only": None, "no_library": True,
        "source": None, "no_cast_gate": True, "task": "t",
    })()

    assets_mod.run_command(cfg, ws, args)
    out = capsys.readouterr().out
    assert "[pexels] api_key" in out
    assert "source local" in out or "source_mode" in out
    assert "shots.json" in out


# ---- 5. doctor：配置自相矛盾要主动报警 --------------------------------------


@pytest.mark.parametrize("mode,key,dirs,expect", [
    ("auto", "", [], "警告"),                 # auto 但没 pexels key
    ("pexels", "", [], "缺失"),               # 全靠 pexels 但没 key → 全失败
    ("library", "", [], "缺失"),              # 用库但库为空
    ("local", "", [], "通过"),                # 本地生图，不需要 key
    ("auto", "some-key", [], "通过"),         # 有 key，auto 没问题
])
def test_source_mode_check_catches_contradictions(mode, key, dirs, expect):
    """★ 在**花钱之前**发现"策略与钥匙不匹配"。"""
    cfg = Config({
        "shots": {"source_mode": mode},
        "pexels": {"api_key": key},
        "library": {"dirs": dirs},
    }, None)
    assert doctor.check_source_mode(cfg).status == expect


def test_source_mode_check_is_registered():
    """★ 新检查必须真的接进体检清单 —— 防"定义了却从不接线"。"""
    cfg = Config({}, None)
    names = [c.name for c in doctor.all_checks(cfg)]
    assert "素材来源策略" in names, f"没接进体检：{names}"
