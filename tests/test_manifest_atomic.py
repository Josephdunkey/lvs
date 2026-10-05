"""manifest 的**原子写**与**写回节流**。

## 为什么单独测这一组

`_write_manifest` 原先是 `write_text`（先截断、再写），而 `_read_manifest` 遇到坏 JSON
只能**重建成空**（`stages: {}`）—— 两者叠加的后果是：**在写盘途中被打断，
断点续跑的全部记录全丢**，下次从零重跑几百镜（票 19 起会留证 + 出声，但记录仍要重跑）。

这个窗口在串行下只是"刚好撞上写盘那一刻"的小概率，但 `mark_stage_progress`
**每镜调一次**会把它放大；将来若并行就是必然。所以这里要钉死两件事：

1. **原子**：写到一半被打断，盘上要么是旧的完整文件、要么是新的完整文件
2. **节流不丢数据**：收尾那次必须落全（否则续跑看到的进度是错的）
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from lvs.workspace import MANIFEST_VERSION, Workspace


def _read(ws: Workspace) -> dict:
    return json.loads(ws.manifest_path.read_text(encoding="utf-8"))


# ---- 原子写 -----------------------------------------------------------------


def test_write_leaves_no_temp_file(tmp_path: Path):
    ws = Workspace(task="t", root=tmp_path).ensure()
    ws.mark_stage("shots", outputs=[ws.path("shots.json")])
    assert not list(ws.dir.glob("*.tmp")), "临时文件必须被 os.replace 消费掉"


def test_manifest_stays_readable_after_many_writes(tmp_path: Path):
    ws = Workspace(task="t", root=tmp_path).ensure()
    for i in range(1, 51):
        ws.mark_stage_progress("assets", done=i, total=50)
    ws.mark_stage("assets", done=50, total=50)
    data = _read(ws)
    assert data["stages"]["assets"]["done"] == 50
    assert data["version"] == MANIFEST_VERSION


def test_atomic_write_does_not_recreate_manifest_as_empty(tmp_path: Path):
    """★ 核心回归：写回**不能**把已有记录弄丢（老实现的截断窗口会导致静默重建）。"""
    ws = Workspace(task="t", root=tmp_path).ensure()
    ws.mark_stage("shots", outputs=[ws.path("shots.json")], count=7)
    (ws.path("shots.json")).write_text("{}", encoding="utf-8")

    for _ in range(30):
        ws.mark_stage_progress("assets", done=1, total=10)

    data = _read(ws)
    assert "shots" in data["stages"], "已有阶段记录被弄丢了"
    assert data["stages"]["shots"].get("count") == 7


def test_write_failure_does_not_crash(tmp_path: Path, monkeypatch):
    """写盘失败（只读盘 / 磁盘满）不该让流水线崩 —— 与"清单损坏不崩"同一取舍。"""
    ws = Workspace(task="t", root=tmp_path).ensure()

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    ws.mark_stage("shots")          # 不该抛
    assert ws.manifest_path.is_file()


# ---- 节流 -------------------------------------------------------------------


def test_progress_writes_are_throttled(tmp_path: Path):
    """★ 300 次进度调用不该写 300 次盘（每镜一次整文件重写是白费 IO）。"""
    ws = Workspace(task="t", root=tmp_path).ensure()
    calls = {"n": 0}
    original = ws._write_manifest

    def counting():  # noqa: ANN202
        calls["n"] += 1
        original()

    ws._write_manifest = counting       # type: ignore[method-assign]
    for i in range(1, 301):
        ws.mark_stage_progress("assets", done=i, total=300)

    assert calls["n"] < 300, f"没节流：写了 {calls['n']} 次"
    # 百分比跳格 → 最多 101 次（0..100）
    assert calls["n"] <= 105, f"节流不够狠：写了 {calls['n']} 次"


def test_progress_never_writes_more_than_once_per_milestone(tmp_path: Path):
    """节流按**百分比跳格**，不看"完成数变了"（done 每镜都变，用它等于不节流）。"""
    ws = Workspace(task="t", root=tmp_path).ensure()
    calls = {"n": 0}
    original = ws._write_manifest

    def counting():  # noqa: ANN202
        calls["n"] += 1
        original()

    ws._write_manifest = counting       # type: ignore[method-assign]
    for i in range(1, 101):
        ws.mark_stage_progress("assets", done=i, total=100)
    # 100 格 → 至多 101 次
    assert calls["n"] <= 102, f"写了 {calls['n']} 次，节流没生效"


def test_final_mark_stage_persists_the_latest_count(tmp_path: Path):
    """★ 节流会"欠"写，但**收尾那条 `mark_stage` 不节流** —— 最终计数一定落全。

    （早先这里靠一个 `flush_manifest()`，但它生产代码从没被调用 ——
    那是"定义了却从不接线 + 文档说谎"，已删除。真正保证落全的是 `mark_stage`。）
    """
    ws = Workspace(task="t", root=tmp_path).ensure()
    for i in range(1, 301):
        ws.mark_stage_progress("assets", done=i, total=300)
    ws.mark_stage("assets", done=300, total=300, failed=0)
    assert _read(ws)["stages"]["assets"]["done"] == 300


def test_throttle_uses_time_window_when_total_unknown(tmp_path: Path, monkeypatch):
    """`total` 未知时 pct 恒为 -1 → 靠**时间窗口**兜底，不能永不写盘。"""
    ws = Workspace(task="t", root=tmp_path).ensure()
    calls = {"n": 0}
    original = ws._write_manifest

    def counting():  # noqa: ANN202
        calls["n"] += 1
        original()

    ws._write_manifest = counting       # type: ignore[method-assign]
    # 第一次写（pct 从 -1 变 -1 不变，但时间窗口起点是 0 → 触发）
    ws.mark_stage_progress("assets", done=1)
    first = calls["n"]
    assert first >= 1, "首次调用就该落一次盘"

    # 模拟时间流逝
    ws._progress_last_write = time.monotonic() - 10
    ws.mark_stage_progress("assets", done=2)
    assert calls["n"] > first, "超过时间窗口必须落盘"


def test_mark_stage_always_writes_final_state(tmp_path: Path):
    """收尾用 `mark_stage`（不走节流）—— 状态该落就一定落。"""
    ws = Workspace(task="t", root=tmp_path).ensure()
    for i in range(1, 201):
        ws.mark_stage_progress("assets", done=i, total=200)
    ws.mark_stage("assets", status="partial", done=200, failed=3, total=200)
    entry = _read(ws)["stages"]["assets"]
    assert entry["status"] == "partial"
    assert entry["failed"] == 3

# ---- 坏清单留证（票 19）------------------------------------------------------
#
# 坏清单（断电写一半 / 手工改错）**不能静默当空**：原先是"读不出来就重建成空"，
# 于是续跑记录无声消失、下一轮从零重跑几百镜，而用户手上没有任何线索。
# 现在的约定：原文件原样留下（现场）→ 另存一份带时间戳的副本（留证）→ stderr 出声
# → runlog 记一条 → 仍然**不抛**、按空清单继续。


def _break(ws: Workspace, text: str) -> Path:
    ws.manifest_path.write_text(text, encoding="utf-8")
    return ws.manifest_path


def test_broken_manifest_is_kept_as_evidence(tmp_path: Path):
    """★ 留证：另存 `manifest.broken-*.json`，内容与原文逐字节相同；原文件不动。"""
    ws = Workspace(task="t", root=tmp_path).ensure()
    broken = _break(ws, '{ "stages": { oops }')
    original = broken.read_bytes()

    data = ws._read_manifest()

    assert data["stages"] == {}                      # 语义不变：读不出来就当空
    kept = list(ws.dir.glob("manifest.broken-*.json"))
    assert len(kept) == 1, f"该留一份证，实际 {len(kept)} 份"
    assert kept[0].read_bytes() == original, "留证内容与原文不一致"
    assert broken.read_bytes() == original, "原文件被动过 —— 现场必须原样保留"


def test_broken_manifest_says_so_on_stderr(tmp_path: Path, capsys):
    """★ 降级必须**可见**：stderr 上一条人话，说清"坏在哪、留到哪、怎么继续"。"""
    ws = Workspace(task="t", root=tmp_path).ensure()
    _break(ws, "{ 这不是 json")

    ws._read_manifest()

    err = capsys.readouterr().err
    assert "manifest.json 读不出来" in err
    assert "manifest.broken-" in err and "本轮按空清单继续" in err


def test_broken_manifest_never_raises(tmp_path: Path):
    """不抛是**现状语义**（下游不能因此挂）：坏 JSON 与非对象 JSON 都只降级。"""
    ws = Workspace(task="t", root=tmp_path).ensure()
    _break(ws, "{ 坏 json")
    assert ws._read_manifest()["stages"] == {}
    _break(ws, "[1, 2, 3]")           # 合法 JSON，但不是清单
    assert ws._read_manifest()["stages"] == {}


def test_two_broken_manifests_do_not_overwrite_each_other(tmp_path: Path):
    """★ 连读两次坏清单 → 两份留证都在（同秒重名要加序号，绝不互相覆盖）。"""
    ws = Workspace(task="t", root=tmp_path).ensure()
    _break(ws, "{ 第一次")
    ws._read_manifest()
    _break(ws, "{ 第二次")
    ws._read_manifest()

    kept = sorted(ws.dir.glob("manifest.broken-*.json"))
    assert len(kept) == 2, f"留证互相覆盖了：{kept}"
    assert {p.read_text(encoding="utf-8") for p in kept} == {"{ 第一次", "{ 第二次"}


def test_broken_manifest_is_recorded_in_runlog(tmp_path: Path):
    """降级也要进 runlog（票 19 第 3 条）—— 事后来查"这轮为什么从零重跑"。"""
    from lvs import runlog

    ws = Workspace(task="t", root=tmp_path).ensure()
    _break(ws, "{ 坏 json")
    ws._read_manifest()

    rows = [r for r in runlog.read(ws) if r.get("event") == "manifest_broken"]
    assert rows, "runlog 里没有 manifest_broken 事件"
    assert rows[-1]["evidence"].startswith("manifest.broken-")


def test_readonly_open_also_keeps_evidence(tmp_path: Path):
    """`Workspace.read`（`lvs board` / `lvs status` 走的那条）同样留证 ——
    坏清单被"看一眼"发现时更要让人知道，否则看板永远显示"这任务还没开始"。"""
    d = tmp_path / ".work" / "t"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text("{ 坏 json", encoding="utf-8")

    ws = Workspace.read("t", root=tmp_path)

    assert ws.manifest == {} or ws.manifest.get("stages") == {}
    assert list(d.glob("manifest.broken-*.json")), "只读打开也必须留证"
