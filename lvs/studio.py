"""手动向导（`lvs studio`）—— 票据 30。

把「素材获取」这一段拆成**有检查点的手动流程**，与 `lvs run` 的全自动流程并存：

    上传拍摄稿 → 解析 / 拆镜
      → 问：下载素材（Pexels）/ 生图（ComfyUI）/ 图文卡片 / 翻已有素材库
      → 执行
      → 问：继续下一步（配音 → 合成），还是改几张图
      → 改图：按镜号重出（换 seed 或改提示词）
      → 满意后接上 voice → build

每个提问都有对应参数（`--action` / `--redo` / `--prompt` / `--seed` / `--yes` / `--stop`），
所以既能人机对话，也能脚本化 —— 非交互终端下**不给参数就直接报错**，不会挂在那里等输入。

与全自动的分工：
- `lvs run`    —— 一键到底，不问任何问题
- `lvs studio` —— 每步停下等你确认，适合"图我要一张张看"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from lvs import assets as assets_mod
from lvs import sources
from lvs import stage as stage_mod
from lvs.config import Config
from lvs.workspace import Workspace
from lvs.errors import LvsError, EXIT_USAGE

ACTION_LABELS = {
    "pexels": "下载实拍素材（Pexels）",
    "local": "本地生图（ComfyUI）",
    "graphic": "图文/图表卡片（本地排版）",
    "library": "翻已有素材库",
}


class StudioError(LvsError, RuntimeError):
    """向导无法继续（非交互终端缺参数 / 输入非法）。"""

    exit_code = EXIT_USAGE


def _run_stage(name: str, config, ws, **options: Any) -> int:  # noqa: ANN001
    """按阶段名跑一步 —— **走 `lvs.stage`，不再自己伪造 Namespace**。

    为什么：改造前这里（和 `run.py`）各写了一份 `argparse.Namespace`，
    把 CLI 的形状渗进了编排层；参数名写错只会静默失效。
    现在阶段的开关表在 `lvs/stage.py`，两边共用一份。
    """
    stage = stage_mod.get(name)
    ctx = stage_mod.StageContext(config=config, ws=ws, options=options)
    return stage_mod.run_stage(stage, ctx, log=lambda *_a: None).code


# ---- 参数解析 --------------------------------------------------------------


def parse_shot_ids(spec: str) -> list[int]:
    """`1-10,15` → `[1..10, 15]`（去重排序）。空或非法抛 `StudioError`。"""
    ids: list[int] = []
    for chunk in str(spec or "").replace("，", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo_raw, _, hi_raw = chunk.partition("-")
            try:
                lo, hi = int(lo_raw), int(hi_raw)
            except ValueError:
                raise StudioError(f"镜号区间看不懂：{chunk!r}（应形如 1-10,15）") from None
            if lo > hi:
                lo, hi = hi, lo
            ids.extend(range(lo, hi + 1))
        else:
            try:
                ids.append(int(chunk))
            except ValueError:
                raise StudioError(f"镜号看不懂：{chunk!r}（应形如 1-10,15）") from None
    if not ids:
        raise StudioError("没有解析出任何镜号（应形如 --redo 1-10,15）")
    return sorted(set(ids))


def parse_actions(raw: str) -> list[str]:
    """`local,graphic` / `all` → 分支列表。"""
    keys = list(assets_mod.ASSET_BRANCHES)
    items = [p.strip().lower() for p in str(raw or "").replace("，", ",").split(",") if p.strip()]
    if not items:
        raise StudioError(f"--action 不能为空；可选 {'/'.join(keys)} 或 all")
    if "all" in items or "全部" in items:
        return keys
    bad = [i for i in items if i not in keys]
    if bad:
        raise StudioError(f"--action 只支持 {'/'.join(keys)}（或 all），收到 {'/'.join(bad)}")
    return items


# ---- 交互 ------------------------------------------------------------------


def _ask(question: str, default: str = "") -> str:
    """问一个问题。**非交互终端直接报错**，不静默取默认值。"""
    if not sys.stdin.isatty():
        raise StudioError(
            "当前不是交互终端，无法提问。请改用参数：\n"
            "  --action pexels,local,graphic,library  --redo 1-10  --prompt \"...\"  --yes  --stop"
        )
    try:
        answer = input(f"{question}\n  [{default}] > ").strip()
    except EOFError:
        return default
    return answer or default


def ask_source(args, auto: bool) -> str:  # noqa: ANN001
    """问「实拍镜的素材从哪儿来」（票 41）。`--source` 优先；`--yes` 取 auto；否则交互问。

    这是素材这一步**最主要**的问题 —— 它决定后面要不要翻库、要不要开 ComfyUI。
    `--action` 仍然可用（那是"这次只跑哪几支"的进阶开关）。
    """
    if getattr(args, "source", None):
        try:
            return sources.normalize(args.source)
        except sources.SourceModeError as exc:
            raise StudioError(str(exc)) from exc
    if auto or getattr(args, "action", None):
        # 已经用 --action 指定了分支，或非交互模式下，就不猜来源
        return sources.MODE_AUTO
    keys = list(sources.MODES)
    menu = "\n".join(f"  {i}) {sources.LABELS[k]}（{k}）" for i, k in enumerate(keys, 1))
    answer = _ask(f"实拍镜的素材从哪儿来？\n{menu}\n  （图文/图表卡片不受这个选择影响）", "1").strip()
    if answer.isdigit() and 1 <= int(answer) <= len(keys):
        return keys[int(answer) - 1]
    try:
        return sources.normalize(answer)
    except sources.SourceModeError:
        return sources.MODE_AUTO


def actions_for(mode: str) -> list[str]:
    """策略 → 这一步实际该跑的分支（只用于打印与 `no_library` 判断）。

    图文/图表卡片**永远**要跑（策略管不着它），所以这里不列 graphic ——
    `--only` 的筛选是按逐镜 `source` 来的，列上它反而会把卡片镜筛掉。
    """
    return list(assets_mod.ASSET_BRANCHES) if mode == sources.MODE_AUTO else [mode]


def ask_actions(args, auto: bool) -> list[str]:  # noqa: ANN001
    """问「这次取哪些素材」。`--action` 优先；`--yes` 取全部；否则交互问。"""
    raw = getattr(args, "action", None)
    if raw:
        return parse_actions(raw)
    if auto:
        return list(assets_mod.ASSET_BRANCHES)
    keys = list(assets_mod.ASSET_BRANCHES)
    menu = "\n".join(f"  {i}) {ACTION_LABELS[k]}（{k}）" for i, k in enumerate(keys, 1))
    answer = _ask(f"这一步要取哪些素材？（编号，逗号分隔；回车=全部）\n{menu}", "1,2,3,4")
    picked: list[str] = []
    for token in answer.replace("，", ",").split(","):
        token = token.strip()
        if token.isdigit() and 1 <= int(token) <= len(keys):
            picked.append(keys[int(token) - 1])
        elif token in keys:
            picked.append(token)
    return picked or keys


def ask_next(args, auto: bool) -> str:  # noqa: ANN001
    """素材看完后的走向：`continue` / `redo` / `stop`。"""
    if getattr(args, "stop", False):
        return "stop"
    if auto:
        return "continue"
    answer = _ask(
        "素材看过了，接下来？\n  1) 继续下一步（配音 → 合成）\n  2) 改几张图\n  3) 先停在这里",
        "1",
    ).strip()
    if answer in ("2", "redo", "改图", "改"):
        return "redo"
    if answer in ("3", "stop", "停", "停在这里"):
        return "stop"
    return "continue"


def _manuscript(ws: Workspace, args, auto: bool) -> Path | None:  # noqa: ANN001
    """定位拍摄稿：参数优先，其次沿用任务里上次解析的那份，最后才问。"""
    raw = getattr(args, "manuscript", None)
    if raw:
        path = Path(raw).expanduser()
        if not path.is_file():
            print(f"拍摄稿不存在：{path}")
            return None
        return path

    recorded = None
    if ws.path("parse.json").is_file():
        try:
            recorded = json.loads(ws.path("parse.json").read_text(encoding="utf-8")).get("source")
        except (OSError, json.JSONDecodeError):
            recorded = None
    if recorded and Path(recorded).is_file():
        print(f"沿用 {ws.task} 上次解析的拍摄稿：{recorded}")
        return Path(recorded)
    if ws.path("source.md").is_file():
        print(f"沿用任务目录里的输入副本：{ws.path('source.md')}")
        return ws.path("source.md")

    if auto:
        raise StudioError("没有拍摄稿：请写成 `lvs studio <拍摄稿.md> --task NAME --yes`")
    answer = _ask("拍摄稿路径（.md）？")
    path = Path(answer).expanduser()
    if not path.is_file():
        print(f"拍摄稿不存在：{path}")
        return None
    return path


# ---- 报告 ------------------------------------------------------------------


def _counts(items: list[Any], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        value = str((item.get(key) if isinstance(item, dict) else None) or "?")
        out[value] = out.get(value, 0) + 1
    return out


def report_shots(ws: Workspace) -> None:
    path = ws.path("shots.json")
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    shots = data.get("shots", [])
    kind = _counts(shots, "kind")
    source = _counts(shots, "source")
    print(f"  分镜 {len(shots)} 个｜画面类型：" + "，".join(f"{k} {v}" for k, v in sorted(kind.items())))
    print("  素材来源：" + "，".join(f"{k} {v}" for k, v in sorted(source.items())))


def report_assets(ws: Workspace) -> None:
    path = ws.path("shots.json")
    if not path.is_file():
        return
    try:
        shots = json.loads(path.read_text(encoding="utf-8")).get("shots", [])
    except (OSError, json.JSONDecodeError):
        return
    for status in ("done", "failed", "pending"):
        ids = [int(s["id"]) for s in shots if s.get("status") == status]
        if not ids:
            continue
        head = ids[:12]
        print(f"  {status:<8}{len(ids):>4} 镜：{head}{' …' if len(ids) > 12 else ''}")


# ---- 改图 ------------------------------------------------------------------


def clear_shot_assets(ws: Workspace, shot: dict[str, Any]) -> int:
    """删掉该镜的已有素材与已合成片段，好让重跑**真的**重做（票据 30）。

    片段也要删：片段缓存只看时长，图换了但时长没变的话会被原样复用。
    """
    removed = ws.clear_shot_artifacts(int(shot["id"]))

    # 含 `library_asset`：要的是"新一张"，不是复用上次的库命中
    for field in sources.STALE_WITH_LIBRARY:
        shot.pop(field, None)
    shot["status"] = "pending"
    return removed


def route_pexels_to_local(ws: Workspace) -> int:
    """选了「生图」却没选「下载素材」时，把 pexels 镜改成本地生图。

    否则这些镜会一直缺素材（它们的 `source` 是 pexels，而这一轮根本不碰 pexels）。
    改的是 `source` 本身 —— 用户的意图就是"这次用生图，不下素材"。

    这里**只用保守翻转**（准确等于 pexels 的镜），不用 `sources.apply_mode` 的全量强制：
    自动策略下用户的逐镜编辑是要尊重的（票 41）。
    """
    data = ws.try_load_shots()
    if not data:
        return 0
    flipped = sources.retarget(data, "pexels", "local")
    if flipped:
        ws.write_shots(data)
    return flipped


def redo_shots(config: Config, ws: Workspace, spec: str, args, only: str | None = None) -> int:  # noqa: ANN001
    """按镜号重出：`--prompt` 改提示词，`--seed` 指定种子，都没有则 seed +1 重抽。

    `only` 是本次向导选中的素材来源（`--action` 收窄，票 30）。会被**放宽到覆盖目标镜
    自身的来源**，否则目标镜会被 `assets.only_matches` 直接筛掉、根本不会被重做。
    """
    ids = parse_shot_ids(spec)
    path = ws.path("shots.json")
    if not path.is_file():
        print(f"未找到 {path}；请先完成拆镜。")
        return 2

    data = json.loads(path.read_text(encoding="utf-8"))
    by_id = {int(s["id"]): s for s in data.get("shots", [])}
    missing = [i for i in ids if i not in by_id]
    if missing:
        print(f"这些镜号不存在，已忽略：{missing[:10]}{' …' if len(missing) > 10 else ''}")
    targets = [by_id[i] for i in ids if i in by_id]
    if not targets:
        return 2

    new_prompt = getattr(args, "prompt", None)
    new_seed = getattr(args, "seed", None)
    for shot in targets:
        if new_prompt:
            shot["prompt"] = str(new_prompt)
        if new_seed is not None:
            shot["seed"] = int(new_seed)
        elif not new_prompt:
            base = int(shot.get("seed") or (int(config.get("comfyui.seed", 42)) + int(shot["id"])))
            shot["seed"] = base + 1   # 连续重抽会一直往前走，不会原地打转
        clear_shot_assets(ws, shot)

    scope = assets_mod.parse_only(only)
    no_library = scope is not None and "library" not in scope
    if scope is not None:
        scope |= {str(s.get("source") or "") for s in targets}   # 别把目标镜自己筛掉
        scope.discard("")
        only = ",".join(sorted(scope)) or None
        no_library = "library" not in scope

    ws.write_shots(data)
    shown = ids[:12]
    print(f"重做 {len(targets)} 镜：{shown}{' …' if len(ids) > 12 else ''}"
          + (f"（新 seed {new_seed}）" if new_seed is not None else "")
          + ("（已替换提示词）" if new_prompt else ""))
    return _run_stage("assets", config, ws, force=False, no_library=no_library, only=only)


# ---- 主流程 ----------------------------------------------------------------


def _prepare(config: Config, ws: Workspace, manuscript: Path, args) -> int:  # noqa: ANN001
    """解析 + 拆镜（幂等：产物在且源没变就跳过）。"""
    parse_path = ws.path("parse.json")
    need_parse = True
    if parse_path.is_file() and not getattr(args, "redo_shots", False):
        try:
            recorded = json.loads(parse_path.read_text(encoding="utf-8")).get("source")
            need_parse = not (recorded and Path(recorded) == manuscript.resolve())
        except (OSError, json.JSONDecodeError):
            need_parse = True

    if need_parse:
        print(f"\n=== parse ===  {manuscript.name}")
        code = _run_stage("parse", config, ws, force=False, manuscript=str(manuscript))
        if code != 0:
            return code
    else:
        print("\n=== parse ===  已有同一份拍摄稿的 parse.json，跳过")

    if ws.is_stage_done("shots") and not getattr(args, "redo_shots", False):
        print("\n=== shots ===  已完成，跳过（--redo-shots 可重做）")
        report_shots(ws)
        return 0

    print("\n=== shots ===")
    code = _run_stage("shots", config, ws, force=False, no_llm=False,
                      visual=getattr(args, "visual", None))
    if code != 0:
        return code
    report_shots(ws)
    return 0


def _assets_scope(actions: list[str]) -> str | None:
    """把选中的分支收窄成 `assets --only`（票 30）。

    三种"取素材"分支全选时就不必收窄（`None`＝全跑）；`library` 不是 `assets --only`
    的分支，由 `no_library` 单独控制，故不并入。
    """
    branches = [a for a in actions if a != "library"]
    return ",".join(branches) if 0 < len(branches) < 3 else None


def run_command(config: Config, ws: Workspace, args) -> int:  # noqa: ANN001
    auto = bool(getattr(args, "yes", False))
    try:
        print("=" * 62)
        print(f"  lvs studio · 任务 {ws.task}（手动向导；全自动请用 `lvs run`）")
        print("=" * 62)

        manuscript = _manuscript(ws, args, auto)
        if manuscript is None:
            return 2

        code = _prepare(config, ws, manuscript, args)
        if code != 0:
            return code

        if getattr(args, "redo", None):
            scope = _assets_scope(parse_actions(args.action)) if getattr(args, "action", None) else None
            return redo_shots(config, ws, args.redo, args, only=scope)

        # 来源策略是这一步的主问题（票 41）；auto 时才退回去问「取哪些素材」的老问题
        mode = ask_source(args, auto)
        print("\n=== assets ===")
        if mode == sources.MODE_AUTO:
            actions = ask_actions(args, auto)
            only_arg = _assets_scope(actions)
            no_library = "library" not in actions
            print("  本次获取：" + "，".join(ACTION_LABELS[a] for a in actions))
            if "local" in actions and "pexels" not in actions:
                # 只勾了「生图」没勾「下载素材」时的兜底：那些 pexels 镜会永远缺素材。
                flipped = route_pexels_to_local(ws)
                if flipped:
                    print(f"  本次不下载素材：把 {flipped} 个 pexels 镜改成本地生图")
        else:
            actions = actions_for(mode)
            only_arg = None          # 策略已经落成逐镜 source，不必再按 source 筛一轮
            no_library = mode != sources.MODE_LIBRARY
            print(f"  实拍镜来源：{sources.LABELS[mode]}")
            print("  本次获取：" + "，".join(ACTION_LABELS[a] for a in actions))
        code = _run_stage(
            "assets", config, ws, force=False, no_library=no_library, only=only_arg,
            source=None if mode == sources.MODE_AUTO else mode,
        )
        report_assets(ws)

        while True:
            choice = ask_next(args, auto)
            if choice != "redo":
                break
            spec = _ask("要重做哪些镜？（如 1-10,15）")
            code = redo_shots(config, ws, spec, args, only=only_arg)
            report_assets(ws)
    except StudioError as exc:
        print(f"\n向导中止：{exc}")
        return 2

    if choice == "stop":
        print(f"\n停在这里。接着来：`lvs studio --task {ws.task}`（已完成的阶段会自动跳过）")
        return code

    for label, run in (
        ("voice", lambda: _run_stage("voice", config, ws, force=False)),
        ("build", lambda: _run_stage("build", config, ws, force=False)),
    ):
        print(f"\n=== {label} ===")
        code = run()
        if code != 0:
            return code

    print(f"\n🎬 成片：{ws.path('final.mp4')}")
    return 0
