"""投稿物料（B站 / 抖音）—— 从成片与拍摄稿生成"可直接粘贴"的稿件与**带字封面**。

## 为什么要有它

成片出来 ≠ 能投稿。B站/抖音真正要交的是**另一套东西**：封面（且封面上要有字）、
标题候选、简介、标签/话题、版权行。此前这些全是**手写的** ——
`<素材库>/08-投稿/00X-投稿物料.md` 一份约 1.5 KB，由人或 agent 逐条编写；
而封面上的三行字**还得再手工叠一次**（002 期交付时真实状态就写着
"三行标题字尚未叠加"）。

代价有两层：每次投稿都要重写一遍（agent 写一次要读稿 + 编文案，是最贵的一步之一），
以及"写了文案但字没叠上"这种**只差最后一步**的交付缺口。

本模块把两件事都变成一条命令：`lvs publish --task X`。

## 输入（一律复用已有产物，不新造真相）

| 来源 | 取什么 |
|---|---|
| `parse.json` → `meta.cover` | 【封面文案】里作者写好的 `行1/行2/行3` —— **封面字是稿子里写的，不是这里编的** |
| `parse.json` → `meta.title_card` | 主用/备选标题（`主用（金句钩子）：A｜B` 这种写法） |
| `parse.json` → `cold_open` + 前两段旁白 | 简介的正文（视频前 30 秒本来就该是简介） |
| `final.mp4` | 时长（简介里的"本期 XX 分钟"、发布检查单要核对） |
| `config` → `[publish]` | 书名/作者/译者/出版信息/标签/合集/分区/版权行（书这类元数据配置里才有） |

任何一项缺失都**只降级、不阻断**：少一段话照样能投稿，缺一个字段却不该让物料交不出来。

## 输出（`.work/<task>/publish/`）

| 文件 | 给谁 |
|---|---|
| `meta.json` | **机器**（与 `result.py` 信封同源）：三行字 / 标签 / 时长 / 路径 |
| `bilibili.md` | 人：标题候选 + 简介 + 标签 + 分区 + 发布检查单 |
| `douyin.md` | 人：口播文案 + 话题 + 竖版封面说明 |
| `cover-bilibili.png` | 1344×768 横版，**已叠三行字** |
| `cover-douyin.png` | 1080×1920 竖版，**已叠三行字** |

`--out <素材库>/08-投稿` 可把这几份一并拷到书库侧留档（不改动 `.work/` 里的原件）。

## 幂等

同样的输入渲染出同样的字节（文案是纯函数；PIL 的 PNG 不写时间戳）。
唯一的例外是 `meta.json` 的 `generated_at`（留一条"何时生成的"线索，
其余字段仍是纯函数结果）。
重复跑只覆盖自己，所以**没有 `--force`** —— 不留"定义了却没人接线"的开关。
底图换了想重渲，直接再跑一次即可。
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from lvs import graphic
from lvs.config import Config, lib_dir
from lvs.errors import UsageError
from lvs.ffmpeg import duration as ff_duration
from lvs.workspace import Workspace

#: B站视频封面（横版）。1344×768 是站方推荐的 16:9 档位之一。
BILIBILI_SIZE = (1344, 768)
#: 抖音竖版封面。
DOUYIN_SIZE = (1080, 1920)
#: B站标题上限（超出会被截断，所以候选要在这里就卡住）。
TITLE_MAX = 40
#: B站简介上限（2000 字）—— 我们只写软目标，超了给警告而不是硬截。
DESC_WARN = 1500
TAG_MAX = 10
DOUYIN_TAG_MAX = 5
DOUYIN_CAPTION_MAX = 1000

_LINE_RE = re.compile(r"^\s*行\s*([0-9一二三四五六七八九十]+)\s*[:：]\s*(.+)$")
_PREFIX_RE = re.compile(r"^\s*(主用|备用|备选|首选)\s*(（[^）]*）|\([^)]*\))?\s*[:：]\s*")
_SERIES_SPLIT = re.compile(r"\s*[｜|]\s*")


class PublishError(UsageError, RuntimeError):
    """投稿物料生成失败（缺前置产物 / 参数不对）。"""


# ---- 纯函数：从已有产物里取文案 ---------------------------------------------


def cover_lines(meta: dict[str, Any], override: str | list[str] | None = None) -> list[str]:
    """封面三行字。优先级：命令行 `--lines` > 稿子的【封面文案】> 标题卡。

    `行1：xxx` 这种带编号的写法按编号排序去重（作者有时只写两行，
    有时多写一段"画面建议" —— 不带编号的那些**不是**封面字，直接丢掉）。
    """
    if override:
        raw = override if isinstance(override, (list, tuple)) else re.split(r"[|｜]", str(override))
        out = [str(x).strip() for x in raw if str(x).strip()]
        if out:
            return out[:3]

    numbered: dict[str, str] = {}
    plain: list[str] = []
    for item in meta.get("cover") or []:
        text = str(item).strip()
        if not text:
            continue
        match = _LINE_RE.match(text)
        if match:
            numbered[match.group(1)] = match.group(2).strip()
        else:
            plain.append(text)
    if numbered:
        def _key(token: str) -> tuple[int, str]:
            return (int(token), "") if token.isdigit() else (99, token)

        return [numbered[k] for k in sorted(numbered, key=_key)][:3]
    # 没编号：取前三条短句（长句多半是"画面建议"那种说明，不该顶在封面上）
    short = [t for t in plain if len(t) <= 20]
    if short:
        return short[:3]
    title = str(meta.get("title_card") or "").strip()
    return [title] if title else []


_INTERNAL_RE = re.compile(r"拍摄稿|分镜|工作稿|\.[a-z]{2,4}\b", re.IGNORECASE)


def _looks_internal(text: str) -> bool:
    """判断一条文案是不是"内部工作标题"（给制作人看的，不该发给观众）。

    实测踩过：`parse.json` 的 `title` 就是**拍摄稿的文件标题**，形如
    `003-雨月物语-夜宿荒宅 拍摄稿 · 《雨月物语》第三期「夜宿荒宅」`。
    原样当标题候选 = 把内部文件名投到公众视频上。
    """
    return bool(_INTERNAL_RE.search(text))


def title_candidates(parse_data: dict[str, Any], meta: dict[str, Any]) -> list[str]:
    """标题候选：从【标题】卡（主用/备选）与封面字里凑，去重、按长度过滤。"""
    out: list[str] = []
    card = str(meta.get("title_card") or "").strip()
    parts = _SERIES_SPLIT.split(_PREFIX_RE.sub("", card)) if card else []
    series = ""
    if len(parts) > 1 and parts[-1].startswith("《"):
        series = parts[-1].strip()
        parts = parts[:-1]
    for part in parts:
        text = part.strip()
        if text:
            out.append(text if not series else f"{text}｜{series}")

    lines = cover_lines(meta)
    if len(lines) >= 2:
        out.append(f"{lines[0]}，{lines[1]}")
    if lines:
        out.append("｜".join(lines))

    book = str(parse_data.get("title") or "").strip()
    if book:
        out.append(f"{lines[0]}｜{book}" if lines else book)

    # 先滤掉内部工作标题；万一稿子**只有**内部标题（没写【标题】卡），
    # 退回原文 —— 宁可难看，也不交一份空标题的素材。
    pool = [t for t in out if not _looks_internal(t)] or out

    seen: list[str] = []
    for text in pool:
        text = text.strip(" ｜|")
        if text and text not in seen:
            seen.append(text)
    if not seen:
        seen = ["（未从拍摄稿取到标题素材：请在【标题】或【封面文案】里写）"]
    return seen


def _first_sentences(texts: list[str], limit: int = 4) -> list[str]:
    """把旁白拆成人读的短句（简介第一段就是视频前 30 秒）。"""
    out: list[str] = []
    for raw in texts:
        for part in re.split(r"(?<=[。！？!?；;])", str(raw)):
            sentence = part.strip()
            if sentence:
                out.append(sentence)
            if len(out) >= limit:
                return out
    return out


def description(parse_data: dict[str, Any], config: Config, lines: list[str]) -> str:
    """B站简介：开场白 + 作品介绍 + 系列钩子 + 版权与画面声明。"""
    cold = [str(t) for t in ((parse_data.get("cold_open") or {}).get("text") or [])]
    if not cold:
        cold = [str((parse_data.get("segments") or [{}])[0].get("narration") or "")]
    opening = _first_sentences(cold, limit=4)

    blocks: list[str] = []
    if opening:
        blocks.append("".join(opening))
    intro = str(config.get("publish.intro", "") or "").strip()
    if intro:
        blocks.append(intro)

    episode = config.get("publish.episode")
    series = str(config.get("publish.series", "") or "").strip()
    hook_bits: list[str] = []
    if series:
        hook_bits.append(f"本期是{series}第 {episode} 期。" if episode else f"本期属于{series}。")
    series_note = str(config.get("publish.series_note", "") or "").strip()
    if series_note:
        hook_bits.append(series_note)
    if hook_bits:
        blocks.append("".join(hook_bits))
    if lines:
        blocks.append("封面字：" + "／".join(lines) + "。")

    credit = _credit_line(config)
    if credit:
        blocks.append(credit)
    image_note = str(config.get("publish.image_note", "画面为 AI 自绘，非原著插图。") or "").strip()
    if image_note:
        blocks.append(image_note)
    return "\n\n".join(b for b in blocks if b.strip())


def _credit_line(config: Config) -> str:
    book = str(config.get("publish.book_title", "") or "").strip()
    author = str(config.get("publish.author", "") or "").strip()
    translator = str(config.get("publish.translator", "") or "").strip()
    publisher = str(config.get("publish.publisher", "") or "").strip()
    if not book and not author:
        return ""
    body = f"原著：《{book}》" if book else "原著："
    if author:
        body += f" {author} 著"
    if translator:
        body += f" / {translator} 译"
    if publisher:
        body += f" / {publisher}"
    return body


_TAG_NOISE_RE = re.compile(r"拍摄稿|分镜|工作稿")
_TAG_MAX_LEN = 12


def _tag_candidates(raw: str) -> list[str]:
    """把"书名 / 篇名"清成能当标签用的短语。

    实测踩过两个坑，都会生成**脏标签**：
    1. `parse.json` 的 `title` 是拍摄稿标题（`003-雨月物语-夜宿荒宅 拍摄稿 · …`），
       原样当标签 = 把内部文件名带进标签区；
    2. `"《雨月物语》夜宿荒宅".strip("《》")` **只**去掉开头的 `《`
       （右端不是 `》`），留下 `雨月物语》夜宿荒宅` —— 缺一半书名号。

    规则：`《X》` 里的 X 一定是干净的书名/篇名，优先取它；
    其余部分去噪、去期号前缀，按分隔符切成短词，**带 `-`/`_` 的一律丢**
    （那是内部文件的连接符，不是给人看的标签）。
    """
    text = str(raw or "").strip()
    if not text:
        return []
    out: list[str] = []
    for name in re.findall(r"《([^》]+)》", text):
        name = name.strip()
        if name:
            out.append(name)
    body = _TAG_NOISE_RE.sub(" ", re.sub(r"[《》「」]", " ", text))
    body = re.sub(r"^\s*\d{2,}[-_\s]*", "", body).strip()
    for piece in re.split(r"[·｜|/、,，\s]+", body):
        piece = piece.strip()
        if 1 < len(piece) <= _TAG_MAX_LEN and not re.search(r"[-_]", piece):
            out.append(piece)
    return out


def tags(parse_data: dict[str, Any], config: Config, lines: list[str]) -> list[str]:
    """标签：配置里手写的优先，再补书名 / 篇名 / 通用讲书词，去重并且 ≤10 个。"""
    out: list[str] = []
    for item in config.get("publish.tags", []) or []:
        text = str(item).strip()
        if text:
            out.append(text)
    book = str(config.get("publish.book_title", "") or parse_data.get("title") or "").strip()
    if book:
        out.extend(_tag_candidates(book))
    if lines and lines[-1]:
        out.extend(_tag_candidates(lines[-1]))
    for extra in ("讲书", "读书", "文学"):
        out.append(extra)

    seen: list[str] = []
    for text in out:
        if text and text not in seen:
            seen.append(text)
    return seen[:TAG_MAX]


def douyin_captions(lines: list[str], tag_list: list[str], config: Config) -> list[str]:
    """抖音口播文案（≥3 条候选，每条都带话题）。"""
    base = "".join(lines) if lines else str(config.get("publish.book_title", "") or "")
    intro = str(config.get("publish.intro", "") or "").strip()
    topics = " ".join(f"#{t}" for t in tag_list[:DOUYIN_TAG_MAX])
    short = lines[0] if lines else base
    # 第 3 条原本只挂 publish.intro；没配 intro 时它就退化成"一串光秃秃的话题"。
    # 没 intro 就用最短的那行封面字兜底（钩子短句，信息流里更好用）。
    cands = [
        f"{base} {topics}".strip(),
        f"{lines[0]}｜{lines[-1]} {topics}".strip() if len(lines) >= 2 else f"{base} {topics}".strip(),
        f"{intro or short} {topics}".strip(),
    ]
    out: list[str] = []
    for text in cands:
        text = text.strip()
        # 去掉话题后什么都不剩 = 不是文案，丢掉（否则会交一条只有 #标签 的候选）。
        if not text.replace(topics, "").strip(" #｜|"):
            continue
        if text not in out:
            out.append(text[:DOUYIN_CAPTION_MAX])
    return out


# ---- 封面渲染（唯一真源：字体/断行走 `graphic`，不另写一套） -----------------


def _gradient(size: tuple[int, int]) -> Any:
    """没有底图时的靛蓝渐变（**不假装**有画面）。"""
    from PIL import Image

    w, h = size
    top, bottom = (28, 36, 60), (10, 12, 20)
    strip = Image.new("RGB", (1, h))
    strip.putdata([
        tuple(int(top[i] + (bottom[i] - top[i]) * (y / max(1, h - 1))) for i in range(3))
        for y in range(h)
    ])
    return strip.resize((w, h), Image.BILINEAR)


def _fit_to_size(img: Any, size: tuple[int, int]) -> Any:
    """中心裁切到目标比例后缩放（不拉伸变形）。"""
    from PIL import Image

    w, h = size
    target = w / h
    cur = img.width / max(1, img.height)
    if cur > target:
        nw = max(1, int(img.height * target))
        left = (img.width - nw) // 2
        img = img.crop((left, 0, left + nw, img.height))
    else:
        nh = max(1, int(img.width / target))
        top = int((img.height - nh) * 0.35)   # 主体多在上半，裁时偏上留
        img = img.crop((0, top, img.width, top + nh))
    return img.resize((w, h), Image.LANCZOS)


def _scrim(size: tuple[int, int], orientation: str, strength: float = 0.82,
           reach: float = 0.72) -> Any:
    """压暗层：横版压左侧、竖版压上部 —— 都是为了让白字有足够对比。"""
    from PIL import Image

    w, h = size
    if orientation == "left":
        strip = Image.new("L", (w, 1))
        strip.putdata([
            int(255 * strength * max(0.0, 1.0 - (x / max(1, w - 1)) / reach)) for x in range(w)
        ])
        alpha = strip.resize((w, h), Image.BILINEAR)
    else:
        strip = Image.new("L", (1, h))
        strip.putdata([
            int(255 * strength * max(0.0, 1.0 - (y / max(1, h - 1)) / reach)) for y in range(h)
        ])
        alpha = strip.resize((w, h), Image.BILINEAR)
    return Image.new("RGB", (w, h), (0, 0, 0)), alpha


def render_cover(base: Path | None, lines: list[str], size: tuple[int, int], out: Path,
                 *, layout: str = "left", font_file: str | None = None) -> Path:
    """把三行字叠到底图上，输出 PNG。`layout` 决定字块位置与压暗方向。

    - `left`（B站横版）：左侧压暗 + 左对齐大字，主体留在右侧；
    - `top`（抖音竖版）：上部压暗 + 居中大字，避开底部操作区。
    """
    from PIL import Image, ImageDraw

    if not lines:
        raise PublishError("封面没有可叠的字（拍摄稿的【封面文案】没写，且没有 --lines）")
    if not graphic.available():
        raise PublishError(
            "封面叠字需要 Pillow 与一款中文字体（NotoSerif/msyh/simhei/simsun/PingFang）；"
            "当前机器两者缺一。"
        )
    font_path = _font_path(font_file)

    w, h = size
    if base is not None and base.is_file():
        with Image.open(base) as im:
            canvas = _fit_to_size(im.convert("RGB"), size)
    else:
        canvas = _gradient(size)

    layer, alpha = _scrim(size, "left" if layout == "left" else "top",
                       strength=0.86, reach=0.86 if layout == "left" else 0.62)
    canvas = Image.composite(layer, canvas, alpha)
    draw = ImageDraw.Draw(canvas)

    if layout == "left":
        box = (int(w * 0.055), int(h * 0.10), int(w * 0.60), int(h * 0.90))
        align_center = False
    else:
        box = (int(w * 0.08), int(h * 0.06), int(w * 0.92), int(h * 0.46))
        align_center = True

    size_hint = int(h * (0.115 if layout == "left" else 0.075))
    min_size = max(28, int(h * 0.035))
    max_w = box[2] - box[0]
    max_h = box[3] - box[1]

    # ★ 先缩字号、**再**允许折行：封面字是"三行"，折行会把三行变成四五块，
    #   结构就散了（作者写的每一行都是完整的一句话）。所以第一轮判据是
    #   "每一行都不超宽"，宁可字小一号；只有缩到下限还放不下才折行 —— 总比溢出强。
    font = graphic.font(min_size, path=font_path)
    wrapped = list(lines)
    size = size_hint
    while size > min_size:
        candidate = graphic.font(size, path=font_path)
        if all(draw.textlength(line, font=candidate) <= max_w for line in lines) \
                and len(lines) * int(size * 1.30) <= max_h:
            font, wrapped = candidate, list(lines)
            break
        size -= 4
    if size <= min_size:
        over = [line for line in lines if draw.textlength(line, font=font) > max_w]
        if over:
            wrapped = graphic.wrap(draw, "\n".join(lines), font, max_w)

    line_h = int(font.size * 1.30)
    block_h = line_h * len(wrapped)
    top = box[1] + max(0, (max_h - block_h) // 2)
    stroke = max(2, font.size // 14)
    for i, line in enumerate(wrapped):
        x = box[0] + (max_w - draw.textlength(line, font=font)) / 2 if align_center else box[0]
        draw.text((x, top + i * line_h), line, font=font, fill=(250, 248, 240),
                  stroke_width=stroke, stroke_fill=(0, 0, 0))

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, "PNG")
    return out


def _font_path(explicit: str | None) -> str | None:
    if explicit:
        path = Path(str(explicit)).expanduser()
        if not path.is_file():
            raise PublishError(f"配置的封面字体不存在：{path}")
        return str(path)
    return graphic.font_path()


# ---- 底图挑选 ---------------------------------------------------------------


def pick_base(ws: Workspace, config: Config, explicit: str | None = None) -> tuple[Path | None, str]:
    """封面底图：`--base` > 配置 `publish.base_image` > 任务里已有的画面。

    自动挑时**刻意从后往前挑**：开场的图多为人像/空镜，末尾几镜常是情绪最足、
    留白最多的一张（手工出封面时选中的也多是这类）。
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(str(explicit)).expanduser())
    configured = str(config.get("publish.base_image", "") or "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    for p in candidates:
        if p.is_dir():
            found = sorted(x for x in p.glob("*.png")) + sorted(x for x in p.glob("*.jpg"))
            if found:
                return found[-1], f"目录里最新的一张：{found[-1]}"
            return None, f"目录里没有图片：{p}"
        if p.is_file():
            return p, f"指定的底图：{p}"
        return None, f"底图不存在：{p}"

    for branch in ("local", "graphic"):
        d = ws.path("assets", branch)
        if not d.is_dir():
            continue
        found = sorted(x for x in d.glob("shot-*.png") if x.is_file())
        if found:
            return found[-1], f"自动挑：assets/{branch}/{found[-1].name}"
    return None, "任务里没有可用画面 → 用纯色渐变底（封面字照叠）"


# ---- 组装与落盘 -------------------------------------------------------------


def build_meta(ws: Workspace, config: Config, *, lines_arg: Any = None, base_arg: str | None = None,
               ) -> dict[str, Any]:
    """把物料**算出来**（不落盘）—— 纯函数便于单测与重复调用。"""
    parse_path = ws.path("parse.json")
    if not parse_path.is_file():
        raise PublishError(
            f"未找到 {parse_path} —— 投稿物料要从拍摄稿里取标题与封面字。\n"
            f"  请先运行：lvs parse <拍摄稿.md> --task {ws.task}"
        )
    try:
        parse_data = json.loads(parse_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishError(f"parse.json 读不出来（{exc}）：{parse_path}") from exc
    meta = parse_data.get("meta") or {}

    lines = cover_lines(meta, lines_arg)
    titles = title_candidates(parse_data, meta)
    tag_list = tags(parse_data, config, lines)
    desc = description(parse_data, config, lines)

    final = ws.path("final.mp4")
    seconds = ff_duration(final) if final.is_file() else None
    if seconds:
        duration_text = f"{int(seconds // 60)} 分 {int(seconds % 60):02d} 秒（约 {seconds / 60:.1f} 分钟）"
    else:
        duration_text = ""

    warnings: list[str] = []
    if not lines:
        warnings.append("拍摄稿没有【封面文案】：封面只有底图、没有字（可加 --lines \"行1|行2|行3\"）")
    if seconds is None:
        warnings.append("没有 final.mp4：时长未知（先跑 `lvs build`）")
    over = [t for t in titles if len(t) > TITLE_MAX]
    if over:
        warnings.append(f"有 {len(over)} 条标题超过 B站 {TITLE_MAX} 字上限，粘贴时会被截断：{over[0]}")

    return {
        "task": ws.task,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "video": {"seconds": round(seconds, 1) if seconds else None, "duration_text": duration_text},
        "book": {
            "title": str(config.get("publish.book_title", "") or parse_data.get("title") or ""),
            "author": str(config.get("publish.author", "") or ""),
            "translator": str(config.get("publish.translator", "") or ""),
            "publisher": str(config.get("publish.publisher", "") or ""),
            "series": str(config.get("publish.series", "") or ""),
            "episode": config.get("publish.episode"),
        },
        "cover": {"lines": lines},
        "bilibili": {
            "title_candidates": titles,
            "recommended_title": titles[0] if titles else "",
            "description": desc,
            "tags": tag_list,
            "category": str(config.get("publish.category", "") or "知识/人文历史"),
            "collection": str(config.get("publish.series", "") or ""),
        },
        "douyin": {
            "captions": douyin_captions(lines, tag_list, config),
            "hashtags": [f"#{t}" for t in tag_list[:DOUYIN_TAG_MAX]],
        },
        "keywords": list(dict.fromkeys(tag_list + lines)),
        "warnings": warnings,
    }


def render_all(ws: Workspace, config: Config, meta: dict[str, Any], *, base_arg: str | None = None,
               ) -> dict[str, Path]:
    """渲染两张封面并写文案文件，返回产物路径表。"""
    out_dir = ws.path("publish")
    out_dir.mkdir(parents=True, exist_ok=True)
    base, why = pick_base(ws, config, base_arg)
    meta["cover"]["base"] = str(base) if base else ""
    meta["cover"]["base_reason"] = why
    font_file = str(config.get("publish.font", "") or "").strip() or None

    lines = list(meta["cover"]["lines"])
    products = {
        "bilibili_cover": render_cover(base, lines, BILIBILI_SIZE, out_dir / "cover-bilibili.png",
                                       layout="left", font_file=font_file),
        "douyin_cover": render_cover(base, lines, DOUYIN_SIZE, out_dir / "cover-douyin.png",
                                     layout="top", font_file=font_file),
    }
    meta["cover"]["paths"] = {k: str(v) for k, v in products.items()}

    (out_dir / "bilibili.md").write_text(_bilibili_md(meta), encoding="utf-8")
    (out_dir / "douyin.md").write_text(_douyin_md(meta), encoding="utf-8")
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    products["bilibili_md"] = out_dir / "bilibili.md"
    products["douyin_md"] = out_dir / "douyin.md"
    products["meta_json"] = out_dir / "meta.json"
    return products


def _bilibili_md(meta: dict[str, Any]) -> str:
    b = meta["bilibili"]
    lines = meta["cover"]["lines"]
    out = [f"# {meta['task']} · B站投稿物料（`lvs publish` 自动生成）", ""]
    if meta["video"]["seconds"]:
        out += [f"> 成片时长：{meta['video']['duration_text']}", ""]
    out += ["## 一、标题候选（按推荐排序）", ""]
    for i, t in enumerate(b["title_candidates"]):
        mark = "（推荐首发）" if i == 0 else ""
        out.append(f"{i + 1}. {t}　`{len(t)} 字`{mark}")
    out += ["", f"> B站标题上限 {TITLE_MAX} 字；超过的候选会被截断。", "",
            "## 二、简介（可直接粘贴）", "", "```", b["description"], "```", "",
            "## 三、标签与分区", "",
            f"- **分区**：{b['category']}",
            f"- **标签**：{' '.join('`' + t + '`' for t in b['tags'])}",
            f"- **合集**：{b['collection'] or '（未配置 publish.series）'}", "",
            "## 四、封面（已叠字，直接上传）", ""]
    for line in lines:
        out.append(f"- {line}")
    out += ["", f"- 横版：`{meta['cover'].get('paths', {}).get('bilibili_cover', '')}`",
            f"- {meta['cover'].get('base_reason', '')}", "",
            "## 五、发布检查单", "",
            "- [ ] 标题选定（默认第 1 条）", "- [ ] 简介粘贴 + 标签",
            "- [ ] 封面已上传（横版）", "- [ ] 片尾署名 / 版权行核对",
            "- [ ] 字幕烧录确认（voice 阶段产物 subtitle.srt）", ""]
    if meta.get("warnings"):
        out += ["## ⚠ 生成时的提示", ""] + [f"- {w}" for w in meta["warnings"]] + [""]
    return "\n".join(out)


def _douyin_md(meta: dict[str, Any]) -> str:
    d = meta["douyin"]
    out = [f"# {meta['task']} · 抖音投稿物料（`lvs publish` 自动生成）", "",
           "## 一、文案候选（含话题）", ""]
    for i, c in enumerate(d["captions"]):
        out.append(f"{i + 1}. {c}")
    out += ["", "## 二、话题标签", "", " ".join(d["hashtags"]), "",
            "## 三、竖版封面（已叠字）", "",
            f"- 竖版：`{meta['cover'].get('paths', {}).get('douyin_cover', '')}`",
            f"- 视频本体仍是横版成片（`final.mp4`），抖音端会自动加黑边或裁切。", ""]
    if meta["video"]["seconds"]:
        out += [f"- 成片时长：{meta['video']['duration_text']}", ""]
    if meta.get("warnings"):
        out += ["## ⚠ 生成时的提示", ""] + [f"- {w}" for w in meta["warnings"]] + [""]
    return "\n".join(out)


def copy_to(out_dir: Path, products: dict[str, Path], *, prefix: str = "") -> list[Path]:
    """把物料拷去书库侧（`--out`）。名字加**任务前缀**，免得不同期互相覆盖。

    ★ 前缀必须是任务名（`UGE03`），不能取 `path.parent.name` ——
    那恒等于 `publish`，拷出来是 `publish-bilibili.md`：既看不出是哪一期，
    也无法在多期共存的 `08-投稿/` 里区分（实测踩过）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for path in products.values():
        stem = f"{prefix}-{path.name}" if prefix else path.name
        dst = out_dir / stem
        shutil.copy2(path, dst)
        copied.append(dst)
    return copied


# ---- 可选：LLM 润色（默认不开，开了也要能降级） -----------------------------

_POLISH_PROMPT = (
    "你是中文长视频投稿小编。根据给定素材，输出 JSON："
    '{"titles": ["≤40字标题", "...共5条"], "description": "150~200字简介", "tags": ["≤10个标签"]}。'
    "标题要具体、有反差或悬念，不要标题党脏话；简介第一段说清这条视频最抓人的事实，"
    "最后保留版权与画面声明行。只输出 JSON。"
)


def polish_with_llm(meta: dict[str, Any], config: Config) -> tuple[dict[str, Any], str]:
    """可选润色。返回 (新 meta, 说明)；任何失败都**原样退回**已算好的物料。"""
    from lvs import llm as llm_mod
    from lvs import llm_provider

    # P2 路由：投稿标题/简介/标签属低风险（人还会再改一遍）→ 默认走本地 ollama
    if not llm_mod.available(config, site=llm_provider.SITE_PUBLISH_META):
        return meta, "未配置 LLM key，跳过润色"
    try:
        client = llm_mod.LLMClient.from_config(config, site=llm_provider.SITE_PUBLISH_META)
        payload = {
            "书名": meta["book"]["title"],
            "封面字": meta["cover"]["lines"],
            "现有标题": meta["bilibili"]["title_candidates"][:3],
            "现有简介": meta["bilibili"]["description"],
            "可用标签": meta["bilibili"]["tags"],
        }
        data = llm_mod.chat_json(client, [
            {"role": "system", "content": _POLISH_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ], temperature=0.4)
    except Exception as exc:  # noqa: BLE001 - 润色是增强，失败不该让物料生成失败
        return meta, f"LLM 润色失败（{type(exc).__name__}: {exc}），已保留规则生成的文案"

    titles = [str(t).strip() for t in (data.get("titles") or []) if str(t).strip()]
    desc = str(data.get("description") or "").strip()
    tg = [str(t).strip() for t in (data.get("tags") or []) if str(t).strip()]
    if titles:
        meta["bilibili"]["title_candidates"] = titles[:5] + [
            t for t in meta["bilibili"]["title_candidates"] if t not in titles
        ]
        meta["bilibili"]["recommended_title"] = meta["bilibili"]["title_candidates"][0]
    if desc:
        meta["bilibili"]["description"] = desc
    if tg:
        meta["bilibili"]["tags"] = tg[:TAG_MAX]
        meta["douyin"]["hashtags"] = [f"#{t}" for t in tg[:DOUYIN_TAG_MAX]]
    meta["bilibili"]["polished_by_llm"] = True
    return meta, "已用 LLM 润色标题/简介/标签"


# ---- 命令入口 ---------------------------------------------------------------


def run_command(config: Config, ws: Workspace, args) -> int:  # noqa: ANN001
    lines_arg = getattr(args, "lines", None)
    base_arg = getattr(args, "base", None)
    use_llm = bool(getattr(args, "llm", False))

    try:
        meta = build_meta(ws, config, lines_arg=lines_arg, base_arg=base_arg)
        if use_llm:
            # P1：投稿文案的 LLM 润色同样进缓存/记账（重跑同一份物料不再花钱）
            from lvs import llm as llm_mod

            llm_mod.configure(ws=ws, stage="publish", config=config)
            meta, note = polish_with_llm(meta, config)
            print(f"  {note}")
        products = render_all(ws, config, meta, base_arg=base_arg)
    except PublishError as exc:
        print(f"投稿物料生成失败：{exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - 渲染异常要说人话，不要 traceback
        print(f"投稿物料生成失败：{type(exc).__name__}: {exc}")
        return 1

    for w in meta.get("warnings") or []:
        print(f"  ⚠ {w}")
    print(f"投稿物料：{ws.path('publish')}")
    for key in ("bilibili_cover", "douyin_cover", "bilibili_md", "douyin_md", "meta_json"):
        print(f"  {key:<16} {products[key]}")

    out_arg = getattr(args, "out", None) or str(config.get("publish.out_dir", "") or "").strip()
    if out_arg:
        target = Path(str(out_arg)).expanduser()
        if not target.is_absolute():
            root = lib_dir(config) or ws.root
            target = Path(root) / target
        try:
            copied = copy_to(target, products, prefix=ws.task)
        except OSError as exc:
            print(f"  拷贝到 {target} 失败（物料仍在 .work 里，不影响使用）：{exc}")
        else:
            print(f"  已拷贝 {len(copied)} 个文件 → {target}")

    try:
        ws.mark_stage(
            "publish",
            outputs=sorted(products.values()),
            status="done",
            cover_lines=len(meta["cover"]["lines"]),
            tags=len(meta["bilibili"]["tags"]),
        )
    except Exception as exc:  # noqa: BLE001 - 记账是增强
        print(f"  [注意] manifest 记账失败（不影响产物）：{exc}")
    return 0
