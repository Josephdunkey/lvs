"""产物指纹与血缘（`lvs/artifact.py`）的测试。

这里专门钉住一个真实踩过的坑：**目录型产物**。

流水线里 `voice` 的上游是 `assets` 目录、`build` 的上游是 `audio` 目录，
而 `refs()` 第一版只收文件（`if p.is_file()`）——目录被静默跳过，
于是"改了 assets 里的图，voice 仍判已完成"，拿旧图继续配音合成，全程不报错。
"""

from __future__ import annotations

import time

import pytest
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
# ---- 原子写底座（atomic_write_text / atomic_write_bytes）--------------------
#
# ★ 变异验证：把实现换回 `path.write_text(...)`（先截断、再写）时，
#   `test_atomic_write_never_opens_target_for_writing` 必须**变红** ——
#   它保证这组测试真的在测"原子"，而不是在测"能写文件"。


def test_atomic_write_text_keeps_old_content_when_replace_fails(tmp_path, monkeypatch):
    """写盘途中被打断（用 os.replace 抛错模拟）→ 目标文件**保持原内容**。"""
    target = tmp_path / "shots.json"
    target.write_text('{"old": 1}', encoding="utf-8")

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    try:
        artifact.atomic_write_text(target, '{"new": 2}')
    except OSError:
        pass      # 吞不吞由调用方决定；这里只关心盘上留下了什么
    assert target.read_text(encoding="utf-8") == '{"old": 1}', "旧内容被破坏了"
    assert not list(tmp_path.glob("*.tmp")), "半成品临时文件没清掉"


def test_atomic_write_never_opens_target_for_writing(tmp_path, monkeypatch):
    """★ 变异守卫：目标文件**只能**经 `os.replace` 出现，绝不直接打开来写。"""
    import builtins

    target = tmp_path / "a.json"
    target.write_text("orig", encoding="utf-8")
    opened: list[str] = []
    real_open = builtins.open

    def spy(file, mode="r", *a, **k):  # noqa: ANN001, ANN002, ANN003, ANN202
        opened.append(f"{file}|{mode}")
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", spy)
    artifact.atomic_write_text(target, "new")

    writes = [o for o in opened if "w" in o.split("|")[1]]
    assert writes and writes[0].startswith(str(target) + ".tmp"), f"直接写了目标文件：{writes}"
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_leaves_no_temp_and_matches_write_text_line_endings(tmp_path):
    """行尾必须与 `Path.write_text` 一致（否则产物指纹漂移 → 下游误判「上游改过」）。"""
    plain = tmp_path / "plain.txt"
    atom = tmp_path / "atom.txt"
    plain.write_text("x\ny\n", encoding="utf-8")
    artifact.atomic_write_text(atom, "x\ny\n")
    assert atom.read_bytes() == plain.read_bytes()
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_bytes_roundtrip(tmp_path):
    target = tmp_path / "seg.mp4"
    artifact.atomic_write_bytes(target, b"\x00\x01\x02")
    assert target.read_bytes() == b"\x00\x01\x02"
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_creates_new_file(tmp_path):
    """目标不存在也要能写（第一次产出）。"""
    target = tmp_path / "new.json"
    artifact.atomic_write_text(target, "{}\n")
    assert target.read_text(encoding="utf-8") == "{}\n"


def test_load_json_safe_returns_default_for_missing_and_broken(tmp_path):
    sentinel = object()
    missing = tmp_path / "none.json"
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert artifact.load_json_safe(missing, default=sentinel) is sentinel
    assert artifact.load_json_safe(broken, default=sentinel) is sentinel
    ok = tmp_path / "ok.json"
    ok.write_text('{"a": 1}', encoding="utf-8")
    assert artifact.load_json_safe(ok, default=None) == {"a": 1}


def test_safe_int_never_raises():
    assert artifact.safe_int("2") == 2
    assert artifact.safe_int("v2", default=1) == 1
    assert artifact.safe_int(None, default=1) == 1
    assert artifact.safe_int(["x"], default=1) == 1
    assert artifact.safe_int(3.9, default=1) == 3


# ---- 接线到既有产物（shots.json / manifest.json）----------------------------


def test_write_shots_json_is_atomic(tmp_path, monkeypatch):
    """`write_shots_json` 也走原子写 —— 手改的真相源不许被半截内容顶掉。"""
    from lvs import workspace as ws_mod

    target = tmp_path / "shots.json"
    target.write_text("[]", encoding="utf-8")

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    try:
        ws_mod.write_shots_json(target, {"shots": []})
    except OSError:
        pass
    assert target.read_text(encoding="utf-8") == "[]", "shots.json 被写坏（非原子）"


def test_manifest_version_survives_hand_edited_garbage(tmp_path):
    """手改成 `"version": "v2"` 不许崩（读不了清单 = 整个任务打不开）。"""
    d = tmp_path / ".work" / "t"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text('{"version": "v2", "stages": {}}', encoding="utf-8")
    ws = Workspace.read("t", root=tmp_path)
    assert ws.manifest_version() == 1


def test_manifest_version_still_warns_on_newer_version(tmp_path, capsys):
    """高版本仍要出声（safe_int 不许把「读不懂」变成「不说」）。"""
    from lvs.workspace import MANIFEST_VERSION

    d = tmp_path / ".work" / "t"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(
        '{"version": %d, "stages": {}}' % (MANIFEST_VERSION + 5), encoding="utf-8"
    )
    Workspace.read("t", root=tmp_path)
    assert "结构版本" in capsys.readouterr().out

# --- commit_file：Windows 瞬时占用要重试（2026-10-05 实测踩到的坑）--------------
# 背景：上一轮 `lvs bgm` 的 PowerShell 循环没死透，与新循环抢同一个 `bgm.wav`，
# `os.replace` 抛 WinError 5 —— 一次 5 分钟的 CPU 生成全白费。
# 杀软实时扫描 / 资源管理器预览 / OneDrive 同步都会制造同样的瞬时占用。


def test_commit_file_retries_through_a_transient_permission_error(tmp_path, monkeypatch):
    """前两次被占用、第三次成功 —— 必须静默重试，不许把成品丢掉。"""
    from lvs import artifact

    part = tmp_path / "x.wav.part"
    part.write_bytes(b"payload")
    target = tmp_path / "x.wav"

    calls = {"n": 0}
    real = artifact.os.replace

    def flaky(src_, dst_):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(5, "Access is denied")
        return real(src_, dst_)

    monkeypatch.setattr(artifact.os, "replace", flaky)
    monkeypatch.setattr(artifact, "_COMMIT_BACKOFF_S", 0.0)

    artifact.commit_file(part, target)

    assert calls["n"] == 3, f"应该重试到第 3 次才成功，实测调用 {calls['n']} 次"
    assert target.read_bytes() == b"payload" and not part.exists()


def test_commit_file_keeps_the_part_and_says_how_to_rescue_when_locked_forever(
    tmp_path, monkeypatch
):
    """一直被占：抛错，但**不许删 `.part`**（里面是几十分钟算出来的成品），
    且报错必须直接告诉人怎么救。"""
    from lvs import artifact

    part = tmp_path / "y.mp4.part"
    part.write_bytes(b"very expensive output")
    target = tmp_path / "y.mp4"

    def always_locked(src_, dst_):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(artifact.os, "replace", always_locked)
    monkeypatch.setattr(artifact, "_COMMIT_BACKOFF_S", 0.0)
    monkeypatch.setattr(artifact, "_COMMIT_TRIES", 3)

    with pytest.raises(PermissionError) as err:
        artifact.commit_file(part, target)

    assert part.exists(), "`.part` 被删了 —— 几十分钟的成品白算"
    assert str(part) in str(err.value), "报错里没写上 `.part` 在哪，人不知道去哪救"
    assert "重试 3 次" in str(err.value)


def test_commit_file_does_not_swallow_other_os_errors(tmp_path, monkeypatch):
    """只重试"被占用"。路径不存在之类的错要原样抛 —— 重试没有意义，只会拖 9 秒。"""
    from lvs import artifact

    part = tmp_path / "z.bin.part"
    part.write_bytes(b"x")

    def boom(src_, dst_):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(artifact.os, "replace", boom)
    monkeypatch.setattr(artifact, "_COMMIT_BACKOFF_S", 0.0)

    with pytest.raises(FileNotFoundError):
        artifact.commit_file(part, tmp_path / "z.bin")
