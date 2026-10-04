"""产物指纹与血缘（`lvs/artifact.py`）的测试。

这里专门钉住一个真实踩过的坑：**目录型产物**。

流水线里 `voice` 的上游是 `assets` 目录、`build` 的上游是 `audio` 目录，
而 `refs()` 第一版只收文件（`if p.is_file()`）——目录被静默跳过，
于是"改了 assets 里的图，voice 仍判已完成"，拿旧图继续配音合成，全程不报错。
"""

from __future__ import annotations

import time
from pathlib import Path

from lvs import artifact
from lvs import stage as stage_mod
from lvs.workspace import Workspace


def _ws(tmp_path: Path) -> Workspace:
    ws = Workspace(task="t", root=tmp_path).ensure()
    assets = ws.path("assets")
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "shot-001.png").write_text("x", encoding="utf-8")
    (ws.path("audio")).mkdir(parents=True, exist_ok=True)
    (ws.path("subtitle.srt")).write_text("s", encoding="utf-8")
    return ws


def test_refs_keeps_directories(tmp_path: Path):
    ws = _ws(tmp_path)
    ups = stage_mod.upstream_artifacts(stage_mod.get("voice"), ws)
    refs = artifact.refs(ups)
    assert any(r.is_dir for r in refs), "assets 目录必须被记进血缘"
    assert any(r.path.endswith("assets") for r in refs)


def test_directory_ref_signature_is_recursive(tmp_path: Path):
    ws = _ws(tmp_path)
    ups = stage_mod.upstream_artifacts(stage_mod.get("voice"), ws)
    (ref,) = [r for r in artifact.refs(ups) if r.is_dir]
    # 目录签名 = 递归指纹，不是单个 stat
    assert ref.sig == artifact.fingerprint([Path(ref.path)])
    assert ref.sig != "", "非空目录的指纹不该是空串"


def test_directory_lineage_invalidates_on_content_change(tmp_path: Path):
    """★ 改了 assets 里的图，voice 必须判失效（这是本文件存在的原因）。"""
    ws = _ws(tmp_path)
    voice = stage_mod.get("voice")
    ws.mark_stage("voice", outputs=[ws.path("audio"), ws.path("subtitle.srt")])
    ws.record_lineage("voice", stage_mod.upstream_artifacts(voice, ws))
    assert ws.is_stage_done("voice")

    time.sleep(0.01)  # 让 mtime_ns 变化
    (ws.path("assets") / "shot-001.png").write_text("CHANGED", encoding="utf-8")

    assert not ws.is_stage_done("voice")
    assert "assets" in ws.stale_reason("voice")


def test_directory_ref_unchanged_detects_modification(tmp_path: Path):
    ws = _ws(tmp_path)
    ups = stage_mod.upstream_artifacts(stage_mod.get("voice"), ws)
    (ref,) = [r for r in artifact.refs(ups) if r.is_dir]
    assert ref.unchanged()

    time.sleep(0.01)
    (ws.path("assets") / "shot-001.png").write_text("CHANGED", encoding="utf-8")
    assert not ref.unchanged()


def test_file_ref_still_uses_stat_signature(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    ref = artifact.ArtifactRef.of(f)
    assert not ref.is_dir
    assert ref.unchanged()

    time.sleep(0.01)
    f.write_text("changed", encoding="utf-8")
    assert not ref.unchanged()


def test_roundtrip_preserves_is_dir(tmp_path: Path):
    ws = _ws(tmp_path)
    ups = stage_mod.upstream_artifacts(stage_mod.get("voice"), ws)
    for ref in artifact.refs(ups):
        back = artifact.ArtifactRef.from_dict(ref.to_dict())
        assert back.is_dir == ref.is_dir
        assert back.sig == ref.sig
        assert back.path == ref.path


def test_empty_directory_signature_is_stable(tmp_path: Path):
    """空目录的指纹是空串，但 `unchanged` 仍该成立（不因"空"误判为变了）。"""
    d = tmp_path / "empty"
    d.mkdir()
    ref = artifact.ArtifactRef.of(d)
    assert ref.is_dir
    assert ref.sig == ""
    assert ref.unchanged()


# ---- manifest 的 schema 版本 -------------------------------------------------
#
# 为什么现在补版本号：改结构而没有版本，只能靠"字段不存在就兜默认值"兼容 ——
# 这在**字段语义变了**（而不是新增）时会静默出错：老数据被按新含义解读，
# 不报错、只是判错。有了版本号，将来能明确说"这是老格式，要迁移"。


def test_new_manifest_records_the_version(tmp_path: Path):
    import json

    from lvs.workspace import MANIFEST_VERSION

    ws = Workspace(task="t", root=tmp_path).ensure()
    data = json.loads(ws.manifest_path.read_text(encoding="utf-8"))
    assert data.get("version") == MANIFEST_VERSION
    assert ws.manifest_version() == MANIFEST_VERSION


def test_legacy_manifest_without_version_reads_as_1(tmp_path: Path):
    """★ 老清单（无 `version`）必须照常工作，且按版本 1 计 —— 向后兼容。"""
    import json

    root = tmp_path
    d = root / ".work" / "t"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(
        json.dumps({"task": "t", "created_at": "x", "stages": {}}), encoding="utf-8"
    )
    ws = Workspace(task="t", root=root)
    ws.manifest = ws._read_manifest()
    assert ws.manifest_version() == 1
    assert ws.manifest.get("stages") == {}


def test_future_manifest_version_warns_but_does_not_crash(tmp_path: Path, capsys):
    """★ 未来版本：明确提示，但**不崩** —— 静默继续比报错更危险。"""
    import json

    d = tmp_path / ".work" / "t"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(
        json.dumps({"version": 99, "task": "t", "stages": {}}), encoding="utf-8"
    )
    ws = Workspace(task="t", root=tmp_path)
    ws.manifest = ws._read_manifest()
    assert ws.manifest_version() == 99
    assert "99" in capsys.readouterr().out


def test_corrupt_manifest_is_rebuilt_with_version(tmp_path: Path):
    import json

    from lvs.workspace import MANIFEST_VERSION

    d = tmp_path / ".work" / "t"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text("{ 坏 json", encoding="utf-8")
    ws = Workspace(task="t", root=tmp_path)
    data = ws._read_manifest()
    assert data["version"] == MANIFEST_VERSION
    assert data["stages"] == {}
    assert json.dumps(data)  # 可序列化
