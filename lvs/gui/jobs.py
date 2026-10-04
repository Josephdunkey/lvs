"""任务运行器（票据 36）：把 `lvs` 当子进程跑，并把日志实时喂给界面。

三条设计约束，都是前面 grill 定下来的：

1. **不 import 阶段内部** —— 只调 `python -m lvs <阶段> --task X`，
   与命令行行为逐字一致（D2 的薄壳原则）。
2. **全局串行** —— 8GB 显存下生图与 TTS 不可同时驻留（spec §11），
   所以同一时刻只允许一个阶段在跑，第二个直接拒绝，不做排队（排队只会让两个都变慢）。
3. **关掉浏览器不影响任务** —— 阶段是独立子进程，界面只是订阅者。

阶段的**磁盘状态**由 `store` 负责；这里只管**活进程**：起、看、停。
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Iterator

from lvs.config import PROJECT_ROOT
from lvs.errors import EXIT_BLOCKED, EXIT_USAGE, LvsError

MAX_LINES = 2000          # 日志环形缓冲：长任务不能无限吃内存
_HISTORY_JOBS = 40        # 保留最近几个 job 的日志供回看
# 自由文本参数：不用 shell，所以没有注入面 —— 只挡控制字符与超长内容
_UNSAFE_TEXT = re.compile(r"[\x00-\x1f\x7f]")
MAX_TEXT = 4000


class JobError(LvsError, RuntimeError):
    """参数不合法、阶段不认识等。"""

    exit_code = EXIT_USAGE


class JobBusy(JobError):
    """已有阶段在跑 —— 全局串行，不做排队。

    归类为 `BlockedError`（退出码 3）：它的解药是"**等正在跑的那个结束**"，
    不是"改参数重来" —— 与门禁的语义同类（等人/等状态，不是错）。
    """

    exit_code = EXIT_BLOCKED


# ★ 阶段的开关表**不再写在这里** —— 单一真源是 `lvs/stage.py`。
#
# 改造前这里抄了一份 `STAGE_SPEC`（每个阶段认哪些开关）+ `STAGE_POSITIONAL`，
# 与 `cli.py` 的 argparse 定义靠"人肉同步"。抄漏一个的后果是
# **界面上少一个开关**（用户以为点了，其实没传下去），而且没有任何报错。
#
# 现在从 `lvs.stage` 直接取：`Option` / `STAGE_SPEC` / `STAGE_POSITIONAL`
# 都是它的视图，`tests/test_architecture.py` 会守住"不许再出现第二份表"。
from lvs.stage import (  # noqa: E402 - 放在原位置，便于读者看到"表已外移"
    ALL as _ALL_STAGES,
    Option,
    STAGES as _PIPELINE_STAGES,
)

# 界面要能跑"阶段 + 编排入口"（run / studio），所以这里是 ALL 的视图
STAGE_SPEC: dict[str, tuple[Option, ...]] = {
    st.name: st.options for st in _ALL_STAGES
}
STAGE_POSITIONAL: dict[str, str] = {
    st.name: st.positional for st in _ALL_STAGES if st.positional
}

# 流水线阶段的顺序（界面按它排"阶段"那一栏）
PIPELINE_ORDER: tuple[str, ...] = tuple(st.name for st in _PIPELINE_STAGES)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "是")


def build_argv(
    stage: str,
    task: str,
    options: dict[str, Any] | None = None,
    *,
    config_path: Path | str | None = None,
    positional: str | None = None,
) -> list[str]:
    """拼出 `[python, -m, lvs, <stage>, --task, <task>, ...]`。

    参数一律走 argv 列表（不用 shell、不做字符串拼接），任务名同理 ——
    带分号、带引号的名字只会被原样当参数传进去。
    """
    if stage not in STAGE_SPEC:
        raise JobError(f"阶段 `{stage}` 不存在（可选：{'/'.join(sorted(STAGE_SPEC))}）")

    options = dict(options or {})
    spec = {opt.key: opt for opt in STAGE_SPEC[stage]}
    unknown = sorted(set(options) - set(spec))
    if unknown:
        raise JobError(f"阶段 `{stage}` 不认识这些参数：{unknown}")

    argv = [sys.executable, "-m", "lvs", stage, "--task", str(task)]
    if config_path:
        argv += ["--config", str(config_path)]

    for key, opt in spec.items():
        if key not in options:
            continue
        value = options[key]
        if opt.kind == "bool":
            if _truthy(value):
                argv.append(opt.flag)
            continue
        text = str(value).strip()
        if not text:
            continue
        if opt.kind == "choice":
            if text not in opt.choices:
                raise JobError(f"`{opt.flag}` 只接受：{'/'.join(opt.choices)}（收到 {text!r}）")
        elif _UNSAFE_TEXT.search(text) or len(text) > MAX_TEXT:
            raise JobError(f"`{opt.flag}` 的值含控制字符或过长（{len(text)} 字）")
        argv += [opt.flag, text]

    if positional:
        if stage not in STAGE_POSITIONAL:
            raise JobError(f"阶段 `{stage}` 不接受位置参数")
        argv.append(str(positional))
    return argv


class Job:
    """一次阶段运行。日志有界，订阅者各自收一份。"""

    def __init__(self, job_id: str, task: str, stage: str, argv: list[str]) -> None:
        self.id = job_id
        self.task = task
        self.stage = stage
        self.argv = list(argv)
        self.started = time.time()
        self.ended: float | None = None
        self.returncode: int | None = None
        self.error: str = ""

        self._lines: deque[tuple[int, str]] = deque(maxlen=MAX_LINES)
        self._seq = 0
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._cancelled = False
        self.sink: Path | None = None      # 日志落盘位置（票 38 / S5）

    # ---- 状态 -------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.returncode is None and not self._cancelled

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def elapsed(self) -> float:
        return (self.ended or time.time()) - self.started

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def lines(self) -> list[str]:
        return [text for _, text in self._lines]

    def tail(self, n: int = 200) -> list[str]:
        return self.lines()[-n:]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "task": self.task, "stage": self.stage,
            "running": self.running, "returncode": self.returncode,
            "ok": self.ok, "elapsed": round(self.elapsed, 1), "error": self.error,
            "lines": len(self._lines),
            "log_path": str(self.sink) if self.sink else "",
        }

    # ---- 日志分发 ---------------------------------------------------------

    def emit(self, text: str) -> None:
        with self._lock:
            seq = self._seq
            self._seq += 1
            self._lines.append((seq, text))
            subs = list(self._subs)
        for q in subs:
            q.put((seq, text))
        if self.sink is not None:
            try:
                with self.sink.open("a", encoding="utf-8") as fh:
                    fh.write(text + "\n")
            except OSError:
                self.sink = None       # 写不进就放弃落盘，别让日志拖垮任务

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def history(self) -> list[tuple[int, str]]:
        with self._lock:
            return list(self._lines)

    def _finish(self, returncode: int) -> None:
        self.returncode = returncode
        self.ended = time.time()
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            q.put((None, None))       # 结束哨兵
        self._done.set()

    def cancel(self) -> bool:
        proc = self._proc
        if proc is None or self.returncode is not None:
            return False
        self._cancelled = True
        self.emit("[gui] 收到停止请求，正在结束子进程…")
        try:
            if os.name == "nt":
                # venv 的 python.exe 是个 shim，杀它可能留下孤儿 worker → 连子树一起杀
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, text=True, errors="ignore",
                )
            else:
                proc.terminate()
        except OSError as exc:
            self.emit(f"[gui] 停止失败：{exc}")
            return False
        return True


class JobRegistry:
    """全局唯一的运行器：同一时刻只允许一个阶段在跑。"""

    def __init__(self, root: Path = PROJECT_ROOT, config_path: Path | str | None = None) -> None:
        self.root = Path(root)
        self.config_path = config_path
        self._current: Job | None = None
        self._history: deque[Job] = deque(maxlen=_HISTORY_JOBS)
        self._lock = threading.Lock()

    # ---- 查询 -------------------------------------------------------------

    def current(self) -> Job | None:
        with self._lock:
            job = self._current
        if job is not None and job.running:
            return job
        return None

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            if self._current is not None and self._current.id == job_id:
                return self._current
            for job in self._history:
                if job.id == job_id:
                    return job
        return None

    def recent(self) -> list[Job]:
        with self._lock:
            return list(self._history)

    # ---- 启动 / 停止 -------------------------------------------------------

    def start(
        self,
        task: str,
        stage: str,
        options: dict[str, Any] | None = None,
        *,
        positional: str | None = None,
        command: list[str] | None = None,
    ) -> Job:
        """起一个阶段。`command` 只给测试用（正常路径由 `build_argv` 拼）。"""
        argv = list(command) if command is not None else build_argv(
            stage, task, options, config_path=self.config_path, positional=positional
        )
        with self._lock:
            live = self._current
            if live is not None and live.running:
                raise JobBusy(
                    f"已有阶段在跑（{live.task} / {live.stage}）。"
                    "显存只够串行，请先等它结束或点「停止」。"
                )
            job = Job(uuid.uuid4().hex[:12], task, stage, argv)
            self._current = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def stop(self) -> bool:
        job = self.current()
        return job.cancel() if job is not None else False

    # ---- 内部 -------------------------------------------------------------

    def _log_path(self, job: Job) -> Path | None:
        """这次运行的日志落盘到 `.work/<task>/logs/`（票 38 / S5）。

        界面里的日志只在内存，服务一重启就没了；落盘后还能从
        `/media/<task>/logs/<file>` 翻回来。任务名先过一遍朴素校验，防越界。
        """
        name = job.task or ""
        if not name or "/" in name or "\\" in name or ".." in name:
            return None
        d = self.root / ".work" / name / "logs"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(job.started))
        return d / f"{stamp}-{job.stage}-{job.id}.log"

    def _run(self, job: Job) -> None:
        job.sink = self._log_path(job)
        job.emit(f"[gui] $ {' '.join(job.argv)}")
        if job.sink is not None:
            job.emit(f"[gui] 日志落盘：{job.sink}")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        # 子进程 stdout 是管道（非 tty），Python 默认块缓冲 → 日志会一段一段地到。
        # 置 PYTHONUNBUFFERED=1 让它逐行 flush，界面的实时日志才真的"实时"。
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        try:
            proc = subprocess.Popen(
                job.argv, cwd=str(self.root),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=env, creationflags=flags,
            )
        except OSError as exc:
            job.error = str(exc)
            job.emit(f"[gui] 启动失败：{exc}")
            self._retire(job, -1)
            return

        job._proc = proc
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                job.emit(line.rstrip("\r\n"))
        except (OSError, ValueError) as exc:      # 进程被杀时管道可能直接断
            job.emit(f"[gui] 日志读取中断：{exc}")
        rc = proc.wait()
        self._retire(job, rc)

    def _retire(self, job: Job, returncode: int) -> None:
        if job.error:
            job.emit(f"[gui] 阶段失败：{job.error}")
        elif returncode == 0:
            job.emit("[gui] 阶段结束（成功）")
        else:
            job.emit(f"[gui] 阶段结束（退出码 {returncode}）")
        job._finish(returncode)
        with self._lock:
            if self._current is job:
                self._current = None
            self._history.append(job)

    # ---- 给 SSE 用 ---------------------------------------------------------

    def stream(self, job_id: str, timeout: float = 1800.0) -> Iterator[str]:
        """先补历史、再接实时；不重不漏（靠行序号去重）。"""
        job = self.get(job_id)
        if job is None:
            return
        q = job.subscribe()
        deadline = time.time() + timeout
        try:
            last = -1
            for seq, text in job.history():
                last = seq
                yield text
            while time.time() < deadline:
                try:
                    seq, text = q.get(timeout=1.0)
                except queue.Empty:
                    if not job.running:
                        break
                    continue
                if seq is None:
                    break
                if seq <= last:
                    continue
                last = seq
                yield text
        finally:
            job.unsubscribe(q)
