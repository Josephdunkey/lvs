"""画面位的 beat 拆分、提示词卫生与 beat 分类（票据 27）。

拍摄稿里的一行 `[画面位]` 往往是**剪辑设计**，而不是画面描述：

    三段原文并排浮现 → 高亮"斩首四万/斩首虏九万"vs"邑三十六/口三万" → 中间一道"科目转换器"

整行当提示词喂给图像模型，模型会把引号里的字**画出来**（观感上就是伪汉字/乱码），
而且图表类 beat 本就不该走生图。本模块只做三件**纯函数**的事：

1. `split_beats()` —— 把一行画面位按 `→` 拆成 beat，并读掉显式的 `[场景]`/`[图表]` 标注
2. `sanitize()`    —— 剥掉引号里的字幕原文、箭头、剪辑动词，只留可拍的场景语言
3. `classify()`    —— 判 beat 是 `scene` 还是 `graphic`：显式标注优先，关键词规则兜底

设计取舍：
- **箭头有二义性**。`时间轴：前403 → 前313 → …` 里的箭头是**数据**，不是 beat 分隔符。
  因此当分片大多数都很短（日期/数字串）时，整行当作**一个** beat（`_looks_like_data`）。
- 同理，带显式标注的 beat 会**吸收**紧随其后的数据型短片段（`[图表] 时间轴：前403 → 前313`）。
- **判不准要能被看见**：没有任何标记词的 beat 用 `has_markers()` 报出，
  由调用方列成清单交人复核（票据 27 的验收要求）。
"""

from __future__ import annotations

import re
from typing import NamedTuple

KIND_SCENE = "scene"
KIND_GRAPHIC = "graphic"


class Beat(NamedTuple):
    """一个画面 beat：`text` 是描述，`kind` 为显式标注（未标注则 None）。"""

    text: str
    kind: str | None = None


# ---- 拆分 ------------------------------------------------------------------

ARROW = re.compile(r"\s*(?:→|⇒|->|=>)\s*")

# 日期/数字类短分片：这种文本里的箭头是数据，不是 beat 分隔符
_DATA_PIECE = re.compile(r"^[前公元\-–—~～至第\d\s.,、%万亿元千百十人年月日:：]+$")

_TAG = re.compile(r"^[\[【]\s*(场景|实拍|画面|图表|图示|图表卡)\s*[\]】]\s*")
# 「模板头」：`时间轴：…` / `分栏：…`，冒号说明这一行是一条完整设计而非并列的多个 beat
_HEAD_TEMPLATE = re.compile(r"[：:]")

_KIND_OF_TAG = {
    "场景": KIND_SCENE, "实拍": KIND_SCENE, "画面": KIND_SCENE,
    "图表": KIND_GRAPHIC, "图示": KIND_GRAPHIC, "图表卡": KIND_GRAPHIC,
}


def _looks_like_data(piece: str) -> bool:
    p = piece.strip()
    # 中文信息密度高：4 字以内才算"短分片"（`前313`、`43万`），
    # 6 字的 `一段画面描述` 已经是正经描述了，不能当数据吞掉。
    return len(p) <= 4 or bool(_DATA_PIECE.match(p))


def _is_tagged(piece: str) -> bool:
    return _TAG.match(piece.strip()) is not None


def _make_beat(raw: str) -> Beat:
    text = (raw or "").strip()
    m = _TAG.match(text)
    if m:
        return Beat(text[m.end():].strip(), _KIND_OF_TAG[m.group(1)])
    return Beat(text, None)


def split_beats(raw: str) -> list[Beat]:
    """把一行 `[画面位]` 拆成 beat 列表；空输入返回空列表。"""
    text = (raw or "").strip()
    if not text:
        return []

    parts = [p.strip() for p in ARROW.split(text) if p.strip()]
    if len(parts) <= 1:
        return [_make_beat(text)]

    # 模板头（`时间轴：` / `分栏：` / `对照：`）：整行是**一条**设计，箭头在里面是内容
    if _HEAD_TEMPLATE.search(parts[0]):
        return [_make_beat(text)]

    # 大多数分片都是短数据（时间轴/年份串）→ 箭头是数据，整行一个 beat
    data_ish = sum(1 for p in parts if _looks_like_data(p))
    if data_ish >= max(2, len(parts) * 0.6):
        return [_make_beat(text)]

    # 带显式标注的 beat 吸收紧随其后的数据型短片段（`[图表] 时间轴：… → 前313`）
    groups: list[list[str]] = [[parts[0]]]
    for i in range(1, len(parts)):
        prev = parts[i - 1]
        if _looks_like_data(parts[i]) and (_looks_like_data(prev) or _is_tagged(prev)):
            groups[-1].append(parts[i])
        else:
            groups.append([parts[i]])
    return [_make_beat(" → ".join(g)) for g in groups]


# ---- 提示词卫生 ------------------------------------------------------------

# 引号内的内容是**屏幕字幕原文**，不是画面 —— 必须整段摘掉，否则会被画出来
QUOTED = re.compile(r'"[^"]*"|“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|《[^》]*》')

# 同上，但**带捕获组**：要的是引号里的内容本身（图文卡片要显示的正是它）
QUOTED_INNER = re.compile(r'"([^"]*)"|“([^”]*)”|「([^」]*)」|『([^』]*)』|《([^》]*)》')

# 纯剪辑动作：写的是"怎么剪"，不是"画面里有什么"
_EDIT_WORDS = (
    "并排浮现", "并排", "浮现", "高亮", "砸屏", "分栏呈现", "分栏",
    "对切", "逐行", "滚屏", "叠化", "叠印", "转场", "切至", "拉远", "推近",
    "字幕", "标注", "呈现", "排版", "核心画面",
)


def has_edit_verbs(text: str) -> bool:
    """文本里有没有纯剪辑动词。

    有它 = 这条写的是"要怎么剪"，不是"屏幕上有什么/是什么内容"。
    卡片内容判定靠它把「三段原文并排浮现」这类**指令**挡在门外。
    """
    return any(word in (text or "") for word in _EDIT_WORDS)

_EMPTY_BRACKETS = re.compile(r"[（(][\s，,、;；:：]*[)）]")
_WHITESPACE = re.compile(r"\s+")


def sanitize(text: str) -> str:
    """剥掉引号原文 / 箭头 / 剪辑动词，返回可拍的场景语言（可能为空串）。"""
    out = QUOTED.sub("", text or "")
    out = ARROW.sub(" ", out)
    for word in _EDIT_WORDS:
        out = out.replace(word, " ")
    out = _EMPTY_BRACKETS.sub(" ", out)
    out = _WHITESPACE.sub(" ", out).strip()
    return out.strip(" ，,、;；:：。.-–—·")


# 卡片正文里的拉丁词（`vs` 之类）前后留白，免得上屏糊成一团
_LATIN = re.compile(r"([A-Za-z]+)")


def card_text(text: str) -> str:
    """图文卡片的正文：**去动词、留数据**（与 `sanitize()` 恰好相反）。

    对提示词来说引号里是"会被画出来的字幕"，要删；
    对卡片来说引号里**正是要显示的数据**，要留 —— 只把引号摘掉、把剪辑动词去掉：

        高亮"斩首四万/斩首虏九万"vs"邑三十六/口三万"
          → 斩首四万/斩首虏九万 vs 邑三十六/口三万
    """
    out = QUOTED.sub(lambda m: m.group(0)[1:-1], text or "")   # 摘引号，留内容
    out = ARROW.sub(" ", out)
    for word in _EDIT_WORDS:
        out = out.replace(word, " ")
    out = _EMPTY_BRACKETS.sub(" ", out)
    out = _LATIN.sub(r" \1 ", out)
    out = _WHITESPACE.sub(" ", out).strip()
    return out.strip(" ，,、;；:：。.-–—·")


# ---- 分类 ------------------------------------------------------------------

# 图表/文字类：屏幕上"排版"出来的信息，交给图表渲染器，不送生图
_GRAPHIC_MARKERS = (
    "时间轴", "时间线", "并排", "分栏", "对照", "对比图", "对切", "砸屏",
    "字幕", "浮现", "高亮", "呈现", "列出", "表格", "清单", "示意", "流向",
    "框图", "曲线", "图表", "图标", "排版", "原文", "逐行", "数据", "数字",
    "账面", "地图", "剖面", "结构", "关系图", "比例", "排行", "公式", "标注",
    "题板", "标题卡", "卡面", "三句", "两首", "分镜表",
)

# 场景类：能实拍出来的画面
_SCENE_MARKERS = (
    "特写", "长镜", "远景", "近景", "中景", "全景", "航拍", "俯瞰", "空镜",
    "人影", "剪影", "身影", "侧光", "逆光", "黄昏", "夜色", "火光", "烛",
    "战场", "宫殿", "城门", "城墙", "城破", "宫库", "市集", "街", "田野",
    "山坡", "山脉", "河流", "沙漠", "雪", "雨", "风沙", "尘土", "军队",
    "士兵", "百姓", "竹简", "玉玺", "铜钱", "黄金", "刀", "马", "旗帜",
    "粮仓", "渡口", "山道", "营帐", "刑场",
)


def has_markers(text: str) -> bool:
    """文本里是否出现任何分类标记词（都没有 = 判不准，需要人复核）。"""
    return any(m in text for m in _GRAPHIC_MARKERS) or any(m in text for m in _SCENE_MARKERS)


def classify(text: str, explicit: str | None = None) -> str:
    """判定 beat 属于 `scene` 还是 `graphic`：显式标注优先，关键词规则兜底。"""
    if explicit in (KIND_SCENE, KIND_GRAPHIC):
        return explicit
    graphic = sum(1 for m in _GRAPHIC_MARKERS if m in text)
    scene = sum(1 for m in _SCENE_MARKERS if m in text)
    return KIND_GRAPHIC if graphic > scene else KIND_SCENE


# ---- 生成前开关：图文图表感 / 实拍剧照感 ------------------------------------

MODE_GRAPHIC = KIND_GRAPHIC   # 图文图表感：graphic beat 交给图表渲染器，不送生图
MODE_PHOTO = "photo"          # 实拍剧照感：所有 beat 都当场景翻译，一律走生图


def resolve_kind(text: str, explicit: str | None = None, mode: str = MODE_GRAPHIC) -> str:
    """按生成前的开关定夺 kind。`photo` 模式一律当场景（图表也翻译成可拍画面）。"""
    if mode == MODE_PHOTO:
        return KIND_SCENE
    return classify(text, explicit)


# ---- 否定式识别（单一真源） --------------------------------------------------

# 为什么要**一处定义**：`lvs check` 拿它报 error、`lvs migrate` 拿它列工作清单。
# 两处各写一套规则必然漂移 —— 先写的窄、后写的宽，用户就不知道该信哪个，
# 最后两边都不信（这类"工具互相打架"比没有工具更糟）。
#
# 判据来自实机教训：Z-Image Turbo 是 cfg=1.0 蒸馏模型，**采样器完全不吃负向**
# （workflows/*.json 里 negative 接的是 ConditioningZeroOut）。
# 推论：提示词里出现的每一个具体名词，模型都会尽力画出来 —— 越说"不要"，越多。

NEGATION_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "C021-en",
        re.compile(
            r"\b(?:no|not|without|never|none|nothing)\b[^,.;]{0,24}?\b"
            r"(?:people|person|man|woman|face|text|letter|word|number|character|logo|"
            r"watermark|sign|signage|label|caption|bubble)\b",
            re.IGNORECASE,
        ),
        "英文否定式：模型会按字面把否定词里的名词画出来",
    ),
    (
        "C021-noText",
        re.compile(
            r"无字|无文字|无任何图案|零文字|没有任何(?:其他)?文字|空白小票|空白铜牌|"
            r"禁写|不写|不得出现|不出现|禁止出现"
        ),
        "「无字/禁写」类提醒：否定词里的「字」会被画成乱码；"
        "零文字要靠全局风格后缀**正面**压制，或改成物件的物理属性",
    ),
    (
        "C021-empty",
        re.compile(r"空无一人|四下无人|一个人也没有|空无|无人"),
        "空镜的「无人」断言：画面越空，模型越要自己补一个人 —— "
        "改成描述画面里**真有的东西**（纹理、光影、风）",
    ),
    (
        "C021-noGive",
        re.compile(r"不给[^，。；、]{0,10}|不画[^，。；、]{0,10}|不做[^，。；、]{0,10}"),
        "构图指令写成否定（不给正面/不给五官/不给过程）：应改为正面构图词"
        "（侧脸构图 / 纯剪影 / 只拍物件）",
    ),
    (
        "C021-zh",
        re.compile(
            r"(?:没有|无|不要|别出现)[^，。；,;]{0,8}"
            r"(?:人|文字|字|脸|招牌|水印|logo|数字|烟|痕|杂物|图案|东西)"
        ),
        "中文否定式：同英文，会被按字面画出来",
    ),
)


def find_negations(text: str) -> list[tuple[str, str, str]]:
    """扫出一段文本里的否定式写法。返回 `[(code, 命中的片段, 原因)]`。

    同一条文本可能命中多条规则（`空无一人、无字` 同时命中 empty 与 noText），
    按规则顺序全部返回 —— 校验器要报全，迁移工作清单只要第一条。
    """
    hits: list[tuple[str, str, str]] = []
    for code, pattern, why in NEGATION_RULES:
        m = pattern.search(text or "")
        if m:
            hits.append((code, m.group(0), why))
    return hits
