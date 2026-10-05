"""拆镜（`lvs shots`）—— 票据 06 + 07 + 08。

把 `parse.json`（段落 / 旁白 / 画面位）变成 `shots.json`（逐句分镜）：

1. **切分（票据 06）**：段落按**句**切成 shot，`narration` 为该句旁白，
   `visual` 优先取"时间上最近的画面位"，否则用段标题兜底。
2. **提示词（票据 07）**：为每镜生成 `prompt`（local 生图，英文）与
   `keywords`（pexels 检索，英文 1–3 词）。
3. **来源判定（票据 08）**：`source ∈ {pexels, local}`，写回并做结构校验。

退化策略（重要）：
- 没有 LLM key 时**不硬崩**，改用启发式（中英小词表 + 规则判定），
  并在每镜标 `generated_by="heuristic"`、在 `shots.json.notes` 说明。
  这样"没有密钥的机器"也能把整条流水线跑通（票据 03 精神）。
- LLM 返回坏 JSON：重试 1 次；仍坏则把原始响应落盘并**报错退出**（票据 08）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from lvs import llm as llm_mod
from lvs import llm_provider
from lvs import prompting, sources
from lvs.config import Config
from lvs.progress import track
from lvs.workspace import Workspace

# ---- 统一画面风格（见 `lvs/styles.py`） --------------------------------------
#
# ★ 2026-10-03 起，风格预设**不再写死在本文件**。原因：只有 3 个内置预设时，
#   每换一个题材（水墨 / 民国月份牌 / 赛博）都得改代码发版 —— 而 `lvs` 要服务的
#   不止一本书。
#
#   现在风格来自四处，优先级：--style-file > 项目素材库 > config 内联 > 内置（29 个）。
#   本模块保留这几个名字**只为向后兼容**（老测试与老脚本在用）：
#   `Style` / `STYLES`（= 内置表）/ `STYLE_DEFAULT` / `STYLE_NAME` / `STYLE_SUFFIX`。

from lvs.styles import (  # noqa: E402 - 放在此处便于读者看到"风格已外移"
    BUILTIN as STYLES,
    DEFAULT_NAME as STYLE_DEFAULT,
    Style,
    StyleError,
)


def style_for(name: str | None, *, config=None, style_file=None) -> Style:  # noqa: ANN001
    """按名字取风格。

    不传 `config` 时只在**内置表**里找（老行为，供纯函数式调用与测试用）；
    传了就走上层合并后的注册表（项目文件 + config 内联都能命中）。

    未知名字**报错**而不是悄悄退回默认 —— 整片风格静默跑偏，
    要等全部出完图才会被发现。
    """
    if config is None and style_file is None:
        key = (name or "").strip() or STYLE_DEFAULT
        try:
            return STYLES[key]
        except KeyError:
            raise StyleError(f"未知画面风格 {key!r}。可选：{', '.join(sorted(STYLES))}") from None
    from lvs.styles import build_registry

    return build_registry(config, style_file=style_file).get(name)


def registry_for(config=None, *, style_file=None):  # noqa: ANN001
    """本次真正生效的风格注册表（含项目自定义）。`lvs styles` 与 `lvs shots` 都用它。"""
    from lvs.styles import build_registry

    return build_registry(config, style_file=style_file)


# 默认风格后缀 —— 老代码/老测试引用它；真实的默认可能被项目风格文件覆盖
STYLE_SUFFIX = STYLES[STYLE_DEFAULT].suffix
STYLE_NAME = STYLE_DEFAULT

# 一个句子超过这个字数就按软标点再切（字幕一行别太长）
MAX_SHOT_CHARS = 40
# 短于这个字数的碎片并入相邻句（避免"但。"这类孤句），合并不丢字。
# 取 3 是关键：中文短句常为 4–5 字（如"第一句。"），不能被误并。
MIN_SHOT_CHARS = 3


# ---- 中文断句 --------------------------------------------------------------

_QUOTE_PAIRS = {
    "「": "」", "『": "』", "“": "”", "‘": "’",
    "《": "》", "（": "）", "(": ")", "【": "】", "[": "]",
}
_HARD_ENDS = set("。！？!?…")
_SOFT_ENDS = set("；;，、,")


def split_sentences(text: str, max_chars: int = MAX_SHOT_CHARS) -> list[str]:
    """把一段旁白切成句子：**不拆坏引号内的原文**，过长句再按软标点切。

    保证：`''.join(结果)` 与输入在去掉空白后一致（不丢字、不重复）。
    """
    text = (text or "").strip()
    if not text:
        return []

    parts: list[str] = []
    buf: list[str] = []
    stack: list[str] = []  # 引号栈（支持嵌套）
    for ch in text:
        buf.append(ch)
        if ch in _QUOTE_PAIRS:
            stack.append(_QUOTE_PAIRS[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
        if not stack and ch in _HARD_ENDS:
            parts.append("".join(buf))
            buf = []
    if buf:
        parts.append("".join(buf))

    out: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if len(part) <= max_chars:
            out.append(part)
        else:
            out.extend(_split_long(part, max_chars))
    return _merge_short(out)


def _split_long(text: str, max_chars: int) -> list[str]:
    """按软标点把长句切成 ≤ max_chars 的块（标点保留在前一块末尾）。"""
    chunks: list[str] = []
    buf: list[str] = []
    for ch in text:
        buf.append(ch)
        if ch in _SOFT_ENDS and len(buf) >= max_chars // 2:
            chunks.append("".join(buf))
            buf = []
        elif len(buf) >= max_chars and ch in _SOFT_ENDS:
            chunks.append("".join(buf))
            buf = []
    if buf:
        chunks.append("".join(buf))

    # 仍超长的硬切（极少见，仅在无标点的长串上发生）
    fixed: list[str] = []
    for c in chunks:
        while len(c) > max_chars * 2:
            fixed.append(c[:max_chars])
            c = c[max_chars:]
        if c:
            fixed.append(c)
    return fixed


def _merge_short(parts: list[str]) -> list[str]:
    """把过短碎片并入相邻句，保证不丢字。"""
    merged: list[str] = []
    for part in parts:
        if merged and len(part) < MIN_SHOT_CHARS:
            merged[-1] = merged[-1] + part
        else:
            merged.append(part)
    return merged


# ---- 画面位映射 ------------------------------------------------------------


def _owner_index(visuals: list[tuple[int, Any]], pos: int) -> int:
    """句子归属哪个画面位：时间上最近的那个；都在它后面则用第一个；没有则 -1。"""
    if not visuals:
        return -1
    owner = -1
    for i, (p, _) in enumerate(visuals):
        if p <= pos:
            owner = i
    return owner if owner >= 0 else 0


def _sentences_with_visual(segment: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """返回该段内 (旁白句, 画面描述, beat kind) 列表。

    两步映射（票据 27）：
    1. 每句先归到**时间上最近的画面位**（原有逻辑）；
    2. 再在该画面位的 beats 上**按序平均分配** —— 这样 36 句旁白摊到 5 个 beat 上，
       相邻几镜各得其画面，而不是整段共用同一条剪辑设计。
    """
    flow = segment.get("flow") or []
    visuals: list[tuple[int, list[dict[str, Any]]]] = []
    sentences: list[tuple[int, str]] = []

    if flow:
        for idx, item in enumerate(flow):
            if item.get("kind") == "visual":
                beats = item.get("beats") or [{"text": item.get("text", ""), "kind": None}]
                visuals.append((idx, beats))
            else:
                for s in split_sentences(item.get("text", "")):
                    sentences.append((idx, s))
    else:  # 老格式兜底（无 flow 字段）
        for line in segment.get("narration", []):
            for s in split_sentences(line):
                sentences.append((0, s))
        marks = [{"text": v, "kind": None} for v in segment.get("visual_marks", [])]
        visuals = [(0, marks)] if marks else []

    heading = segment.get("heading") or ""

    # 按归属的画面位把句子分组（保持原顺序）
    groups: list[tuple[int, list[str]]] = []
    for pos, sent in sentences:
        owner = _owner_index(visuals, pos)
        if groups and groups[-1][0] == owner:
            groups[-1][1].append(sent)
        else:
            groups.append((owner, [sent]))

    out: list[tuple[str, str, str | None]] = []
    for owner, sents in groups:
        if owner < 0:
            out.extend((sent, heading or sent, None) for sent in sents)
            continue
        beats = visuals[owner][1]
        n, m = len(sents), len(beats)
        for k, sent in enumerate(sents):
            beat = beats[min(m - 1, k * m // n)] if m else None
            visual = (beat or {}).get("text") or heading or sent
            out.append((sent, visual, (beat or {}).get("kind")))
    return out


# ---- 启发式（无 LLM 时） ---------------------------------------------------

# 小型中→英词表：只为让 pexels 在没有 LLM 时也有机会命中
_LEXICON = {
    "竹简": "bamboo slips", "古籍": "ancient book", "皇帝": "emperor", "天子": "emperor",
    "宫殿": "palace", "城墙": "city wall", "战场": "battlefield", "军队": "army",
    "士兵": "soldier", "百姓": "crowd of people", "农田": "farmland", "河流": "river",
    "山脉": "mountains", "火焰": "fire", "地图": "ancient map", "账簿": "ledger",
    "铜钱": "ancient coins", "沙漠": "desert", "夜空": "night sky", "大雨": "heavy rain",
    "大雪": "snowfall", "城墙": "city wall", "桥梁": "stone bridge", "树木": "trees",
    "街道": "ancient street", "马车": "horse carriage", "寺庙": "temple", "旗帜": "banner",
}

_LOCAL_MARKERS = (
    "竹简", "古籍", "地图", "图表", "示意", "账", "简牍", "文字", "数字", "表",
    "统计", "时间轴", "概念", "抽象", "符号", "印章", "诏书", "编年", "对照",
    "标题", "公式", "数据", "清单", "结构", "关系",
)
_PEXELS_MARKERS = (
    "人", "城", "街", "山", "河", "云", "天", "马", "军队", "士兵", "百姓",
    "宫殿", "田野", "火", "雨", "雪", "夜", "门", "桥", "树", "沙漠", "战场",
    "水面", "日出", "日落", "人群", "市集", "村庄",
)


def heuristic_keywords(visual: str) -> list[str]:
    """从画面描述里挑英文检索词（命中词表优先），无则退回中文原词。"""
    hits: list[str] = []
    for zh, en in _LEXICON.items():
        if zh in visual and en not in hits:
            hits.append(en)
    if hits:
        return hits[:3]
    # 退路：取画面描述里最长的 2–4 字连续中文片段
    zh_runs = re.findall(r"[\u4e00-\u9fff]{2,4}", visual)
    return zh_runs[:3] or [visual[:12]]


def heuristic_prompt(visual: str, style: Style | None = None) -> str:
    """无 LLM 时的生图提示词：中文画面 + 英文风格后缀（Z-Image 中文理解强）。"""
    core = (visual or "").strip().rstrip("。.")
    return f"{core}. {(style or STYLES[STYLE_DEFAULT]).suffix}"


def heuristic_source(visual: str) -> str:
    """规则判定：偏抽象/古籍/图表 → local；偏实拍场景 → pexels。"""
    local = sum(1 for m in _LOCAL_MARKERS if m in visual)
    pexels = sum(1 for m in _PEXELS_MARKERS if m in visual)
    return "local" if local >= pexels else "pexels"


# ---- 组装 shots ------------------------------------------------------------


def build_skeleton(parse_data: dict[str, Any], style_name: str = STYLE_NAME) -> list[dict[str, Any]]:
    """纯本地：把 parse.json 变成 shots 骨架（06）。时间字段留空，待 `lvs voice` 计算。

    `style_name` 记进每镜的 `style` 字段（仅为留痕；真正拼提示词用 `Style.suffix`）。
    """
    shots: list[dict[str, Any]] = []
    next_id = 1

    cold = parse_data.get("cold_open")
    if cold and cold.get("text"):
        meta = parse_data.get("meta") or {}
        # 兜底顺序：冷开场自带的画面位 → 标题文案 → 一句通用空镜。
        # ★ 为什么把冷开场自己的画面位排在标题之前：片头 0–30 秒是全片最贵的位置，
        #   而 `title_card` 是**文案**不是画面 —— 拿它当提示词等于叫模型把标题画出来
        #   （实测过：10 镜的提示词全变成「主用（金句钩子）：他念了一整夜的经…」）。
        #
        # 一律走 **beat 列表**（`cold_open["beats"]`，parse 已拆好）—— 与正文段同一个粒度：
        # 一条 `A → B` 的画面位在正文里会拆成两镜，冷开场也照样拆，不搞两套。
        beats = [str(b.get("text") or "") for b in (cold.get("beats") or [])]
        beats = [b for b in beats if b]
        if not beats:
            beats = [str(m) for m in (cold.get("visual_marks") or []) if m]
        fallback_visual = meta.get("title_card") or "史诗感的历史开场空镜"
        sentences: list[str] = []
        for line in cold["text"]:
            sentences.extend(split_sentences(line))
        spread = _spread_marks(sentences, beats) if beats else [fallback_visual] * len(sentences)
        for sent, visual in zip(sentences, spread):
            shots.append(
                _new_shot(
                    next_id, 0, "冷开场", sent, visual or fallback_visual,
                    style_name=style_name,
                )
            )
            next_id += 1

    for seg in parse_data.get("segments", []):
        for sent, visual, kind in _sentences_with_visual(seg):
            shots.append(
                _new_shot(
                    next_id, seg.get("index", 0), seg.get("heading", ""),
                    sent, visual, kind, style_name=style_name,
                )
            )
            next_id += 1

    return _renumber(_merge_dangling(shots))


# 短于此字数、或以冒号结尾的句子，是"引导下一句"的残片，不足以单独成镜
_DANGLING_MIN = 4


def _spread_marks(sentences: list[str], marks: list[str]) -> list[str]:
    """把 N 条画面位铺到 M 个句子上。

    归属模型与正文段一致：**画面位管它前面的句子**，直到下一条画面位出现。
    条数不匹配时也不要崩：画面位少于句子 → 后面的句子沿用最后一条；
    多于句子 → 多出来的并进最后一句（不丢，免得画面位数对不上清单表）。
    """
    if not sentences:
        return []
    if not marks:
        return [""] * len(sentences)
    if len(marks) == 1:
        return [marks[0]] * len(sentences)
    out: list[str] = []
    # 平均分：第 i 条画面位负责第 round(i*M/N) 起的若干句
    n = len(marks)
    m = len(sentences)
    for idx in range(m):
        # 该句归属第几条画面位（0-based）
        k = min(n - 1, (idx * n) // m)
        out.append(marks[k])
    return out


def _is_dangling(shot: dict[str, Any]) -> bool:
    text = (shot.get("narration") or "").rstrip()
    return len(text) < _DANGLING_MIN or text.endswith(("：", ":"))


def _merge_dangling(shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把"他说："/"是口。"这类残片并入**后一句**（只在同一段内合并，保留文字顺序）。

    这些碎片单成一片只有 1 秒左右，Ken Burns 看不出运动，字幕也读不完整。
    """
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(shots):
        cur = shots[i]
        j = i + 1
        while (
            _is_dangling(cur)
            and j < len(shots)
            and shots[j]["segment"] == cur["segment"]
        ):
            cur["narration"] = cur["narration"] + shots[j]["narration"]
            if not cur.get("visual"):
                cur["visual"] = shots[j]["visual"]
                cur["kind"] = shots[j].get("kind")
            j += 1
        out.append(cur)
        i = j
    return out


def _renumber(shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for idx, shot in enumerate(shots, start=1):
        shot["id"] = idx
    return shots


def _new_shot(
    sid: int, segment: int, heading: str, narration: str, visual: str,
    kind: str | None = None, style_name: str = STYLE_NAME,
) -> dict[str, Any]:
    return {
        "id": sid,
        "segment": segment,
        "segment_heading": heading,
        "narration": narration,
        "visual": visual,
        "kind": kind,
        "scene": "",
        "source": None,
        "prompt": "",
        "keywords": [],
        "library_asset": None,
        "resolved_by": None,
        "style": style_name,
        "start": None,
        "end": None,
        "status": "pending",
        "generated_by": "heuristic",
    }


# ---- LLM 增厚（07 + 08） ---------------------------------------------------

_SYSTEM = (
    "你是历史纪录片的画面导演。给你一段画面的 beat 设计，以及该段落里的分镜旁白，"
    "为每一镜判断它对应哪个 beat、这个 beat 属于场景还是图表，并产出提示词。"
    "只输出 JSON，不要解释。"
)

_RULES = f"""规则：
1. beat：这一镜对应第几个 beat（从 0 开始的下标）。同一个 beat 可以覆盖多镜，
   但**相邻几镜要尽量摊到不同 beat**，不要整段共用一条画面。
2. kind：\"scene\"（拍得出来的画面）还是 \"graphic\"（屏幕上的文字/时间轴/对照/图表）。
   口诀：这句话的画面是**拍**出来的，还是**排**出来的？
3. kind=scene 时，scene 是可拍的场景描述（中文 ≤ 40 字）：
   **严禁**照抄引号里的字幕原文，**严禁**出现"高亮/砸屏/分栏/浮现/逐行/→"这类剪辑动作。
4. kind=scene 时，prompt 是英文生图提示词，只写景别/光线/材质/构图/情绪；
   **严禁**任何文字、招牌、字幕、人名、数字。末尾统一追加："{STYLE_SUFFIX}"
5. kind=scene 时，keywords 是英文检索词 1–3 个；source 写实场景 \"pexels\"，古籍/示意 \"local\"。
6. kind=graphic 时，scene/prompt/source 一律留空字符串，keywords 留空数组。

输出格式：JSON 数组，每项
{{"id": <整数>, "beat": <整数>, "kind": "scene|graphic", "scene": "...",
  "prompt": "...", "keywords": ["..."], "source": "pexels|local|"}}"""


def _rules(style: Style | None = None) -> str:
    """提示词规则：把默认风格后缀替换成本次选定的风格（票 44）。

    规则里那处后缀是嵌在长文本里的，用替换而非重排模板 —— 少一次"模板与代码漂移"。
    """
    suffix = (style or STYLES[STYLE_DEFAULT]).suffix
    return _RULES if suffix == STYLE_SUFFIX else _RULES.replace(STYLE_SUFFIX, suffix)


def _group_by_segment(shots: list[dict[str, Any]]) -> list[tuple[Any, list[dict[str, Any]]]]:
    groups: list[tuple[Any, list[dict[str, Any]]]] = []
    for shot in shots:
        if groups and groups[-1][0] == shot.get("segment"):
            groups[-1][1].append(shot)
        else:
            groups.append((shot.get("segment"), [shot]))
    return groups


def _apply_llm_item(
    shot: dict[str, Any],
    item: dict[str, Any] | None,
    beats: list[dict[str, Any]],
    mode: str,
    kinds: dict[str, str] | None = None,
    style: Style | None = None,
) -> None:
    """把 LLM 的一镜结果写回 shot；缺字段/下标越界一律回退到已有值。

    `kinds` 是**跨批次共享**的 `{beat 文本: kind}` 记忆（票 32 现象 A）。
    LLM 是**逐镜**判的，同一条 beat 铺在多个镜上时判定会漂（真机：同一段 beat 文本
    4 镜判成生图、1 镜判成卡片 —— 同一个屏幕元素一会儿照片、一会儿字卡）。
    按 beat 记忆首次判定，整组必然一致。为 None 时保持原行为（不记忆）。

    `style` 是本次选定的画面风格（票 44）；为 None 用默认（历史纪录片感）。
    """
    item = item or {}
    idx = item.get("beat")
    if not isinstance(idx, int) or isinstance(idx, bool) or not (0 <= idx < len(beats)):
        idx = None

    if idx is not None:
        beat = beats[idx] or {}
        shot["visual"] = beat.get("text") or shot["visual"]
        explicit = beat.get("kind")
    else:
        explicit = shot.get("kind")

    beat_text = str(shot.get("visual") or "")
    declared = str(item.get("kind") or "").strip().lower()
    remembered = (kinds or {}).get(beat_text)
    if remembered:
        kind = remembered
    elif mode != prompting.MODE_PHOTO and declared in (prompting.KIND_SCENE, prompting.KIND_GRAPHIC):
        kind = declared
    else:
        kind = prompting.resolve_kind(beat_text, explicit, mode)
    if kinds is not None and beat_text:
        kinds[beat_text] = kind
    shot["kind"] = kind
    shot["generated_by"] = "llm"

    if kind == prompting.KIND_GRAPHIC:
        shot["source"] = prompting.KIND_GRAPHIC
        shot["scene"] = ""
        shot["prompt"] = ""
        shot["keywords"] = []
        return

    scene = str(item.get("scene") or "").strip() or prompting.sanitize(shot.get("visual") or "")
    scene = scene or shot.get("segment_heading") or shot["narration"]
    shot["scene"] = scene

    suffix = (style or STYLES[STYLE_DEFAULT]).suffix
    prompt = str(item.get("prompt") or "").strip()
    if prompt and suffix.split(",")[0] not in prompt:
        prompt = f"{prompt.rstrip('.')}. {suffix}"
    shot["prompt"] = prompt or heuristic_prompt(scene, style)

    kws = item.get("keywords") or []
    if isinstance(kws, str):
        kws = [kws]
    kws = [str(k).strip() for k in kws if str(k).strip()][:3]
    shot["keywords"] = kws or heuristic_keywords(scene)

    src = str(item.get("source") or "").strip().lower()
    shot["source"] = src if src in {"pexels", "local"} else heuristic_source(scene)


def _llm_enrich(
    client: llm_mod.LLMClient,
    shots: list[dict[str, Any]],
    ws: Workspace,
    segment_beats: dict[Any, list[dict[str, Any]]],
    mode: str = prompting.MODE_GRAPHIC,
    batch: int = 20,
    style: Style | None = None,
) -> None:
    """分批调用 LLM，写回 beat 归位 / kind / scene / prompt / keywords / source。

    按**段**分批（不是按固定镜数跨界），一段的 beat 表因此只传一次；
    段内再按 `batch` 切块。坏 JSON 落盘并报错（票据 08）。
    """
    rules = _rules(style)
    # 跨批次共享的 {beat 文本: kind} 记忆（票 32 现象 A）——
    # 同一 beat 落在不同批次里也要复用同一个判定，所以它不能是按批清空的
    kinds: dict[str, str] = {}

    chunks: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
    for seg_id, group in _group_by_segment(shots):
        beats = segment_beats.get(seg_id) or []
        for i in range(0, len(group), batch):
            chunks.append((beats, group[i : i + batch]))

    for beats, chunk in track(chunks, "拆镜"):
        payload = {
            "beats": [b.get("text") for b in beats],
            "shots": [
                {"id": s["id"], "narration": s["narration"], "visual": s["visual"]}
                for s in chunk
            ],
        }
        messages = [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": rules + "\n\n分镜：\n" + json.dumps(payload, ensure_ascii=False),
            },
        ]
        try:
            data = llm_mod.chat_json(client, messages, retries=1, temperature=0.4)
        except llm_mod.LLMError as exc:
            raw = getattr(exc, "raw", None)
            path = llm_mod.dump_raw(ws, "shots", raw, str(exc))
            raise llm_mod.LLMError(
                f"{exc}\n  原始响应已落盘：{path}（批次 id {chunk[0]['id']}–{chunk[-1]['id']}）"
            ) from exc

        if isinstance(data, dict):
            data = data.get("shots") or data.get("items") or data.get("data") or []
        if not isinstance(data, list):
            path = llm_mod.dump_raw(ws, "shots", json.dumps(data, ensure_ascii=False), "结构不是数组")
            raise llm_mod.LLMError(f"LLM 返回不是数组；原始响应已落盘：{path}")

        by_id = {
            int(item["id"]): item
            for item in data
            if isinstance(item, dict) and isinstance(item.get("id"), int)
        }
        for shot in chunk:
            _apply_llm_item(shot, by_id.get(shot["id"]), beats, mode, kinds, style)


def _heuristic_fill(
    shots: list[dict[str, Any]], mode: str = prompting.MODE_GRAPHIC, style: Style | None = None
) -> None:
    """无 LLM 时的兜底：beat 分类 + 提示词卫生（票据 27）。

    `graphic` beat 不进生图：`source` 记为 `graphic`，`prompt` 留空，交给图表渲染器。
    `scene` beat 用 `prompting.sanitize()` 剥掉字幕原文与剪辑动词后再拼提示词 ——
    净化后为空（整条 beat 都是剪辑动作）时退回段标题，再退回旁白。
    """
    for shot in shots:
        if shot.get("generated_by") == "llm" and shot.get("kind"):
            continue  # 已经有 LLM 结果，不覆盖
        visual = shot.get("visual") or ""
        kind = prompting.resolve_kind(visual, shot.get("kind"), mode)
        shot["kind"] = kind
        if kind == prompting.KIND_GRAPHIC:
            shot["source"] = prompting.KIND_GRAPHIC
            shot["scene"] = ""
            shot["prompt"] = ""
            shot["keywords"] = []
            shot["generated_by"] = "heuristic"
            continue

        scene = prompting.sanitize(visual) or shot.get("segment_heading") or shot["narration"]
        shot["scene"] = scene
        shot["prompt"] = heuristic_prompt(scene, style)
        shot["keywords"] = shot.get("keywords") or heuristic_keywords(scene)
        if shot["source"] not in {"pexels", "local"}:
            shot["source"] = heuristic_source(scene)
        shot["generated_by"] = "heuristic"


# ---- 校验与合并（票据 08） -------------------------------------------------

# ★ `source` 的合法取值与"一份分镜表必须长什么样"，定义在 `lvs/handoff.py`
#   —— 产出方（本模块）与消费方（tts / assets / build）共用**同一份契约**。
#   本模块留 `VALID_SOURCES` 这个名字只为向后兼容（老测试/老脚本在用）。
from lvs import handoff  # noqa: E402

VALID_SOURCES = handoff.VALID_SOURCES


# ---- 提示词否定式巡检 -------------------------------------------------------
#
# 为什么必须有这一步：拍摄稿写 `[画面位]` 时是禁否定式的（`lvs check` 的 C021 守着），
# 但**提示词是 LLM 在拆镜阶段重写的**。它会把「檐下没有灯火」顺手译成
# `no lamps beneath`、「身边一个人也没有」译成 `no figures present` ——
# 而这两句在 cfg=1.0 的蒸馏模型上等于**命令它画几盏灯、画一个人**
# （「空镜长人」就是同一机制造的）。拍摄稿那边守得再严也管不到这里。
#
# 实测（2026-10-03，白峰 435 镜）：10 镜命中，含 `no lamps beneath` / `no figures present`
# / `no light anywhere` / `no living soul`。
_PROMPT_NEG = re.compile(
    r"(?<![A-Za-z])(?:no|not|without|never|none|nothing|nobody|nowhere)(?![A-Za-z])",
    re.IGNORECASE,
)

# 交给 LLM 的改写规则。要点：**不换主体**（只把否定句换成正面物理陈述），
# 且必须仍然描述「这一镜原本要的画面」，不许为了躲否定词换个场景。
_NEG_FIX_RULES = """你在修一批 AIGC 生图提示词里的**否定式**。

背景：目标模型是 cfg=1.0 的蒸馏模型，**没有负向引导** ——
提示词里出现的每个名词都会被尽力画出来。所以 `no lamps beneath` 会**画出几盏灯**，
`no figures present` 会**画出一个人**，`no light anywhere` 会**画出一处光**。

任务：逐条改成**正面的物理陈述**，主体不变（还是原来那个画面），只把"没有什么"换成"有什么"。
- `no lamps beneath` → `ice-cold unlit eaves` / `bare shadowed eaves`
- `no figures present` → `an utterly vacant courtyard`
- `no light anywhere` → `pitch-black pine forest`
- `no living soul` → `an abandoned empty sea`

硬约束：
1. 不许出现 no / not / without / never / none / nothing / nobody / nowhere 及任何否定词。
2. 不许引入新主体、不许换场景，画面意图与原来一致。
3. 保留原来的镜头术语（景别／光位／氛围）与风格描述，不要动句尾的风格后缀。
4. 每条都要给，不要漏。

只输出 JSON 数组，每项 `{"id": <镜号>, "prompt": "<改写后的完整提示词>"}`，不要解释。"""


def prompt_negations(shots: list[dict[str, Any]]) -> dict[int, list[str]]:
    """返回 `{镜号: [命中的否定词]}`。

    只查**英文**提示词：中文那一路是启发式兜底（LLM 漏项时），
    它的否定式来源仍是拍摄稿，由 `lvs check` 的 C021 守。
    """
    out: dict[int, list[str]] = {}
    for shot in shots:
        text = str(shot.get("prompt") or "").strip()
        if not text or re.match(r"^[\u4e00-\u9fff]", text):
            continue
        hits = sorted({m.group(0).lower() for m in _PROMPT_NEG.finditer(text)})
        if hits:
            out[int(shot.get("id") or 0)] = hits
    return out


def _llm_fix_negations(
    client: llm_mod.LLMClient,
    shots: list[dict[str, Any]],
    flagged: dict[int, list[str]],
    *,
    batch: int = 20,
) -> int:
    """把命中的提示词交回 LLM 改写成正面陈述，就地写回。返回成功改写的镜数。

    改写**只动 prompt**，不碰 narration/visual/source —— 画面意图是上游的产物，
    这里只负责把"怎么说"换成模型吃得下的说法。
    """
    by_id = {int(s.get("id") or 0): s for s in shots}
    ids = sorted(i for i in flagged if i in by_id)
    fixed = 0
    for i in range(0, len(ids), batch):
        chunk = ids[i : i + batch]
        payload = {
            "shots": [
                {"id": j, "prompt": by_id[j].get("prompt"), "visual": by_id[j].get("visual")}
                for j in chunk
            ]
        }
        messages = [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": _NEG_FIX_RULES + "\n\n待改写：\n" + json.dumps(payload, ensure_ascii=False),
            },
        ]
        data = llm_mod.chat_json(client, messages, retries=1, temperature=0.2)
        if isinstance(data, dict):
            data = data.get("shots") or data.get("items") or data.get("data") or []
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                sid = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            new_prompt = str(item.get("prompt") or "").strip()
            shot = by_id.get(sid)
            # ★ 只接受"确实变干净了"的结果。改写本身写错了否定词（或空返回）就保留原句，
            #   交给下面那轮复检报出来 —— 绝不能把没修好的东西当成修好了。
            if shot is None or not new_prompt or _PROMPT_NEG.search(new_prompt):
                continue
            if new_prompt == shot.get("prompt"):
                continue
            shot["prompt"] = new_prompt
            fixed += 1
    return fixed


def _validate(shots: list[dict[str, Any]]) -> list[str]:
    """结构校验：返回问题列表（空 = 通过）。判据见 `lvs/handoff.py`。"""
    return handoff.validate_shots(shots)


def _load_existing(ws: Workspace) -> tuple[dict[int, dict[str, Any]], str]:
    """上一版 `shots.json`：按镜号索引的分镜 + 它记着的来源策略。"""
    data = ws.try_load_shots()
    by_id = {int(s["id"]): s for s in data.get("shots", []) if isinstance(s, dict) and "id" in s}
    return by_id, sources.mode_of(data)


def _preserve_manual(
    shots: list[dict[str, Any]],
    existing: dict[int, dict[str, Any]],
    *,
    keep_source: bool = True,
) -> int:
    """保护人工编辑：`source` 与 `library_asset` 不被覆盖（除非 --force）。

    `keep_source=False` 用于显式指定了来源策略的时候 —— 策略是更强的意图，
    这时候"保留上次的 source"会直接把策略掀掉（票 41）。
    """
    fields = ("source", "library_asset") if keep_source else ("library_asset",)
    kept = 0
    for shot in shots:
        old = existing.get(shot["id"])
        if not old:
            continue
        for field in fields:
            if old.get(field) not in (None, "", []):
                if shot.get(field) != old[field]:
                    shot[field] = old[field]
                    kept += 1
    return kept


# ---- 命令入口 --------------------------------------------------------------


def _segment_beats(parse_data: dict[str, Any]) -> dict[Any, list[dict[str, Any]]]:
    """每段的 beat 表（按 flow 里 visual 项的先后展平）。"""
    out: dict[Any, list[dict[str, Any]]] = {}
    for seg in parse_data.get("segments", []):
        out[seg.get("index", 0)] = [
            beat
            for item in (seg.get("flow") or [])
            if item.get("kind") == "visual"
            for beat in (item.get("beats") or [])
        ]
    return out


def _warn_no_llm(shots: list[dict[str, Any]]) -> list[str]:
    """无 LLM 时的响亮警告：beat 归位只是平均分配，判不准的 beat 需要人工过一眼。"""
    uncertain = [
        s
        for s in shots
        if s.get("kind") != prompting.KIND_GRAPHIC
        and not prompting.has_markers(s.get("visual") or "")
    ]
    lines = [
        "⚠️ 未用 LLM：beat 归位是「按序平均分配」，不是语义切分 —— 相邻镜仍可能落到同一画面。"
    ]
    if uncertain:
        lines.append(
            f"⚠️ {len(uncertain)} 个分镜的 beat 没有任何分类标记词，已默认按「场景」处理，建议人工过一眼："
        )
        lines += [f"     shot {int(s['id']):03d}｜{(s.get('visual') or '')[:40]}" for s in uncertain[:8]]
        if len(uncertain) > 8:
            lines.append(f"     …另有 {len(uncertain) - 8} 个")
    return lines


# ---- 只读读口（省 token 用）-------------------------------------------------
#
# 病根：`shots.json` 516 KB / 8 千行；agent 想知道"第 193 镜到底写了什么"，
# 过去只能把整份读进上下文（≈ 20 万 token）。AGENTS.md 曾经只能写"禁止读" ——
# 而"禁止"本身就是缺陷：人总要有一个读口。这两个函数就是那个读口：
# 一个打**一镜**（peek），一个打**全表**（index，一镜一行）。


def _duration_text(s: dict[str, Any]) -> str:
    """这一镜有多长：优先用配音实测时长，其次 start/end 之差。"""
    dur = s.get("audio_duration")
    if isinstance(dur, (int, float)) and dur > 0:
        return f"{float(dur):.1f}s"
    start, end = s.get("start"), s.get("end")
    if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end >= start:
        return f"{float(end) - float(start):.1f}s"
    return "?s"


def _shot_block(s: dict[str, Any], *, full: bool = False) -> str:
    parts = [
        f"#{s.get('id')}  {_duration_text(s)}  "
        f"kind={s.get('kind') or '-'}  source={s.get('source') or '-'}  "
        f"status={s.get('status') or '-'}"
    ]
    for label, key in (("场景", "scene"), ("提示词", "prompt"), ("旁白", "narration")):
        value = str(s.get(key) or "").strip()
        if not value:
            continue
        if key == "prompt" and not full and len(value) > PROMPT_PREVIEW:
            value = f"{value[:PROMPT_PREVIEW]}…（看全文：`lvs shots --peek {s.get('id')} --full`）"
        parts.append(f"{label}：{value}")
    keywords = [str(k) for k in (s.get("keywords") or [])]
    if keywords:
        parts.append("检索词：" + ", ".join(keywords))
    return "\n".join(parts)


#: `--peek` 默认把提示词截到这么长：这个项目的提示词带风格后缀，单条就有 600+ 字符。
#: 该不该改提示词，看开头就够了；真要逐字看全文，加 `--full`。
PROMPT_PREVIEW = 140


def peek_text(ws: Workspace, shot_id: int, *, full: bool = False) -> str:
    """单个分镜的摘要（约 300 token）。镜号不存在时给一句提示，**不抛异常**。

    默认把提示词截到 `PROMPT_PREVIEW` 字（并附一句怎么看全文）——
    实测 #193 全量输出 788 字符，截断后约 310，而两者对“这一镜对不对”的判断力差别很小。
    """
    shots = ws.try_load_shots().get("shots") or []
    for s in shots:
        if int(s.get("id", -1)) == shot_id:
            return _shot_block(s, full=full)
    return f"没有镜 #{shot_id}（本任务共 {len(shots)} 镜；`lvs shots --index` 看全表）"


def index_text(ws: Workspace, *, width: int = 40, limit: int | None = None) -> str:
    """一镜一行（516 KB → 约 30 KB）。

    只打"挑镜要看的列"：镜号 / 时长 / kind / source / 场景（截断）。
    提示词与旁白**不在这里** —— 那两样用 `peek_text` 单取一镜，别整份读。
    """
    shots = ws.try_load_shots().get("shots") or []
    if not shots:
        return "（没有 shots.json）先跑：lvs shots --task <任务名>"
    rows: list[str] = []
    for s in shots:
        scene = " ".join(str(s.get("scene") or s.get("visual") or "").split())
        if width > 0 and len(scene) > width:
            scene = scene[: width - 1] + "…"
        rows.append(
            f"{int(s.get('id', 0)):>4} {_duration_text(s):>7} "
            f"{(s.get('kind') or '-'):<7} {(s.get('source') or '-'):<8} {scene}"
        )
    if limit is not None:
        rows = rows[: int(limit)]
    return "\n".join(rows)


def run_command(config: Config, ws: Workspace, args) -> int:  # noqa: ANN001
    # 只读读口（S2）：`--peek N` / `--index` 不跑拆镜、不读 parse.json、不碰门禁，
    # 只是把 516 KB 的 shots.json 摘成几行。
    peek = getattr(args, "peek", None)
    if peek is not None:
        print(peek_text(ws, int(peek), full=bool(getattr(args, "full", False))))
        return 0
    if getattr(args, "index", False):
        print(index_text(ws, width=int(getattr(args, "index_width", 40) or 40),
                         limit=getattr(args, "index_limit", None)))
        return 0

    parse_path = ws.path("parse.json")
    if not parse_path.is_file():
        print(f"未找到 {parse_path}。请先运行：lvs parse <拍摄稿.md> --task {ws.task}")
        return 2

    force = bool(getattr(args, "force", False))
    if not force and ws.is_stage_done("shots", [ws.path("shots.json")]):
        print(f"shots 已完成（{ws.path('shots.json')}）；加 --force 可重做。")
        return 0

    visual_mode = str(
        getattr(args, "visual", None) or config.get("shots.visual_mode", prompting.MODE_GRAPHIC)
    ).strip().lower()
    if visual_mode not in (prompting.MODE_GRAPHIC, prompting.MODE_PHOTO):
        print(f"画面模式只支持 {prompting.MODE_GRAPHIC}（图文图表感）/ {prompting.MODE_PHOTO}（实拍剧照感），收到 {visual_mode!r}")
        return 2

    # 素材来源策略（票 41）：`--source` > 上次跑记下的 > 配置默认（sources.resolve 里只有一份实现）
    try:
        source_mode = sources.resolve(
            getattr(args, "source", None),
            sources.mode_of(ws.try_load_shots()),
            config.get("shots.source_mode"),
        )
    except sources.SourceModeError as exc:
        print(str(exc))
        return 2

    # 画面风格（票 44）：`--style-file` > 项目风格文件 > config 内联 > 内置。
    # 未知名字报错而非静默退回默认 —— 风格静默跑偏要等全片出完才发现。
    try:
        reg = registry_for(config, style_file=getattr(args, "style_file", None))
        style = reg.get(getattr(args, "style", None) or config.get("shots.style"))
    except StyleError as exc:
        print(str(exc))
        return 2
    if reg.notes:
        for n in reg.notes:
            print(f"[风格] {n}")
    print(f"画面风格：{style.name}（{style.desc or '内置'}）")

    parse_data = json.loads(parse_path.read_text(encoding="utf-8"))
    shots = build_skeleton(parse_data, style_name=style.name)
    if not shots:
        print("解析结果里没有可用的旁白；请检查拍摄稿结构（`lvs parse` 的 notes）。")
        return 2

    notes: list[str] = []
    segment_beats = _segment_beats(parse_data)
    use_llm = not getattr(args, "no_llm", False) and llm_mod.available(config)
    if use_llm:
        # P2 路由：拆镜（beat 归位 / 逐镜提示词）是**敏感环节**，固定走远程
        client = llm_mod.LLMClient.from_config(config, site=llm_provider.SITE_SHOTS_BEATS)
        # P1：接上 LLM 缓存 + 成本记账（`--no-cache` / LVS_LLM_CACHE=0 可关）
        llm_mod.configure(ws=ws, stage="shots", config=config)
        print(f"用 LLM 做 beat 归位 / 分类 / 场景翻译 / 提示词（模型 {client.model}，画面模式 {visual_mode}）…")
        try:
            _llm_enrich(client, shots, ws, segment_beats, mode=visual_mode, style=style)
        except llm_mod.LLMError as exc:
            print(f"LLM 拆镜失败：{exc}")
            return 2
        _heuristic_fill(shots, mode=visual_mode, style=style)  # 补齐 LLM 漏掉的字段
        notes.append(
            f"beat 归位/kind/scene/prompt/keywords/source 由 LLM（{client.model}）生成；"
            f"画面模式 {visual_mode}，画面风格 {style.name}。"
        )
    else:
        why = "已指定 --no-llm" if getattr(args, "no_llm", False) else "未配置 app.openai_api_key"
        print(f"未使用 LLM（{why}），改用启发式拆镜 —— 提示词/检索词质量会下降。")
        _heuristic_fill(shots, mode=visual_mode, style=style)
        warnings = _warn_no_llm(shots)
        for line in warnings:
            print(line)
        notes.append(
            f"未使用 LLM（{why}）：beat 归位为「按序平均分配」、kind 为关键词规则判定，"
            f"prompt/keywords/source 为启发式结果；画面模式 {visual_mode}，画面风格 {style.name}。"
            "建议填 key 后 --force 重跑。"
        )

    # 保护人工编辑（显式给了来源策略时，策略胜过"上次的 source"）
    existing, _recorded_mode = _load_existing(ws)
    if existing and not force:
        kept = _preserve_manual(shots, existing, keep_source=source_mode == sources.MODE_AUTO)
        if kept:
            print(f"保留了 {kept} 处人工编辑（source / library_asset）。")
            notes.append(f"重跑时保留了 {kept} 处人工编辑的 source/library_asset（--force 可覆盖）。")

    # 素材来源策略：把「意向」落成逐镜的 source（票 41）。
    # 放在 _preserve_manual 之后 —— 显式策略要能压过"上次的 source"。
    moved = sources.apply_mode({"shots": shots}, source_mode)
    if source_mode != sources.MODE_AUTO:
        extra = f"，改了 {moved} 个实拍镜的来源" if moved else ""
        print(f"素材来源策略：{sources.LABELS[source_mode]}{extra}")
        notes.append(
            f"实拍镜来源由策略固定为 {source_mode}（{sources.LABELS[source_mode]}）；"
            "图文/图表 beat 与手工钉死的镜不受影响。"
        )

    problems = _validate(shots)
    if problems:
        print("shots 结构校验失败：")
        for p in problems[:20]:
            print(f"  - {p}")
        return 2

    by_source: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for s in shots:
        by_source[s["source"]] = by_source.get(s["source"], 0) + 1
        by_kind[s["kind"]] = by_kind.get(s["kind"], 0) + 1

    # ---- 提示词否定式巡检（cfg=1.0 模型没有负向引导）----
    # 拍摄稿那边由 `lvs check` 的 C021 守着，但提示词是 LLM 重写的，得在这里再守一道。
    flagged = prompt_negations(shots)
    if flagged:
        print(f"提示词否定式巡检：{len(flagged)} 镜命中"
              f"（cfg=1.0 无负向引导，否定词里的名词会被**画出来**）")
        if use_llm and not getattr(args, "no_negation_repair", False):
            print("  交回 LLM 改写成正面物理陈述…")
            try:
                fixed = _llm_fix_negations(client, shots, flagged)
                print(f"  已改写 {fixed} 镜")
            except llm_mod.LLMError as exc:
                print(f"  改写调用失败：{exc}")
            flagged = prompt_negations(shots)
            if flagged:
                print(f"  ⚠ 改写后仍有 {len(flagged)} 镜含否定词")
        elif not use_llm:
            print("  未用 LLM，跳过自动改写（人工改 shots.json 的 prompt 后重跑 assets）")
        else:
            print("  已指定 --no-negation-repair，跳过自动改写")
        if flagged:
            sample = "、".join(f"#{i}:{'/'.join(w)}" for i, w in sorted(flagged.items())[:6])
            print(f"  逐镜清单见 shots.json 的 `negation_flags`（前几处：{sample}）")
            notes.append(
                f"⚠ {len(flagged)} 镜的提示词仍含否定式（cfg=1.0 下会被按字面画出来）："
                f"{sample}。已记入 shots.json 的 `negation_flags`。"
            )

    out = ws.write_shots({
        "source": parse_data.get("source"),
        "title": parse_data.get("title"),
        "style": style.name,
        "visual_mode": visual_mode,
        "source_mode": source_mode,
        "count": len(shots),
        "by_source": by_source,
        "by_kind": by_kind,
        "negation_flags": flagged,
        "notes": notes,
        "shots": shots,
    })
    ws.mark_stage(
        "shots", outputs=[out], count=len(shots),
        by_source=by_source, by_kind=by_kind, visual_mode=visual_mode, source_mode=source_mode,
        style=style.name,
    )

    print(f"拆镜完成：{len(shots)} 个分镜（{visual_mode} 模式，风格 {style.name}）")
    print("  画面类型：" + "，".join(f"{k} {v}" for k, v in sorted(by_kind.items())))
    print("  素材来源：" + "，".join(f"{k} {v}" for k, v in sorted(by_source.items())))
    print(f"  产物：{out}")
    for note in notes:
        print(f"  [note] {note}")
    return 0
