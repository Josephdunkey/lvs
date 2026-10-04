"""HTML/CSS 卡片渲染（票据 34）。

把 `cards.Card` 排成 1920×1080 的 PNG —— 用本机已装的无头浏览器
（Edge / Chrome）截图，而不是用 Pillow 画。换来的东西很实在：
真字体（思源宋体 / 思源黑体）、渐变、阴影、网格、自适应字号。

设计取向：**墨色宋体的气质 + 规整面板的结构**。
- 暖墨底 + 宋体显示字（内容本就是古籍，宋体在这里是有理由的，不是"creative 就上衬线"）
- 唯一强调色：朱砂红，只用来做"主值"的短竖线，不做发光、不做第二强调色
- 圆角统一 12px；面板只用 1px 细边 + 极浅底色
- **没有标题、没有页脚**：段落标题是脚本脚手架，页脚旁白与烧录字幕重复

浏览器缺失时 `available()` 为假，调用方（`graphic`）回退到 Pillow 版式。
"""

from __future__ import annotations

import html
import os
import shutil
import subprocess
import tempfile
import time
from functools import lru_cache
from pathlib import Path

from lvs import cards
from lvs.errors import LvsError, EXIT_FAILED

W, H = 1920, 1080

MARGIN = 150
GAP = 90
PANEL_RADIUS = 12

FONT_SERIF = '"Noto Serif SC","Source Han Serif SC","STZhongsong","STSong","SimSun",serif'
FONT_SANS = '"Noto Sans SC","Source Han Sans SC","Microsoft YaHei",sans-serif'

ACCENT = "#c85a41"
FG = "#ece4d6"
MUTED = "rgba(236,228,214,.48)"
HAIR = "rgba(236,228,214,.11)"
PANEL_BG = "rgba(236,228,214,.05)"

# 环境变量优先；再按平台常见安装路径找
_BROWSER_CANDIDATES = (
    os.environ.get("LVS_BROWSER", ""),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)


class CardError(LvsError, RuntimeError):
    """卡片渲染失败（找不到浏览器 / 截图没产出 / 浏览器报错）。"""

    exit_code = EXIT_FAILED


# ---- 浏览器探测 ------------------------------------------------------------


@lru_cache(maxsize=1)
def browser() -> str | None:
    """返回可用的无头浏览器可执行文件；找不到返回 `None`。"""
    for candidate in _BROWSER_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    for name in ("msedge", "google-chrome", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def available() -> bool:
    return browser() is not None


# ---- 样式与版式 ------------------------------------------------------------

_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:1920px;height:1080px;overflow:hidden}
.card{position:relative;width:1920px;height:1080px;overflow:hidden;
  color:#ece4d6;
  font-family:"Noto Serif SC","Source Han Serif SC","STZhongsong","STSong","SimSun",serif;
  background:
    radial-gradient(1500px 950px at 8% -12%, #241d19 0%, rgba(22,18,16,0) 62%),
    linear-gradient(173deg,#171310 0%,#0d0b0a 100%);}
.vignette{position:absolute;inset:0;
  background:radial-gradient(1250px 820px at 50% 48%, rgba(0,0,0,0) 42%, rgba(0,0,0,.52) 100%);}
.stage{position:absolute;inset:0;display:flex;flex-direction:column;justify-content:center;
  padding:0 150px;}
/* 朱砂短竖线 = 唯一的强调语汇，三个版式共用 */
.tick{position:absolute;width:4px;background:#c85a41}

/* statement：2~3 个短值横排 */
.row{display:flex;align-items:flex-end;gap:90px;flex-wrap:nowrap}
.datum{position:relative;padding-left:36px;line-height:1}
.datum .tick{left:0;top:12%;bottom:12%}
.datum .n{font-weight:700;letter-spacing:.01em;font-variant-numeric:tabular-nums;white-space:nowrap}
.datum .u{font-family:"Noto Sans SC","Source Han Sans SC","Microsoft YaHei",sans-serif;
  font-size:30px;letter-spacing:.14em;color:rgba(236,228,214,.5);margin-left:18px;white-space:nowrap}

/* compare：两组左右分栏 */
.panels{display:grid;grid-template-columns:1fr 1fr;gap:34px}
.panel{position:relative;background:rgba(236,228,214,.05);border:1px solid rgba(236,228,214,.11);
  border-radius:12px;padding:64px 56px;display:flex;flex-direction:column;
  justify-content:center;gap:22px;min-height:320px}
.panel .tick{left:0;top:56px;bottom:56px}
.panel.hi{padding-left:64px}
.panel .v{font-weight:700;line-height:1.16;font-variant-numeric:tabular-nums}
/* 第二行往后降级：否则「因」和「果」一样大，读起来没有主次 */
.panel .v2{font-weight:700;line-height:1.32;color:rgba(236,228,214,.6);
  font-variant-numeric:tabular-nums}

/* timeline：横向时间轴 */
.nodes{position:relative;display:flex;justify-content:space-between;align-items:flex-start}
.nodes::before{content:"";position:absolute;left:6%;right:6%;top:10px;height:2px;
  background:rgba(236,228,214,.2)}
.node{flex:1;display:flex;flex-direction:column;align-items:center;text-align:center}
.node .dot{width:22px;height:22px;border-radius:50%;background:#c85a41;margin-bottom:48px;
  box-shadow:0 0 0 10px rgba(200,90,65,.13)}
.node .lab{font-weight:700;letter-spacing:.02em;line-height:1.25;padding:0 14px;
  font-variant-numeric:tabular-nums}

/* list：3~4 行竖排条目 */
.list{background:rgba(236,228,214,.05);border:1px solid rgba(236,228,214,.11);
  border-radius:12px;padding:22px 0}
.li{position:relative;padding:32px 60px 32px 84px;line-height:1.5}
.li + .li{border-top:1px solid rgba(236,228,214,.09)}
.li .tick{left:46px;top:32%;bottom:32%}
"""

_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
/*CSS*/
</style></head>
<body><div class="card"><div class="vignette"></div>
<div class="stage">
/*BODY*/
</div></div>
<script>
/* 自适应字号：**让浏览器自己量**（Python 估宽估错了一次，实测才是真的）。
   `[data-fitbox]` 是量体，`[data-shrink]` 是要缩的字，`[data-fitcell]` 是各自独立的格子。 */
(function () {
  function over(box) {
    if (box.scrollWidth > box.clientWidth + 1) return true;
    var maxh = parseInt(box.dataset.maxh || "0", 10);
    if (maxh && box.scrollHeight > maxh) return true;
    var cells = box.querySelectorAll("[data-fitcell]");
    for (var i = 0; i < cells.length; i++) {
      if (cells[i].scrollWidth > cells[i].clientWidth + 1) return true;
    }
    return false;
  }
  var boxes = document.querySelectorAll("[data-fitbox]");
  for (var b = 0; b < boxes.length; b++) {
    var box = boxes[b];
    var targets = box.querySelectorAll("[data-shrink]");
    if (!targets.length) continue;
    for (var guard = 0; guard < 200 && over(box); guard++) {
      var shrunk = false;
      for (var t = 0; t < targets.length; t++) {
        var el = targets[t];
        var size = parseFloat(window.getComputedStyle(el).fontSize);
        if (size > 22) { el.style.fontSize = (size - 2) + "px"; shrunk = true; }
      }
      if (!shrunk) break;
    }
  }
})();
</script>
</body></html>
"""


def _esc(text: str) -> str:
    return html.escape(text or "", quote=True)


# 起始字号：给得偏大，交给页内脚本按实测缩到刚好放得下
_START_STATEMENT = 150
_START_COMPARE = 108
_START_TIMELINE = 78
_START_LIST = 44


def _body_statement(card: cards.Card) -> str:
    out = []
    for item in card.items:
        parts = cards.split_num(item)
        if parts:
            inner = (
                f'<span class="n" data-shrink style="font-size:{_START_STATEMENT}px">{_esc(parts[0])}</span>'
                f'<span class="u">{_esc(parts[1])}</span>'
            )
        else:
            inner = f'<span class="n" data-shrink style="font-size:{_START_STATEMENT}px">{_esc(item)}</span>'
        out.append(f'<div class="datum"><span class="tick"></span>{inner}</div>')
    return '<div class="row" data-fitbox>' + "".join(out) + "</div>"


def _body_compare(card: cards.Card) -> str:
    out = []
    for i, item in enumerate(list(card.items)[:2]):
        lines = [seg.strip() for seg in item.split("/") if seg.strip()] or [item]
        vals = []
        for j, line in enumerate(lines):
            cls = "v" if j == 0 else "v2"
            size = _START_COMPARE if j == 0 else int(_START_COMPARE * 0.5)
            vals.append(f'<div class="{cls}" data-shrink style="font-size:{size}px">{_esc(line)}</div>')
        cls = "panel hi" if i == 0 else "panel"
        tick = '<span class="tick"></span>' if i == 0 else ""
        out.append(f'<div class="{cls}" data-fitcell>{tick}{"".join(vals)}</div>')
    return '<div class="panels" data-fitbox>' + "".join(out) + "</div>"


def _body_timeline(card: cards.Card) -> str:
    items = list(card.items) or [""]
    nodes = "".join(
        f'<div class="node" data-fitcell><span class="dot"></span>'
        f'<span class="lab" data-shrink style="font-size:{_START_TIMELINE}px">{_esc(item)}</span></div>'
        for item in items
    )
    return f'<div class="nodes" data-fitbox>{nodes}</div>'


def _body_list(card: cards.Card) -> str:
    items = list(card.items) or [""]
    rows = "".join(
        f'<div class="li"><span class="tick"></span>'
        f'<span data-shrink style="font-size:{_START_LIST}px">{_esc(item)}</span></div>'
        for item in items
    )
    return f'<div class="list" data-fitbox data-maxh="880">{rows}</div>'


LAYOUTS: dict[str, object] = {
    cards.KIND_STATEMENT: _body_statement,
    cards.KIND_COMPARE: _body_compare,
    cards.KIND_TIMELINE: _body_timeline,
    cards.KIND_LIST: _body_list,
}


def html_for(card: cards.Card) -> str:
    """把一张卡排成完整 HTML 文档（尚未栅格化）。"""
    build = LAYOUTS.get(card.kind) or _body_statement
    body = build(card)  # type: ignore[operator]
    return _PAGE.replace("/*CSS*/", _CSS).replace("/*BODY*/", body)


# ---- 栅格化 ----------------------------------------------------------------


def _shoot(exe: str, html_text: str, out: Path) -> None:
    """把 HTML 截成 PNG。浏览器偶尔先退出后落盘，所以查两次。"""
    out.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as td:
        page = Path(td) / "card.html"
        page.write_text(html_text, encoding="utf-8")
        cmd = [
            exe,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={Path(td) / 'profile'}",
            f"--screenshot={out}",
            f"--window-size={W},{H}",
            page.as_uri(),
        ]
        subprocess.run(cmd, capture_output=True, text=True, errors="ignore")
        for _ in range(10):
            if out.is_file() and out.stat().st_size > 0:
                return
            time.sleep(0.2)


def render(card: cards.Card, out_path: Path) -> Path:
    """把 `card` 渲染成 1920×1080 PNG，返回写入路径。"""
    exe = browser()
    if not exe:
        raise CardError("找不到可用的无头浏览器（Edge / Chrome）；可用环境变量 LVS_BROWSER 指定")

    # 必须绝对路径：浏览器解析 `--screenshot=` 用的是它自己的 CWD
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    page = html_for(card)
    for _ in range(2):
        _shoot(exe, page, out_path)
        if out_path.is_file() and out_path.stat().st_size > 0:
            return out_path
    raise CardError(f"浏览器没有产出截图：{out_path}")
