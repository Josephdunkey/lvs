"""拍摄稿解析（`lvs parse`）—— 票据 04 + 05。

输入：一篇结构化拍摄稿 Markdown（如
`D:\\素材库\\知识视频素材库\\05-拍摄稿\\K005-*.md`）。
输出：`.work/<task>/parse.json`，含

- `meta`              标题 / 封面文案（元数据，**不进旁白**）
- `cold_open`         冷开场（0:00–0:30，**是旁白**，见下）
- `segments[]`        正文各段：时间码 + 旁白行 + 画面位 + excluded
- `visual_mark_table` §三 画面位清单表的结构化记录
- `notes[]`           降级原因与需要人复核的不确定项

设计取舍（重要）：
- `spec §5` 把 `一、传达层` 整章列为 excluded，但该章中的 **`【冷开场】` 是片头旁白**。
  整章排除会丢掉 0:00–0:30。因此这里把 `【冷开场】` 单独抽为 `cold_open`（作为旁白），
  仅 `【标题】`/`【封面文案】` 归入 `meta`。此偏离记入 `notes`。
- 正文中独立成行的 `[...]` 标记一律视为 **excluded**；`[重参与点…]` 会连带其后直到空行的块。
- 行内提示 `（转场）`/`（停顿）` 只**剥离标记**、保留其余文字（它们是提示词，不是整段）。
- 一切缺失都降级并在 `notes` 记原因，**不抛异常**。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from lvs.prompting import split_beats

# ---- 正则 ------------------------------------------------------------------

_RE_H1 = re.compile(r"^#\s+(.+)$")
_RE_H2 = re.compile(r"^##\s+(.+)$")
_RE_H3 = re.compile(r"^###\s*(.+)$")
_RE_SEG_HEAD = re.compile(r"^###\s*【(?P<title>[^】]*)】\s*(?P<rest>.*)$")
_RE_RANGE_SEP = re.compile(r"\s*(?:[–—~～]|-{1,2}|至)\s*")
_RE_TIMECODE = re.compile(r"^(\d{1,3}):([0-5]?\d)(?::([0-5]?\d))?$")
_RE_VISUAL_MARK = re.compile(r"^\[画面位\]\s*(.*)$")
_RE_REENGAGE = re.compile(r"^\[重参与点[^\]]*\]\s*(.*)$")
_RE_BRACKET = re.compile(r"^\[(?P<name>[^\]]+)\]\s*$")
_RE_META_TAG = re.compile(r"^\*\*【(?P<name>[^】]+)】\*\*")
_RE_VISUAL_LIKE = re.compile(r"^(?:第[一二三四五六七八九十百零\d]+个)?画面\s*[:：]")
# 讲稿元标记：给审计/人读的脚手架，不是给观众听的。半角形一并防
# （用户拍板 2026-10-04：TTS 不许把「原文」两个字念出来）。
_RE_STAGE_MARKER = re.compile(r"〔(?:原文|注|附)〕|\[(?:原文|注|附)\]")
_RE_CUE = re.compile(r"（(?:转场|停顿|停|留白|重音|渐出|渐入)）")

_END_WORDS = {"结尾", "结束", "末", "片尾", "END", "end"}

# 章节名 → 角色
def _classify_section(title: str) -> str:
    if "传达层" in title:
        return "meta"
    if "正文" in title or "讲稿" in title:
        return "body"
    if "画面位清单" in title or "画面清单" in title:
        return "table"
    if "留存" in title or "自检" in title:
        return "excluded"
    if "史实" in title or "红线" in title or "核验" in title:
        return "excluded"
    return "other"


# ---- 时间码 ----------------------------------------------------------------


def parse_timecode(text: str) -> float | None:
    """`0:30` → 30.0；`1:02:03` → 3723.0；`结尾` → None；无法解析 → None。"""
    text = (text or "").strip()
    if not text or text in _END_WORDS:
        return None
    m = _RE_TIMECODE.match(text)
    if not m:
        return None
    hours = 0
    minutes = int(m.group(1))
    seconds = int(m.group(2))
    if m.group(3) is not None:
        hours, minutes, seconds = minutes, seconds, int(m.group(3))
    return float(hours * 3600 + minutes * 60 + seconds)


def parse_range(text: str) -> tuple[float | None, float | None, str]:
    """`0:30–1:00` → (30.0, 60.0)；`16:00–结尾` → (960.0, None)。"""
    text = (text or "").strip()
    parts = _RE_RANGE_SEP.split(text, maxsplit=1)
    if len(parts) == 2:
        return parse_timecode(parts[0]), parse_timecode(parts[1]), text
    return parse_timecode(text), None, text


# ---- 文本清洗 --------------------------------------------------------------


def clean_line(line: str) -> str:
    """去掉行内提示与 Markdown 记号，保留文字本身（不改写措辞）。"""
    text = line.strip()
    text = _RE_CUE.sub("", text)
    text = text.replace("**", "")
    text = re.sub(r"`([^`]*)`", r"\1", text)  # 行内代码/引文
    if text.startswith("`") and text.endswith("`") and len(text) >= 2:
        text = text[1:-1]
    return text.strip()


def strip_stage_markers(text: str) -> str:
    """剥掉讲稿元标记（〔原文〕〔注〕〔附〕及半角形），只留要念的字。

    **单一剥离点**：`parse` 生成 narration 的那一行调用它。配音合成、字幕文本、
    字幕时间轴（`tts.build_srt` / `char_time_curve`）全部只读 narration，
    所以在这一处剥掉，音频与字幕天然同源、不会错位。
    """
    return _RE_STAGE_MARKER.sub("", text)


def _is_blank(line: str) -> bool:
    return not line.strip()


def beats_of(visual: str) -> list[dict[str, Any]]:
    """把一行画面位拆成 beat（票据 27）。

    画面位写的是**剪辑设计**，一行常含多个 beat（`A → B → C`）。拆开才能给每镜
    分配到真正属于自己的画面，而不是整段几十镜共用一条设计。
    """
    return [{"text": b.text, "kind": b.kind} for b in split_beats(visual)]


# ---- 主解析 ----------------------------------------------------------------


def parse_script(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    notes: list[str] = []

    # 1) 标题
    title = ""
    for line in lines:
        m = _RE_H1.match(line)
        if m:
            title = m.group(1).strip()
            break

    # 2) 顶级分节（## ）
    headings = [
        (i, m.group(1).strip())
        for i, line in enumerate(lines)
        if (m := _RE_H2.match(line))
    ]
    sections: list[dict[str, Any]] = []
    for idx, (line_no, name) in enumerate(headings):
        end = headings[idx + 1][0] if idx + 1 < len(headings) else len(lines)
        sections.append(
            {"name": name, "role": _classify_section(name), "start": line_no + 1, "end": end}
        )

    if not headings:
        notes.append("未找到任何 `## ` 分节标题；按单段处理整个文档。")

    meta: dict[str, Any] = {}
    cold_open: dict[str, Any] | None = None
    segments: list[dict[str, Any]] = []
    visual_mark_table: list[dict[str, Any]] = []
    uncertain: list[dict[str, Any]] = []

    for sec in sections:
        block = lines[sec["start"]: sec["end"]]
        base_line = sec["start"] + 1  # 1-based 行号
        if sec["role"] == "meta":
            m, co, unc = _parse_meta_section(block, base_line, notes)
            meta.update(m)
            if co:
                cold_open = co
            uncertain.extend(unc)
        elif sec["role"] == "body":
            segs, marks_table, unc, sub_notes = _parse_body_section(block, base_line)
            segments.extend(segs)
            uncertain.extend(unc)
            notes.extend(sub_notes)
        elif sec["role"] == "table":
            visual_mark_table = _parse_mark_table(block, base_line, notes)
        elif sec["role"] == "excluded":
            # 整章排除（留存自检 / 史实核验）
            pass

    if not any(s["role"] == "body" for s in sections):
        notes.append("未找到 `## 二、正文讲稿` 分节；试图把全文当作正文（降级）。")
        segs, marks_table, unc, sub_notes = _parse_body_section(
            lines, 1, drop_headings=True
        )
        segments = segs or segments
        visual_mark_table = visual_mark_table or marks_table
        uncertain.extend(unc)
        notes.extend(sub_notes)

    if not visual_mark_table:
        notes.append("未找到 `## 三、画面位清单` 表；`visual_mark_table` 为空（降级）。")

    return {
        "title": title,
        "meta": meta,
        "cold_open": cold_open,
        "segments": segments,
        "visual_mark_table": visual_mark_table,
        "uncertain": uncertain,
        "notes": notes,
    }


_MARK_KIND = re.compile(r"\[(?:画面位|场景|图表)\]\s*")


def _clean_mark(line: str) -> str:
    """`[画面位] [场景] A → [场景] B` → `A → B`。

    与正文那条路径**同源**（都用 `clean_line`，`[场景]` 由 `split_beats` 逐 beat 剥）：
    这里洗不干净，`[画面位] [场景]` 会被原样带进生图提示词 —— 而模型是**照字面画**的。
    保留 `→`：它是 beat 分隔符，`beats_of()` 还要用它拆。
    """
    return clean_line(_MARK_KIND.sub("", (line or "").strip()))


def _parse_meta_section(
    block: list[str], base_line: int, notes: list[str]
) -> tuple[dict[str, Any], dict[str, Any] | None, list[dict[str, Any]]]:
    """解析 `一、传达层`：抽【标题】【封面文案】为 meta，【冷开场】为 cold_open。

    ★ 冷开场块里的 `[画面位]` 也要收（见下 `cold_open["visual_marks"]`）：
    片头 0–30 秒是全片最贵的位置，而它的旁白只住在这一块里 ——
    收不到画面位，这十几镜的提示词就只能退化成 `meta.title_card`（标题文案），
    等于把"标题"当画面画出来。
    """
    meta: dict[str, Any] = {}
    cold_open: dict[str, Any] | None = None
    uncertain: list[dict[str, Any]] = []

    current: str | None = None
    bucket: list[str] = []
    visual_bucket: list[str] = []  # 本块里的 `[画面位]` 行（与旁白分开收）
    header_raw: list[str] = []  # 标签行原文（含 `（时间码…）` 说明），供提取时间码

    def flush() -> None:
        nonlocal current, bucket, cold_open, header_raw, visual_bucket
        if current is None:
            return
        name, lines_ = current, list(bucket)
        marks = list(visual_bucket)
        scan = list(header_raw)
        current, bucket, header_raw, visual_bucket = None, [], [], []
        texts = [clean_line(x) for x in lines_ if not _is_blank(x)]
        texts = [t for t in texts if t]
        if "冷开场" in name:
            # 时间码落在标签行里，如「（0:00–0:30，直接从最戏剧的一刻切入）」
            start, end = 0.0, None
            for raw in scan + lines_:
                m = re.search(r"(\d{1,2}:\d{2})\s*[–—-]\s*(\d{1,2}:\d{2})", raw)
                if m:
                    start = parse_timecode(m.group(1)) or 0.0
                    end = parse_timecode(m.group(2))
                    break
            cold_open = {
                "start": start, "end": end, "text": texts,
                "source": "一、传达层/【冷开场】",
                # ★ 拆成 beat 再存：与正文段**同一套**处理。
                #   直接存整行会把 `[场景] A → [场景] B` 原样带进提示词
                #   （`[场景]` 本来就是靠 `split_beats` 逐 beat 剥掉的）。
                "visual_marks": [_clean_mark(m) for m in marks],
                "beats": [b for m in marks for b in beats_of(_clean_mark(m))],
            }
        elif "标题" in name:
            meta["title_card"] = texts[0] if texts else ""
        elif "封面" in name:
            meta["cover"] = texts
        else:
            uncertain.append({"kind": "meta_block", "name": name, "lines": texts, "line": base_line})

    for offset, raw in enumerate(block):
        stripped = raw.strip()
        m = _RE_META_TAG.match(stripped)
        if m:
            flush()
            current = m.group("name").strip()
            bucket = []
            header_raw = [stripped]
            # 标签后同一行可能还有内容；先剥掉 `（…）` 说明（那是写作提示，非内容）
            rest = stripped[m.end():].strip()
            rest = re.sub(r"^（[^）]*）\s*", "", rest)
            if rest:
                bucket.append(rest)
            continue
        if current is not None and stripped.startswith(">"):
            inner = stripped.lstrip(">").strip()
            # `> [画面位] …` 也算画面位（作者可能照着正文的写法给引文块加前缀）
            if inner.startswith("[画面位]") or inner.startswith("[画面位："):
                visual_bucket.append(_clean_mark(inner))
            else:
                bucket.append(inner)
            continue
        if current is not None and stripped.startswith("[画面位]"):
            # ★ 裸 `[画面位]` 行：早先会走到下面的 `flush()`，把冷开场**拦腰截断** ——
            # 于是「前半段旁白进 cold_open、后半段落进 uncertain」，而且不报错。
            # 现在明确收进本块的画面位，不再当作块边界。
            visual_bucket.append(_clean_mark(stripped))
            continue
        if current is not None and _is_blank(raw):
            continue
        if current is not None:
            # 非引用行：如 `备选：...`
            flush()

    flush()

    if cold_open is None:
        notes.append("`一、传达层` 中未找到 `【冷开场】`；片头将缺失（降级）。")
    else:
        notes.append(
            "已把 `一、传达层/【冷开场】` 抽为 cold_open 作为旁白"
            "（spec §5 原先把整章列为 excluded，会丢掉片头）。"
        )
    return meta, cold_open, uncertain


def _parse_body_section(
    block: list[str], base_line: int, drop_headings: bool = False
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    segments: list[dict[str, Any]] = []
    uncertain: list[dict[str, Any]] = []
    notes: list[str] = []

    current: dict[str, Any] | None = None
    pending_bracket: str | None = None  # 独立 [...] 标记名，收集其后的块
    pending_bracket_line: int = base_line

    def ensure_segment() -> dict[str, Any]:
        nonlocal current
        if current is None:
            current = {
                "index": len(segments) + 1,
                "heading": "(未命名段)",
                "start": None,
                "end": None,
                "timecode_raw": "",
                "narration": [],
                "visual_marks": [],
                "flow": [],
                "excluded": [],
            }
        return current

    def close_segment() -> None:
        nonlocal current
        if current is not None:
            segments.append(current)
            current = None

    def flush_bracket(consumed: list[str]) -> None:
        nonlocal pending_bracket
        if pending_bracket is None:
            return
        kind = "cold_close_lines" if "冷断句" in pending_bracket else "bracket_block"
        uncertain.append(
            {
                "kind": kind,
                "marker": pending_bracket,
                "lines": [clean_line(x) for x in consumed if x.strip()],
                "line": pending_bracket_line,
            }
        )
        pending_bracket = None

    i = 0
    bracket_buf: list[str] = []
    while i < len(block):
        raw = block[i]
        line_no = base_line + i
        stripped = raw.strip()

        # 段标题
        m_head = _RE_SEG_HEAD.match(stripped)
        if m_head:
            close_segment()
            if pending_bracket is not None:
                flush_bracket(bracket_buf)
                bracket_buf = []
            start, end, raw_range = parse_range(m_head.group("rest"))
            current = {
                "index": len(segments) + 1,
                "heading": m_head.group("title").strip(),
                "start": start,
                "end": end,
                "timecode_raw": raw_range,
                "narration": [],
                "visual_marks": [],
                "flow": [],
                "excluded": [],
            }
            if start is None:
                notes.append(f"第 {len(segments) + 1} 段标题未含可解析时间码：{stripped!r}（降级）")
            i += 1
            continue

        if _RE_H3.match(stripped) or _RE_H2.match(stripped):
            close_segment()
            i += 1
            continue

        if not stripped or stripped == "---":
            if pending_bracket is not None:
                flush_bracket(bracket_buf)
                bracket_buf = []
            i += 1
            continue

        # 独立 [...] 标记：结束上一块的收集
        m_bracket = _RE_BRACKET.match(stripped)
        if m_bracket:
            if pending_bracket is not None:
                flush_bracket(bracket_buf)
                bracket_buf = []
            name = m_bracket.group("name").strip()
            if name.startswith("画面位"):
                # 独立 `[画面位]`（无内容）：仅作标记，不产生画面位
                i += 1
                continue
            if name.startswith("重参与点"):
                # 连带其后直到空行的整块，全部属 excluded，绝不进旁白
                seg = ensure_segment()
                j = i + 1
                body_lines: list[str] = []
                while j < len(block) and block[j].strip() and not _RE_BRACKET.match(block[j].strip()):
                    body_lines.append(block[j].strip())
                    j += 1
                seg["excluded"].append(
                    {
                        "kind": "reengagement",
                        "text": " ".join(clean_line(x) for x in body_lines),
                        "line": line_no,
                    }
                )
                i = j
                continue
            pending_bracket = name
            pending_bracket_line = line_no
            bracket_buf = []
            i += 1
            continue

        # `[画面位] 描述`（带内容，非独立行）
        m_mark = _RE_VISUAL_MARK.match(stripped)
        if m_mark:
            seg = ensure_segment()
            visual = clean_line(m_mark.group(1))
            seg["visual_marks"].append(visual)
            # `flow` 保留旁白/画面位的**相对先后**，供拆镜时把每句映射到"最近的画面位"；
            # `beats` 是这一行画面位拆出来的画面单元（票据 27），供拆镜按 beat 分配。
            seg["flow"].append({"kind": "visual", "text": visual, "beats": beats_of(visual)})
            i += 1
            continue

        m_re = _RE_REENGAGE.match(stripped)
        if m_re:
            seg = ensure_segment()
            # 收集后续直到空行
            j = i + 1
            body_lines = [m_re.group(1)] if m_re.group(1) else []
            while j < len(block) and block[j].strip() and not _RE_BRACKET.match(block[j].strip()):
                body_lines.append(block[j].strip())
                j += 1
            seg["excluded"].append(
                {"kind": "reengagement", "text": " ".join(clean_line(x) for x in body_lines), "line": line_no}
            )
            i = j
            continue

        if stripped.startswith(">"):
            # ★ 引文块是**块语义**（2026-10-04 实锤 001/002 共 36 处重复念）：
            #   `> 〔引原文〕` 只是标记行，正文行（无 `>` 前缀）若按单行处理会落进
            #   narration → 同一句在行内〔原文〕与块正文各念一遍。
            #   现在连带收集其后直到空行/结构边界（段标题、[标记]）的整块，全部 excluded。
            seg = ensure_segment()
            body_lines = [stripped.lstrip(">").strip()]
            j = i + 1
            while j < len(block):
                nxt_raw = block[j]
                nxt = nxt_raw.strip()
                if (
                    not nxt
                    or nxt == "---"
                    or _RE_BRACKET.match(nxt)
                    or _RE_VISUAL_MARK.match(nxt)
                    or _RE_SEG_HEAD.match(nxt)
                    or _RE_H2.match(nxt)
                    or _RE_H3.match(nxt)
                ):
                    break
                body_lines.append(nxt.lstrip(">").strip())
                j += 1
            seg["excluded"].append(
                {"kind": "blockquote", "text": clean_line(" ".join(x for x in body_lines if x)), "line": line_no}
            )
            i = j
            continue

        # 仍在收集某个 [...] 块
        if pending_bracket is not None:
            if _is_blank(raw):
                flush_bracket(bracket_buf)
                bracket_buf = []
            else:
                bracket_buf.append(stripped)
            i += 1
            continue

        # 疑似画面描述行（如 `第一个画面：一份新到账的户籍…`）：不进旁白，记入 uncertain 供复核
        if _RE_VISUAL_LIKE.match(stripped):
            uncertain.append(
                {"kind": "visual_like", "marker": "", "lines": [clean_line(stripped)], "line": line_no}
            )
            i += 1
            continue

        # 其余 → 旁白（元标记不进旁白：不念、不上字幕）
        text = strip_stage_markers(clean_line(stripped))
        if text:
            seg = ensure_segment()
            seg["narration"].append(text)
            seg["flow"].append({"kind": "narration", "text": text})
        i += 1

    if pending_bracket is not None:
        flush_bracket(bracket_buf)
    close_segment()

    return segments, [], uncertain, notes


def _parse_mark_table(
    block: list[str], base_line: int, notes: list[str]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    header: list[str] | None = None
    for i, raw in enumerate(block):
        stripped = raw.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if header is None:
            header = cells
            continue
        if all(set(c) <= set("-: ") for c in cells):  # 分隔行
            continue
        if len(cells) < 3:
            notes.append(f"画面位清单第 {base_line + i} 行不足 3 列，已跳过：{stripped!r}")
            continue
        time_raw = cells[0].replace("**", "").strip()
        rows.append(
            {
                "time_raw": time_raw,
                "time_seconds": parse_timecode(time_raw),
                "visual": clean_line(cells[1]),
                "suggestion": clean_line(cells[2]),
            }
        )
    return rows


# ---- 命令入口 --------------------------------------------------------------


def run_command(config, ws, args) -> int:  # noqa: ANN001 - 由 cli 传入
    manuscript = getattr(args, "manuscript", None)
    if not manuscript:
        print("用法：lvs parse <拍摄稿.md> [--task NAME]")
        return 2

    src = Path(manuscript)
    if not src.is_file():
        print(f"拍摄稿不存在：{src}")
        return 2

    text = src.read_text(encoding="utf-8")
    result = parse_script(text)

    # 输入副本落盘（spec §6）
    (ws.path("source.md")).write_text(text, encoding="utf-8")
    out = ws.path("parse.json")
    out.write_text(
        json.dumps(
            {"source": str(src.resolve()), **result},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    ws.mark_stage("parse", outputs=[out])

    n_seg = len(result["segments"])
    n_narr = sum(len(s["narration"]) for s in result["segments"]) + (
        len(result["cold_open"]["text"]) if result["cold_open"] else 0
    )
    n_marks = sum(len(s["visual_marks"]) for s in result["segments"])
    print(f"解析完成：{src.name}")
    print(f"  cold_open：{'有' if result['cold_open'] else '无'}"
          f"（{len(result['cold_open']['text']) if result['cold_open'] else 0} 行）")
    print(f"  正文段数：{n_seg}；旁白行：{n_narr}；段内画面位：{n_marks}")
    print(f"  画面位清单：{len(result['visual_mark_table'])} 条")
    print(f"  产物：{out}")
    for note in result["notes"]:
        print(f"  [note] {note}")
    return 0
