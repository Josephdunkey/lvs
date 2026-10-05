"""`lvs status --brief` 的 5 分钟缓存（lvs/summary.py）—— 守的是"省 token 不能省掉正确性"。

## 这组测试守的是什么

1. **同一状态同一秒跑两次，第二次不许再扫盘**：门的判定要 stat 整个任务目录，
   实测 `lvs status --brief` 每次 10 s 上下；续跑时一条命令反复问"我在哪"，
   这笔钱必须省。判据是"扫盘次数"（用计数器），不是"感觉变快了"。
2. **状态一变缓存必须失效**：`gates.json` 被改、任务目录多出一张图 →
   下一次必须重扫。过期状态比慢状态更贵：agent 照它敲下一条命令会踩空。
3. **缓存只有 5 分钟**，超时重扫。
4. **缓存坏掉 / 文件被删 → 当没有**，绝不把 `lvs status` 弄挂。
5. **换了筛选条件（--task / --all）不许串味**。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from lvs import summary


def _task(root: Path, name: str, *, images: int = 0) -> Path:
    d = root / ".work" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text("{}", encoding="utf-8")
    if images:
        local = d / "assets" / "local"
        local.mkdir(parents=True, exist_ok=True)
        for i in range(images):
            (local / f"shot-{i:03d}.png").write_bytes(b"x")
    return d


def _count_scans(monkeypatch) -> list[int]:
    """把 gate_state 换成一个计数器 —— "有没有真的重扫"是唯一可信判据。"""
    calls = [0]
    real = summary.gate_state

    def counting(task, config_name, *, root=summary.PROJECT_ROOT):
        calls[0] += 1
        return real(task, config_name, root=root)

    monkeypatch.setattr(summary, "gate_state", counting)
    return calls


def test_second_call_is_served_from_cache(tmp_path: Path, monkeypatch):
    _task(tmp_path, "UGE11", images=2)
    calls = _count_scans(monkeypatch)

    first = summary.collect(prefix="UGE", root=tmp_path)
    info = summary.CacheInfo()
    second = summary.collect(prefix="UGE", root=tmp_path, info=info)

    assert calls[0] == 1, "第二次调用又扫了一遍盘 —— 缓存没生效"
    assert info.used is True
    assert [r.task for r in second] == [r.task for r in first]
    assert second[0].images == 2 and second[0].artifacts == first[0].artifacts


def test_a_changed_gates_file_invalidates_the_cache(tmp_path: Path, monkeypatch):
    d = _task(tmp_path, "UGE11")
    calls = _count_scans(monkeypatch)
    summary.collect(prefix="UGE", root=tmp_path)

    gates = d / "gates.json"
    gates.write_text('{"G1": {"open": true}}', encoding="utf-8")
    future = time.time() + 5
    os.utime(gates, (future, future))          # 保证 mtime 一定变（文件系统粒度可能粗）

    info = summary.CacheInfo()
    summary.collect(prefix="UGE", root=tmp_path, info=info)
    assert calls[0] == 2, "gates.json 变了却还是给旧结果 —— 缓存失效判据不够"
    assert info.used is False


def test_a_new_image_invalidates_the_cache(tmp_path: Path, monkeypatch):
    d = _task(tmp_path, "UGE11", images=1)
    calls = _count_scans(monkeypatch)
    summary.collect(prefix="UGE", root=tmp_path)

    (d / "assets" / "local" / "shot-999.png").write_bytes(b"x")
    future = time.time() + 5
    os.utime(d / "assets" / "local", (future, future))

    rows = summary.collect(prefix="UGE", root=tmp_path)
    assert calls[0] == 2
    assert rows[0].images == 2


def test_cache_expires_after_the_ttl(tmp_path: Path, monkeypatch):
    _task(tmp_path, "UGE11")
    calls = _count_scans(monkeypatch)
    summary.collect(prefix="UGE", root=tmp_path)

    base = time.time()
    monkeypatch.setattr(summary.time, "time", lambda: base + summary.CACHE_TTL_SECONDS + 1)
    info = summary.CacheInfo()
    summary.collect(prefix="UGE", root=tmp_path, info=info)
    assert calls[0] == 2, "过了 TTL 还在吃缓存"
    assert info.used is False


def test_a_corrupt_cache_file_is_ignored(tmp_path: Path, monkeypatch):
    _task(tmp_path, "UGE11")
    cache = tmp_path / ".work" / ".status-brief.json"
    cache.write_text("{不是 json", encoding="utf-8")
    calls = _count_scans(monkeypatch)

    rows = summary.collect(prefix="UGE", root=tmp_path)
    assert calls[0] == 1
    assert [r.task for r in rows] == ["UGE11"]
    assert json.loads(cache.read_text(encoding="utf-8"))["version"] == summary.CACHE_VERSION


def test_different_selection_does_not_reuse_the_cache(tmp_path: Path, monkeypatch):
    _task(tmp_path, "UGE11")
    _task(tmp_path, "NW01")
    calls = _count_scans(monkeypatch)

    summary.collect(prefix="UGE", root=tmp_path)          # 只看 UGE → 1 个任务
    rows = summary.collect(show_all=True, prefix="UGE", root=tmp_path)   # --all → 2 个任务
    assert calls[0] == 3, "换了筛选条件却复用了另一份结果"
    assert [r.task for r in rows] == ["NW01", "UGE11"]


def test_use_cache_false_always_rescans(tmp_path: Path, monkeypatch):
    _task(tmp_path, "UGE11")
    calls = _count_scans(monkeypatch)
    summary.collect(prefix="UGE", root=tmp_path)
    summary.collect(prefix="UGE", root=tmp_path, use_cache=False)
    assert calls[0] == 2


def test_brief_output_still_fits_in_eight_lines_with_a_cache_note(tmp_path: Path, monkeypatch):
    for i in range(6):
        _task(tmp_path, f"UGE{i:02d}")
    rows = summary.collect(prefix="UGE", root=tmp_path)
    text = summary.format_table(rows, brief=True, note="(缓存 12s 前的结果；重扫加 --fresh)")
    lines = text.splitlines()
    assert len(lines) <= 8, lines
    assert lines[-1].startswith("…另有")     # 任务多于 5 行时，尾注让位给"还有几个任务"
    short = summary.format_table(rows[:2], brief=True, note="(缓存 12s 前的结果；重扫加 --fresh)")
    assert short.splitlines()[-1].startswith("(缓存")
