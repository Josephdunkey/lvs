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
from lvs import media
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


_EPISODE_RE = re.compile(r"第\s*([0-9一二三四五六七八九十]+)\s*期")
_CN_DIGIT = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_number(raw: str) -> int | None:
    """`三` / `十三` / `二十三` → 3 / 13 / 23；认不出来（如 `廿`）返回 None。"""
    if raw.isdigit():
        return int(raw)
    if not raw or any(ch not in _CN_DIGIT and ch != "十" for ch in raw):
        return None
    if "十" not in raw:
        return _CN_DIGIT.get(raw) if len(raw) == 1 else None
    head, _, tail = raw.partition("十")
    tens = _CN_DIGIT.get(head, 1) if head else 1
    units = _CN_DIGIT.get(tail, 0) if tail else 0
    return tens * 10 + units


def episode_hint(parse_data: dict[str, Any]) -> int | None:
    """从拍摄稿标题里的「第 N 期」反解期号（比 config 里那个手改的数字可靠）。

    ★ 实测坑（2026-10-05）：`config.雨月物语.toml` 的 `episode` 是**跟书绑定**的，
      但期号跟**本期**绑定 —— 给 005 把它改成 5 之后，回头重出 003 的物料，
      简介就写成了"本期是……第 5 期"。而拍摄稿标题
      `003-雨月物语-夜宿荒宅 拍摄稿 · 《雨月物语》第三期「夜宿荒宅」`
      自带真值，优先用它；config 只作兜底。
    """
    texts = [str(parse_data.get("title") or "")]
    texts += [v for v in (parse_data.get("meta") or {}).values() if isinstance(v, str)]
    for text in texts:
        match = _EPISODE_RE.search(text)
        if match:
            num = _cn_number(match.group(1))
            if num:
                return num
    return None


def description(parse_data: dict[str, Any], config: Config, lines: list[str],
                *, episode: int | None = None) -> str:
    """B站简介：开场白 + 作品介绍 + 系列钩子 + 版权与画面声明。

    `episode` 由 `build_meta` 解好再传（拍摄稿优先 / config 兜底），这里不再自己读 config。
    """
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

    if episode is None:
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


def _band_mask(size: tuple[int, int], top: int, bottom: int, soft: int) -> Any:
    """纵向软边遮罩：`top..bottom` 之间全量，向两侧各羽化 `soft` 像素。

    ★ 2026-10-05 用户："主要不要挡画面"。横版以前的压暗是**整条左列从顶黑到底**，
      画面上下两头全被吃掉；现在只压"字块所在的那条横带"，两头恢复原亮度。
    """
    from PIL import Image

    w, h = size
    soft = max(1, soft)
    col: list[int] = []
    for y in range(h):
        if y < top:
            v = max(0.0, 1.0 - (top - y) / soft)
        elif y > bottom:
            v = max(0.0, 1.0 - (y - bottom) / soft)
        else:
            v = 1.0
        col.append(int(255 * v))
    strip = Image.new("L", (1, h))
    strip.putdata(col)
    return strip.resize((w, h), Image.BILINEAR)


def _scrim(size: tuple[int, int], orientation: str, strength: float = 0.82,
           reach: float = 0.72, band: tuple[int, int] | None = None,
           soft: int = 0) -> Any:
    """压暗层：横版压左侧、竖版压上部 —— 都是为了让字有足够对比。

    `band=(top, bottom)` 时再叠一层纵向软边遮罩（只有这条横带被压暗），
    `soft` 为羽化像素数；横版默认走 band。
    """
    from PIL import Image

    w, h = size
    if orientation == "left":
        strip = Image.new("L", (w, 1))
        strip.putdata([
            int(255 * strength * max(0.0, 1.0 - (x / max(1, w - 1)) / reach)) for x in range(w)
        ])
        alpha = strip.resize((w, h), Image.BILINEAR)
        if band is not None:
            alpha = Image.composite(alpha, Image.new("L", (w, h), 0),
                                    _band_mask(size, band[0], band[1], soft))
    else:
        strip = Image.new("L", (1, h))
        strip.putdata([
            int(255 * strength * max(0.0, 1.0 - (y / max(1, h - 1)) / reach)) for y in range(h)
        ])
        alpha = strip.resize((w, h), Image.BILINEAR)
    return Image.new("RGB", (w, h), (0, 0, 0)), alpha


#: 行首禁则：这些标点不许出现在一行的开头（中文排版基本规则）。
#: 实测痛点：`《雨月物语》夜宿荒宅` 折行后 `》` 掉到最后一行行首 —— 观感很业余。
_NO_LINE_START = "，。、；：？！）》」』】”’…·"


def _fix_orphan_punct(rows: list[str]) -> list[str]:
    """把行首的收尾标点并回上一行（避头尾）。宁可上一行略超宽，也不让标点单吊。"""
    out = list(rows)
    for i in range(1, len(out)):
        while out[i] and out[i][0] in _NO_LINE_START:
            out[i - 1] += out[i][0]
            out[i] = out[i][1:]
    return [row for row in out if row]



def _wrap_balanced(draw: Any, line: str, font: Any, max_w: float) -> list[str]:  # noqa: ANN001
    """折行后再把"孤字行 / 太短的末行"摊平。

    ★ 实测痛点（2026-10-05 抖音竖版）：`他七年没回家` 被贪心折行切成
      `他七年没回` + `家` —— 末行只有一个字，封面立刻显得业余。
      对策：末行短到不足最长行的 40% 时，按**字数**把整句均分重切
      （中文近似等宽，均分观感最好）；均分后任一行超宽 → 退回贪心结果，
      保证"只敢优化、绝不画坏"。
    """
    rows = list(graphic.wrap(draw, line, font, max_w) or [line])
    if len(rows) < 2:
        return rows
    widest = max(len(r) for r in rows)
    if len(rows[-1]) * 2 >= widest:
        return rows
    n, total = len(rows), len(line)
    even = [line[i * total // n:(i + 1) * total // n] for i in range(n)]
    if all(even) and all(draw.textlength(r, font=font) <= max_w for r in even):
        return even
    return rows


#: 封面字"呼吸系数"：折行撑满算出来的最大字号再乘这个数。
#: 1.00 = 顶满字框（第一版，用户："有点太大"）→ 0.85 → 0.80
#: → **0.76**（用户 2026-10-05 第三轮："再小半个字号，主要不要挡画面"）。
#: 0.76 是**能守住下面这条红线的最小档**（实测 douyin 基准句 8.33% > 8%）：
#: ★ 改这个数前先看 tests/test_publish.py::test_cover_text_is_big_enough
#:   （单行字高必须 ≥ 屏高 8% —— 当年那批只有 2.8%，等于没字）。
_COVER_FILL = 0.76

#: 压暗强度（0..1）。用户第三轮要"不要挡画面"：0.78 → 0.70 → **0.62**。
#: 深墨字 + 奶白描边本身就吃得开，压暗只负责"托住字"，不该把画面蒙成灰玻璃。
_COVER_SCRIM = 0.62

_COVER_INK = (18, 24, 44)
_COVER_PAPER = (250, 248, 240)


#: 封面**专用**字体偏好：重黑体优先。
#: ★ 为什么不能用 `graphic.font_path()`（2026-10-05 实测）：那条链第一个命中是
#:   `NotoSerifSC-VF.ttf` —— **衬线可变字体**，默认字重偏细，配上粗描边就成了
#:   "空心描边字"（用户："那几张图不好看"）。封面要的是砸在脸上的粗黑，
#:   数据卡片继续用那一套，不动。
_COVER_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\NotoSansSC-VF.ttf",   # 可变字重黑体，能拉到 Black(900)
    r"C:\Windows\Fonts\msyhbd.ttc",          # 微软雅黑 Bold
    r"C:\Windows\Fonts\Dengb.ttf",           # 等线 Bold
    r"C:\Windows\Fonts\simhei.ttf",          # 黑体
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)


def _cover_font_path() -> str | None:
    """封面字体：重黑体优先，找不到才退回卡片那套（保证不因缺字体而崩）。"""
    for candidate in _COVER_FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return graphic.font_path()


def _cover_font(size: int, path: str | None = None) -> Any:
    """封面字形的**唯一入口**：可变字体拉到最重（Black/900），静态字体照用。"""
    from PIL import ImageFont

    resolved = path or _cover_font_path()
    if not resolved:
        raise PublishError("封面叠字找不到中文字体（试过 NotoSansSC/msyhbd/Dengb/simhei/PingFang）")
    font = ImageFont.truetype(resolved, size)
    for setter in (lambda: font.set_variation_by_name("Black"), lambda: font.set_variation_by_axes([900])):
        try:
            setter()
            break
        except (AttributeError, OSError, ValueError, TypeError):
            continue   # 静态字体没有变体轴 —— 正常情况，不是错误
    return font


def _trim_flat_bands(img: Any, *, tol: float = 9.0, min_frac: float = 0.06) -> Any:
    """裁掉**平涂边带**（左/右/上/下四个方向）。

    ★ 为什么需要（2026-10-05 实测，不是理论担心）：出图时若想"给封面字留白"，
      留成一块**平涂色块**，封面会像被劈成两半 —— 实测 `base_003.png` 左侧 638 px
      （41.5% 宽）的灰度标准差 < 4，`hbase_002..005` 也各有 35–43% 的左带。
      留白要靠构图（天空、墙面），不能靠填色；真填了，这里自动裁掉。

    判据：降到 ≤240 px 宽后逐列/逐行算灰度标准差（平涂 ≈ 0，画面 ≥ 10），
    从四条边往里走到第一条"有内容"的线。裁得太狠（剩下的任一边不足 40%）或
    四边都没到 `min_frac` → 当作误判，原图返回。**不用 numpy**（Pillow 够用，不添依赖）。
    """
    from PIL import Image

    gray = img.convert("L")
    W, H = gray.size
    sw = min(240, W)
    sh = max(1, round(H * sw / max(1, W)))
    small = gray.resize((sw, sh), Image.BILINEAR)
    px = list(small.getdata())

    def _std(vals: list[int]) -> float:
        mean = sum(vals) / len(vals)
        return (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5

    left = 0
    while left < sw and _std([px[y * sw + left] for y in range(sh)]) < tol:
        left += 1
    right = 0
    while right < sw - left and _std([px[y * sw + sw - 1 - right] for y in range(sh)]) < tol:
        right += 1
    top = 0
    while top < sh and _std(px[top * sw:(top + 1) * sw]) < tol:
        top += 1
    bottom = 0
    while bottom < sh - top and _std(px[(sh - 1 - bottom) * sw:(sh - bottom) * sw]) < tol:
        bottom += 1

    sx, sy = W / sw, H / sh
    cut_l, cut_r, cut_t, cut_b = left * sx, right * sx, top * sy, bottom * sy
    if max(cut_l, cut_r, cut_t, cut_b) < min_frac * min(W, H):
        return img
    box = (int(cut_l), int(cut_t), W - int(cut_r), H - int(cut_b))
    if box[2] - box[0] < W * 0.4 or box[3] - box[1] < H * 0.4:
        return img
    return img.crop(box)


def render_cover(base: Path | None, lines: list[str], size: tuple[int, int], out: Path,
                 *, layout: str = "left", font_file: str | None = None) -> Path:
    """把封面字叠到底图上，输出 PNG。`layout` 决定字块位置与压暗方向。

    - `left`（B站横版）：左侧压暗 + 左对齐大字，主体留在右侧；
    - `top`（抖音竖版）：上部压暗 + 居中大字，避开底部操作区。

    ★ 2026-10-05 重做（用户反馈"那几张图不好看 / 封面的字也不够大"）：
      ① 字号口径从"不许折行、放不下就缩"改成"**折行撑满**" —— 旧口径为了让三行
         保持三行，10 字以上的长句必被压到 4% 屏高（1536 宽实测字号 ≈ 60 px）；
         现在按折行后的总高反解**最大**字号（同一个盒子能到 ≈ 200 px），
        再乘 `_COVER_FILL` 留一圈呼吸；
      ② 底图先过 `_trim_flat_bands`：画面必须铺满（见那里的实测数字）；
      ③ 配色改为深墨字 + 奶白描边（见 `_COVER_INK`）。
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
            canvas = _fit_to_size(_trim_flat_bands(im.convert("RGB")), size)
    else:
        canvas = _gradient(size)

    # 压暗层挪到"字块算完"之后再合成 —— 只有知道字块在哪，才能只压那一条横带。
    draw = ImageDraw.Draw(canvas)

    # ★ 字块**占地**（2026-10-05 用户第二轮反馈："再小半个字号，主要不要挡画面"）：
    #   竖版把字收进上半屏（底 0.74 → 0.62）—— 下半屏整个留给画面；
    #   横版不再横贯整幅（右 0.95 → 0.80）—— 右侧至少留出 20% 让主体露脸。
    #   配合 `_COVER_FILL` 降半号，同一句话占的像素面积降了约 1/3。
    if layout == "left":
        box = (int(w * 0.055), int(h * 0.08), int(w * 0.80), int(h * 0.90))
        align_center = False
    else:
        box = (int(w * 0.05), int(h * 0.05), int(w * 0.95), int(h * 0.62))
        align_center = True
    max_w = box[2] - box[0]
    max_h = box[3] - box[1]

    # ★ 折行撑满：从大到小试字号，第一个"折行后总高放得下"的就是它。
    #   行数变了（三行可能折成六行）是**要的**效果 —— 封面靠字大，不靠字少。
    def _layout(size: int) -> tuple[Any, list[str]]:
        """按字号折行 + 避头尾，返回 (font, rows)。"""
        f = _cover_font(size, font_path)
        wrapped: list[str] = []
        for line in lines:
            wrapped.extend(_wrap_balanced(draw, line, f, max_w))
        return f, _fix_orphan_punct(wrapped)

    cap = int(h * (0.20 if layout == "left" else 0.155))
    min_size = max(28, int(h * 0.05))
    size_px = cap
    while size_px > min_size:
        font, rows = _layout(size_px)
        if len(rows) * int(size_px * 1.26) <= max_h:
            break
        size_px -= 4
    else:                                       # 到下限还放不下：就用下限，宁可挤
        font, rows = _layout(min_size)
        size_px = min_size

    # ★ 呼吸系数：撑满是"别把字做小"，但顶到框边就太满了 ——
    #   用户 2026-10-05 看完成品说"那个字又有点太大了，稍微小点"。
    #   缩一号再重折一次（字号变小只会让行数不增，所以必定还是放得下）。
    size_px = max(min_size, int(size_px * _COVER_FILL))
    font, rows = _layout(size_px)

    line_h = int(font.size * 1.26)
    block_h = line_h * len(rows)
    top = box[1] + max(0, (max_h - block_h) // 2)

    # ★ 压暗只围字块走（2026-10-05 第三轮："主要不要挡画面"）：
    #   横版不再是整条左列从顶黑到底，只压字块那条横带（上下各留半行做羽化），
    #   射程也从 0.72 收到 0.62；竖版射程贴着字块末行收，不再一律吃满 0.52 屏高。
    #   强度 0.70 → `_COVER_SCRIM`(0.62)：画面明显更亮，字靠描边照样立得住。
    pad = line_h // 2
    if layout == "left":
        reach: float = 0.62
        band: tuple[int, int] | None = (max(0, top - pad), min(h, top + block_h + pad))
    else:
        reach = max(0.26, min(0.52, (top + block_h + pad) / h))
        band = None
    layer, alpha = _scrim(size, "left" if layout == "left" else "top",
                          strength=_COVER_SCRIM, reach=reach, band=band,
                          soft=max(8, int(h * 0.05)))
    canvas = Image.composite(layer, canvas, alpha)
    draw = ImageDraw.Draw(canvas)
    # 描边 0.07 字高：0.11 时**描边把字骨吃掉了**（实测渲染成了空心描边字，
    # 不像 001/002 的粗黑字）。粗黑体 + 细一圈奶白边才是房规。
    stroke = max(3, int(font.size * 0.07))
    for i, row in enumerate(rows):
        x = box[0] + (max_w - draw.textlength(row, font=font)) / 2 if align_center else box[0]
        draw.text((x, top + i * line_h), row, font=font, fill=_COVER_INK,
                  stroke_width=stroke, stroke_fill=_COVER_PAPER)

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, "PNG")
    return out



def _font_path(explicit: str | None) -> str | None:
    if explicit:
        path = Path(str(explicit)).expanduser()
        if not path.is_file():
            raise PublishError(f"配置的封面字体不存在：{path}")
        return str(path)
    return _cover_font_path()


# ---- 底图挑选 ---------------------------------------------------------------


# 封面底图的图片白名单 —— 真源 `lvs.media`（缺陷 B02/Q02）。这里不再写第二份字面量：
# 原先 `pick_base` 自己 `glob("*.png") + glob("*.jpg")`，漏了 jpeg/webp/tif ——
# 一目录的 .webp 会被报成"目录里没有图片"。回归断言见 `tests/test_media.py`。
IMAGE_EXTS = media.IMAGE_EXTS


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
            found = media.glob_images(p)
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
        found = [x for x in media.glob_images(d) if x.name.startswith("shot-")]
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
    episode = episode_hint(parse_data)
    episode_from_script = episode is not None
    if episode is None:
        episode = config.get("publish.episode")
    desc = description(parse_data, config, lines, episode=episode)

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
    configured_episode = config.get("publish.episode")
    try:
        mismatch = (episode_from_script and configured_episode is not None
                    and int(configured_episode) != int(episode))
    except (TypeError, ValueError):
        mismatch = False
    if mismatch:
        warnings.append(
            f"config 里 publish.episode = {configured_episode}，但拍摄稿写的是第 {episode} 期；"
            f"物料按期号 {episode} 出（config 那个数是手改的，换期容易忘）"
        )

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
            "episode": episode,
            "episode_source": "拍摄稿标题" if episode_from_script else "config",
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
