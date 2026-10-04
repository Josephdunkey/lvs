"""任务目录清理（`lvs cleanup.py`）的测试。

这是**删除**功能，所以测试的重点不是"能不能删"，而是**"什么情况下绝不删"**：
- 默认（不给 `--yes`）必须一个字节都不动
- 路径穿越必须被拦住（`--task ../../x` 不能删到 `.work` 外面）
- 三种模式同时给必须报错（猜"哪个优先"的代价是删错东西）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lvs import cleanup


def _mk(root: Path, *names: str) -> Path:
    """造一个假的 `.work/` 与几个任务目录。返回 `.work` 路径。"""
    work = root / ".work"
    for n in names:
        d = work / n
        d.mkdir(parents=True, exist_ok=True)
        (d / "shots.json").write_text("{}", encoding="utf-8")
        (d / "blob.bin").write_bytes(b"x" * 512)
    return work


# ---- 列出与选择 ------------------------------------------------------------


def test_list_tasks_reports_size_and_files(tmp_path: Path):
    _mk(tmp_path, "t1")
    tasks = cleanup.list_tasks(tmp_path)
    assert [t.name for t in tasks] == ["t1"]
    assert tasks[0].files == 2
    assert tasks[0].size > 0


def test_list_tasks_on_missing_work_dir_is_empty(tmp_path: Path):
    assert cleanup.list_tasks(tmp_path) == []


def test_resolve_by_task(tmp_path: Path):
    _mk(tmp_path, "t1", "t2")
    assert [t.name for t in cleanup.resolve_targets(tmp_path, task="t2")] == ["t2"]


def test_resolve_by_keep_keeps_the_newest(tmp_path: Path):
    import os
    import time

    work = _mk(tmp_path, "old", "mid", "new")
    # 显式拉开 mtime（不靠写入顺序碰运气）
    base = time.time() - 1000
    for i, n in enumerate(("old", "mid", "new")):
        os.utime(work / n, (base + i * 10, base + i * 10))

    got = [t.name for t in cleanup.resolve_targets(tmp_path, keep=1)]
    assert got == ["old", "mid"], "应删最旧的两个、保留最新的一个"


def test_resolve_all(tmp_path: Path):
    _mk(tmp_path, "a", "b")
    assert sorted(t.name for t in cleanup.resolve_targets(tmp_path, all_=True)) == ["a", "b"]


def test_resolve_requires_a_mode(tmp_path: Path):
    _mk(tmp_path, "a")
    with pytest.raises(cleanup.CleanError):
        cleanup.resolve_targets(tmp_path)


# ---- 安全：绝不越界 --------------------------------------------------------


@pytest.mark.parametrize("bad", ["../../evil", "a/b", "..", "/abs", "a\\b"])
def test_path_traversal_is_rejected(tmp_path: Path, bad: str):
    """★ 任务名要拼进**删除路径**，允许 `/` 或 `..` 就是允许删到 `.work` 外面。"""
    _mk(tmp_path, "ok")
    with pytest.raises(cleanup.CleanError):
        cleanup.resolve_targets(tmp_path, task=bad)


def test_assert_inside_rejects_outside_path(tmp_path: Path):
    base = tmp_path / ".work"
    base.mkdir()
    with pytest.raises(cleanup.CleanError):
        cleanup._assert_inside(tmp_path / "elsewhere", base)
    with pytest.raises(cleanup.CleanError):
        cleanup._assert_inside(base, base)          # 自己也不许（那是整个 .work）


def test_assert_inside_accepts_child(tmp_path: Path):
    base = tmp_path / ".work"
    child = base / "t1"
    child.mkdir(parents=True)
    assert cleanup._assert_inside(child, base) == child.resolve()


def test_mutually_exclusive_modes_error(tmp_path: Path):
    """同时给多个模式就报错 —— 猜"哪个优先"的代价是删错。"""
    _mk(tmp_path, "a")
    with pytest.raises(cleanup.CleanError):
        cleanup.resolve_targets(tmp_path, task="a", all_=True)
    with pytest.raises(cleanup.CleanError):
        cleanup.resolve_targets(tmp_path, keep=1, all_=True)


def test_nonexistent_task_is_named_in_the_error(tmp_path: Path):
    _mk(tmp_path, "real")
    with pytest.raises(cleanup.CleanError) as ei:
        cleanup.resolve_targets(tmp_path, task="ghost")
    assert "ghost" in str(ei.value) and "real" in str(ei.value)


def test_negative_keep_rejected(tmp_path: Path):
    _mk(tmp_path, "a")
    with pytest.raises(cleanup.CleanError):
        cleanup.resolve_targets(tmp_path, keep=-1)


# ---- 默认只报告：一个字节都不动 ---------------------------------------------


class _Args:
    def __init__(self, **kw):
        self.task_filter = None
        self.keep = None
        self.all = False
        self.yes = False
        self.__dict__.update(kw)


def test_dry_run_deletes_nothing(tmp_path: Path, monkeypatch, capsys):
    """★ 不给 `--yes` 时**必须什么都不删** —— 这是这个命令最重要的性质。"""
    work = _mk(tmp_path, "t1", "t2")
    monkeypatch.setattr(cleanup, "work_root", lambda root=None: work)

    rc = cleanup.run_command(None, None, _Args(all=True))
    assert rc == 0
    assert sorted(p.name for p in work.iterdir()) == ["t1", "t2"], "dry-run 不该删东西"
    out = capsys.readouterr().out
    assert "--yes" in out, "该告诉用户怎么才真删"


def test_with_yes_it_actually_deletes(tmp_path: Path, monkeypatch):
    work = _mk(tmp_path, "t1", "t2")
    monkeypatch.setattr(cleanup, "work_root", lambda root=None: work)

    rc = cleanup.run_command(None, None, _Args(all=True, yes=True))
    assert rc == 0
    assert list(work.iterdir()) == []


def test_with_yes_and_task_deletes_only_that_one(tmp_path: Path, monkeypatch):
    work = _mk(tmp_path, "keepme", "dropme")
    monkeypatch.setattr(cleanup, "work_root", lambda root=None: work)

    cleanup.run_command(None, None, _Args(task_filter="dropme", yes=True))
    assert [p.name for p in work.iterdir()] == ["keepme"]


def test_empty_work_dir_is_not_an_error(tmp_path: Path, monkeypatch, capsys):
    work = tmp_path / ".work"
    work.mkdir()
    monkeypatch.setattr(cleanup, "work_root", lambda root=None: work)
    assert cleanup.run_command(None, None, _Args(all=True)) == 0
    assert "没有需要清理" in capsys.readouterr().out


def test_bad_args_return_usage_code(tmp_path: Path, monkeypatch, capsys):
    work = _mk(tmp_path, "a")
    monkeypatch.setattr(cleanup, "work_root", lambda root=None: work)
    assert cleanup.run_command(None, None, _Args()) == cleanup.EXIT_USAGE
    assert "三选一" in capsys.readouterr().out


def test_human_size_formats():
    assert cleanup._human(0) == "0 B"
    assert cleanup._human(2048) == "2.0 KB"
    assert cleanup._human(5 << 20) == "5.0 MB"
