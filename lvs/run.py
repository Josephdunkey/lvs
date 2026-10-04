"""编排（`lvs run`）—— 票据 03 + 16。

把 `parse → shots → assets → voice → build` 串起来，遵循 spec §7.1 的编排约定：

- **文件即状态**：每个阶段入口先看自己的产物在不在（`manifest.json` + 实际文件）
- **断点续跑**：删掉某阶段产物再 `lvs run`，只重跑该阶段及其下游
- **失败隔离**：`--keep-going` 时单阶段失败不中断（默认遇错即停，便于定位）。
  ★ 但它**不跳过门禁** —— 门禁拦住是"等人决定"，不是"出错"，
  跳过去等于放弃人审（见 `stage.StageResult.blocked`）。
- **`--force`** 全量重跑

另有 `--demo`：**走路骨架**（票据 03）——不配 LLM / Pexels / ComfyUI 也能跑通，
用 3 个硬编码分镜 + ffmpeg 生成的占位图，验证"图 → 声 → 字幕 → 成片"整条管道。
"""

from __future__ import annotations

import json
from pathlib import Path

from lvs import errors
from lvs import guard
from lvs import pipeline as pipeline_mod
from lvs import stage as stage_mod
from lvs.config import Config
from lvs.ffmpeg import FFmpegError, run as ff_run, tools
from lvs.workspace import Workspace


# ---- 门禁（人审闸门） -------------------------------------------------------


def _gate_hold(ws: Workspace, stage: str, config: Config, args) -> int:
    """阶段入口的门禁检查。返回 0 = 放行，3 = 停下等人审。

    **为什么放在阶段入口而不是结尾**：门禁守的是"下一步要不要花钱"。
    放在结尾只能事后追认，放在入口才真的拦住。
    （`parse` 不受约束 —— 它免费且可逆，且它的产物正是 G0 的审阅对象。）
    """
    if getattr(args, "skip_gates", False):
        print(f"\n[门禁] --skip-gates：{stage} 阶段不做人审检查（本次跳过不记账）")
        return 0
    if not pipeline_mod.enforce_enabled(config):
        print(f"\n[门禁] config 里 pipeline.enforce=false：跳过 {stage} 阶段的人审检查")
        return 0

    for gate in pipeline_mod.BY_STAGE.get(stage, []):
        st = pipeline_mod.evaluate(ws, gate, config=config)
        if st.open:
            continue
        print()
        print(pipeline_mod.block_message(ws, st, reason=f"`lvs run` 即将进入 {stage} 阶段"))
        print()
        print("  放行后重跑同一条 `lvs run` 即可从原地继续（已完成阶段会跳过）。")
        print("  确要一次性跑完（不推荐，等于放弃人审）：加 `--skip-gates`。")
        return 3
    return 0


def _gate_summary(ws: Workspace, config: Config) -> None:
    """跑完打印门禁账本 —— 让人一眼看到还剩哪道门。"""
    sts = pipeline_mod.statuses(ws, config=config)
    print()
    print(pipeline_mod.render_status(ws, sts))


# ---- demo（走路骨架） ------------------------------------------------------


DEMO_SHOTS = [
    "公元前256年，周天子最后一次结账。",
    "九鼎易主，天下再无共主。",
    "一个时代的账本，就此合上。",
]


def _demo_images(ws: Workspace, shots: list[dict]) -> None:
    """用 ffmpeg 生成 3 张占位图（无需 Pillow / 网络）。"""
    ffmpeg, _ = tools()
    colors = ["0x1b2a41", "0x3d2b1f", "0x2b3a2b"]
    for shot, color in zip(shots, colors):
        dst = ws.path("assets", "local", f"shot-{shot['id']:03d}.png")
        vf = (
            f"drawtext=text='shot {shot['id']:03d}':fontcolor=white@0.85:fontsize=72:"
            f"x=(w-text_w)/2:y=(h-text_h)/2"
        )
        ff_run([
            ffmpeg, "-y", "-f", "lavfi",
            "-i", f"color=c={color}:s=1344x768",
            "-frames:v", "1", "-vf", vf, str(dst),
        ])
        shot["asset_path"] = str(dst)
        shot["resolved_by"] = "local"
        shot["status"] = "done"


def run_demo(config: Config, ws: Workspace, args) -> int:
    print("演示模式（走路骨架）：无需 LLM / Pexels / ComfyUI")
    shots: list[dict] = []
    for i, narration in enumerate(DEMO_SHOTS, start=1):
        shots.append({
            "id": i,
            "segment": 1,
            "segment_heading": "demo",
            "narration": narration,
            "visual": narration,
            "source": "local",
            "prompt": f"demo shot {i}",
            "keywords": ["demo"],
            "library_asset": None,
            "resolved_by": None,
            "style": "historical-documentary",
            "start": None,
            "end": None,
            "status": "pending",
            "generated_by": "demo",
        })

    try:
        tools()
    except FFmpegError as exc:
        print(str(exc))
        return 2

    _demo_images(ws, shots)
    ws.write_shots({"title": "demo", "count": len(shots), "shots": shots})
    ws.mark_stage("parse", status="skipped", note="demo 模式跳过")
    ws.mark_stage("shots", outputs=[ws.path("shots.json")], count=len(shots))

    code = _run_two(config, ws, args, "voice")
    if code:
        return code
    return _run_two(config, ws, args, "build")


def _show_recent_failures(ws: Workspace, limit: int = 5) -> None:
    """从**运行轨迹**里捞最后几条逐镜失败 —— 这正是轨迹存在的理由。

    长跑挂了之后最想知道的是"从哪一镜开始不对"。stdout 早滚掉了，
    而 `logs/run-*.jsonl` 还留着（见 `lvs/runlog.py`）。
    读不到就静默跳过（轨迹是可选的增强）。
    """
    try:
        from lvs import runlog

        rows = runlog.failures(ws)[-limit:]
    except Exception:  # noqa: BLE001 - 轨迹是增强，读不到不影响主流程
        return
    if not rows:
        return
    print(f"\n  最近 {len(rows)} 条逐镜失败（完整轨迹见 .work/{ws.task}/logs/）：")
    for r in rows:
        shot = r.get("shot", "?")
        stage = r.get("stage", "?")
        reason = str(r.get("reason") or "")[:80]
        print(f"    [{stage}] 镜 {shot}：{reason}")


def _run_two(config: Config, ws: Workspace, args, name: str) -> int:  # noqa: ANN001
    """demo 路径只跑 voice/build 两步 —— 同样走 `stage.py`，不走自己的 Namespace。

    保持"**只有一个地方**伪造 Namespace"（`StageContext.as_namespace`）——
    否则架构约束就成了一句空话。
    """
    stage = stage_mod.get(name)
    ctx = stage_mod.StageContext(
        config=config, ws=ws,
        options=stage_mod.collect_options(stage, {"force": bool(getattr(args, "force", False))}),
        force=bool(getattr(args, "force", False)),
    )
    return stage_mod.run_stage(stage, ctx).code


# ---- 正式编排 --------------------------------------------------------------


def run_pipeline(config: Config, ws: Workspace, args) -> int:
    """照 `lvs.stage.STAGES` 走一遍 —— **顺序不再写在这里**。

    改造前这里是一长串 if/else，每个阶段手写 `_stage_args(force=…, no_llm=…)`。
    那样的问题不是啰嗦，是**漏一个参数就静默失效**：
    `getattr(args, "旧名", None)` 拿到 None，那个开关就没传下去，不报错。
    现在阶段表在 `lvs/stage.py`，开关按 `stage.options` 自动对齐。
    """
    if getattr(args, "demo", False):
        return run_demo(config, ws, args)

    force = bool(getattr(args, "force", False))

    # parse 先跑：它免费、可逆，且它的产物正是 G0 的审阅对象。
    # `quiet=True`：`run_command` 已在门禁之前跑过一次，这里跳过时不再重复打印。
    code = _ensure_parse(
        config, ws, getattr(args, "manuscript", None), force, quiet=True
    )
    if code:
        return code

    # ★ 从这里开始照单执行。`pending()` 跳过已完成的阶段（`--force` 时全要）。
    keep_going = bool(getattr(args, "keep_going", False))
    failures: list[stage_mod.StageResult] = []
    partials: list[stage_mod.StageResult] = []

    for st in stage_mod.pending(ws, force=force):
        if st.name == "parse":
            # parse 已由 `_ensure_parse` 负责（它有自己的"源文件变了没有"判据）。
            # 不显式跳过的话，在"manifest 里没有 parse 记录"的边界情况下它会跑两次 ——
            # 真实 Workspace 靠 `mark_stage` 掩盖了这个，但边界情况下不该靠运气。
            continue
        ctx = stage_mod.StageContext(
            config=config, ws=ws,
            options=stage_mod.collect_options(st, args),
            force=force,
        )
        res = stage_mod.run_stage(st, ctx, before=_make_gate_hook(ws, config, args))
        if res.ok:
            continue
        if res.blocked:
            # 门禁/守卫拦住 —— **`--keep-going` 也不跳过**。
            # 跳过去就等于放弃人审，而门禁正是这套流水线存在的理由。
            return res.code
        if res.partial:
            # 逐镜失败的**部分**失败：几镜没取到素材 / 没合出音。
            # 这正是"标记该镜、其余继续"的本意 —— 不该中断整条，否则一镜坏就白跑整集。
            # 记下来，最后汇总并把退出码带上 1。
            partials.append(res)
            print(f"\n[部分失败] {st.name} 退出码 {res.code}"
                  "（逐镜隔离：标记失败镜，继续往后跑）")
            continue
        failures.append(res)
        print(f"\n[失败] {st.name} 退出码 {res.code}"
              + ("，--keep-going：继续跑后面的阶段" if keep_going else ""))
        if not keep_going:
            return res.code

    if failures:
        print("\n⚠ 以下阶段失败，其余已按 --keep-going 继续：")
        for res in failures:
            print(f"    {res.name:<8} 退出码 {res.code}")
        print("  修好后重跑同一条 `lvs run`（已完成的阶段会自动跳过）。")
        _show_recent_failures(ws)
        final = ws.path("final.mp4")
        if final.is_file():
            print(f"\n🎬 成片（可能不完整）：{final}")
        _gate_summary(ws, config)
        return max(res.code for res in failures)

    final = ws.path("final.mp4")
    if partials:
        print("\n⚠ 成片已产出，但有**失败件**（逐镜隔离，未中断）：")
        for res in partials:
            print(f"    {res.name:<8} 退出码 {res.code}")
        print("  修好原因后重跑同一条 `lvs run`（已成功的镜会跳过，只补缺件）。")
        _show_recent_failures(ws)
        print(f"\n🎬 成片（有缺件）：{final}")
        _gate_summary(ws, config)
        return errors.EXIT_FAILED

    print(f"\n🎬 成片：{final}")
    _gate_summary(ws, config)
    return 0


def _ensure_parse(
    config: Config, ws: Workspace, manuscript: str | None, force: bool, *, quiet: bool = False
) -> int:
    """parse 单独一步（幂等）：已有 parse.json 且源文件未变就跳过。

    为什么不让它进统一循环：它要按"源文件路径变了没有"判过期，
    而其他阶段按 workspace 的 manifest 判 —— 这是它独有的语义。

    `quiet=True`：跳过时**不打**"已有 parse.json，跳过"那行。
    给 `run_pipeline` 用 —— `run_command` 已在门禁之前跑过一次 parse，
    这里再打一遍会让人以为**重复跑了两次**（实测踩过，很伤信任）。
    """
    stage = stage_mod.get("parse")
    src = Path(manuscript) if manuscript else None
    parse_path = ws.path("parse.json")
    need = force or not parse_path.is_file()
    if not need and src and src.is_file():
        try:
            recorded = json.loads(parse_path.read_text(encoding="utf-8")).get("source")
            if recorded and Path(recorded) != src.resolve():
                need = True
        except (OSError, json.JSONDecodeError):
            need = True
    if not need:
        if not quiet:
            print("\n=== parse ===\n已有 parse.json，跳过（--force 可重做）")
        return 0

    if not src or not src.is_file():
        print(f"拍摄稿不存在：{src}")
        return 2
    ws.ensure()
    ctx = stage_mod.StageContext(
        config=config, ws=ws,
        options={"manuscript": str(src), **stage_mod.collect_options(stage, {})},
        force=force,
    )
    return stage_mod.run_stage(stage, ctx).code


def _make_gate_hook(ws: Workspace, config: Config, args):  # noqa: ANN001
    """给 `stage.run_stage` 用的门禁钩子：返回非 0 就不执行该阶段。"""

    def hook(stage, ctx) -> int:  # noqa: ANN001, ANN202
        return _gate_hold(ws, stage.name, config, args)

    return hook


def _next_stage(ws: Workspace, args) -> str | None:
    """本次 `lvs run` 会执行到的第一个阶段（都已做完则 None）。"""
    todo = stage_mod.pending(ws, force=bool(getattr(args, "force", False)))
    todo = [s for s in todo if s.name != "parse"]
    return todo[0].name if todo else None


def run_command(config: Config, ws: Workspace, args) -> int:  # noqa: ANN001
    # ---- 0) `--demo` 走骨架，与门禁 / GPU 守卫都无关，直接进去
    if getattr(args, "demo", False):
        return run_pipeline(config, ws, args)

    force = bool(getattr(args, "force", False))

    # ---- 0.5) 先跑**不受门禁约束**的免费阶段（parse）----
    #
    # ★★ 顺序很关键，这里真踩过：
    # G0 的审阅对象就是 `parse.json`（见 G0 的 `why`："parse 本身免费且可逆，
    # 它的产物正是这道门的审阅对象"）。但原先门禁检查在 parse 之前，
    # 于是**全新项目上第一次 `lvs run` 会立刻卡在 G0**，
    # 打印"看产物："后面**一个文件都没有** —— 让你看一个还不存在的东西。
    # 用户会以为 `lvs run` 坏了。
    #
    # 现在先把 parse 跑掉（幂等：已有且源文件未变就跳过），再过门禁，
    # G0 就能真的列出 `parse.json` 给你审。
    code = _ensure_parse(config, ws, getattr(args, "manuscript", None), force)
    if code:
        return code

    # ---- 1) 门禁优先于 GPU 前置检查 ----
    #
    # 为什么要调这个顺序：门禁问的是"**该不该**往下走"，守卫问的是"这一步**能不能**跑"。
    # 反过来（先守卫）的话，用户会看到「GPU 冲突：请关掉 ComfyUI」——
    # 而他真正卡住的原因是**还没审 G0**。让人去关一个跟当前这一步无关的服务，
    # 比不给提示更糟。
    # 门禁检查是纯读、无副作用，先跑一遍不会浪费任何东西。
    stage = _next_stage(ws, args)
    if stage is not None:
        code = _gate_hold(ws, stage, config, args)
        if code:
            return code

    # ---- 1) GPU 守卫（票据 17）：在真正吃显存的阶段前检查；冲突则明确报错。
    #
    # 注意：这里**快速失败**（整条 return 2），而 `lvs assets` / `lvs voice` 是"逐镜失败隔离"。
    # 两者语义不同，不是不一致：
    #   - run：GPU 冲突是**前置条件**不满足（TTS 服务与生图不能共存），整条跑下去只会反复 OOM，
    #     所以立刻停下、让人先停掉另一个服务；
    #   - 单阶段：到这一镜才发现取不到素材/合不出音，属**单件失败**，标记该镜、其余继续（§7.1）。
    try:
        shots_data = ws.try_load_shots() or None    # 读取语义归 Workspace（票 42）
        stages_needed: list[str] = []
        if shots_data and any(s.get("source") == "local" for s in shots_data.get("shots", [])):
            stages_needed.append("imagegen")
        if str(config.get("tts.backend", "edge")) == "openai_speech":
            stages_needed.append("tts")
        for st in stages_needed:
            note = guard.check(st, config)
            if note:
                print(f"[GPU 守卫] {note}")
    except guard.GPUConflict as exc:
        print(f"[GPU 守卫] {exc}")
        return 2

    return run_pipeline(config, ws, args)
