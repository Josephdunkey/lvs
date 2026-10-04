"""任务目录（`.work/<task>/`）与产物清单（`manifest.json`）。

设计要点（见 spec §7.1）：**文件即状态**。每个阶段把自己的产物与完成情况写进
`manifest.json`，阶段入口先读它判断能否跳过 —— 这是"断点续跑"的基础。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from lvs import artifact
from lvs.config import PROJECT_ROOT

WORK_DIRNAME = ".work"
MANIFEST_NAME = "manifest.json"

#: manifest 的结构版本。
#:
#: | 版本 | 变化 |
#: |---|---|
#: | 1 | 原始（无 `version` 字段 —— 读到时按 1 处理） |
#: | 2 | 顶层加 `version`；阶段条目加 `inputs`（产物血缘） |
#:
#: **为什么现在补**：改结构而没有版本号，只能靠"字段不存在就兜默认值"来兼容 ——
#: 这在**字段语义变了**（而不是新增）的时候会静默出错：老数据被按新含义解读，
#: 不报错、只是判错。有了版本号，将来能明确说"这是老格式，要迁移"。
MANIFEST_VERSION = 2

#: `mark_stage_progress` 的写盘节流窗口（秒）。
#:
#: 逐镜循环每镜调一次 `mark_stage_progress`，而 manifest 是整文件重写。
#: 原子写保证不写坏，但每镜 fsync 一次是白费 IO —— 窗口内的后续调用会被跳过，
#: 直到下一次"完成数 +1"或"百分比跳格"或超过这个窗口。
#: 0.5s 的取舍：看板刷新频率远低于此，而 IO 降到 1/几十。
_PROGRESS_MIN_INTERVAL = 0.5

# 任务目录下的固定子目录（spec §6）
SUBDIRS = (
    "audio",
    "assets/library",
    "assets/pexels",
    "assets/local",
    "assets/graphic",
    "shots-motion",
    "logs",
)

# 逐镜素材的分支，落点是 `assets/<branch>/shot-NNN.*`。
# 只在这里定义一次 —— 之前 assets / studio / gui.store 各抄了一份，顺序还不一样（票 40）。
ASSET_BRANCHES = ("pexels", "local", "graphic", "library")


def shot_name(sid: int) -> str:
    """镜号 → 逐镜产物的文件名主干。"""
    return f"shot-{int(sid):03d}"


def slugify(name: str) -> str:
    """把任意字符串变成安全的目录名：保留中英文与数字，其余转 `-`。"""
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "-", name.strip(), flags=re.UNICODE)
    cleaned = cleaned.strip("-")
    return cleaned or "default"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def stage_status(failures: int) -> str:
    """阶段状态：无失败即 `done`，有任一失败即 `partial`（须可被重跑补缺）。"""
    return "done" if not failures else "partial"


def write_shots_json(path: Path, data: dict[str, Any]) -> None:
    """`shots.json` 的**唯一**写法（UTF-8 不转义 + 2 空格缩进 + 尾换行）。

    `Workspace.write_shots` 与界面（它拿到的是任务目录而非 Workspace）都走这里，
    免得同一个文件有第二种格式 —— 它同时是给人手改、给界面读的真相源（D14）。
    """
    artifact.atomic_write_text(
        path, json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    )


@dataclass
class Workspace:
    """某一次运行的工作目录。"""

    task: str
    root: Path = PROJECT_ROOT
    manifest: dict[str, Any] = field(default_factory=dict)

    #: `mark_stage_progress` 的写盘节流状态（见 `_PROGRESS_MIN_INTERVAL`）。
    #: 放在实例上而非类上，免得两个任务互相影响。
    _progress_last_write: float = field(default=0.0, repr=False, compare=False)
    _progress_last_pct: int = field(default=-1, repr=False, compare=False)

    # ---- 路径 -------------------------------------------------------------

    @property
    def dir(self) -> Path:
        return self.root / WORK_DIRNAME / self.task

    @property
    def manifest_path(self) -> Path:
        return self.dir / MANIFEST_NAME

    def path(self, *parts: str) -> Path:
        return self.dir.joinpath(*parts)

    def load_shots(self, *, error: type[Exception] = RuntimeError) -> dict[str, Any]:
        """读取本任务的 `shots.json`。

        缺失时抛 `error`（调用方传入自己的异常类型，好让各阶段给出各自的提示）。
        抽到此处是为了消除 assets / tts 里两份逐字相同的读取逻辑。
        """
        path = self.path("shots.json")
        if not path.is_file():
            raise error(f"未找到 {path}。请先运行：lvs shots --task {self.task}")
        return json.loads(path.read_text(encoding="utf-8"))

    def try_load_shots(self) -> dict[str, Any]:
        """读取本任务的 `shots.json`；**缺失或坏掉都当空 dict**，绝不抛。

        与 `load_shots` 只差缺失时的行为：那个抛异常（阶段要求前置产物必须在），
        这个返回空（调用方只是想知道"上次记了什么"，没有就是没有）。
        抽到此处同样是为了消重复 —— `shots` 与 `studio` 各有一份逐字相同的实现。
        """
        path = self.path("shots.json")
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def write_shots(self, data: dict[str, Any]) -> Path:
        """把 `shots.json` 写回磁盘（统一格式：UTF-8 不转义 + 2 空格缩进 + 尾换行）。

        多处曾各自拼同一串 `json.dumps(..., ensure_ascii=False, indent=2) + "\\n"`，
        抽到此处归一，免得各阶段写出的格式悄悄漂移。
        """
        path = self.path("shots.json")
        write_shots_json(path, data)
        return path

    def clear_shot_artifacts(self, sid: int) -> int:
        """删掉该镜的已有素材与已合成片段，返回删了几个文件。

        **只删文件、不碰 `shots.json`** —— 状态与"钉死"由调用方决定。
        因此 studio 的「重做这一镜」与 GUI 的「换一张我自己选的图」能共用这一份，
        而不是各写一遍（票 40）。

        片段为什么要删：片段缓存**只看时长**，图换了但时长没变会被原样复用（票 30）。
        """
        name = shot_name(sid)
        removed = 0
        for branch in ASSET_BRANCHES:
            directory = self.path("assets", branch)
            if not directory.is_dir():
                continue
            for path in directory.glob(f"{name}.*"):
                if path.is_file():
                    path.unlink()
                    removed += 1
        segment = self.path("segments", f"{name}.mp4")
        if segment.is_file():
            segment.unlink()
            removed += 1
        return removed

    # ---- 生命周期 ---------------------------------------------------------

    def ensure(self) -> "Workspace":
        """创建任务目录骨架，并初始化 `manifest.json`（幂等）。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        for sub in SUBDIRS:
            (self.dir / sub).mkdir(parents=True, exist_ok=True)

        if self.manifest_path.is_file():
            self.manifest = self._read_manifest()
        else:
            self.manifest = {
                "version": MANIFEST_VERSION,
                "task": self.task,
                "created_at": _now(),
                "stages": {},
            }
            self._write_manifest()
        return self

    @classmethod
    def read(cls, task: str, root: Path = PROJECT_ROOT) -> "Workspace":
        """**只读**打开：加载已存在的 `manifest.json`，但绝不创建任何目录/文件。

        给 `lvs board` 这类"只看不写"的命令用 —— 它们不该因看一眼状态就凭空造出
        一个任务目录（票 23 验收项 / spec §16）。产物不存在时就保持空 manifest。
        """
        ws = cls(task=task, root=root)
        if ws.manifest_path.is_file():
            ws.manifest = ws._read_manifest()
        return ws

    def _read_manifest(self) -> dict[str, Any]:
        try:
            with open(self.manifest_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            # 清单损坏不该让整条流水线崩掉 —— 重建即可
            return {"version": MANIFEST_VERSION, "task": self.task, "created_at": _now(), "stages": {}}

        if not isinstance(data, dict):
            return {"version": MANIFEST_VERSION, "task": self.task, "created_at": _now(), "stages": {}}

        # 不带 `version` 的老清单 = 版本 1（原始格式），行为与从前一致。
        # 若版本**高于**本代码已知的：明确提示，但不崩 ——
        # 新版工具写的清单被老版工具读到，最可能的现象是"某些新字段被忽略"，
        # 静默继续比报错更危险，所以这里要说一声。
        seen = artifact.safe_int(data.get("version") or 1, 1)
        if seen > MANIFEST_VERSION:
            print(
                f"[清单] {self.manifest_path.name} 的结构版本是 {seen}，"
                f"本工具只认到 {MANIFEST_VERSION} —— 可能有字段读不懂。"
                "建议升级 `lvs` 后再操作这个任务。"
            )
        data.setdefault("version", seen)
        return data

    def manifest_version(self) -> int:
        """本任务清单的结构版本（老清单无字段时按 1 计）。"""
        return artifact.safe_int(self.manifest.get("version") or 1, 1)

    def _write_manifest(self) -> None:
        """**原子**写回清单。

        ★ 为什么必须原子：`write_text` 是「先截断、再写」—— 在截断与写完之间
        （进程被 Ctrl-C / 断电 / 崩溃），盘上留下的是一个**被截断的 JSON**。
        而 `_read_manifest` 遇到坏 JSON 会**静默重建成空**（`stages: {}`）——
        于是**断点续跑的全部记录被无声丢弃**，下次从零重跑几百镜。

        这个窗口在串行下只是小概率（刚好撞上写盘那一刻），但**每镜写一次**
        （654 镜 = 654 次整文件重写）把它放大；将来若并行，会变成必然。

        做法（临时文件 → fsync → os.replace）已收敛到 `artifact.atomic_write_text`，
        与 `shots.json`、门禁账本共用一份实现 —— 免得三处各自演化。
        这里吞掉 `OSError` 是刻意的：写盘失败（只读盘 / 磁盘满）不该让流水线崩，
        与「清单损坏不崩」的既有取舍一致。
        """
        payload = json.dumps(self.manifest, ensure_ascii=False, indent=2) + "\n"
        try:
            artifact.atomic_write_text(self.manifest_path, payload)
        except OSError:
            pass   # 临时文件已由 atomic_write_text 清理；不抛见上

    # ---- 阶段状态 ---------------------------------------------------------

    def is_stage_done(self, stage: str, outputs: list[Path] | None = None) -> bool:
        """阶段已完成 **且** 产物齐全才为 True（产物被删则应重跑）。

        `outputs` 省略时用 manifest 里记录的 `outputs[]` —— 这是断点续跑的关键：
        删掉任一记录在案的产物，该阶段即判定为未完成。状态为 `partial`（有失败镜）
        也一律视作未完成，好让 `lvs run` 重跑补齐。
        """
        if outputs is not None:
            # 显式给了 outputs 就按它判（老调用方的语义）
            entry = self.manifest.get("stages", {}).get(stage)
            if not entry or entry.get("status") != "done":
                return False
            return all(p.exists() for p in outputs)
        return not self.stale_reason(stage)

    def mark_stage(
        self,
        stage: str,
        outputs: list[Path] | None = None,
        status: str = "done",
        inputs: list[Path] | None = None,
        **extra: Any,
    ) -> None:
        """记录一个阶段完成。`inputs` 是**本次消费的上游产物**，用于事后判失效。

        ★ 为什么必须记 inputs：只记 outputs 的话，"上游改了"是发现不了的 ——
        实测过的静默 bug：重新 parse 之后 `shots` 仍被判为已完成，
        于是拿着**旧解析结果**继续往下跑，全程不报错。
        记下上游产物当时的签名，`is_stage_done` 才能回答"它还是最新的吗"。
        """
        entry: dict[str, Any] = {"status": status, "at": _now()}
        if outputs:
            entry["outputs"] = [str(p) for p in outputs]
        if inputs is not None:
            entry["inputs"] = [r.to_dict() for r in artifact.refs(inputs)]
        entry.update(extra)
        self.manifest.setdefault("stages", {})[stage] = entry
        self._write_manifest()

    def record_lineage(self, stage: str, inputs: list[Path]) -> None:
        """**只补血缘**，不动 outputs/status —— 给 `stage.run_stage` 统一调用。

        阶段模块自己 `mark_stage` 时不知道编排层认定的上游是谁；
        由阶段表（`lvs/stage.py`）统一补写，保证"上游"只有一处定义。
        """
        entry = self.manifest.setdefault("stages", {}).get(stage)
        if not entry:
            return
        entry["inputs"] = [r.to_dict() for r in artifact.refs(inputs)]
        self._write_manifest()

    def stale_reason(self, stage: str) -> str:
        """该阶段为什么该重跑（空串 = 产物齐全且上游未变）。"""
        entry = self.manifest.get("stages", {}).get(stage)
        if not entry or entry.get("status") != "done":
            return "阶段未完成"
        missing = [p for p in entry.get("outputs", []) if not Path(p).exists()]
        if missing:
            return f"产物缺失：{Path(missing[0]).name}"
        recorded = [artifact.ArtifactRef.from_dict(d) for d in entry.get("inputs", [])]
        changed = [r for r in recorded if r.path and not r.unchanged()]
        if changed:
            return f"上游产物已变：{Path(changed[0].path).name}"
        return ""

    def mark_stage_progress(
        self,
        stage: str,
        *,
        done: int | None = None,
        skipped: int | None = None,
        failed: int | None = None,
        total: int | None = None,
    ) -> None:
        """把"还在跑"的进度**并进** manifest（不覆盖已有字段）。

        长阶段（素材 654 镜 ≈ 二十分钟）跑到一半被 Ctrl-C / 断电 / 崩掉时，盘上要留下
        一条 `partial` 记录 —— 否则重开界面、看 `lvs board` 都只能从零看起（票 26）。

        `status` 一律写 `partial`：**只有真跑完才允许是 `done`**（D25）。
        收尾时由 `mark_stage` 写正确状态（可能是 done，也可能是"有失败"的 partial）。

        计数**具名**、且只在非 None 时写：`done` 是"已完成数"的唯一名字
        （素材与配音同用，免得看板去兜两个名字）；拼错键名会直接 `TypeError`，
        而不是被静默并进 JSON 后再也读不出来。

        ★ **写回节流**：逐镜循环里这个方法**每镜调一次**，而 manifest 是**整文件重写**
        （654 镜 = 654 次）。原子写保证了不写坏，但没必要为每一镜都做一次 fsync ——
        所以这里按"**百分比跳格** 或 **距上次写盘超过 `_PROGRESS_MIN_INTERVAL` 秒**"才落盘。

        ★ **收尾不需要额外 flush**：节流会"欠"几次写盘，但循环结束后那条
        `mark_stage` 是**不节流**的（见它末尾的 `_write_manifest`），
        最终状态与计数一定落全。所以这里欠的只是"过程中的中间值"——
        而它本来就是给进度显示用的，掉了也不影响续跑判据。

        （早先这里配过一个 `flush_manifest()`，但生产代码从没调用它 ——
        而它的文档还写着"收尾必须调"。那是**文档与实现不符**，已删除。）
        """
        entry = self.manifest.setdefault("stages", {}).setdefault(stage, {})
        entry["status"] = "partial"
        entry["at"] = _now()
        for key, val in (("done", done), ("skipped", skipped), ("failed", failed), ("total", total)):
            if val is not None:
                entry[key] = val

        now = time.monotonic()
        total_v = entry.get("total") or 0
        pct = int(100 * (entry.get("done") or 0) / total_v) if total_v else -1
        # 落盘条件（任一满足）：
        #   · 百分比跳了一格 —— 看板看得见的里程碑
        #   · 距上次写盘超过时间窗口 —— 兜底（`total` 未知时 pct 恒为 -1，靠它）
        # ★ **刻意不看"完成数变了"**：done 每镜都 +1，用它会退化成每镜都写
        #   （第一版就是这么写的，300 次调用写了 300 次，节流形同虚设）。
        if pct != self._progress_last_pct or (now - self._progress_last_write) >= _PROGRESS_MIN_INTERVAL:
            self._progress_last_pct = pct
            self._progress_last_write = now
            self._write_manifest()

    def record_timing(self, stage: str, seconds: float) -> None:
        """把这一次的**耗时**记进 manifest。

        原先 `StageResult.seconds` 只打在 stdout —— 跑完就没了，
        于是"这步上次跑了多久"下次无从回答（而长跑的排期全靠这个数）。
        记在 manifest 里：`lvs board` 与将来的 agent 都能读到。

        ★ 用 `last_seconds` 而不是覆盖语义字段：它是"上一次的实际值"，
        与 `status` / `outputs` 属于不同维度，名字上也要区分开。
        """
        entry = self.manifest.setdefault("stages", {}).get(stage)
        if not entry:
            return
        try:
            entry["last_seconds"] = round(float(seconds), 1)
            total = float(entry.get("total_seconds") or 0.0) + float(seconds)
            entry["total_seconds"] = round(total, 1)
        except (TypeError, ValueError):
            return
        self._write_manifest()
