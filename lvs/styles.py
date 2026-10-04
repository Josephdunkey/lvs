"""画面风格注册表 —— 让"换题材"不需要改代码。

## 为什么要有这个模块

风格预设原先**写死在 `shots.py` 的 `STYLES` 字典里**，只有 3 个。后果是：
每做一个新题材（历史纪录片 → 日系黑白漫画 → 厚涂油画 → 水墨 → 赛博），
都要**改代码、发版**才能出图。这对"以后也会用于各种项目"是硬伤。

现在风格有四个来源，按**具体程度**排序（越贴近内容越优先）：

| 优先级 | 来源 | 谁写 | 用途 |
|---|---|---|---|
| 1 | `--style-file` | 命令行 | 临时试风格 |
| 2 | `[styles.<name>]`（config 内联） | 装机级 | 本机常做的题材 |
| 3 | `<素材库>/00-设定/风格预设.toml` | **项目级** | 这本书的视觉设定（跟素材走） |
| 4 | `BUILTIN`（本模块，29 个） | 代码 | 开箱可用的兜底 |

★ 项目级排在 config 内联之上：风格是"这本书长什么样"的一部分，
它应该和定妆卡、拍摄稿放在一起，而不是散在装机配置里。

## 风格文件格式

```toml
# <素材库>/00-设定/风格预设.toml
default = "ink-wash"

[styles.ink-wash]
suffix = "chinese ink wash painting, ..."
desc = "水墨 / 写意"
tags = ["古典绘画"]
monochrome = false
```

## 写 suffix 的三条红线（都是实机踩出来的）

1. **不要写艺术家姓名**。`in the style of X` 会被字面画成「X 本人」——
   cfg=1.0 的蒸馏模型不区分"风格"与"主体"。
2. **不要写否定词**（`no text` / `without trees`）。这类模型没有负向引导，
   否定句里的名词会被**原样画出来**。零文字要靠**正面**陈述
   （`blank unmarked surfaces`），不是 `no text`。
3. **不要写 `dynamic panel composition` 一类"多格"词**。它会被读成「漫画分格纸」，
   模型会往格子里填人（实测 1 人 → 2 人）。要单幅就明写
   `single full-frame composition, one uninterrupted picture`。

## `monochrome` 为什么是风格属性

"本产线要求纯黑白"曾经是**流水线常数**，写在 `[qc].chroma_max` 里。
但换成彩色题材（油画 / 水彩 / 赛博）后，彩度超标是**对的**，不是异常。
所以"是否单色"跟着风格走：`lvs qc` 只对 `monochrome = true` 的风格查彩度。
"""

from __future__ import annotations
from lvs.errors import UsageError

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Style:
    """整片统一的画面风格。

    - `name` 记进 `shots.json`；`suffix` 拼进生图提示词
    - `desc` / `tags` 只用于 `lvs styles` 给人看（选择时要有依据）
    - `monochrome` 决定 `lvs qc` 是否查彩度
    """

    name: str
    suffix: str
    desc: str = ""
    tags: tuple[str, ...] = ()
    monochrome: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "suffix": self.suffix,
            "desc": self.desc,
            "tags": list(self.tags),
            "monochrome": self.monochrome,
        }


def _s(name: str, desc: str, tags: tuple[str, ...], suffix: str, *, mono: bool = False) -> Style:
    return Style(name=name, suffix=suffix, desc=desc, tags=tags, monochrome=mono)


# 所有预设共用的收尾：单幅、无分格、胶片颗粒感。
# 抽出来是因为这三条**每一条都是踩出来的**（见模块 docstring 的红线 3）。
_ONE_FRAME = "single full-frame composition, one uninterrupted picture"
_FILM = "subtle film grain"


# ---- 内置预设（29 个，按题材分组；新题材请写进项目的 00-设定/风格预设.toml） ----

BUILTIN: dict[str, Style] = {
    # ============ 摄影 / 电影 ============
    "historical-documentary": _s(
        "historical-documentary", "历史纪录片（古中国 / 王朝叙事）", ("摄影电影",),
        "cinematic historical documentary still, ancient China, dramatic natural lighting, "
        f"muted earthy tones, {_ONE_FRAME}, {_FILM}, 16:9 composition",
    ),
    "cinematic-film-noir": _s(
        "cinematic-film-noir", "黑色电影（高对比、雨夜、硬阴影）", ("摄影电影",),
        "high-contrast black and white film noir still, hard directional key light casting "
        "sharp geometric shadows, wet asphalt reflecting street lamps, venetian-blind "
        f"stripes across interior walls, deep blacks with a narrow tonal range, {_ONE_FRAME}, "
        f"{_FILM}, 16:9 composition",
        mono=True,
    ),
    "cinematic-teal-orange": _s(
        "cinematic-teal-orange", "现代电影感（青橙调、浅景深）", ("摄影电影",),
        "contemporary cinematic still, teal shadows against warm orange skin highlights, "
        "shallow depth of field with creamy bokeh, anamorphic lens flare, fine sensor grain, "
        f"{_ONE_FRAME}, 16:9 composition",
    ),
    "documentary-natural-light": _s(
        "documentary-natural-light", "纪实自然光（无摆拍感）", ("摄影电影",),
        "observational documentary photograph, available natural light only, flat neutral "
        "colour, slightly imperfect handheld framing, honest unstaged look, visible ambient "
        f"detail, {_ONE_FRAME}, 16:9 composition",
    ),
    "vintage-70s-film": _s(
        "vintage-70s-film", "70 年代胶片（暖褪色、颗粒粗）", ("摄影电影",),
        "1970s colour film photograph, faded warm emulsion, amber and mustard cast, "
        "coarse visible grain, soft halation around highlights, gentle vignetting, "
        f"{_ONE_FRAME}, 16:9 composition",
    ),
    "studio-portrait-mono": _s(
        "studio-portrait-mono", "影棚人像（单色、柔光）", ("摄影电影",),
        "studio portrait in monochrome, large softbox key light with gentle wrap-around "
        "falloff, smooth graduated grey backdrop, crisp catchlights in the eyes, fine "
        f"skin texture preserved, medium-format clarity, {_ONE_FRAME}",
        mono=True,
    ),
    # ============ 黑白 / 版画 ============
    "jp-youth-manga-bw": _s(
        "jp-youth-manga-bw", "日系青年黑白漫画（写实比例、单幅）", ("黑白版画",),
        # **去掉 `dynamic panel composition`** —— 会被读成「漫画分格纸」，往格子里填人。
        "strictly monochrome black and white, Japanese seinen manga illustration in a "
        "realistic style, realistic anatomical proportions, black and white ink line art, "
        "halftone screentone shading, bold expressive brush strokes, dramatic chiaroscuro, "
        f"{_ONE_FRAME}, {_FILM}, grayscale, 16:9",
        mono=True,
    ),
    "gekiga-dark-bw": _s(
        "gekiga-dark-bw", "剧画（暗调、硬派、笔触重）", ("黑白版画",),
        "monochrome gekiga illustration, heavy black ink masses with minimal white space, "
        "harsh angular hatching, deep shadow occupying most of the frame, muscular "
        "carved linework, stark unglamorous realism, grayscale only, "
        f"{_ONE_FRAME}, 16:9 composition",
        mono=True,
    ),
    "pen-ink-sketch": _s(
        "pen-ink-sketch", "钢笔素描（交叉排线、纸感）", ("黑白版画",),
        "monochrome pen and ink sketch on textured paper, fine cross-hatching building all "
        "volume, confident varied-width linework, tonal modelling carried entirely by "
        "hatch density, visible paper tooth, grayscale only, "
        f"{_ONE_FRAME}, 16:9 composition",
        mono=True,
    ),
    "woodcut-print": _s(
        "woodcut-print", "木刻版画（刀痕粗、黑白分明）", ("黑白版画",),
        "woodcut print, bold gouged knife marks with visible tool tracks, solid black "
        "carved areas against raw paper white, irregular hand-cut edges, relief-print "
        "ink texture, two-tone only, "
        f"{_ONE_FRAME}, 16:9 composition",
        mono=True,
    ),
    "charcoal-drawing": _s(
        "charcoal-drawing", "炭笔（糊、柔、有擦痕）", ("黑白版画",),
        "monochrome charcoal drawing on coarse paper, smudged soft gradation, lifted "
        "highlights rubbed out with a finger, dense velvety blacks, dusty loose edges, "
        "grayscale only, "
        f"{_ONE_FRAME}, 16:9 composition",
        mono=True,
    ),
    "linocut-graphic": _s(
        "linocut-graphic", "亚麻油毡版画（平面、装饰性强）", ("黑白版画",),
        "monochrome linocut relief print, solid black ink on paper white, flat graphic "
        "shapes with confident carved contours, strong positive-negative interplay, "
        "slight roller texture, grayscale only, "
        f"{_ONE_FRAME}, 16:9 composition",
        mono=True,
    ),
    # ============ 古典 / 传统绘画 ============
    "van-gogh-impasto": _s(
        "van-gogh-impasto", "厚涂油画（漩涡笔触、强烈补色）", ("古典绘画",),
        "dense impasto oil painting, thick paint laid on with a palette knife in raised "
        "ridges, energetic swirling comma-shaped brushstrokes, heavily drawn expressive "
        "contours, coarse visible linen canvas texture, vivid cobalt blue, chrome yellow, "
        "olive green and deep umber palette, dramatic chiaroscuro, "
        f"{_ONE_FRAME}, 16:9 composition",
    ),
    "chinese-ink-wash": _s(
        "chinese-ink-wash", "水墨写意（留白、墨分五色）", ("古典绘画",),
        "monochrome chinese ink wash painting, expressive free brushwork with dry and wet "
        "brush variation, misty distance achieved by dilute ink, generous unpainted paper "
        "left as sky and water, single-hue ink gradation from jet black to palest grey, "
        "grayscale only, "
        f"{_ONE_FRAME}, 16:9 composition",
        mono=True,
    ),
    "gongbi-color": _s(
        "gongbi-color", "工笔重彩（细线勾勒、矿物色）", ("古典绘画",),
        "chinese gongbi fine-line painting, meticulous even contour lines drawn in "
        "mineral pigment over silk, flat layered washes of cinnabar, azurite and malachite, "
        "ornamental detail rendered with patient precision, brush texture fully smoothed "
        "into an even surface, "
        f"{_ONE_FRAME}, 16:9 composition",
    ),
    "ukiyo-e-woodblock": _s(
        "ukiyo-e-woodblock", "浮世绘（平涂、轮廓线、装饰）", ("古典绘画",),
        "japanese woodblock print, flat unshaded colour areas bounded by clean printed "
        "contours, indigo, vermilion and muted ochre palette, decorative wave and cloud "
        "patterns, registration marks and paper fibre visible, "
        f"{_ONE_FRAME}, 16:9 composition",
    ),
    "oil-classical-chiaroscuro": _s(
        "oil-classical-chiaroscuro", "古典油画（暗背景、单一光源）", ("古典绘画",),
        "classical oil painting, single warm light source emerging from near-total "
        "darkness, deep transparent brown underpainting, glazed flesh tones with subtle "
        "translucency, restrained earth palette of umber, ochre and lead white, "
        f"{_ONE_FRAME}, 16:9 composition",
    ),
    "watercolour-gentle": _s(
        "watercolour-gentle", "水彩（透明、渗染、留白）", ("古典绘画",),
        "transparent watercolour painting, wet-in-wet bleeding, soft accidental blooms "
        "where pigment pooled, granulating texture in the washes, unpainted paper used as "
        "the lightest value, delicate pencil underdrawing showing through, "
        f"{_ONE_FRAME}, 16:9 composition",
    ),
    # ============ 年代 / 民国 ============
    "republican-era-poster": _s(
        "republican-era-poster", "民国月份牌（擦笔重彩、摩登）", ("年代民国",),
        "1930s Shanghai calendar poster, soft airbrushed shading blending into flat "
        "colour, idealized glamorous figures, restrained pastel palette of rose, jade and "
        "cream, decorative art-deco border motifs, aged paper toning, "
        f"{_ONE_FRAME}, 16:9 composition",
    ),
    "old-photo-sepia": _s(
        "old-photo-sepia", "老照片（棕褐、银盐、划痕）", ("年代民国",),
        # 刻意**不标单色**：棕褐是带色度的（暖棕调），标成单色会让 `lvs qc` 把
        # 正常的棕调报成「彩度异常」。判据要跟着画面走，不是跟着"看着像老照片"走。
        "antique silver-gelatin photograph, warm sepia toning, soft uneven focus toward "
        "the frame edges, fine emulsion scratches and dust specks, slight chemical "
        "staining, period-accurate clothing and props, "
        f"{_ONE_FRAME}, 16:9 composition",
    ),
    "soviet-constructivist": _s(
        "soviet-constructivist", "构成主义海报（斜构图、红黑）", ("年代民国",),
        "constructivist poster, aggressive diagonal composition cutting across the frame, "
        "flat red, black and bone-white areas only, bold geometric overlays and "
        "photographic fragments, hard-edged factory-daylight, "
        f"{_ONE_FRAME}, 16:9 composition",
    ),
    # ============ 现代插画 / 科普 ============
    "flat-vector-editorial": _s(
        "flat-vector-editorial", "扁平矢量插画（知识科普通用）", ("现代插画",),
        "flat vector editorial illustration, limited palette of five or six solid colours, "
        "clean geometric shapes rendered as solid flat fills, generous negative space, "
        "crisp edges, poster-like clarity, "
        f"{_ONE_FRAME}, 16:9 composition",
    ),
    "minimal-line-art": _s(
        "minimal-line-art", "极简线稿（一根线、大量留白）", ("现代插画",),
        "minimalist monochrome line art, a single confident uniform-weight stroke "
        "describing the whole subject, forms suggested by outline alone against vast "
        "empty paper, maximum restraint, grayscale only, "
        f"{_ONE_FRAME}, 16:9 composition",
        mono=True,
    ),
    "risograph-zine": _s(
        "risograph-zine", "丝网印小志（错版、荧光两色）", ("现代插画",),
        "risograph zine print, two fluorescent spot inks deliberately misregistered, "
        "visible halftone dot texture, ink smudging at shape edges, photocopied grain "
        "and slight paper crinkle, "
        f"{_ONE_FRAME}, 16:9 composition",
    ),
    "paper-cut-collage": _s(
        "paper-cut-collage", "剪纸拼贴（硬边、纸质感）", ("现代插画",),
        "cut-paper collage, hard scissor-cut edges with a soft drop shadow proving the "
        "layering, visible fibrous paper stock in each fragment, torn and cut pieces "
        "overlapped flat against the picture plane, "
        f"{_ONE_FRAME}, 16:9 composition",
    ),
    # ============ 幻想 / 概念 ============
    "cyberpunk-neon-noir": _s(
        "cyberpunk-neon-noir", "赛博朋克（霓虹、湿雨、密集城市）", ("幻想概念",),
        "cyberpunk city night, saturated magenta and cyan neon bleeding into wet haze, "
        "dense vertical signage masses, steam venting from grates, rain-slick reflective "
        "ground, deep blue ambient shadow, "
        f"{_ONE_FRAME}, 16:9 composition",
    ),
    "surreal-dreamscape": _s(
        "surreal-dreamscape", "超现实梦境（比例错置、平静异样）", ("幻想概念",),
        "surreal dreamscape, objects rendered with photographic realism but implausible "
        "in scale and placement, impossibly still air, long soft shadows from an unseen "
        "low sun, unsettling calm, "
        f"{_ONE_FRAME}, 16:9 composition",
    ),
    "dark-fantasy-etching": _s(
        "dark-fantasy-etching", "暗黑铜版蚀刻（细密、古老）", ("幻想概念",),
        "monochrome copperplate etching, dense fine intaglio linework describing form "
        "entirely through burin strokes, deep bitten shadows, aged paper with plate mark, "
        "archaic allegorical emblem quality, grayscale only, "
        f"{_ONE_FRAME}, 16:9 composition",
        mono=True,
    ),
    "clay-3d-render": _s(
        "clay-3d-render", "3D 黏土渲染（卡通材质、柔光）", ("幻想概念",),
        "3D render in a soft matte clay material, simplified rounded forms, subtle "
        "subsurface scattering, single large area light with soft contact shadows, "
        "gentle pastel palette, clean studio floor, "
        f"{_ONE_FRAME}, 16:9 composition",
    ),
    "pixel-art-retro": _s(
        "pixel-art-retro", "像素风（低分辨率、限色）", ("幻想概念",),
        "16-bit pixel art, strict limited palette, hard-edged square pixels with aliased "
        "blocky edges, dithering used for gradients, flat screen-lit shading, "
        f"{_ONE_FRAME}, 16:9 composition",
    ),
}

DEFAULT_NAME = "historical-documentary"

STYLE_FILENAME = "风格预设.toml"


class StyleError(UsageError, KeyError):
    """风格名不存在。消息面向用户，必须列出可选值。"""


@dataclass
class StyleRegistry:
    """合并后的风格表。`source` 记下每个名字来自哪，便于回答"这条是哪来的"。"""

    styles: dict[str, Style] = field(default_factory=dict)
    default: str = DEFAULT_NAME
    source: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def names(self) -> list[str]:
        return sorted(self.styles)

    def get(self, name: str | None) -> Style:
        """按名取风格。未知名字**报错**而不是悄悄退回默认 ——
        整片风格静默跑偏，要等全部出完图才会被发现。"""
        key = (name or "").strip() or self.default
        style = self.styles.get(key)
        if style is None:
            near = [n for n in self.names() if key[:3].lower() in n.lower()] if key else []
            lines = [f"未知画面风格 {key!r}。"]
            if near:
                lines.append(f"  你是不是想找：{', '.join(near)}")
            lines.append(f"  可选（{len(self.styles)} 个）：{', '.join(self.names())}")
            lines.append("  全部预设与说明见：`lvs styles`")
            lines.append(
                "  给本项目加风格：写 <素材库>/00-设定/风格预设.toml，"
                "或用 `lvs init` 生成骨架。"
            )
            raise StyleError("\n".join(lines))
        return style

    def by_tag(self) -> dict[str, list[Style]]:
        out: dict[str, list[Style]] = {}
        for st in self.styles.values():
            for tag in st.tags or ("未分类",):
                out.setdefault(tag, []).append(st)
        for group in out.values():
            group.sort(key=lambda s: s.name)
        return out

    def monochrome(self, name: str | None) -> bool:
        """这个风格是否要求纯黑白。未知名字**按不要求**处理 ——
        宁可少报一条彩度告警，也不要因为读不到风格就拦下整批图。"""
        try:
            return self.get(name).monochrome
        except StyleError:
            return False


# ---- 风格内容 lint ----------------------------------------------------------
#
# 为什么要有它：这三条红线**写在文档里时没人会看**。
# 2026-10-03 那天我自己写了 30 个预设，全踩了 —— 直到把断言补成测试才发现。
# 测试守住了"内置预设"，但**项目自己写的风格文件**（<素材库>/00-设定/风格预设.toml）
# 不在测试覆盖里，只有这里能拦。

# 否定词：必须**逐词**判，不能用 prompting.NEGATION_RULES ——
# 后者的名词表只覆盖 people / text / face 那一类，管不到
# `shading` / `gradients` / `anti-aliasing`，而风格里出现的正是这些。
_NEGATION = re.compile(r"(?<![A-Za-z])(?:no|not|without|never|none|nothing)(?![A-Za-z])", re.I)
# 艺术家属名：cfg=1.0 的模型会把「某某的风格」字面画成「某某本人」
_ARTIST = re.compile(r"\bin the style of\b|\bstyle of [A-Z]", re.I)
# 多格词：会被读成漫画分格纸，模型往格子里填人（实测 1 人 → 2 人）
_PANEL = re.compile(r"panel composition|comic panels?|multi-?panel", re.I)
_MONO_KEYS = ("grayscale", "monochrome", "black and white", "two-tone")


def lint_style(style: Style) -> list[str]:
    """体检一条风格。返回人类可读的问题列表（空 = 通过）。

    只报告、不自动改写 —— 改法要看上下文（`no shading` 是改成
    `forms modelled by hatch density` 还是 `flat unmodulated shapes`，
    取决于这条风格本来要什么效果）。
    """
    out: list[str] = []
    text = style.suffix or ""
    low = text.lower()

    m = _NEGATION.search(text)
    if m:
        out.append(
            f"含否定词「{m.group(0)}」—— cfg=1.0 的模型没有负向引导，"
            "否定句里的名词会被**原样画出来**。改成正面陈述。"
        )
    m = _ARTIST.search(text)
    if m:
        out.append(
            f"疑似艺术家属名「{m.group(0)}」—— 模型会把「某某的风格」画成**某某本人**。"
            "改成描述媒介 / 笔触 / 色调 / 光影。"
        )
    m = _PANEL.search(text)
    if m:
        out.append(
            f"含多格词「{m.group(0)}」—— 会被读成漫画分格纸，模型往格子里填人。"
            "要单幅就写 `single full-frame composition`。"
        )
    if "full-frame" not in low and "full frame" not in low:
        out.append("没声明单幅 —— 补 `single full-frame composition, one uninterrupted picture`。")
    if style.monochrome and not any(k in low for k in _MONO_KEYS):
        out.append(
            "标了 `monochrome = true`，但 suffix 里没有正面的黑白陈述 —— "
            "补 `grayscale only`（靠「不提彩色」是无效的，模型不吃负向）。"
        )
    return out


def lint_registry(reg: StyleRegistry) -> dict[str, list[str]]:
    """体检**非内置**的风格（内置的由 `tests/test_styles.py` 守着）。"""
    return {
        name: problems
        for name, style in reg.styles.items()
        if reg.source.get(name, "内置") != "内置" and (problems := lint_style(style))
    }


# ---- 文件加载 ---------------------------------------------------------------


def _parse_file(path: Path) -> tuple[dict[str, Style], str | None, str | None]:
    """读一个风格文件。返回 `(styles, default, 错误说明)`。

    格式不对**不抛异常打断出图**，但也不静默吞掉 —— 调用方会把 `notes` 打出来。
    风格文件写错了却一声不响，是最容易让人"出完全片才发现不对"的那类问题。
    """
    try:
        data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {}, None, f"{path.name} 读取失败：{exc}"

    table = data.get("styles")
    if not isinstance(table, dict):
        return {}, None, f"{path.name} 缺少 `[styles.<名字>]` 段"

    out: dict[str, Style] = {}
    for name, body in table.items():
        style = _style_from_toml(str(name), body)
        if style is not None:
            out[str(name)] = style

    default = data.get("default")
    return out, (str(default).strip() if default else None), None


def _style_from_toml(name: str, body: Any) -> Style | None:
    """一条风格的两个写法：`x = "suffix"` 或 `[styles.x] suffix = "…"`。"""
    if isinstance(body, str):
        return Style(name=name, suffix=body.strip()) if body.strip() else None
    if not isinstance(body, dict):
        return None
    suffix = str(body.get("suffix") or "").strip()
    if not suffix:
        return None
    tags = body.get("tags")
    if isinstance(tags, list):
        tag_tuple = tuple(str(t).strip() for t in tags if str(t).strip())
    elif isinstance(tags, str) and tags.strip():
        tag_tuple = (tags.strip(),)
    else:
        tag_tuple = ("项目自定义",)
    return Style(
        name=name,
        suffix=suffix,
        desc=str(body.get("desc") or "").strip(),
        tags=tag_tuple,
        monochrome=bool(body.get("monochrome", False)),
    )


def project_style_file(lib: Path) -> Path:
    """项目风格文件的规范位置。"""
    return Path(lib) / "00-设定" / STYLE_FILENAME


def configured_style_file(config=None) -> Path | None:  # noqa: ANN001
    """本次真正会读到的那个项目级风格文件（不存在返回 None）。"""
    from lvs.config import lib_dir

    if config is not None:
        explicit = config.get("shots.style_file")
        if explicit:
            p = Path(str(explicit)).expanduser()
            if p.is_file():
                return p
    lib = lib_dir(config)
    if lib is not None:
        p = project_style_file(lib)
        if p.is_file():
            return p
    return None


def build_registry(config=None, *, style_file: str | Path | None = None) -> StyleRegistry:  # noqa: ANN001
    """合并四个来源，得到最终风格表。优先级：--style-file > 项目文件 > config 内联 > 内置。"""
    from lvs.config import lib_dir

    styles: dict[str, Style] = dict(BUILTIN)
    source: dict[str, str] = {k: "内置" for k in BUILTIN}
    notes: list[str] = []
    default = DEFAULT_NAME

    # 1) 项目级（跟素材走）
    lib = lib_dir(config)
    if lib is not None:
        p = project_style_file(lib)
        if p.is_file():
            got, dflt, err = _parse_file(p)
            styles.update(got)
            source.update({k: f"项目 · {p.name}" for k in got})
            default = dflt or default
            if err:
                notes.append(err)

    # 2) 命令行 / 配置显式指定的文件
    explicit = style_file or (config.get("shots.style_file") if config is not None else None)
    if explicit:
        p = Path(str(explicit)).expanduser()
        if p.is_file():
            got, dflt, err = _parse_file(p)
            styles.update(got)
            source.update({k: f"指定文件 · {p.name}" for k in got})
            default = dflt or default
            if err:
                notes.append(err)
        else:
            notes.append(f"风格文件不存在：{p}")

    # 3) config 内联（装机级）—— 能盖掉同名条目，方便本机试
    inline = config.get("styles") if config is not None else None
    if isinstance(inline, dict):
        for name, body in inline.items():
            style = _style_from_toml(str(name), body)
            if style is not None:
                styles[str(name)] = style
                source[str(name)] = "config 内联"

    # 4) 默认名：`[shots].style` > 文件里的 default > 内置默认
    chosen = (config.get("shots.style") if config is not None else None) or default

    reg = StyleRegistry(styles=styles, default=str(chosen), source=source, notes=notes)
    for n in notes:
        print(f"[风格] {n}")
    return reg


def scaffold_style_file(lib: Path, *, name: str = "project-default") -> Path:
    """给新项目写一份风格文件骨架（`lvs init` 用）。已存在则不覆盖。"""
    path = project_style_file(lib)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        return path
    body = '''# 本项目的画面风格预设（跟着素材走，不属于装机配置）
#
# 用法：`lvs shots --style <名字>` 或 config 的 `[shots].style = "<名字>"`
# 优先级：--style-file > config [styles.*] > 本文件 > lvs 内置（`lvs styles` 可看全部）
#
# ⚠ 写 suffix 的三条红线（cfg=1.0 蒸馏模型，都是实机踩出来的）：
#   1. 不写艺术家姓名 —— `in the style of X` 会被字面画成「X 本人」
#   2. 不写否定词 —— 没有负向引导，否定句里的名词会被原样画出来
#      零文字要靠正面陈述（blank unmarked surfaces），不是 `no text`
#   3. 不写 `dynamic panel composition` 一类多格词 —— 会被读成漫画分格纸，往格子里填人

default = "project-default"

[styles.project-default]
desc = "本项目默认风格（改成本书的视觉设定）"
tags = ["项目自定义"]
monochrome = false
suffix = "cinematic still, <媒介 / 笔触 / 色调 / 光影 / 画幅 写在这里>"

# 再加就往下续：
# [styles.another]
# desc = "..."
# monochrome = false
# suffix = "..."
'''
    path.write_text(body, encoding="utf-8")
    return path


# ---- 命令前端（`lvs styles`） -----------------------------------------------


def render_catalog(reg: StyleRegistry, *, current: str = "", lib: Path | None = None) -> str:
    """把风格表排成可读的目录：按题材分组，标注是否单色、来自哪里。

    `lib` 是本次生效的素材库根（可能为 None）—— 用它打印"本项目的风格文件在哪"，
    而不是从注册表反推（反推不出来，见 git 历史里那个永远返回 None 的版本）。
    """
    issues = lint_registry(reg)
    lines = [f"画面风格预设 · 共 {len(reg.styles)} 个（默认：{reg.default}）", ""]
    if issues:
        lines.append(f"⚠ 有 {len(issues)} 个**项目自定义**风格没通过内容体检（见下）")
        lines.append("")
    for tag in sorted(reg.by_tag()):
        lines.append(f"【{tag}】")
        for st in reg.by_tag()[tag]:
            marks = []
            if st.name == reg.default:
                marks.append("默认")
            if st.name == current:
                marks.append("★ 本任务在用")
            if st.monochrome:
                marks.append("单色")
            origin = reg.source.get(st.name, "")
            tail = f"  [{origin}]" if origin and origin != "内置" else ""
            mark = f"  ({'、'.join(marks)})" if marks else ""
            warn = "  ⚠" if st.name in issues else ""
            lines.append(f"  {st.name:<28}{st.desc}{mark}{tail}{warn}")
        lines.append("")

    lines += [
        "用法：`lvs shots --style <名字>` / `lvs run --style <名字>`；",
        "     或在 config.toml 写 `[shots] style = \"<名字>\"` 定默认。",
    ]
    if lib:
        sf = project_style_file(lib)
        if sf.is_file():
            lines.append(f"本项目的风格文件：{sf}")
        else:
            # 路径**存在与否**要如实说。原来不管有没有都打印路径，
            # 人会以为"这个文件在生效"，而其实一条自定义风格都没读到。
            lines.append(
                f"本项目还没有风格文件（`lvs init` 会在 {sf.parent} 生成骨架）——"
                "现在用的是内置预设。"
            )
    else:
        lines.append(
            "本项目还没配 `[paths].lib` —— 配了之后会自动读 "
            "<素材库>/00-设定/风格预设.toml（给这本书定专属风格）。"
        )
    lines.append("")
    lines.append("要加自己的风格：`lvs init` 生成骨架，或直接改上面的风格文件。")

    if issues:
        lines.append("")
        lines.append("──── 内容体检（这三条红线都是实机踩出来的）────")
        for name in sorted(issues):
            lines.append(f"  ⚠ {name}")
            for why in issues[name]:
                lines.append(f"      · {why}")
    return "\n".join(lines)


def catalog_json(reg: StyleRegistry, *, current: str = "") -> dict[str, Any]:
    """给 Agent 消费的目录。Agent 选风格时需要 `desc` 和 `monochrome` 这两个字段。"""
    return {
        "default": reg.default,
        "current": current,
        "count": len(reg.styles),
        "styles": [
            {**st.to_dict(), "source": reg.source.get(st.name, ""), "is_default": st.name == reg.default}
            for st in sorted(reg.styles.values(), key=lambda x: x.name)
        ],
        "notes": reg.notes,
        "issues": lint_registry(reg),
    }


def run_command(config, args) -> int:  # noqa: ANN001 - 由 cli 传入
    import json as _json

    from lvs.config import lib_dir

    try:
        reg = build_registry(config, style_file=getattr(args, "style_file", None))
    except Exception as exc:  # noqa: BLE001 - 风格文件坏了不该让命令崩
        print(f"读取风格失败：{exc}")
        return 2
    lib = lib_dir(config)

    current = str(getattr(args, "current", None) or config.get("shots.style") or "")
    if bool(getattr(args, "json", False)):
        print(_json.dumps(catalog_json(reg, current=current), ensure_ascii=False, indent=2))
        return 0

    # 只筛某一类：`lvs styles 黑白版画`
    only = str(getattr(args, "tag", None) or "").strip()
    if only:
        groups = {k: v for k, v in reg.by_tag().items() if only in k}
        if not groups:
            print(f"没有分类含 {only!r}。可用：{'、'.join(sorted(reg.by_tag()))}")
            return 2
        filtered = StyleRegistry(
            styles={st.name: st for g in groups.values() for st in g},
            default=reg.default, source=reg.source, notes=reg.notes,
        )
        print(render_catalog(filtered, current=current, lib=lib))
        return 0

    print(render_catalog(reg, current=current, lib=lib))
    return 0
