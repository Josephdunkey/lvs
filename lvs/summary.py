"""`lvs status` —— 一行一任务的续跑摘要（agent 续跑的**唯一入口**）。

为什么要有它：过去"我现在做到哪了"要靠读 MEMORY + 当日日志 + 手册 + shots.json
（实测约 715 KB ≈ 22 万 token）重建现场。本模块**不读 shots.json 正文**：

* 门禁走 `lvs.pipeline.statuses()`（**进程内**调用，不 spawn 子进程）——
  门禁是单一真源，摘要不另立一套判据；
* 图片数用目录列举（`assets/local/shot-*.png`）；
* 产物就绪只看文件在不在（6 个标记位）。

默认输出一份表格（≤ 8 行，约 200 token），够 agent 判断"下一步敲哪条命令"。

用法：
    lvs status --brief                  # 默认：活跃书（前缀 UGE）
    lvs status --brief --task UGE03     # 只看一个任务
    lvs status --brief --all            # 全部真实任务
    lvs status --json                   # 机器可读

—— 与 `.work/tools/continuation_summary.py` 的关系：那份是原型（在 gitignore 里的
`.work/` 下、GBK 控制台会抛、README 一字未提）。这里是它的**正式版**：搬进 CLI、
强制 UTF-8、加了机器可读输出与守卫测试。原型保留，但请一律用 `lvs status`。
"""
from __future__ import annotations

import datetime
import json
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

from lvs.config import PROJECT_ROOT, Config, ConfigError

WORK_DIRNAME = ".work"

#: 测试夹具 / 内部目录：默认不列（真实书期目里没有这些）。
SKIP_TASKS = frozenset({"agentprobe", "default", "unit-test-task", "tmp", "tools"})

#: 演示任务：`--all` 才列。
DEMO_TASKS = frozenset({"DEMO-A", "DEMO-B"})

#: 任务前缀 → config。G2(定妆) 的指纹靠 config 定位 cast lock.json，
#: **带错 config = 把"已批准"误判成 stale**（AGENTS.md 第 5 条踩过）。
#: 未知前缀（如 MM）不传 config：宁可标「≈ 指纹不可信」，也不要瞎猜一份。
CONFIG_FOR_PREFIX: dict[str, str] = {
    "UGE": "config.雨月物语.toml",
    "NW": "config.toml",
    "DEMO": "config.toml",
}

#: 产物就绪标记位的顺序（Y = 在，- = 无）。
ARTIFACTS: tuple[str, ...] = (
    "parse.json",
    "shots.json",
    "audio/narration.mp3",
    "subtitle.srt",
    "final.mp4",
    "publish/meta.json",
)
ARTIFACT_KEYS = "parse / shots / narration / srt / final / publish"

#: `--brief` 最多打几行任务行 —— 加上表头/分隔/尾注正好 8 行封顶。
MAX_BRIEF_ROWS = 5

#: `--brief` 的结果缓存（省 token 的主要手段之一：门禁判定要 stat 全任务）。
#: ★ 为什么必须有失效判据（而不是"缓存 5 分钟就完事"）：门禁/图片数/产物
#:   全都会在几十秒内被上一条命令改掉 —— **过期状态比慢状态更贵**
#:   （agent 照它敲下一条命令就会踩空）。所以缓存 key 里带"任务目录指纹"：
#:   任务目录自身 + 一级子项（gates.json / manifest.json / publish / assets…）的
#:   mtime 最大值，任一变动即重扫。
CACHE_FILENAME = ".status-brief.json"
CACHE_VERSION = 1
CACHE_TTL_SECONDS = 300.0


def _fix_console() -> None:
    """让中文在任何 Windows 控制台都能打印；不支持时静默忽略。

    与 `lvs.cli._configure_stdio` 同语义。为什么这里再写一遍：本模块要能被
    **单独调用**（测试 / 别处的脚本直接 import），不能指望 CLI 入口先跑过。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


@dataclass
class TaskRow:
    """一个任务的摘要行。字段全是标量，好直接 `json.dumps`。"""

    task: str
    config: str
    images: int
    open_gates: int
    total_gates: int
    next_gate: str
    artifacts: str
    recent: str
    error: str = ""

    @property
    def gates_text(self) -> str:
        return f"{self.open_gates}/{self.total_gates}" if self.total_gates else "-"

    @property
    def open_all(self) -> bool:
        return bool(self.total_gates) and self.open_gates == self.total_gates


# ---- 采集 -------------------------------------------------------------------


def task_dirs(root: Path = PROJECT_ROOT) -> list[Path]:
    """`.work/*/manifest.json` 所在目录（= 任务目录）。

    只 glob 文件名、**不读 manifest 正文** —— 摘要是给 agent 省 token 的，
    自己先把一行 JSON 读进来就本末倒置了。
    """
    work = root / WORK_DIRNAME
    if not work.is_dir():
        return []
    return sorted(p.parent for p in work.glob("*/manifest.json"))


def _cfg_name_for(task: str, override: Path | None) -> str:
    if override is not None:
        return override.name
    for prefix, name in CONFIG_FOR_PREFIX.items():
        if task.upper().startswith(prefix):
            return name
    return ""


def select_tasks(
    paths: list[Path], *, task: str | None = None, prefix: str = "UGE", show_all: bool = False
) -> list[Path]:
    """挑要列的任务：`--task` 精确一个 > `--all` 全部 > `--prefix` 前缀。"""
    chosen: list[Path] = []
    want = (task or "").strip().lower()
    for p in paths:
        name = p.name
        if name in SKIP_TASKS:
            continue
        if want:
            if name.lower() == want:
                chosen.append(p)
        elif show_all:
            chosen.append(p)
        elif name not in DEMO_TASKS and name.startswith(prefix):
            chosen.append(p)
    return chosen


def _count_images(task_dir: Path) -> int:
    """分镜图张数：只列举、不开图。"""
    total = 0
    for sub in ("assets/local", "assets/graphic"):
        d = task_dir / sub
        if d.is_dir():
            total += sum(1 for _ in d.glob("shot-*.png"))
    return total


def _artifact_mark(task_dir: Path) -> str:
    return "".join("Y" if (task_dir / name).is_file() else "-" for name in ARTIFACTS)


#: 「最近」列看哪些路径：关键产物 + 任务目录的直接子目录。
#: 为什么不 rglob：UGE04（594 张图 + 363 段音轨）实测全树遍历要 **2.4 s**；
#: 而这个列只用来回答“这任务还热着吗”，不值得为“分钟级精度”付两秒。
_RECENT_TARGETS: tuple[str, ...] = ("manifest.json",) + ARTIFACTS


def _recent(task_dir: Path) -> str:
    """最近活动时间（`%m-%d %H:%M`，**近似**）：关键产物 + 一级子目录的 mtime 取最大。

    子目录用目录自身的 mtime（里面新增/删除文件会刷新它）——
    比全树遍历便宜三个数量级，对“这一步什么时候跑完的”这件事精度足够。
    """
    stamps: list[float] = []
    for name in _RECENT_TARGETS:
        try:
            stamps.append((task_dir / name).stat().st_mtime)
        except OSError:
            continue
    try:
        for child in task_dir.iterdir():
            if child.is_dir():
                try:
                    stamps.append(child.stat().st_mtime)
                except OSError:  # pragma: no cover - 竞争删除
                    continue
    except OSError:  # pragma: no cover - 任务目录被删
        pass
    if not stamps:
        return ""
    return datetime.datetime.fromtimestamp(max(stamps)).strftime("%m-%d %H:%M")


def gate_state(task: str, config_name: str, *, root: Path = PROJECT_ROOT) -> tuple[int, int, str, str]:
    """(已放行数, 总数, 下一道门, 错误信息)。**绝不抛** —— 一张表不能被一个任务带崩。

    config 必须带对：G2(定妆) 的判据要拿 config 定位 cast lock.json，
    config 缺失会把"已批准"误判成 stale（当年就是这么误判的）。
    """
    from lvs import pipeline as pipeline_mod
    from lvs.workspace import Workspace

    config = None
    if config_name:
        path = root / config_name
        if path.is_file():
            try:
                config = Config.load(path)
            except ConfigError as exc:
                return 0, 0, "-", f"配置错误：{exc}"
    ws = Workspace.read(task, root=root)
    try:
        statuses = pipeline_mod.statuses(ws, config=config)
    except Exception as exc:  # noqa: BLE001 - 见 docstring：摘要工具必须兜住一切
        return 0, 0, "-", f"{type(exc).__name__}: {exc}"
    opened = sum(1 for s in statuses if s.open)
    if opened == len(statuses):
        return opened, len(statuses), "✔全开", ""
    nxt = next((s for s in statuses if not s.open), None)
    return opened, len(statuses), (f"{nxt.gate.id} {nxt.gate.key}" if nxt else "-"), ""


def collect(
    *,
    task: str | None = None,
    prefix: str = "UGE",
    show_all: bool = False,
    config_override: Path | None = None,
    root: Path = PROJECT_ROOT,
    use_cache: bool = True,
    max_age: float = CACHE_TTL_SECONDS,
    info: CacheInfo | None = None,
) -> list[TaskRow]:
    """采集任务摘要。`use_cache` 时命中同 key + 同指纹 + 未过期的缓存就直接返回。

    `info`（可选，出参）：告诉调用方这次是"现扫"还是"读缓存、多旧"。
    """
    chosen = select_tasks(task_dirs(root), task=task, prefix=prefix, show_all=show_all)
    key = {
        "task": (task or "").strip().lower() or None,
        "prefix": prefix,
        "all": bool(show_all),
        "config": config_override.name if config_override is not None else "",
        "tasks": [p.name for p in chosen],
    }
    if use_cache and chosen:
        cached = read_cache(root)
        if cached and cached.get("key") == key:
            age = time.time() - float(cached.get("written_at") or 0)
            stamps = {p.name: _task_stamp(p) for p in chosen}
            if age <= max_age and cached.get("stamps") == stamps:
                if info is not None:
                    info.used = True
                    info.age_s = age
                    info.generated_at = str(cached.get("generated_at") or "")
                return [TaskRow(**row) for row in cached["rows"]]

    rows: list[TaskRow] = []
    for task_dir in chosen:
        name = task_dir.name
        cfg_name = _cfg_name_for(name, config_override)
        opened, total, nxt, err = gate_state(name, cfg_name, root=root)
        rows.append(
            TaskRow(
                task=name,
                config=cfg_name or "≈",
                images=_count_images(task_dir),
                open_gates=opened,
                total_gates=total,
                next_gate=nxt,
                artifacts=_artifact_mark(task_dir),
                recent=_recent(task_dir),
                error=err,
            )
        )
    if use_cache:
        write_cache(root, key, rows, chosen)
    return rows


# ---- 结果缓存（--brief 的 5 分钟缓存） --------------------------------------


@dataclass
class CacheInfo:
    """这次结果是现扫的还是缓存来的（`--json` 会带上，人看的表格用不着）。"""

    used: bool = False
    age_s: float = 0.0
    generated_at: str = ""


#: 摘要**正文之外**还会读的目录：图片数（`_count_images`）就是数这两个里的
#: `shot-*.png`。必须单独列进来 —— 往里加一张图只刷新**它自己**的 mtime，
#: 不会刷新上层的 `assets/`（第一版漏了这个，测试 `test_a_new_image_...` 抓到）。
_STAMP_SUBDIRS: tuple[str, ...] = ("assets/local", "assets/graphic")


def _task_stamp(task_dir: Path) -> float:
    """任务目录指纹：目录自身 + 一级子项 + 图片目录的 mtime 最大值（只 stat，不读正文）。

    覆盖到"变了就会改变摘要"的所有东西：`gates.json` / `manifest.json` /
    `publish/` / `assets/`（一级子项）以及 `assets/local|graphic`（图片数）。
    新增/删除图片会刷新这两个目录的 mtime → 缓存失效 → 不会报旧张数。
    """
    targets = [task_dir, *(task_dir / sub for sub in _STAMP_SUBDIRS)]
    try:
        targets.extend(task_dir.iterdir())
    except OSError:  # pragma: no cover - 任务目录被删
        pass
    stamps: list[float] = []
    for path in targets:
        try:
            stamps.append(path.stat().st_mtime)
        except OSError:  # pragma: no cover - 竞争删除
            continue
    return max(stamps) if stamps else 0.0


def _cache_path(root: Path) -> Path:
    return root / WORK_DIRNAME / CACHE_FILENAME


def read_cache(root: Path) -> dict:
    """读缓存；文件不在 / 坏了 / 版本对不上 → 返回 {}（当没有）。**绝不抛**。"""
    try:
        raw = _cache_path(root).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {}
    if not isinstance(data.get("rows"), list):
        return {}
    return data


def write_cache(root: Path, key: dict, rows: list[TaskRow], chosen: list[Path]) -> None:
    """落盘；任何失败都吞掉（缓存是增强，绝不能把 status 弄挂）。"""
    payload = {
        "version": CACHE_VERSION,
        "written_at": time.time(),
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "key": key,
        "stamps": {p.name: _task_stamp(p) for p in chosen},
        "rows": [asdict(r) for r in rows],
    }
    try:
        path = _cache_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:  # pragma: no cover - 磁盘满 / 只读
        pass


# ---- 渲染 -------------------------------------------------------------------


def _width(text: str) -> int:
    """显示宽度：中日韩全角字符占 2 列（否则表头与数据行会错位）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


#: 列宽（按显示宽度算，见 `_pad`）。
_COLUMNS: tuple[tuple[str, int], ...] = (
    ("任务", 7),
    ("config", 22),
    ("图", 5),
    ("门禁", 6),
    ("next", 11),
    ("产物", 7),
    ("最近", 11),
)


def _row(cells: list[str]) -> str:
    return " ".join(_pad(c, w) for c, (_, w) in zip(cells, _COLUMNS)).rstrip()


def format_table(rows: list[TaskRow], *, brief: bool = True, empty: str = "",
                 note: str = "") -> str:
    """表格正文。`brief` 时**保证 ≤ 8 行**（表头 + 分隔 + ≤5 行任务 + ≤1 行尾注）。

    `empty`：一个任务都没选中时打什么（调用方知道是否带了 `--task`，提示能写得更准）。
    """
    if not rows:
        return empty or "(没有任务：.work/*/manifest.json 一个都没有 —— 先跑 `lvs init`)"
    lines = [_row([name for name, _ in _COLUMNS]), _row(["-" * w for _, w in _COLUMNS])]
    shown = rows[:MAX_BRIEF_ROWS] if brief else rows
    for r in shown:
        lines.append(
            _row(
                [
                    r.task,
                    r.config,
                    str(r.images),
                    r.gates_text,
                    r.next_gate,
                    r.artifacts,
                    r.recent,
                ]
            )
        )
    if brief:
        tail = ""
        errored = next((r for r in rows if r.error), None)
        if errored is not None:
            tail = f"! {errored.task}: {errored.error}"
        elif len(rows) > len(shown):
            tail = f"…另有 {len(rows) - len(shown)} 个任务（--all 看全部）"
        if tail:
            lines.append(tail)
        elif note:                      # 尾注位空着才用缓存提示补位（仍是 ≤8 行）
            lines.append(note)
    else:
        lines.append("")
        lines.append(f"产物列顺序：{ARTIFACT_KEYS}；Y=在，-=无")
        lines.append("门禁 = 已放行/总数；next = 下一道该过的门（`lvs gate --next` 看怎么过）；「✔全开」= 六门全过可交付")
        lines.append("config 列「≈」= 该任务无对应 config，G2(定妆) 指纹不可信（别据此判 stale）")
        lines.append("图 = assets/local + assets/graphic 的 shot-*.png 张数（不是镜数；精确镜数用 `lvs shots --index`）")
    return "\n".join(lines)


def run_command(config_path: Path | None, args) -> int:  # noqa: ANN001 - 由 cli 传入
    """`lvs status` 的入口。**不需要配置文件**（找不到也照样列，config 列打「≈」）。"""
    _fix_console()
    explicit = bool(getattr(args, "config", None))
    fresh = bool(getattr(args, "fresh", False))
    info = CacheInfo()
    rows = collect(
        task=getattr(args, "task", None),
        prefix=getattr(args, "prefix", None) or "UGE",
        show_all=bool(getattr(args, "show_all", False)),
        config_override=config_path if explicit else None,
        use_cache=not fresh,
        info=info,
    )
    if getattr(args, "json", False):
        payload = {
            "tasks": [asdict(r) for r in rows],
            "cache": {"used": info.used, "age_s": round(info.age_s, 1),
                      "generated_at": info.generated_at},
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    empty = ""
    if getattr(args, "task", None):
        empty = (f"没有任务 `{args.task}`：.work/{args.task}/manifest.json 不存在"
                 "（列全部：`lvs status --all`）")
    brief = not bool(getattr(args, "legend", False))
    # 缓存提示**只占尾注那一行**（brief 的铁律是 ≤8 行）；有错误要报时错误优先。
    if brief and info.used and not any(r.error for r in rows):
        print(format_table(rows, brief=True, empty=empty, note=(
            f"(缓存 {info.age_s:.0f}s 前的结果；重扫加 --fresh)")))
    else:
        print(format_table(rows, brief=brief, empty=empty))
    return 0
