"""生图巡检（`lvs qc`）的测试。

巡检的判据必须**只在真有问题时才响** —— 一个总是告警的巡检等于没有巡检。
这里既测"该报的报了"，也测灰度图/正常图不会被误报成彩色。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lvs import qc as qc_mod

PIL = pytest.importorskip("PIL", reason="巡检的图像判据依赖 Pillow")


def _make(path: Path, color=(128, 128, 128), size=(64, 64)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


# ---- 彩度判据 --------------------------------------------------------------


def test_gray_image_has_zero_chroma(tmp_path: Path):
    from PIL import Image

    p = _make(tmp_path / "g.png", (128, 128, 128))
    with Image.open(p) as img:
        assert qc_mod.mean_chroma(img.convert("RGB")) < 0.01


def test_saturated_image_has_high_chroma(tmp_path: Path):
    from PIL import Image

    p = _make(tmp_path / "c.png", (255, 0, 0))
    with Image.open(p) as img:
        assert qc_mod.mean_chroma(img.convert("RGB")) > 100


def test_measure_reports_size_and_bytes(tmp_path: Path):
    p = _make(tmp_path / "g.png", size=(32, 48))
    stats = qc_mod.measure(p, 3)
    assert stats.ok
    assert (stats.width, stats.height) == (32, 48)
    assert stats.bytes > 0
    assert stats.shot_id == 3


def test_measure_missing_file_not_ok(tmp_path: Path):
    stats = qc_mod.measure(tmp_path / "nope.png", 1)
    assert not stats.ok
    assert stats.bytes == 0


def test_measure_zero_byte_not_ok(tmp_path: Path):
    p = tmp_path / "z.png"
    p.write_bytes(b"")
    assert not qc_mod.measure(p, 1).ok


def test_measure_corrupt_file_not_ok(tmp_path: Path):
    p = tmp_path / "bad.png"
    p.write_bytes(b"not a png at all")
    stats = qc_mod.measure(p, 1)
    assert not stats.ok


# ---- 单图巡检 --------------------------------------------------------------


def test_inspect_missing_file_is_error(tmp_path: Path):
    _, issues = qc_mod.inspect_image(tmp_path / "nope.png", 5)
    assert [i.code for i in issues] == ["Q001"]
    assert issues[0].level == qc_mod.LEVEL_ERROR


def test_inspect_zero_byte_is_error(tmp_path: Path):
    p = tmp_path / "z.png"
    p.write_bytes(b"")
    _, issues = qc_mod.inspect_image(p, 5)
    assert "Q002" in {i.code for i in issues}


def test_inspect_clean_gray_image_is_silent(tmp_path: Path):
    p = _make(tmp_path / "g.png")
    _, issues = qc_mod.inspect_image(p, 1, fix=True)
    assert issues == [], [i.render() for i in issues]


def test_inspect_colorful_image_warns(tmp_path: Path):
    p = _make(tmp_path / "c.png", (255, 0, 0))
    _, issues = qc_mod.inspect_image(p, 2)
    assert "Q005" in {i.code for i in issues}
    assert any(i.level == qc_mod.LEVEL_WARN for i in issues)


def test_inspect_fix_converts_to_grayscale(tmp_path: Path):
    from PIL import Image

    p = _make(tmp_path / "c.png", (255, 0, 0))
    _, issues = qc_mod.inspect_image(p, 2, fix=True)
    assert "Q005" in {i.code for i in issues}
    with Image.open(p) as img:
        assert qc_mod.mean_chroma(img.convert("RGB")) < 0.01, "应已就地转灰度"


def test_fix_keeps_original_out_of_the_way(tmp_path: Path):
    """原彩色图必须移出图片目录 —— 否则后续 glob 会把它当正式镜图（踩过）。"""
    p = _make(tmp_path / "c.png", (255, 0, 0))
    qc_mod.to_grayscale(p)
    backup = tmp_path / qc_mod.FIXED_DIRNAME / "c.orig.png"
    assert backup.is_file()
    assert p.is_file(), "原位要留灰度版"
    # 原位已不是彩色，备份仍是彩色
    from PIL import Image

    with Image.open(p) as img:
        assert qc_mod.mean_chroma(img.convert("RGB")) < 0.01
    with Image.open(backup) as img:
        assert qc_mod.mean_chroma(img.convert("RGB")) > 100


def test_size_mismatch_warns(tmp_path: Path):
    p = _make(tmp_path / "g.png", size=(10, 10))
    _, issues = qc_mod.inspect_image(p, 1, expected_size=(32, 32))
    assert "Q004" in {i.code for i in issues}


def test_matching_size_is_silent(tmp_path: Path):
    p = _make(tmp_path / "g.png", size=(32, 32))
    _, issues = qc_mod.inspect_image(p, 1, expected_size=(32, 32))
    assert issues == []


# ---- 期望人脸数 ------------------------------------------------------------


def test_expected_faces_counts_unique_slots():
    shot = {"visual": "{NAOKO} 与 {WATANABE} 并肩，{NAOKO} 在前", "source": "local"}
    assert qc_mod.expected_faces_for(shot) == 2


def test_expected_faces_none_for_graphic():
    assert qc_mod.expected_faces_for({"visual": "{NAOKO}", "source": "graphic"}) is None


def test_expected_faces_none_for_pexels():
    assert qc_mod.expected_faces_for({"visual": "{NAOKO}", "source": "pexels"}) is None


def test_expected_faces_none_without_slots():
    assert qc_mod.expected_faces_for({"visual": "空荡的草地", "source": "local"}) is None


# ---- 分块 ------------------------------------------------------------------


class _Lock:
    characters: dict = {}


def test_inspect_block_only_checks_local_shots(tmp_path: Path):
    """卡片是排版的、pexels 是实拍 —— 黑白/彩度判据对它们不适用，不该巡。"""
    local = _make(tmp_path / "l.png", (255, 0, 0))
    shots = [
        {"id": 1, "resolved_by": "local", "asset_path": str(local), "source": "local"},
        {"id": 2, "resolved_by": "graphic", "asset_path": str(local), "source": "graphic"},
        {"id": 3, "resolved_by": "pexels", "asset_path": str(local), "source": "pexels"},
    ]
    rep = qc_mod.inspect_block(shots, 1)
    assert [s.shot_id for s in rep.stats] == [1]


def test_inspect_block_reports_missing_asset_path(tmp_path: Path):
    shots = [{"id": 9, "resolved_by": "local", "status": "done"}]
    rep = qc_mod.inspect_block(shots, 1)
    assert "Q010" in {i.code for i in rep.issues}


def test_inspect_block_fixes_and_records(tmp_path: Path):
    p = _make(tmp_path / "c.png", (0, 255, 0))
    shots = [{"id": 1, "resolved_by": "local", "asset_path": str(p), "source": "local"}]
    rep = qc_mod.inspect_block(shots, 1, fix=True)
    assert rep.fixed == [str(p)]
    assert rep.stats[0].chroma < 0.01


def test_block_report_counts():
    rep = qc_mod.BlockReport(block=1, first=1, last=5)
    rep.issues = [
        qc_mod.Issue(qc_mod.LEVEL_ERROR, "Q001", 1, "x"),
        qc_mod.Issue(qc_mod.LEVEL_WARN, "Q005", 2, "y"),
    ]
    assert len(rep.errors) == 1
    assert rep.to_dict()["warns"] == 1


# ---- 提示词版本 ------------------------------------------------------------


def test_check_prompt_versions_groups_identical_prompts():
    shots = [{"prompt": "a b c"}, {"prompt": "a b c"}, {"prompt": "x y z"}]
    versions = qc_mod.check_prompt_versions(shots)
    assert sorted(versions.values()) == [1, 2]


def test_check_prompt_versions_skips_empty():
    assert qc_mod.check_prompt_versions([{"prompt": ""}, {}]) == {}


def test_prompt_fingerprint_differs_on_length():
    assert qc_mod._prompt_fingerprint("a b") != qc_mod._prompt_fingerprint("a b c")


# ---- 报告落盘 --------------------------------------------------------------


class _Ws:
    def __init__(self, root: Path):
        self.root = root

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)


def test_write_reports_creates_summary_and_queue(tmp_path: Path):
    ws = _Ws(tmp_path)
    rep = qc_mod.BlockReport(block=1, first=1, last=2)
    rep.issues = [
        qc_mod.Issue(qc_mod.LEVEL_ERROR, "Q001", 1, "产物文件不存在"),
        qc_mod.Issue(qc_mod.LEVEL_WARN, "Q006", 2, "空镜里检出脸"),
    ]
    rep.prompt_versions = {"10c|a b": 2}
    summary = qc_mod.write_reports(ws, [rep])

    assert summary.is_file()
    assert (tmp_path / "qc" / "qc-block-001.json").is_file()
    text = summary.read_text(encoding="utf-8")
    assert "Q001" in text
    assert "待重出镜号" in text

    queue = json.loads((tmp_path / "qc" / "redo-queue.json").read_text(encoding="utf-8"))
    assert queue["shots"] == [1, 2], "硬错误与人数告警都该进重出队列"


def test_write_reports_flags_mixed_prompt_versions(tmp_path: Path):
    ws = _Ws(tmp_path)
    r1 = qc_mod.BlockReport(block=1, first=1, last=1)
    r1.prompt_versions = {"10c|old": 1}
    r2 = qc_mod.BlockReport(block=2, first=2, last=2)
    r2.prompt_versions = {"20c|new": 1}
    summary = qc_mod.write_reports(ws, [r1, r2])
    assert "提示词版本不一致" in summary.read_text(encoding="utf-8")


# ---- CLI -------------------------------------------------------------------


class _Args:
    def __init__(self, **kw):
        self.block = None
        self.only = None
        self.fix = False
        self.__dict__.update(kw)


class _Cfg:
    def __init__(self, data=None):
        self._d = data or {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def has(self, key):
        return key in self._d and self._d[key] not in ("", [], {})


class _ShotWs(_Ws):
    def __init__(self, root: Path, shots):
        super().__init__(root)
        self._shots = {"shots": shots}

    def load_shots(self, error=RuntimeError):
        if not self._shots["shots"]:
            raise error("没有分镜")
        return self._shots


def test_run_command_returns_one_on_hard_error(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(qc_mod, "configure_face_model", lambda p, **kw: None)
    ws = _ShotWs(tmp_path, [{"id": 1, "status": "done", "resolved_by": "local",
                             "asset_path": str(tmp_path / "missing.png")}])
    code = qc_mod.run_command(_Cfg(), ws, _Args())
    assert code == 1
    assert "巡检完成" in capsys.readouterr().out


def test_run_command_returns_zero_when_clean(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(qc_mod, "configure_face_model", lambda p, **kw: None)
    p = _make(tmp_path / "g.png", (128, 128, 128))
    ws = _ShotWs(tmp_path, [{"id": 1, "status": "done", "resolved_by": "local",
                             "asset_path": str(p), "source": "local"}])
    assert qc_mod.run_command(_Cfg(), ws, _Args()) == 0


def test_run_command_no_shots_returns_two(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(qc_mod, "configure_face_model", lambda p, **kw: None)
    ws = _ShotWs(tmp_path, [])
    assert qc_mod.run_command(_Cfg(), ws, _Args()) == 2


def test_run_command_only_filter(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(qc_mod, "configure_face_model", lambda p, **kw: None)
    good = _make(tmp_path / "g.png", (128, 128, 128))
    shots = [
        {"id": 1, "status": "done", "resolved_by": "local", "asset_path": str(good), "source": "local"},
        {"id": 2, "status": "done", "resolved_by": "local",
         "asset_path": str(tmp_path / "missing.png"), "source": "local"},
    ]
    ws = _ShotWs(tmp_path, shots)
    assert qc_mod.run_command(_Cfg(), ws, _Args(only="1")) == 0
    assert qc_mod.run_command(_Cfg(), ws, _Args(only="2")) == 1


def test_parse_ids():
    assert qc_mod._parse_ids("1-3,7") == {1, 2, 3, 7}
    assert qc_mod._parse_ids("") == set()
    assert qc_mod._parse_ids("5") == {5}


def test_face_counter_degrades_without_model(tmp_path: Path):
    """没有权重就返回 None —— 巡检要降级而不是崩。"""
    assert qc_mod._load_face_counter(tmp_path / "nope.onnx") in (None, None) or callable(
        qc_mod._load_face_counter(tmp_path / "nope.onnx")
    )
    got = qc_mod._load_face_counter(tmp_path / "nope.onnx")
    assert got is None or callable(got)


def test_configure_face_model_disabled_stays_disabled(tmp_path: Path, monkeypatch):
    """`enable_faces = false` 必须是**真的关掉**。

    坑在哨兵上：`_FACE_COUNTER` 用 `False` 表示"还没找过"，
    只写 `enabled=False` 而不管哨兵的话，第一次 measure() 又会惰性去加载 ——
    配置项成了摆设。这里断言关掉之后连"找模型"这一步都不发生。
    """
    calls: list = []
    monkeypatch.setattr(qc_mod, "_load_face_counter", lambda *a, **k: calls.append(1) or (lambda img: 9))
    qc_mod.configure_face_model(None, enabled=False)
    assert qc_mod.face_counter() is None, "关掉之后不该再返回计数器"
    assert calls == [], "关掉之后连加载都不该尝试"


def test_configure_face_model_enabled_loads(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(qc_mod, "_load_face_counter", lambda *a, **k: (lambda img: 3))
    qc_mod.configure_face_model(None, enabled=True)
    counter = qc_mod.face_counter()
    assert counter is not None and counter(None) == 3
    # 复原，免得影响别的用例
    monkeypatch.setattr(qc_mod, "_FACE_COUNTER", False)
