"""任务目录清理（`lvs clean`）—— `.work/` 会随每次跑而长大，得有个正规出口。

## 为什么需要它

`.work/<任务>/` 存的是**中间产物**（分镜图、逐镜音轨、片段、日志）。
一个 270 镜的集子跑完就有几百 MB；实测本仓库 `.work` 已 **923 MB**。
这些**都是可重跑出来的**（除了人手的编辑，但那在 `shots.json` / 拍摄稿里，
而 `shots.json` 也在任务目录里 —— 所以**删除是有代价的**，必须让人确认）。

## 安全设计（删除是不可逆的，宁可多问一句）

1. **默认只报告**（dry-run）：列出会删什么、多大、上次改动时间。**不删任何东西。**
2. `--yes` 才真删。
3. **只动 `PROJECT_ROOT/.work/` 底下的直接子目录**，且：
   - 目标路径必须**解析后仍在该目录内**（防 `--task ../../x` 这类穿越）
   - 任务名必须是**单段**（不含 `/` `\\` `..`）
4. **绝不碰素材库**（`[paths].lib`）与仓库里的任何其他目录 ——
   素材库里是原始资料（拍摄稿、定妆卡、图片素材），删了重跑不出来。
5. 删用 `shutil.rmtree`，但每个目标都先过一次 `_assert_inside`。

## 与"断点续跑"的关系

删掉任务目录 = 放弃那个任务的进度。**下次跑同一条命令会从零开始。**
所以默认按"最旧"排序并保留最近 N 个（`--keep`），而不是无脑清空。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from lvs.config import PROJECT_ROOT
from lvs.errors import EXIT_USAGE, UsageError
from lvs.workspace import WORK_DIRNAME

#: 任务名允许的字符。**刻意收紧**：这是要拼进删除路径的东西，
#: 允许 `/` 或 `..` 就等于允许路径穿越。中文与连字符照收（本项目任务名里有）。
import re

_SAFE_TASK = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff.-]*$", re.UNICODE)


class CleanError(UsageError):
    """清理参数不合法（任务名可疑、目标不在 .work 下等）。

    归类为 `UsageError`（退出码 2）：这是"改参数再来"，不是运行时故障。
    """


@dataclass(frozen=True)
class TaskDir:
    name: str
    path: Path
    size: int          # 字节
    files: int
    mtime: float       # 最近改动时间（epoch）

    @property
    def human_size(self) -> str:
        return _human(self.size)


def _human(n: int) -> str:
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


def work_root(root: Path | None = None) -> Path:
    return (root or PROJECT_ROOT) / WORK_DIRNAME


def _assert_inside(target: Path, base: Path) -> Path:
    """确保 `target` 解析后仍在 `base` 内 —— 防路径穿越，这是删除前的最后一道闸。"""
    base_r = base.resolve()
    target_r = target.resolve()
    if target_r == base_r or base_r not in target_r.parents:
        raise CleanError(
            f"拒绝操作：{target} 不在 {base} 之内。\n"
            "  清理只动 `.work/` 底下的任务目录，绝不碰素材库或其他位置。"
        )
    return target_r


def _measure(path: Path) -> tuple[int, int, float]:
    """返回 (字节数, 文件数, 最近 mtime)。读不到的条目跳过。"""
    size = files = 0
    newest = path.stat().st_mtime if path.exists() else 0.0
    for f in path.rglob("*"):
        try:
            st = f.stat()
        except OSError:
            continue
        if f.is_file():
            size += st.st_size
            files += 1
        newest = max(newest, st.st_mtime)
    return size, files, newest


def list_tasks(root: Path | None = None) -> list[TaskDir]:
    """列出 `.work/` 下的任务目录（按最近改动**从旧到新**排序）。"""
    base = work_root(root)
    if not base.is_dir():
        return []
    out: list[TaskDir] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        size, files, mtime = _measure(child)
        out.append(TaskDir(name=child.name, path=child, size=size, files=files, mtime=mtime))
    return sorted(out, key=lambda t: t.mtime)


def resolve_targets(
    root: Path | None = None,
    *,
    task: str | None = None,
    keep: int | None = None,
    all_: bool = False,
) -> list[TaskDir]:
    """算出这次要清理哪些任务目录。**不删**，只算。

    三种模式互斥（`--task` / `--keep N` / `--all`），都没给就报错 ——
    不提供"默认清空"这种行为，太容易误伤。
    """
    base = work_root(root)
    tasks = list_tasks(root)

    # 三种模式互斥。同时给多个就报错 —— 猜"哪个优先"是危险的好意：
    # `--task a --all` 可能是打错了，而猜错的代价是删掉不该删的任务目录。
    given = [n for n, v in (("--task", bool(task)), ("--keep", keep is not None), ("--all", all_)) if v]
    if len(given) > 1:
        raise CleanError(f"这三种模式互斥，只能给一个：{'、'.join(given)}")

    if task:
        if not _SAFE_TASK.match(task):
            raise CleanError(
                f"任务名 {task!r} 含不安全的字符。只允许中英文/数字/下划线/连字符/点，"
                "且必须单段（不含路径分隔符）。"
            )
        hit = [t for t in tasks if t.name == task]
        if not hit:
            raise CleanError(
                f"`.work/` 下没有任务 {task!r}。现有："
                + ("、".join(t.name for t in tasks) or "（空）")
            )
        target = hit[0]
        _assert_inside(target.path, base)
        return [target]

    if keep is not None:
        if keep < 0:
            raise CleanError("--keep 不能是负数")
        return tasks[: max(0, len(tasks) - keep)]

    if all_:
        return tasks

    raise CleanError(
        "要清什么？三选一：\n"
        "  --task <名>   只清这一个任务\n"
        "  --keep N      保留最近 N 个任务，其余清掉\n"
        "  --all         清掉全部任务\n"
        "（默认只报告；加 --yes 才真删）"
    )


def run_command(config, ws, args) -> int:  # noqa: ANN001 - 由 cli 传入
    try:
        targets = resolve_targets(
            None,
            task=getattr(args, "task_filter", None),
            keep=getattr(args, "keep", None),
            all_=bool(getattr(args, "all", False)),
        )
    except CleanError as exc:
        print(str(exc))
        return EXIT_USAGE

    if not targets:
        print("没有需要清理的任务目录。")
        return 0

    total = sum(t.size for t in targets)
    print(f"将清理 {len(targets)} 个任务目录，共 {_human(total)}：")
    for t in targets:
        print(f"  {t.name:<24} {t.human_size:>10}  {t.files:>6} 个文件")

    if not bool(getattr(args, "yes", False)):
        print()
        print("  ⚠ 这是**只报告**（未删除任何东西）。")
        print("    任务目录里有 `shots.json`（可能是你手改过的）与全部中间产物 ——")
        print("    删掉就得从零重跑。确认后再加 `--yes`：")
        mode = "--all" if getattr(args, "all", False) else (
            f"--keep {getattr(args, 'keep', '')}" if getattr(args, "keep", None) is not None
            else f"--task {getattr(args, 'task_filter', '')}"
        )
        print(f"      lvs clean {mode} --yes")
        return 0

    base = work_root(None)
    removed = 0
    for t in targets:
        _assert_inside(t.path, base)     # 删除前再验一次（防御性）
        try:
            shutil.rmtree(t.path)
            removed += 1
        except OSError as exc:
            print(f"  [失败] {t.name}：{exc}")
    print(f"\n已删除 {removed}/{len(targets)} 个任务目录，释放 {_human(total)}。")
    return 0 if removed == len(targets) else 1
