"""拍摄稿解析（`lvs parse`）—— 票据 04 + 05。

输入：一篇结构化拍摄稿 Markdown（如
`D:\\fanshu\\资治通鉴\\10-语料\\知识视频素材库\\05-拍摄稿\\K005-*.md`）。
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


def _is_blank(line: str) -> bool:
    return not line.strip()


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


def _parse_meta_section(
    block: list[str], base_line: int, notes: list[str]
) -> tuple[dict[str, Any], dict[str, Any] | None, list[dict[str, Any]]]:
    """解析 `一、传达层`：抽【标题】【封面文案】为 meta，【冷开场】为 cold_open。"""
    meta: dict[str, Any] = {}
    cold_open: dict[str, Any] | None = None
    uncertain: list[dict[str, Any]] = []

    current: str | None = None
    bucket: list[str] = []
    header_raw: list[str] = []  # 标签行原文（含 `（时间码…）` 说明），供提取时间码

    def flush() -> None:
        nonlocal current, bucket, cold_open, header_raw
        if current is None:
            return
        name, lines_ = current, list(bucket)
        scan = list(header_raw)
        current, bucket, header_raw = None, [], []
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
            cold_open = {"start": start, "end": end, "text": texts, "source": "一、传达层/【冷开场】"}
        elif "标题" in name:
            meta["title_card"] = texts[0] if texts else ""
        elif "封面" in name:
            meta["cover"] = texts
        else:
            uncertain.append({"kind": "meta_block", "name": name, "lines": texts, "line": base_line})

    for offset, raw in enumerate(block):
        m = _RE_META_TAG.match(raw.strip())
        if m:
            flush()
            current = m.group("name").strip()
            bucket = []
            header_raw = [raw.strip()]
            # 标签后同一行可能还有内容；先剥掉 `（…）` 说明（那是写作提示，非内容）
            rest = raw.strip()[m.end():].strip()
            rest = re.sub(r"^（[^）]*）\s*", "", rest)
            if rest:
                bucket.append(rest)
            continue
        if current is not None and raw.strip().startswith(">"):
            bucket.append(raw.strip().lstrip(">").strip())
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
            ensure_segment()["visual_marks"].append(clean_line(m_mark.group(1)))
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
            ensure_segment()["excluded"].append(
                {"kind": "blockquote", "text": clean_line(stripped.lstrip(">").strip()), "line": line_no}
            )
            i += 1
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

        # 其余 → 旁白
        text = clean_line(stripped)
        if text:
            ensure_segment()["narration"].append(text)
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
    script = getattr(args, "script", None)
    if not script:
        print("用法：lvs parse <拍摄稿.md> [--task NAME]")
        return 2

    src = Path(script)
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
