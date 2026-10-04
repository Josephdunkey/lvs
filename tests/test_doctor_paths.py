"""多项目体检项（`doctor.check_paths_lib`）的测试。

这里钉住一个**静默失效**：`[paths].lib` 配了但路径不存在时，`cast_dir` 会一路
派生到 `<lib>/_cast`，而那个目录不存在时定妆闸门读不到锁、锚定不注入，**不报错**。
体检项要把这种情况显式报成"警告"，而不是只查"配没配"。
"""

from __future__ import annotations

from pathlib import Path

from lvs import doctor as doctor_mod


class _Cfg:
    """最小 Config 替身：只实现 doctor 用到的 `get`。"""

    def __init__(self, lib: str | None, cast_dir: str = ""):
        self._lib = lib
        self._cast_dir = cast_dir

    def get(self, key, default=None):
        if key == "paths.lib":
            return self._lib
        if key == "cast.dir":
            return self._cast_dir
        if key == "cast.card":
            return ""
        return default


def test_unconfigured_is_warn(tmp_path: Path):
    r = doctor_mod.check_paths_lib(_Cfg(None))
    assert r.status == doctor_mod.WARN
    assert "未配置" in r.detail


def test_configured_and_existing_is_pass(tmp_path: Path):
    from lvs import cast as cast_mod

    lib = tmp_path / "素材库"
    (lib / "00-设定").mkdir(parents=True)
    (lib / "00-设定" / "00-人物定妆卡.md").write_text("# 定妆卡\n", encoding="utf-8")
    (lib / "_cast").mkdir()
    (lib / "_cast" / "lock.json").write_text(
        '{"characters": {}}', encoding="utf-8"
    )

    cfg = _Cfg(str(lib))
    assert cast_mod.cast_dir(cfg).is_dir()
    assert cast_mod.find_card(cfg) is not None
    r = doctor_mod.check_paths_lib(cfg)
    assert r.status == doctor_mod.PASS, r.detail


def test_configured_but_missing_dir_is_warn(tmp_path: Path):
    r = doctor_mod.check_paths_lib(_Cfg(str(tmp_path / "不存在")))
    assert r.status == doctor_mod.WARN
    assert "路径不存在" in r.detail


def test_configured_but_no_cast_card_is_warn(tmp_path: Path):
    lib = tmp_path / "素材库"
    lib.mkdir()
    r = doctor_mod.check_paths_lib(_Cfg(str(lib)))
    assert r.status == doctor_mod.WARN
    assert "定妆卡" in r.detail
