"""阶段注册表 —— 流水线"阶段"这个概念的**单一真源**。

## 为什么需要它（这是本项目的最大架构缺陷）

改造之前，"有哪些阶段、每个阶段认哪些参数、产物是什么、被哪道门守"这件事
**散落在五处**，而且每处都只写自己关心的那部分：

| 位置 | 写的是 |
|---|---|
| `cli.py` | 每个阶段的 argparse 参数与位置参数 |
| `gui/jobs.py` | `STAGE_SPEC` + `STAGE_POSITIONAL`（**把参数表又抄了一遍**） |
| `run.py` | 硬编码的阶段顺序 + **伪造 `argparse.Namespace`** 去调各阶段 |
| `studio.py` | 再伪造一遍 `Namespace` |
| `pipeline.py` | `GATE.stage` 字符串 |
| `workspace.py` | `is_stage_done(stage)` 的字符串 |

后果不是"不好看"，而是**静默失效**：
改一个 CLI 参数名，`run.py` 里 `getattr(args, "旧名", None)` 会拿到 `None`，
于是那个开关**被悄悄忽略** —— 不报错、不警告，跑完才发现"我明明加了 `--no-llm`"。

## 本模块提供什么

- `Stage`：阶段的名字 / 中文标题 / 位置参数 / 开关表 / 入口门禁 / 产物 / 依赖
- `STAGES` / `ORDER` / `BY_NAME`：表
- `StageContext` / `StageResult`：统一的执行上下文与结果
- `run_stage()`：**唯一**的执行入口（计时 + 门禁 + 调用 + 产物登记都在这）
- `collect_options()`：从任意 args（Namespace 或 dict）里取该阶段认得的开关

## 与既有实现的关系（渐进式，不是大爆炸重写）

各阶段模块里既有的 `run_command(config, ws, args)` **保持不动** ——
它有 800+ 个测试在守，动它是纯风险。本模块用一个 `Stage.impl` 字段指过去，
在**一个地方**做 `options → Namespace` 的适配（`as_namespace()`）。

也就是说：过渡期的适配层从"两处各写一遍"收敛成"**一处**"，
而"单一真源"这件事从这里开始。下一步（已写进架构审查的遗留清单）
是把各阶段的 `run_command` 拆成 `核心函数(显式参数)` + `CLI 适配器`，
那时 `as_namespace()` 就可以删掉。

## 分层约定

`stage.py` **只依赖标准库**：它要能被 `cli` / `gui` / `run` / `studio` 任意一方 import
而不产生环。所以阶段的实现用**字符串**（`"lvs.shots:run_command"`）延迟解析。
"""

from __future__ import annotations

import argparse
import importlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from lvs import errors  # ★ 唯一允许的 lvs 依赖：`errors` 是**零依赖叶子**，
                        # 引它不会成环，也让退出码语义只有一处定义。
                        # `tests/test_architecture.py` 会验证它确实还是叶子。


# ---- 开关 ------------------------------------------------------------------


@dataclass(frozen=True)
class Option:
    """一个阶段可传的开关。**一份定义，三处使用**：CLI 帮助 / 界面表单 / 运行器拼 argv。"""

    key: str                                  # 对应 `args.<key>`（下划线形式）
    flag: str                                 # 命令行写法 `--no-llm`
    label: str                                # 人看的名字
    kind: str = "bool"                        # bool | choice | text
    choices: tuple[str, ...] = ()
    default: str = ""
    hint: str = ""

    def cli_names(self) -> list[str]:
        """这个开关在命令行上接受的所有名字（含下划线变体，供契约测试用）。"""
        return [self.flag, self.flag.replace("-", "_")]


# ---- 阶段 ------------------------------------------------------------------


@dataclass(frozen=True)
class Stage:
    """流水线的一个阶段（或一个编排入口）。

    ★ 判据：**换一个阶段要改的东西，应该只在这一个 dataclass 里写一次。**
    """

    name: str                                 # 任务名/命令行名，如 "shots"
    title: str                                # 中文标题，如 "拆镜与提示词"
    short: str = ""                           # 界面/看板用的 2–4 字短名（空则退回 title）
    impl: str = ""                            # `"lvs.shots:run_command"`；driver 可为空
    kind: str = "stage"                       # stage（流水线阶段）| driver（编排入口）
    positional: str = ""                      # 位置参数的 metavar，如 "拍摄稿.md"
    positional_required: bool = False
    options: tuple[Option, ...] = ()
    gates: tuple[str, ...] = ()               # 入口门禁 id（G0..G5）；空 = 不受门禁约束。
                                              # ★ 是**元组**不是一个值：`assets` 被两道门守
                                              # （G1 分镜/提示词 + G2 定妆），单值表达不了 ——
                                              # 写单值的时候测试当场抓到了这个不一致。
    artifacts: tuple[str, ...] = ()           # 本阶段的产物（workspace 相对路径）
    needs: tuple[str, ...] = ()               # 必须先完成的阶段
    memo: str = ""                            # 一句话职责（给人看）
    gpu: str = ""                             # 显存相关：imagegen | tts | 空

    def option(self, key: str) -> Option | None:
        return next((o for o in self.options if o.key == key), None)

    def option_keys(self) -> tuple[str, ...]:
        return tuple(o.key for o in self.options)


# `--force` 五个阶段都认，单独拎出来免得抄五遍
_FORCE = Option("force", "--force", "强制重做（丢弃已有产物）")
_SOURCE = Option(
    "source", "--source", "实拍镜来源", "choice", ("auto", "pexels", "local", "library"), "auto",
    "auto=逐镜判定 / pexels=下载 / local=本地生图 / library=本地素材库（未命中回退生图）",
)
_VISUAL = Option(
    "visual", "--visual", "画面模式", "choice", ("graphic", "photo"), "graphic",
    "graphic=图文图表感 / photo=实拍剧照感",
)
_STYLE = Option("style", "--style", "画面风格", "text", (), "", "`lvs styles` 列全部预设")
_STYLE_FILE = Option("style_file", "--style-file", "风格预设文件", "text", (), "",
                     "临时试风格用（TOML）")


# ★ 顺序即流水线顺序。`run.py` 直接照它走。
STAGES: tuple[Stage, ...] = (
    Stage(
        "parse", "解析拍摄稿", short="解析", impl="lvs.parse:run_command",
        positional="拍摄稿.md", positional_required=False,
        options=(_FORCE,), artifacts=("parse.json",),
        memo="把拍摄稿拆成段落 / 旁白 / 画面位。免费且可逆，不受门禁约束。",
    ),
    Stage(
        "shots", "拆镜与提示词", short="拆镜", impl="lvs.shots:run_command",
        options=(_FORCE,
                 Option("no_llm", "--no-llm", "不调 LLM（启发式拆镜）"),
                 _VISUAL, _SOURCE, _STYLE, _STYLE_FILE),
        gates=("G0",), artifacts=("shots.json",), needs=("parse",),
        memo="逐句分镜 + 每镜生图提示词与素材来源判定。花 LLM 钱。",
    ),
    Stage(
        "assets", "素材获取与生图", short="素材", impl="lvs.assets:run_command",
        options=(_FORCE,
                 Option("no_library", "--no-library", "本次不翻本地素材库"),
                 Option("only", "--only", "只取这些来源", "text", (), "",
                        "逗号分隔：pexels,local,graphic,library（留空=全部）"),
                 _SOURCE,
                 Option("no_cast_gate", "--no-cast-gate", "跳过定妆闸门（纯空镜集用）")),
        gates=("G1", "G2"), artifacts=("assets",), needs=("shots",), gpu="imagegen",
        memo="本地素材库 → Pexels / 本地生图。花 GPU 小时，是最贵的一步。",
    ),
    Stage(
        "voice", "配音与字幕", short="配音", impl="lvs.tts:run_command",
        options=(_FORCE,),
        gates=("G3",), artifacts=("audio", "subtitle.srt"), needs=("assets",), gpu="tts",
        memo="逐镜配音 + 字幕。与本地生图不能同时驻留显存（8GB）。",
    ),
    Stage(
        "build", "合成成片", short="合成", impl="lvs.build:run_command",
        options=(_FORCE,),
        gates=("G4",), artifacts=("final.mp4",), needs=("voice",),
        memo="ffmpeg 合成：画面轨 + 音轨 + 烧录字幕 → final.mp4。",
    ),
)

# 编排入口（不是流水线阶段，但界面上是一等公民）
DRIVERS: tuple[Stage, ...] = (
    Stage(
        "run", "一键（走到下一道门）", short="一键", impl="lvs.run:run_command", kind="driver",
        positional="拍摄稿.md",
        options=(_FORCE, _SOURCE, _VISUAL, _STYLE, _STYLE_FILE,
                 Option("no_library", "--no-library", "本次不翻本地素材库"),
                 Option("no_cast_gate", "--no-cast-gate", "跳过定妆闸门"),
                 Option("no_llm", "--no-llm", "拆镜不调用 LLM"),
                 Option("skip_gates", "--skip-gates", "本次不检查流水线门禁"),
                 Option("keep_going", "--keep-going", "硬失败也继续（逐镜部分失败本就继续；门禁始终会停）"),
                 Option("demo", "--demo", "走路骨架（3 个硬编码分镜）")),
        memo="parse → shots → assets → voice → build，执行到下一道门禁为止。",
    ),
    Stage(
        "studio", "手动向导（逐步确认）", short="向导", impl="lvs.studio:run_command", kind="driver",
        options=(_SOURCE,
                 Option("redo", "--redo", "只重做这些镜", "text", (), "", "如 1-10,15"),
                 Option("action", "--action", "收窄素材来源", "text"),
                 Option("prompt", "--prompt", "替换该镜提示词", "text"),
                 Option("seed", "--seed", "指定 seed（不给则 +1 重抽）", "text"),
                 Option("redo_shots", "--redo-shots", "强制重做拆镜"),
                 Option("stop", "--stop", "素材出完就停"),
                 Option("yes", "--yes", "所有提问取默认值")),
        memo="素材段逐步确认（取素材 → 出完再问继续/改图）。",
    ),
    Stage(
        "publish", "投稿物料（B站 / 抖音）", short="投稿", impl="lvs.publish:run_command", kind="driver",
        options=(
            Option("out", "--out", "投稿目录（可拷贝一份留档）", "text", (), "",
                   "通常是 <素材库>/08-投稿；相对路径按 [paths].lib 解析"),
            Option("base", "--base", "封面底图", "text", (), "", "不填则自动挑任务里最后一张画面"),
            Option("lines", "--lines", "封面三行字", "text", (), "", "用 | 分隔；默认取拍摄稿【封面文案】"),
            Option("llm", "--llm", "用 LLM 润色标题/简介（需 key，失败自动退回规则文案）"),
        ),
        memo="封面（带字）+ 标题 + 简介 + 标签 → .work/<任务>/publish/。B站横版 + 抖音竖版。",
    ),
)

ALL: tuple[Stage, ...] = STAGES + DRIVERS
BY_NAME: dict[str, Stage] = {s.name: s for s in ALL}
# ★ 流水线顺序的唯一来源。`run.py` 不再自己维护一份。
ORDER: tuple[str, ...] = tuple(s.name for s in STAGES)
STAGE_NAMES: frozenset[str] = frozenset(ORDER)

# 阶段名 → 守它的门禁 id 们（`pipeline.py` 的 GATES 是这套绑定的另一半）。
# `tests/test_architecture.py` 会断言两边一致 —— 两边各说各的正是要防的事。
GATES_BY_STAGE: dict[str, tuple[str, ...]] = {s.name: s.gates for s in STAGES if s.gates}


def get(name: str) -> Stage:
    """按名取阶段；不认识就报错（并列出可选项，别让人猜）。"""
    stage = BY_NAME.get(str(name).strip())
    if stage is None:
        raise KeyError(f"未知阶段 {name!r}。可用：{', '.join(s.name for s in ALL)}")
    return stage


def resolve_impl(impl: str):
    """`"lvs.shots:run_command"` → 函数对象。延迟导入，保持本模块零依赖。"""
    if not impl:
        raise KeyError("这个阶段没有实现（driver 占位）")
    module_path, _, attr = impl.partition(":")
    module = importlib.import_module(module_path)
    try:
        return getattr(module, attr)
    except AttributeError as exc:  # pragma: no cover - 配置写错
        raise KeyError(f"{module_path} 里没有 {attr}") from exc


# ---- 执行 ------------------------------------------------------------------


@dataclass
class StageContext:
    """一次阶段执行需要的一切。

    刻意**不用 `argparse.Namespace`** —— 那是 CLI 的形状，不该渗进核心。
    编排器与界面都构造这个。
    """

    config: Any
    ws: Any
    options: dict[str, Any] = field(default_factory=dict)
    force: bool = False

    def as_namespace(self, task: str) -> argparse.Namespace:
        """★ 过渡期适配器：既有的 `run_command(config, ws, args)` 要 Namespace。

        **只有这一个地方**做这件事。等各阶段把核心函数拆出来（见架构审查遗留清单），
        这个函数就该消失 —— 它存在的意义是"让五处伪造收敛成一处"。
        """
        return argparse.Namespace(task=task, config=None, **self.options)


@dataclass
class StageResult:
    name: str
    code: int = 0
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def blocked(self) -> bool:
        """被门禁/守卫拦住 —— **不是失败**，是等人做决定。

        与失败分开很重要：`--keep-going` 该跳过"这一阶段出错了"，
        但**不该**跳过"这一步需要人审" —— 后者跳过去就等于放弃门禁。
        """
        return self.code == errors.EXIT_BLOCKED

    @property
    def partial(self) -> bool:
        """跑完了，但**有失败件**（逐镜隔离：某几镜没取到素材 / 没合出音）。

        这也是"正常流程的一部分" —— 逐镜隔离的本意就是"标记该镜、其余继续"，
        所以**不该中断整条流水线**；但退出码要如实带 1，免得脚本以为一切正常。
        （`assets` / `tts` / `build` 原先无论有没有失败件都 `return 0`，
        于是 `errors.EXIT_FAILED` 形同虚设、`--keep-going` 也看不到失败。）
        """
        return self.code == errors.EXIT_FAILED

    @property
    def status(self) -> str:
        return {0: "ok", 1: "partial", 2: "bad-input", 3: "blocked"}.get(
            self.code, f"failed({self.code})"
        )


def collect_options(stage: Stage, args: Any) -> dict[str, Any]:
    """从任意 args（Namespace / dict / object）里挑出**这个阶段认得的**开关。

    这是"单一真源"的直接收益：编排器不再手写 `_stage_args(force=…, no_llm=…)`
    那一串 kwargs —— 漏一个就静默失效。这里按 `stage.options` 自动对齐。
    """
    out: dict[str, Any] = {}
    for opt in stage.options:
        value = args.get(opt.key) if isinstance(args, dict) else getattr(args, opt.key, None)
        if value is not None:
            out[opt.key] = value
    return out


def note_stage_end(ws: Any, config: Any, name: str, code: int, *, seconds: float = 0.0) -> None:  # noqa: ANN401
    """记一个阶段的收尾可观测性：**耗时落 manifest** + **一条轨迹事件**。

    ★ 两条入口**共用这一个函数**：`run`（走 `run_stage`）与**直接敲单条命令**
    （走 `cli._run_and_report`）。分开写会导致"逐条驱动"和"一键 run"看到的
    轨迹不一样 —— 而 agent 恰恰是逐条驱动的（见 `docs/架构方向-决策记录.md`）。

    全是增强：任何失败都不影响阶段结果（不新增故障点）。
    """
    if seconds and hasattr(ws, "record_timing"):
        try:
            ws.record_timing(name, seconds)
        except Exception:  # noqa: BLE001
            pass
    try:
        from lvs import runlog

        if runlog.enabled(config):
            row: dict[str, Any] = {"exit_code": int(code)}
            if seconds:
                row["seconds"] = round(float(seconds), 1)
            runlog.event(ws, name, "stage_end", **row)
    except Exception:  # noqa: BLE001 - 观测不该变成新的故障点
        pass


def run_stage(
    stage: Stage,
    ctx: StageContext,
    *,
    before: Any = None,
    log: Any = print,
) -> StageResult:
    """执行一个阶段：**唯一**的入口。

    `before` 是阶段入口的守卫钩子（`run.py` 用它检查门禁），签名为
    `before(stage, ctx) -> int | None`；返回非 0 则**不执行**该阶段，
    并把那个码当作结果 —— 这样"门禁拦住"与"阶段自己失败"能被上层区分。
    """
    if before is not None:
        blocked = before(stage, ctx)
        if blocked:
            return StageResult(name=stage.name, code=int(blocked))

    log(f"\n=== {stage.name} · {stage.title} ===")
    t0 = time.time()
    impl = resolve_impl(stage.impl)
    if stage.positional and stage.positional_required and not ctx.options.get("manuscript"):
        log(f"  缺少位置参数：{stage.positional}")
        return StageResult(name=stage.name, code=2, seconds=time.time() - t0)

    try:
        code = int(impl(ctx.config, ctx.ws, ctx.as_namespace(getattr(ctx.ws, "task", ""))) or 0)
    except KeyboardInterrupt:
        # 用户按了 Ctrl-C：**不吞**，让它一路冒到 `cli.main`（那里会打"已中断"）。
        # 捕获它会让 Ctrl-C 变成"某阶段失败返回 1"，用户以为程序卡住了。
        raise
    except errors.LvsError as exc:
        # ★ 领域错误转成返回码，而不是让它冒泡。
        #
        # 为什么必须在**这一层**转：`run.py` 的 `--keep-going` 判的是"返回码"，
        # 异常冒泡的话它根本看不到 —— 于是"某个阶段抛异常"就等于"整条崩"，
        # 而 `--keep-going` 的语义（失败也往下走）就成了一句空话。
        # 退出码用错误自带的语义（2=输入不对 / 3=等人审），与 `cli.main` 的兜底一致。
        code = errors.exit_code_for(exc)
        log(f"  ✗ {type(exc).__name__}：{exc}")
    except Exception as exc:  # noqa: BLE001 - 阶段内部未预期异常：转成失败，别崩整条
        code = errors.EXIT_FAILED
        log(f"  ✗ 未预期错误：{type(exc).__name__}: {exc}")
    dt = time.time() - t0
    # 成功后补写**产物血缘**：记下上游产物的签名，下次就能判"这一步还新鲜吗"。
    # 放在这里（而不是各阶段自己写）是为了让"上游是谁"只有一处定义。
    if code == 0 and hasattr(ctx.ws, "record_lineage"):
        try:
            ctx.ws.record_lineage(stage.name, upstream_artifacts(stage, ctx.ws))
        except Exception as exc:  # noqa: BLE001 - 血缘是增强，不该让阶段失败
            log(f"  [血缘] 记录失败（不影响本次结果）：{exc}")

    # ---- 可观测性（`lvs/runlog.py`）：耗时落 manifest + 一条轨迹事件 ----
    # 与 CLI 单命令路径**共用** `note_stage_end`，免得两条路的轨迹不一样。
    note_stage_end(ctx.ws, ctx.config, stage.name, code, seconds=dt)

    log(f"--- {stage.name} 完成，耗时 {dt:.1f}s（退出码 {code}）---")
    return StageResult(name=stage.name, code=code, seconds=dt)


def pending(ws: Any, *, force: bool = False) -> list[Stage]:
    """本次要跑的阶段：按顺序，跳过已完成的（`force` 时全要）。"""
    out: list[Stage] = []
    for stage in STAGES:
        if force or not ws.is_stage_done(stage.name):
            out.append(stage)
    return out


def artifacts_of(stage: Stage, ws: Any) -> list[Path]:
    """把 `artifacts` 名字解析成 workspace 内的真实路径。"""
    return [ws.path(*name.split("/")) for name in stage.artifacts]


def upstream_artifacts(stage: Stage, ws: Any) -> list[Path]:
    """本阶段**消费**的上游产物（= `needs` 里各阶段的产物）。

    用途是"产物血缘"：记下它们的签名，下次就能回答"上游改了没有、
    这一步还新鲜吗"。不记的话会出现这种静默 bug ——
    重新 parse 之后 `shots` 仍被判为已完成，于是拿旧解析结果继续往下跑。
    """
    out: list[Path] = []
    for name in stage.needs:
        up = BY_NAME.get(name)
        if up is not None:
            out.extend(artifacts_of(up, ws))
    return out


def names_like(pattern: str) -> list[str]:
    """给 CLI / 界面用的名字提示。"""
    return [s.name for s in ALL if pattern.lower() in s.name.lower()]


def option_flags(stages: Iterable[Stage] | None = None) -> set[str]:
    """所有阶段用到的开关名集合（契约测试用来对齐 CLI 与界面）。"""
    out: set[str] = set()
    for s in (stages if stages is not None else ALL):
        for o in s.options:
            out.add(o.key)
    return out
