"""图文卡片渲染入口（票据 28 / 34）。

对调用方只有一件事：**给我一张 `cards.Card`，还你一张 1920×1080 的 PNG**。

出图有两条路：

1. **HTML/CSS（首选）** —— `cardhtml` 用本机无头浏览器截图。版式自由、真字体，
   是「好看」这条要求的主要载体。
2. **Pillow（回退）** —— 没装浏览器、或截图失败时用。等价版式，观感朴素，
   但保证在纯 Python 环境里也能出片。

两条路的输入是同一个 `Card`，所以**内容逻辑只有一份**（在 `cards` 里），
换渲染器不会让卡上出现的东西变样。

设计取舍：
- 配色与成片一致（暖墨底 + 朱砂单点缀）；圆角统一。
- **没有标题、没有页脚**：段落标题是脚本脚手架，页脚旁白与烧录字幕重复。
- `Pillow` 未装且没有浏览器时 `available()` 为假，调用方降级为占位帧，不硬崩。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from lvs import cardhtml, cards

W, H = 1920, 1080

BG = (18, 20, 24)
FG = (236, 234, 228)
MUTED = (146, 146, 140)
ACCENT = (214, 168, 92)
PANEL = (30, 33, 38)

# 一个异常类型覆盖两条路：调用方只 need `except graphic.GraphicError`
GraphicError = cardhtml.CardError

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\NotoSerifSC-VF.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
)

# 条目上限只有一处定义（在 `cards`）：`Card.items` 出厂就 ≤ cards.MAX_ITEMS，
# 回退版式再按自己的数字截一遍只会漂（原先这里是 5，与 cards 的 4 不一致，且永远截不到）
MAX_ITEMS = cards.MAX_ITEMS


# ---- 字体 ------------------------------------------------------------------


@lru_cache(maxsize=1)
def _font_path() -> str | None:
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def _pillow_available() -> bool:
    try:
        import PIL.Image  # noqa: F401
        import PIL.ImageDraw  # noqa: F401
        import PIL.ImageFont  # noqa: F401 - 探测式导入（pyflakes 不认 noqa）
    except ModuleNotFoundError:
        return False
    return _font_path() is not None


def available() -> bool:
    """任一条渲染路可走即可：有浏览器走 HTML，否则至少还有 Pillow。"""
    return cardhtml.available() or _pillow_available()


@lru_cache(maxsize=48)
def _font(size: int):
    from PIL import ImageFont

    path = _font_path()
    if not path:
        raise GraphicError("找不到可用的中文字体（试过 NotoSerif/msyh/simhei/simsun/PingFang）")
    return ImageFont.truetype(path, size)


# ---- 文本工具 --------------------------------------------------------------


def _wrap(draw, text: str, font, max_w: float) -> list[str]:
    """按字宽断行（中文逐字；英文尽量在空格处断）。"""
    lines: list[str] = []
    cur = ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if cur and draw.textlength(cur + ch, font=font) > max_w:
            if ch == " ":
                lines.append(cur)
                cur = ""
                continue
            cut = cur.rfind(" ")
            # 那 12 不是魔法数：只有"最后这半截词本身还不够长"时才退到空格断行；
            # 若半截词已超阈值，说明它比一整行还长，退格也放不下，只能硬切。
            # （实测：正常英文在空格断、超长词才切开、中文逐字断 —— 见 WrapTest。）
            if 0 < cut > len(cur) - 12:
                lines.append(cur[:cut])
                cur = cur[cut + 1 :] + ch
            else:
                lines.append(cur)
                cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def _fit(draw, text: str, max_w: float, max_h: float, size: int, min_size: int = 22) -> tuple[list[str], int]:
    """自动缩字号直到放进 (max_w, max_h)。返回 (行列表, 最终字号)。"""
    while size > min_size:
        font = _font(size)
        lines = _wrap(draw, text, font, max_w)
        line_h = int(size * 1.35)
        if len(lines) * line_h <= max_h:
            return lines, size
        size -= 4
    font = _font(min_size)
    return _wrap(draw, text, font, max_w), min_size


def _block(draw, lines: list[str], size: int, cx: int, top: int, fill, center: bool = True) -> None:
    font = _font(size)
    line_h = int(size * 1.35)
    for i, line in enumerate(lines):
        x = cx - draw.textlength(line, font=font) / 2 if center else cx
        draw.text((x, top + i * line_h), line, font=font, fill=fill)


def ellipsize(text: str, measure, max_w: float) -> str:
    """把 `text` 截到宽度 ≤ `max_w`（用 `measure(text)` 量宽），末尾加省略号。

    连省略号都放不下时返回空串 —— 宁可不出字，也不能溢出边框。
    """
    text = text or ""
    if max_w <= 0:
        return ""
    if measure(text) <= max_w:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if measure(text[:mid] + "…") <= max_w:
            low = mid
        else:
            high = mid - 1
    return text[:low] + "…" if low > 0 else ""


# ---- Pillow 版式（回退） ---------------------------------------------------

MARGIN = 150


def _frame(draw) -> None:
    """只留一条强调线；不再有标题与右下角标签。"""
    draw.rectangle([60, 60, W - 60, H - 60], outline=(44, 47, 52), width=2)
    draw.rectangle([60, 60, 60 + 180, 60 + 8], fill=ACCENT)


def _draw_statement(draw, items: list[str]) -> None:
    """数据行：每项左侧一条朱砂短竖线，值大、单位小。"""
    items = items or [""]
    font_u = _font(32)
    gap = 90
    widths = []
    for item in items:
        parts = cards.split_num(item)
        if parts:
            number, unit = parts
            widths.append((number, unit, draw.textlength(number, font=_font(150)), draw.textlength(unit, font=font_u)))
        else:
            widths.append((item, "", draw.textlength(item, font=_font(90)), 0.0))
    total = sum(w[2] + w[3] + (14 if w[3] else 0) + 36 for w in widths) + gap * max(0, len(items) - 1)
    if total > W - 2 * MARGIN:                      # 放不下就整体缩一号
        scale = (W - 2 * MARGIN) / total
        for i, (number, unit, nw, uw) in enumerate(widths):
            size = max(30, int((150 if unit else 90) * scale))
            widths[i] = (number, unit, draw.textlength(number, font=_font(size)), draw.textlength(unit, font=font_u))
    total = sum(w[2] + w[3] + (14 if w[3] else 0) + 36 for w in widths) + gap * max(0, len(items) - 1)

    x = (W - total) / 2
    for number, unit, nw, uw in widths:
        size = 150
        while size > 30 and draw.textlength(number, font=_font(size)) > nw + 1:
            size -= 4
        if not unit:
            size = 90
            while size > 24 and draw.textlength(number, font=_font(size)) > nw + 1:
                size -= 2
        draw.rectangle([x, H / 2 - 70, x + 4, H / 2 + 70], fill=ACCENT)
        draw.text((x + 36, H / 2 - 70), number, font=_font(size), fill=FG)
        if unit:
            draw.text((x + 36 + nw + 14, H / 2 + 10), unit, font=font_u, fill=MUTED)
        x += nw + uw + (14 if unit else 0) + 36 + gap


def _draw_compare(draw, items: list[str]) -> None:
    items = (items or [""])[:2]
    n = len(items)
    gap = 34
    total = W - 2 * MARGIN
    col_w = (total - gap * (n - 1)) // max(1, n)
    top, bottom = 380, H - 380
    for i, item in enumerate(items):
        x = MARGIN + i * (col_w + gap)
        draw.rounded_rectangle([x, top, x + col_w, bottom], radius=12, fill=PANEL, outline=(60, 64, 70), width=1)
        if i == 0:
            draw.rectangle([x, top + 56, x + 4, bottom - 56], fill=ACCENT)
        lines = [seg.strip() for seg in item.split("/") if seg.strip()] or [item]
        y = top + 56
        for j, line in enumerate(lines):
            size = 108 if j == 0 else 54
            fitted, size = _fit(draw, line, col_w - 120, 200, size, 24)
            fill = FG if j == 0 else MUTED
            for k, text in enumerate(fitted):
                draw.text((x + 64, y), text, font=_font(size), fill=fill)
                y += int(size * 1.3)
            y += 12


def _draw_timeline(draw, items: list[str]) -> None:
    items = (items or [""])[:MAX_ITEMS]
    n = len(items)
    y = H // 2
    x0, x1 = MARGIN + 60, W - MARGIN - 60
    draw.line([x0, y, x1, y], fill=(70, 74, 80), width=2)
    step = 0 if n == 1 else (x1 - x0) / (n - 1)
    for i, item in enumerate(items):
        cx = int(x0 + i * step)
        draw.ellipse([cx - 11, y - 11, cx + 11, y + 11], fill=ACCENT)
        lines, size = _fit(draw, item, step * 0.9 if n > 1 else 600, 200, 78, 24)
        _block(draw, lines, size, cx, y + 52, FG)


def _draw_list(draw, items: list[str]) -> None:
    items = (items or [""])[:MAX_ITEMS]
    n = len(items)
    top, bottom = 300, H - 300
    draw.rounded_rectangle([MARGIN, top, W - MARGIN, bottom], radius=12,
                           fill=PANEL, outline=(60, 64, 70), width=1)
    row_h = (bottom - top) / max(1, n)
    for i, item in enumerate(items):
        cy = top + row_h * i
        if i:
            draw.line([MARGIN, cy, W - MARGIN, cy], fill=(52, 56, 62), width=1)
        draw.rectangle([MARGIN + 46, cy + row_h * 0.32, MARGIN + 49, cy + row_h * 0.68], fill=ACCENT)
        lines, size = _fit(draw, item, W - 2 * MARGIN - 150, row_h - 40, 44, 22)
        _block(draw, lines, size, MARGIN + 84, cy + (row_h - len(lines) * size * 1.35) / 2, FG, center=False)


_LAYOUTS = {
    cards.KIND_STATEMENT: _draw_statement,
    cards.KIND_COMPARE: _draw_compare,
    cards.KIND_TIMELINE: _draw_timeline,
    cards.KIND_LIST: _draw_list,
}


def _render_pillow(card: cards.Card, out_path: Path) -> Path:
    if not _pillow_available():
        raise GraphicError("Pillow 未安装或系统缺中文字体，无法渲染图文卡片（可 pip install Pillow）")

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(image)
    _frame(draw)
    (_LAYOUTS.get(card.kind) or _draw_statement)(draw, list(card.items))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path, "PNG")
    return out_path


# ---- 入口 ------------------------------------------------------------------


def _assert_rendered(path: Path) -> None:
    """D23：产物必须**真的读一遍**才算数 —— 存在且非空不等于是一张能用的图。

    Pillow 路径同样是"写成功即返回"，浏览器路径只看 `size > 0`；
    这里统一解码一次并核对尺寸，坏图当场抛错（调用方按失败镜处理）。
    """
    try:
        from PIL import Image

        with Image.open(path) as im:
            size = im.size
            im.verify()
    except Exception as exc:                        # noqa: BLE001 - 任何解码失败都算坏产物
        raise GraphicError(f"卡片产物无法解码：{path}（{exc}）") from exc
    if size != (W, H):
        raise GraphicError(f"卡片尺寸不对：{path} 是 {size}，应为 {(W, H)}")


_warned_fallback = False


def render(card: cards.Card, out_path: Path) -> Path:
    """把 `card` 渲染成 1920×1080 PNG。

    首选 HTML/CSS（好看得多）；没有浏览器、或浏览器截图失败则回退 Pillow。
    **回退会喊一声**：两套版式长得不一样，混在同一条片子里而不出声，等于悄悄降级。
    """
    global _warned_fallback

    path: Path | None = None
    if cardhtml.available():
        try:
            path = cardhtml.render(card, out_path)
            # D23 的校验**必须在 try 里**：截图命令返回成功、产物却是张坏图时，
            # 要退回去用 Pillow，而不是把整镜判失败（票 42）。
            _assert_rendered(path)
        except GraphicError as exc:
            # 浏览器在但没截出可用的图 → 别把整条链拖死，退回 Pillow。
            # 但要留痕：可能是浏览器慢/挂了，静默降级会让整片观感不一致且查不出原因。
            path = None
            reason = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
            if not _warned_fallback:
                print(f"[卡片] HTML 渲染失败，本次改用 Pillow 回退版式（观感会不同）：{reason}")
                _warned_fallback = True
    if path is None:
        path = _render_pillow(card, out_path)

    _assert_rendered(path)
    return path

# ---- 对外公开的字体 / 断行 API ----------------------------------------------
#
# ★ 为什么要有这两个薄封装：`publish`（投稿封面叠字）要用的正是**这套**
#   字体链与断行规则。直接 import 私有名 `_font` / `_wrap` 等于把"同一件事"
#   在两个模块里各写一遍的前奏 —— 这个项目在这一点上吃过太多次亏。
#   所以加的是**别名**（零逻辑），而不是复制一份实现。


def font_path() -> str | None:
    """内置中文字体链里第一个可用的字体文件；一个都没有则 `None`。"""
    return _font_path()


def font(size: int, *, path: str | None = None):  # noqa: ANN201 - PIL.ImageFont.FreeTypeFont
    """字号 → 字体对象。`path` 给定时用它（封面要指定字体），否则走内置字体链。"""
    if path is None:
        return _font(size)
    from PIL import ImageFont

    return ImageFont.truetype(str(path), size)


def wrap(draw, text: str, font, max_w: float) -> list[str]:  # noqa: ANN001
    """按字宽断行（中文逐字 / 英文尽量在空格断）。"""
    return _wrap(draw, text, font, max_w)


def fit(draw, text: str, max_w: float, max_h: float, size: int,  # noqa: ANN001
        min_size: int = 22) -> tuple[list[str], int]:
    """自动缩字号直到放进 (max_w, max_h)。返回 (行列表, 最终字号)。"""
    return _fit(draw, text, max_w, max_h, size, min_size)
