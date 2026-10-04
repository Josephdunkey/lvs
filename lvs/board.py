"""看板（`lvs board`）—— 把流水线进度摊成一块看板。

长视频跑起来动辄几十分钟，光看滚动的日志很难一眼知道"到哪了"。这个命令把
`.work/<task>/` 里的状态（`manifest.json` + `shots.json`）读出来，渲染成：

- **终端看板**：五个阶段各一行（状态 + 明细 + 进度条），末尾一行分镜统计
- **HTML 看板**（`--html`）：自包含单文件，五列卡片 + 分镜进度，方便贴给别人看

纯读取，不改任何产物。缺失的状态按"未开始"显示，不报错。
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from typing import Any

from lvs.progress import render_bar
from lvs.workspace import Workspace

# ★ 阶段顺序**不在这里定义** —— 唯一真源是 `lvs.stage.ORDER`。
#
# 这一行原先是第 6 份阶段顺序副本（另外五份在 cli / gui-jobs / run / pipeline / workspace）。
# 加一个阶段要改六处，漏一处就静默不一致 —— 看板会少一栏，而且不报错。
# `tests/test_architecture.py::test_no_module_hardcodes_the_stage_order` 现在会拦住它。
from lvs.stage import BY_NAME, ORDER as STAGE_ORDER

# 阶段显示名也从阶段表来（`(短名, 名字)`）—— 与顺序一样，只该有一处定义。
STAGE_LABEL = {n: (BY_NAME[n].short or BY_NAME[n].title, n) for n in STAGE_ORDER}
# 状态 → (图标, 中文)
STATUS_STYLE = {
    "done": ("✅", "已完成"),
    "partial": ("🟡", "有失败"),
    "failed": ("❌", "失败"),
    "skipped": ("⏭️", "已跳过"),
    "todo": ("⬜", "未开始"),
}


@dataclass
class StageCard:
    stage: str
    status: str
    detail: str = ""
    done: int = 0
    total: int = 0

    @property
    def label(self) -> str:
        return STAGE_LABEL.get(self.stage, (self.stage, self.stage))[0]

    @property
    def icon(self) -> str:
        return STATUS_STYLE.get(self.status, STATUS_STYLE["todo"])[0]

    @property
    def status_text(self) -> str:
        # `partial` 有两种含义，给人看到的下一步不同：
        #   停在半路（done < total）→ 接着跑；跑完但有失败 → 去修那几镜（票 26）
        if self.status == "partial" and self.total and self.done < self.total:
            return "部分完成"
        return STATUS_STYLE.get(self.status, STATUS_STYLE["todo"])[1]


@dataclass
class Board:
    task: str
    stages: list[StageCard] = field(default_factory=list)
    shots_total: int = 0
    shots_ready: int = 0      # 已配到素材
    shots_failed: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    title: str = ""

    @property
    def shots_pending(self) -> int:
        return max(0, self.shots_total - self.shots_ready - self.shots_failed)


# ---- 采集 ------------------------------------------------------------------


def _done_count(entry: dict[str, Any]) -> int:
    """阶段已完成的镜数。**唯一**的计数口径：素材与配音都写 `done`（票 42 审查）。"""
    return int(entry.get("done", 0) or 0)


def _stage_detail(stage: str, entry: dict[str, Any], ctx: dict[str, Any]) -> str:
    if stage == "parse":
        return f"正文 {ctx.get('segments', 0)} 段"
    if stage == "shots":
        return f"{ctx.get('total', 0)} 镜"
    if stage in ("assets", "voice"):
        total = int(entry.get("total", ctx.get("total", 0)) or 0)
        ok = _done_count(entry)
        skipped = int(entry.get("skipped", 0) or 0)
        failed = int(entry.get("failed", 0) or 0)
        bits = [f"{ok + skipped}/{total}"]
        if failed:
            bits.append(f"失败 {failed}")
        return "  ".join(bits)
    if stage == "build":
        dur = entry.get("duration")
        if dur:
            burned = "烧字幕" if entry.get("subtitle_burned") else "无字幕"
            return f"{dur:.1f}s（{burned}）"
        return ""
    return ""


def collect(ws: Workspace) -> Board:
    """从 manifest + shots.json 汇总出一块看板。缺文件一律降级，不抛错。"""
    manifest = ws.manifest or {}
    stages_raw = manifest.get("stages", {})

    ctx: dict[str, Any] = {"segments": 0, "total": 0}
    parse_path = ws.path("parse.json")
    if parse_path.is_file():
        try:
            ctx["segments"] = len(json.loads(parse_path.read_text(encoding="utf-8")).get("segments", []))
        except (OSError, json.JSONDecodeError):
            pass

    board = Board(task=ws.task)
    shots_data: dict[str, Any] = {}
    shots_path = ws.path("shots.json")
    if shots_path.is_file():
        try:
            shots_data = json.loads(shots_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            shots_data = {}
    shots = shots_data.get("shots", [])
    ctx["total"] = len(shots)
    board.title = str(shots_data.get("title") or manifest.get("title") or "")

    for stage in STAGE_ORDER:
        entry = stages_raw.get(stage)
        if not entry:
            board.stages.append(StageCard(stage, "todo"))
            continue
        status = str(entry.get("status", "done"))
        # 只有逐镜的阶段（素材/配音）才有"n/N"进度条
        if stage in ("assets", "voice"):
            total = int(entry.get("total", ctx["total"]) or 0)
            done = _done_count(entry) + int(entry.get("skipped", 0) or 0)
        else:
            total, done = 0, 0
        board.stages.append(
            StageCard(stage=stage, status=status,
                      detail=_stage_detail(stage, entry, ctx), done=done, total=total)
        )

    board.shots_total = len(shots)
    board.shots_ready = sum(1 for s in shots if s.get("asset_path"))
    board.shots_failed = sum(1 for s in shots if s.get("status") == "failed")
    tally: dict[str, int] = {}
    for s in shots:
        key = s.get("resolved_by") or (s.get("source") if s.get("source") else None)
        if key:
            tally[str(key)] = tally.get(str(key), 0) + 1
    board.by_source = dict(sorted(tally.items(), key=lambda kv: -kv[1]))
    return board


# ---- 终端渲染 --------------------------------------------------------------


def render_text(board: Board) -> str:
    lines: list[str] = []
    head = f"看板 · 任务 {board.task}"
    if board.title and board.title != board.task:
        head += f"　「{board.title}」"
    lines.append(head)
    lines.append("")
    for card in board.stages:
        bar = ""
        if card.total > 0:
            bar = "  " + render_bar(card.done, card.total, 20)
        name = f"{card.label:<2}"
        lines.append(f"  {card.icon} {name} {card.status_text:<4}{bar}  {card.detail}".rstrip())
    lines.append("")
    if board.shots_total:
        pct = int(100 * board.shots_ready / board.shots_total)
        lines.append(
            f"  分镜 {board.shots_total}：已配 {board.shots_ready}（{pct}%）"
            f"｜未配 {board.shots_pending}｜失败 {board.shots_failed}"
        )
        if board.by_source:
            lines.append("  来源：" + "，".join(f"{k} {v}" for k, v in board.by_source.items()))
    else:
        lines.append("  还没有 shots.json —— 先跑 `lvs shots`（或 `lvs run <拍摄稿.md>`）。")
    return "\n".join(lines)


# ---- HTML 渲染 -------------------------------------------------------------

_CSS = """
:root{--bg:#f6f7f9;--fg:#1c2024;--muted:#6b7280;--card:#fff;--line:#e5e7eb;
--done:#16a34a;--partial:#d97706;--todo:#9ca3af;--fail:#dc2626;--accent:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--fg:#e6e8eb;--muted:#9aa3ad;--card:#171b21;
--line:#262c35;--done:#4ade80;--partial:#fbbf24;--todo:#5b6470;--fail:#f87171;--accent:#60a5fa}}
*{box-sizing:border-box}
body{margin:0;padding:28px;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin-bottom:20px}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.card .name{font-weight:600;font-size:15px;display:flex;justify-content:space-between;align-items:center}
.card .st{font-size:12px;color:var(--muted);margin-top:2px}
.card .detail{font-size:13px;margin-top:10px;color:var(--fg)}
.bar{height:8px;border-radius:6px;background:var(--line);overflow:hidden;margin-top:10px}
.bar>i{display:block;height:100%;background:var(--done)}
.s-done{color:var(--done)}.s-partial{color:var(--partial)}.s-todo{color:var(--todo)}
.s-failed{color:var(--fail)}.s-skipped{color:var(--todo)}
.stats{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.stats .big{font-size:26px;font-weight:700}
.stats .row{display:flex;gap:22px;flex-wrap:wrap;margin-top:6px}
.stats .k{color:var(--muted);font-size:12px}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;background:var(--line);
font-size:12px;margin:2px 4px 0 0}
"""

_STATUS_CLASS = {"done": "s-done", "partial": "s-partial", "failed": "s-failed",
                 "skipped": "s-skipped", "todo": "s-todo"}


def render_html(board: Board) -> str:
    def esc(x: Any) -> str:
        return html.escape(str(x))

    cards = []
    for c in board.stages:
        pct = int(100 * c.done / c.total) if c.total else 0
        fill = (
            f'<div class="bar"><i style="width:{pct}%"></i></div>' if c.total else ""
        )
        cards.append(
            f'<div class="card"><div class="name"><span>{esc(c.label)}</span>'
            f'<span class="{_STATUS_CLASS.get(c.status, "s-todo")}">'
            f'{esc(c.icon)} {esc(c.status_text)}</span></div>'
            f'<div class="st">{esc(STAGE_LABEL.get(c.stage, ("", c.stage))[1])}</div>'
            f'<div class="detail">{esc(c.detail) or "&nbsp;"}</div>{fill}</div>'
        )

    pills = "".join(f'<span class="pill">{esc(k)} {v}</span>' for k, v in board.by_source.items())
    pct = int(100 * board.shots_ready / board.shots_total) if board.shots_total else 0
    title = f"看板 · {esc(board.task)}"
    sub = f'任务 <code>{esc(board.task)}</code>'
    if board.title and board.title != board.task:
        sub += f' ｜ {esc(board.title)}'
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{_CSS}</style></head>
<body>
<h1>{title}</h1><div class="sub">{sub}</div>
<div class="cols">{"".join(cards)}</div>
<div class="stats">
  <div class="big">{board.shots_ready}/{board.shots_total} <span class="k">镜已配素材（{pct}%）</span></div>
  <div class="row">
    <div><div class="k">未配</div><div>{board.shots_pending}</div></div>
    <div><div class="k">失败</div><div>{board.shots_failed}</div></div>
    <div><div class="k">总数</div><div>{board.shots_total}</div></div>
  </div>
  <div style="margin-top:12px">{pills}</div>
</div>
</body></html>
"""


# ---- 命令入口 --------------------------------------------------------------


def as_envelope(board: "Board", ws: Workspace) -> dict[str, Any]:
    """看板的机器可读形态 —— 给 agent 的**进度与下一步**。

    为什么单独做这个：`gate --json` 回答的是"**下一道门**"，
    而 agent 在长任务里还需要回答"**现在整体到哪了 / 有没有失败件**"。
    没有它，agent 只能去解析中文散文里的 ✅⬜ 方块字 —— 最脆的那种依赖。
    """
    stages = [
        {
            "stage": c.stage,
            "label": c.label,
            "status": c.status,
            "done": c.done,
            "total": c.total,
            "detail": c.detail,
        }
        for c in board.stages
    ]
    done_stages = [c["stage"] for c in stages if c["status"] == "done"]
    todo_stages = [c["stage"] for c in stages if c["status"] not in ("done", "skipped")]
    return {
        "task": board.task,
        "stages": stages,
        "stages_done": done_stages,
        "stages_todo": todo_stages,
        "next_stage": todo_stages[0] if todo_stages else "",
        "shots": {
            "total": board.shots_total,
            "ready": board.shots_ready,
            "failed": board.shots_failed,
            "by_source": dict(board.by_source),
        },
    }


def run_command(config, ws: Workspace, args) -> int:  # noqa: ANN001 - 由 cli 传入
    board = collect(ws)
    if getattr(args, "json", False):
        # 机器可读：**只打 JSON**（agent 靠它判断进度与下一步，不能被散文掺进来）。
        print(json.dumps(as_envelope(board, ws), ensure_ascii=False, indent=2))
        return 0
    print(render_text(board))
    if getattr(args, "html", False):
        out = ws.path("board.html")
        out.parent.mkdir(parents=True, exist_ok=True)  # --html 是显式要落盘，允许建目录
        out.write_text(render_html(board), encoding="utf-8")
        print(f"\nHTML 看板：{out}")
    return 0
