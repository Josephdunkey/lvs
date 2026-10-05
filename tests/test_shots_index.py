"""`shots.index.json` 侧车索引（P1 / T1）+ "大产物必配索引"判据。

## 为什么要有侧车

`shots.json` 0.5–0.9 MB，agent 一读就常驻重发（≈ 20 万 token 的老病根）。
每镜一行的索引让它先读几十 KB 的小表，再决定 `--peek` 哪几镜。

## 判据

* 写 `shots.json`（**唯一**写法 `workspace.write_shots_json`）就同步写侧车；
* 每镜有 `id / 人物 / 一句话 / 时长 / 是否图文 beat`；
* 侧车 ≤ `max(源 2%, 32 KB)`（超预算先缩『一句话』，最后才丢人物）；
* **≤ 64 KB 的产物不配侧车**（读了不比原文便宜，还得同步两份）。
"""

from __future__ import annotations

import json
from pathlib import Path

from lvs import cast, workspace
from lvs.workspace import Workspace


def _shot(i: int, **kw) -> dict:
    shot = {
        "id": i,
        "scene": "破败荒宅内，晨光初透，残破屋顶露出一弯残月",
        "visual": "荒宅内部的清晨",
        "kind": "scene",
        "source": "local",
        "start": 0.0,
        "end": 4.0,
    }
    shot.update(kw)
    return shot


def _data(n: int, *, prompt_chars: int = 900, scene: str | None = None) -> dict:
    shots = [
        _shot(i, prompt=("very long english prompt " * 200)[:prompt_chars],
              **({"scene": scene} if scene else {}))
        for i in range(1, n + 1)
    ]
    return {"shots": shots, "count": n}


def _budget(source_bytes: int) -> int:
    return max(int(source_bytes * workspace.INDEX_BUDGET_RATIO), workspace.INDEX_MIN_BUDGET_BYTES)


# ---- 命名与预算 --------------------------------------------------------------


def test_index_name() -> None:
    assert workspace.shots_index_name("shots.json") == "shots.index.json"


def test_threshold_matches_the_architecture_rule() -> None:
    """判据里的 64 KB 与实现里的常量必须是**同一个数**。"""
    assert workspace.INDEX_MIN_SOURCE_BYTES == 64 * 1024


def test_write_shots_index_refuses_small_sources(tmp_path: Path) -> None:
    path = tmp_path / "shots.json"
    path.write_text("{}", encoding="utf-8")
    assert workspace.write_shots_index(path, _data(2), source_bytes=1000) is None
    assert not (tmp_path / "shots.index.json").exists()


# ---- 写 shots.json 就写侧车 --------------------------------------------------


def test_writer_creates_the_sidecar_for_big_shots_json(tmp_path: Path) -> None:
    ws = Workspace(task="t", root=tmp_path).ensure()
    data = _data(200)
    ws.write_shots(data)

    source = ws.path("shots.json")
    index = ws.path("shots.index.json")
    assert source.stat().st_size > workspace.INDEX_MIN_SOURCE_BYTES
    assert index.is_file(), "> 64 KB 的产物必须配 *.index.json"

    payload = json.loads(index.read_text(encoding="utf-8"))
    assert payload["count"] == 200
    assert len(payload["shots"]) == 200
    first = payload["shots"][0]
    assert set(first) >= {"i", "d", "g"}, "每镜必须有 id / 时长 / 图文 beat"
    assert first["i"] == 1 and first["g"] is False
    assert index.stat().st_size <= _budget(source.stat().st_size)


def test_gui_writer_also_writes_the_sidecar(tmp_path: Path) -> None:
    """界面走的是同一个 `write_shots_json`（它只有任务目录，没有 Workspace）。"""
    path = tmp_path / "shots.json"
    workspace.write_shots_json(path, _data(200))
    assert (tmp_path / "shots.index.json").is_file()


def test_small_shots_json_gets_no_sidecar(tmp_path: Path) -> None:
    ws = Workspace(task="t", root=tmp_path).ensure()
    ws.write_shots(_data(3, prompt_chars=20))
    assert ws.path("shots.json").is_file()
    assert not ws.path("shots.index.json").exists()


def test_sidecar_is_written_atomically(tmp_path: Path, monkeypatch) -> None:
    """索引写盘走 `artifact.atomic_write_text`（半截文件会让 agent 读到坏 JSON）。"""
    seen: list[Path] = []
    real = workspace.artifact.atomic_write_text

    def spy(path, text):
        seen.append(Path(path))
        return real(path, text)

    monkeypatch.setattr(workspace.artifact, "atomic_write_text", spy)
    ws = Workspace(task="t", root=tmp_path).ensure()
    ws.write_shots(_data(200))
    assert ws.path("shots.index.json") in seen


def test_index_write_failure_does_not_break_shots_json(tmp_path: Path, monkeypatch) -> None:
    ws = Workspace(task="t", root=tmp_path).ensure()

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(workspace, "write_shots_index", boom)
    ws.write_shots(_data(200))
    assert json.loads(ws.path("shots.json").read_text(encoding="utf-8"))["count"] == 200


# ---- 每镜的五个字段 ----------------------------------------------------------


def test_duration_prefers_measured_audio() -> None:
    assert workspace._seconds_of({"audio_duration": 3.25, "start": 0, "end": 9}) == 3.2
    assert workspace._seconds_of({"start": 1.0, "end": 4.0}) == 3.0
    assert workspace._seconds_of({}) == 0.0


def test_graphic_flag_covers_kind_and_source() -> None:
    assert workspace._is_graphic({"kind": "graphic"}) is True
    assert workspace._is_graphic({"source": "graphic"}) is True
    assert workspace._is_graphic({"kind": "scene", "source": "local"}) is False


def test_slot_extraction_matches_cast() -> None:
    """`_slots_of` 与 `cast.SLOT` 是同一写法的两处 —— 用同一批样本守住它们一致。"""
    samples = [
        "{NAOKO} 站在开阔的草地上",
        "{彻} 与 {直子} 并肩走过",
        "没有槽位的普通镜头",
        "{A}{B}{C}{D}{E}{F}{G}{H} 八个人",
        "",
    ]
    for text in samples:
        assert set(workspace._slots_of({"visual": text})) == cast.extract_slots(text), text


def test_slots_are_capped(tmp_path: Path) -> None:
    ws = Workspace(task="t", root=tmp_path).ensure()
    shot = _shot(1, visual="{A}{B}{C}{D}{E}{F}{G}{H}{I}", prompt="x" * 900)
    ws.write_shots({"shots": [shot] * 300, "count": 300})
    payload = json.loads(ws.path("shots.index.json").read_text(encoding="utf-8"))
    assert len(payload["shots"][0]["c"]) == workspace.INDEX_CHARS_MAX


# ---- 预算收敛（超了先缩『一句话』）------------------------------------------


def test_normal_index_keeps_a_summary(tmp_path: Path) -> None:
    index = workspace.build_shots_index(_data(100), source_bytes=200_000)
    assert index["summary_chars"] == workspace.INDEX_SUMMARY_CHARS
    assert index["shots"][0]["s"], "预算够时必须有『一句话』"


def test_summary_shrinks_then_fields_are_dropped_when_budget_is_tight() -> None:
    """★ 判据：侧车 ≤ 预算 —— 预算不够时先缩『一句话』，最后才丢人物。"""
    data = _data(2000, prompt_chars=40)
    source_bytes = 4_000_000                     # 2% = 80 KB > 32 KB 下限
    index = workspace.build_shots_index(data, source_bytes=source_bytes)
    text = json.dumps(index, ensure_ascii=False, separators=(",", ":"))
    assert len(text.encode("utf-8")) <= 80_000
    assert index["summary_chars"] == 0
    assert "s" not in index["shots"][0]
    assert "c" not in index["shots"][0]
    assert index["shots"][0]["i"] == 1 and "d" in index["shots"][0]


def test_degraded_index_still_lists_every_shot() -> None:
    data = _data(500, prompt_chars=40)
    index = workspace.build_shots_index(data, source_bytes=4_000_000)
    assert len(index["shots"]) == 500


def test_index_is_much_smaller_than_the_source(tmp_path: Path) -> None:
    ws = Workspace(task="t", root=tmp_path).ensure()
    ws.write_shots(_data(300))
    source = ws.path("shots.json").stat().st_size
    index = ws.path("shots.index.json").stat().st_size
    assert index <= _budget(source)
    assert index < source / 4, f"索引 {index} 相对源 {source} 不够小"
