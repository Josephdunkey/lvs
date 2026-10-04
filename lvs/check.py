"""拍摄稿格式校验（`lvs check`）—— 出稿即验，不等到 parse。

**为什么要有这一层**：`lvs parse` 面对烂稿子是**降级**而不是报错（这是对的 ——
解析阶段不该因为一个缺时间码就拒绝干活）。但降级是**静默**的：段标题少个 `【】`、
旁白整段不换行、画面位写成否定句，parse 都能跑完，问题要到配音走调、出图长人才暴露。

本模块把"已经知道会出事的写法"全部前置成一张清单：

- **error**：一定会产生错产物（否定句会被画出来、加粗脚手架会被 TTS 念出来）
- **warn** ：大概率要人工返工（旁白没断句、画面位密度太稀、文末表对不上）
- **info** ：风格建议（画面位没写 `[场景]`/`[图表]` 显式标注）

设计取舍：
- **纯函数**：`check_manuscript(text)` 不碰磁盘、不调 LLM，好测也好嵌进技能。
- **复用 parse**：结构性问题一律拿 `parse.parse_script()` 的结果判定，而不是自己再写一套
  正则 —— 否则校验器与解析器会各有一套"什么算场景标题"的理解，慢慢漂移。
- **规则表驱动**：提示词卫生那几条复用 `prompting` 的词表，单一真源。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lvs import parse as parse_mod
from lvs import prompting

LEVEL_ERROR = "error"
LEVEL_WARN = "warn"
LEVEL_INFO = "info"

_LEVEL_ORDER = {LEVEL_ERROR: 0, LEVEL_WARN: 1, LEVEL_INFO: 2}
_LEVEL_MARK = {LEVEL_ERROR: "✗", LEVEL_WARN: "!", LEVEL_INFO: "·"}


@dataclass
class Finding:
    """一条校验结论。`line` 为 1-based 行号，未知时为 0。"""

    level: str
    code: str
    message: str
    line: int = 0
    hint: str = ""

    def render(self) -> str:
        loc = f"L{self.line}" if self.line else "  - "
        tail = f"\n      → {self.hint}" if self.hint else ""
        return f"  {_LEVEL_MARK.get(self.level, '?')} [{self.code}] {loc} {self.message}{tail}"


# ---- 单条规则 --------------------------------------------------------------

# 艺术家姓名：会把「某某的风格」字面画成「某某本人」
_RE_ARTIST = re.compile(r"in the style of\s+\S+", re.IGNORECASE)

# 剪辑动词：描述"怎么剪"，不是"屏幕上有什么"
_RE_EDIT_VERB = re.compile(
    r"高亮|砸屏|分栏|并排浮现|浮现|逐行|滚屏|叠化|叠印|推近|拉远|对切|字幕|排版|呈现"
)

# 引号原文：会被画成伪汉字/乱码
_RE_QUOTED = re.compile(r'"[^"]{2,}"|“[^”]{2,}”|「[^」]{2,}」|『[^』]{2,}』|《[^》]{2,}》')

# TTS 脚手架：加粗标记会被逐字念出来
_RE_BOLD = re.compile(r"\*\*[^*]+\*\*")

# 人物槽位
_RE_CAST_SLOT = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)\}")

# 画面位显式标注
_RE_VISUAL_TAG = re.compile(r"^[\[【]\s*(场景|实拍|画面|图表|图示|图表卡)\s*[\]】]")

# 旁白单行长度上限（汉字）。超过即"没断句"，一句一行是硬要求
NARRATION_MAX_CHARS = 60

# 画面位密度基准：约 100 汉字 / 条；低于 150 才告警
DENSITY_WARN_CHARS_PER_MARK = 150

# 单组长尾上限
LONG_TAIL_CHARS = 200

# 必填章名关键词
_REQUIRED_SECTIONS = {
    "正文": ("正文", "讲稿"),
    "画面位清单": ("画面位清单", "画面清单"),
}


@dataclass
class CheckResult:
    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == LEVEL_ERROR]

    @property
    def warns(self) -> list[Finding]:
        return [f for f in self.findings if f.level == LEVEL_WARN]

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: (_LEVEL_ORDER.get(f.level, 9), f.line))


# ---- 主校验 ----------------------------------------------------------------


def _norm_sentence(text: str) -> str:
    """比对两句旁白是否"逐字相同"：去掉空白与句末标点，别让排版差异骗过判据。"""
    return re.sub(r"[\s　]+", "", text or "").rstrip("。！？…、，,")


def check_manuscript(
    text: str,
    *,
    known_cast: set[str] | None = None,
) -> CheckResult:
    """校验一篇拍摄稿。

    `known_cast`：定妆库里已登记的人物 ID（取自 `lock.json` / `cast.json`）。
    给了它就检查 `{NAME}` 槽位是否都在册；没给就跳过这条（不假装知道）。
    """
    result = CheckResult()
    add = result.findings.append

    lines = text.splitlines()

    # ---- 结构：交给 parse 判，避免两套理解 ----
    data = parse_mod.parse_script(text)

    headings = [ln for ln in lines if parse_mod._RE_H2.match(ln)]  # noqa: SLF001 - 同包内部约定
    if not headings:
        add(Finding(LEVEL_ERROR, "C001", "没有任何 `## ` 分节标题", 0,
                    "拍摄稿必须至少含 `## 一、传达层` / `## 二、正文讲稿` / `## 三、画面位清单`"))

    if not data.get("cold_open"):
        add(Finding(LEVEL_ERROR, "C002", "未找到 `【冷开场】`", 0,
                    "在 `一、传达层` 下写 `**【冷开场】**（0:00–0:30）` 再空行，然后 `> 旁白`"))

    # ---- 冷开场重复（C005）----
    # 为什么是 error 而不是 warn：`build_skeleton` 先把 `cold_open` 铺成一串镜，
    # 再遍历**全部** `正文讲稿` 段。所以正文里若再写一个「① 冷开场」，
    # 那几十秒会被**原文念两遍**（2026-10-03 实测：镜 001–010 与 011–020 逐字相同）。
    # 画面与字幕都跟着多一份，而**全流程不报任何错** —— 属于"静默产出错产物"那一类。
    cold = data.get("cold_open") or {}
    cold_lines = {_norm_sentence(t) for t in (cold.get("text") or []) if t}
    cold_lines.discard("")
    if cold_lines:
        for seg in data.get("segments", []):
            dup = [
                f for f in seg.get("flow", [])
                if f.get("kind") == "narration"
                and _norm_sentence(str(f.get("text") or "")) in cold_lines
            ]
            if len(dup) >= 3:
                add(Finding(
                    LEVEL_ERROR, "C005",
                    f"第 {seg.get('index')} 段「{seg.get('heading')}」与 `【冷开场】` 有 {len(dup)} 句逐字重复",
                    seg.get("line", 0),
                    "冷开场只在 `一、传达层` 里写一次 —— 流水线会把它铺成片头的镜。"
                    "正文讲稿里**不要**再写一个冷开场段（否则那 30 秒会被念两遍），"
                    "正文从第 2 段开始；段号顺延即可。",
                ))
                break  # 报第一个就够，重复写会刷屏

    for label, keys in _REQUIRED_SECTIONS.items():
        if not any(any(k in h for k in keys) for h in headings):
            add(Finding(LEVEL_ERROR, "C003", f"缺少「{label}」分节", 0,
                        f"章名要含关键词：{' / '.join(keys)}（解析器按关键词分类）"))

    if not data.get("visual_mark_table"):
        add(Finding(LEVEL_WARN, "C004", "未找到画面位清单表", 0,
                    "文末加 `## 三、画面位清单` + 三列表格，供人工核对与覆盖率校验"))

    # 逐行扫描
    # ★ 引文块**块语义**（与 parse 2026-10-04 修复同口径）：`> 〔引原文〕` 标记行
    #   及其后到空行/结构边界的整块都不是旁白。此前只跳 `>` 开头的行，块正文
    #   （无 `> ` 前缀）落进「其余视为旁白」→ C041 误报（003 实测 5 处 67–88 字）。
    in_quote = False
    for i, raw in enumerate(lines, start=1):
        s = raw.strip()
        if s.startswith(">") and "〔引原文〕" in s:
            in_quote = True
            check_line(raw, i, lines, result, known_cast, add)
            continue
        if in_quote:  # 块语义：块内行跳过旁白判据
            if not s or s.startswith("#") or s.startswith("[画面位]") or s.startswith(">"):
                in_quote = False  # 空行 / 结构边界：出块，正常检查
            else:
                continue  # 块正文行：整行跳过（不进 TTS，不适用旁白判据）
        check_line(raw, i, lines, result, known_cast, add)

    # ---- 段落级：时间码 / 旁白长度 / 密度 ----
    total_narration = 0
    total_marks = 0
    for seg in data.get("segments", []):
        head = f"第 {seg.get('index')} 段"
        if seg.get("start") is None and seg.get("timecode_raw"):
            add(Finding(LEVEL_ERROR, "C010",
                        f"{head}「{seg.get('heading')}」的时间码无法解析：{seg.get('timecode_raw')!r}",
                        0, "写成 `### 【场景名】0:00–1:07`；片尾可用「结尾」"))
        elif seg.get("start") is None:
            add(Finding(LEVEL_ERROR, "C010",
                        f"{head}「{seg.get('heading')}」缺少时间码", 0,
                        "写成 `### 【场景名】0:00–1:07`"))

        narration_chars = sum(len(re.sub(r"\s", "", n)) for n in seg.get("narration") or [])
        total_narration += narration_chars
        marks = len(seg.get("visual_marks") or [])
        total_marks += marks

        # 长尾：单段旁白很多却没有画面位
        if narration_chars > LONG_TAIL_CHARS and marks == 0:
            add(Finding(LEVEL_WARN, "C011",
                        f"{head}「{seg.get('heading')}」{narration_chars} 字却 0 条画面位",
                        0, f"单段 > {LONG_TAIL_CHARS} 汉字须回补画面位，否则一段旁白只配一张图"))

    result.stats.update(
        {
            "segments": len(data.get("segments", [])),
            "narration_chars": total_narration,
            "visual_marks": total_marks,
            "mark_table_rows": len(data.get("visual_mark_table") or []),
            "uncertain": len(data.get("uncertain") or []),
        }
    )

    # 密度
    if total_marks:
        per = total_narration / total_marks
        result.stats["chars_per_mark"] = round(per, 1)
        if per > DENSITY_WARN_CHARS_PER_MARK:
            add(Finding(LEVEL_WARN, "C012",
                        f"画面位密度偏稀：{per:.0f} 汉字/条（基准约 100）",
                        0, "画面位不够会导致一张图压很久；按每约 100 汉字一条补"))

    # 覆盖率：正文画面位数 vs 文末表条数
    rows = len(data.get("visual_mark_table") or [])
    if rows and total_marks and abs(rows - total_marks) > max(2, total_marks * 0.2):
        add(Finding(LEVEL_WARN, "C013",
                    f"正文画面位 {total_marks} 条，文末清单 {rows} 条，相差过多",
                    0, "表是给人核对的，两边对不上说明有一边漏了"))

    # parse 自己报的降级
    for note in data.get("notes") or []:
        if "未找到" in note or "降级" in note:
            add(Finding(LEVEL_WARN, "C014", f"解析降级：{note}"))

    return result


def check_line(
    raw: str,
    line_no: int,
    lines: list[str],
    result: CheckResult,
    known_cast: set[str] | None,
    add,
) -> None:
    """逐行规则。抽出来是为了让 `check_manuscript` 保持可读。"""
    stripped = raw.strip()
    if not stripped:
        return

    # 传达层的标签行 `**【标题】**` / `**【冷开场】**` 是**结构标记**，不是旁白。
    # 不排除的话 C040（旁白加粗）会把每一篇正常稿子都判成 error —— 那么这条规则
    # 就等于失效（人会把整个校验器当噪声忽略）。
    if parse_mod._RE_META_TAG.match(stripped):  # noqa: SLF001
        return

    # ---- 画面位行 ----
    m_mark = parse_mod._RE_VISUAL_MARK.match(stripped)  # noqa: SLF001
    if m_mark:
        body = m_mark.group(1).strip()
        if not body:
            add(Finding(LEVEL_WARN, "C020", "空的 `[画面位]`（只有标记没有内容）", line_no,
                        "删掉它，或补上画面描述"))
            return

        negations = prompting.find_negations(body)
        if negations:
            code, snippet, why = negations[0]
            add(Finding(LEVEL_ERROR, "C021",
                        f"画面位里有否定式描述「{snippet}」（{why}）", line_no,
                        "改成正面陈述：写画面里**有什么**，不写没有什么。"
                        "判据与 `lvs migrate` 的工作清单同源（prompting.NEGATION_RULES）"))

        if _RE_ARTIST.search(body):
            add(Finding(LEVEL_ERROR, "C022",
                        "画面位里有 `in the style of <人名>`（会被画成那个人本人）", line_no,
                        "风格只用「技法＋媒介＋调色板」描述"))

        if _RE_QUOTED.search(body):
            add(Finding(LEVEL_WARN, "C023",
                        "画面位里有引号原文（容易被画成伪汉字）", line_no,
                        "把要上屏的文字放进 `[画面位] [图表] …`，走卡片渲染"))

        if _RE_EDIT_VERB.search(body):
            add(Finding(LEVEL_WARN, "C024",
                        "画面位里有剪辑动词（描述的是怎么剪，不是画面里有什么）", line_no,
                        "改成实际可拍的场景语言"))

        if not _RE_VISUAL_TAG.match(body):
            add(Finding(LEVEL_INFO, "C025",
                        "画面位没写 `[场景]` / `[图表]` 显式标注（解析器要靠关键词猜）", line_no,
                        "写上标注就不会猜错"))

        for name in _RE_CAST_SLOT.findall(body):
            if known_cast is not None and name not in known_cast:
                add(Finding(LEVEL_ERROR, "C026",
                            f"人物槽位 `{{{name}}}` 不在定妆库里", line_no,
                            f"已登记：{', '.join(sorted(known_cast)) or '（空）'}；"
                            "名字要与定妆卡登记名一致"))
        return

    # ---- 独立方括号标记 ----
    m_bracket = parse_mod._RE_BRACKET.match(stripped)  # noqa: SLF001
    if m_bracket:
        name = m_bracket.group("name")
        if name.startswith(("画面位", "重参与点")):
            return
        add(Finding(LEVEL_WARN, "C030",
                    f"独立标记 `[{name}]` 不属于已知三类", line_no,
                    "只认 `[画面位]` / `[重参与点]` / `> 引用`；其余会进 uncertain 清单"))
        return

    # ---- 标题 ----
    if stripped.startswith("#"):
        if parse_mod._RE_H3.match(stripped) and not parse_mod._RE_SEG_HEAD.match(stripped):  # noqa: SLF001
            add(Finding(LEVEL_WARN, "C031",
                        "三级标题不是场景头（缺 `【场景名】`）", line_no,
                        "写成 `### 【场景名】0:00–1:07`"))
        return

    # ---- 引用块 / 表格 ----
    if stripped.startswith(">") or stripped.startswith("|"):
        return
    if stripped == "---":
        return

    # ---- 其余视为旁白 ----
    if _RE_BOLD.search(stripped):
        add(Finding(LEVEL_ERROR, "C040",
                    "旁白里有 `**加粗**` 脚手架，会被 TTS 逐字念出来", line_no,
                    "删掉 `**`（写作提示请另起 `>` 引用行）"))

    plain = parse_mod.clean_line(stripped)
    if len(re.sub(r"\s", "", plain)) > NARRATION_MAX_CHARS:
        add(Finding(LEVEL_WARN, "C041",
                    f"旁白单行 {len(plain)} 字，偏长（要求一句一行）", line_no,
                    "拆成短句，每句一行；长句会让配音节奏和字幕都变糟"))


# ---- 命令入口 --------------------------------------------------------------


def collect_known_cast(root: Path | None = None, config=None) -> set[str] | None:  # noqa: ANN001
    """从定妆库读已登记的人物 ID；库不存在返回 None（= 不检查这条）。

    ★ 优先走 `cast.cast_dir(config)`：它尊重 `[paths].lib` / `[cast].dir` 的
    多项目派生。上一版只扫仓库根的 `cast/lock.json`，多项目化之后真实定妆库
    落到了 `<素材库>/_cast/lock.json`，于是 C026（人物槽位是否在册）**永远读不到
    库、静默失效**——校验器以为自己没资格判断，其实只是看错了地方。
    """
    if config is not None:
        from lvs import cast as cast_mod

        directory = cast_mod.cast_dir(config)
        lock = cast_mod.load_lock(directory)
        if lock.characters:
            return set(lock.characters)
        registry = cast_mod.load_registry(directory)
        if registry:
            return set(registry)
        return None

    # 没传 config 时退回旧行为（扫 `cast/lock.json` / `cast/cast.json`），
    # 供独立调用与既有测试用。
    if root is None:
        from lvs.config import PROJECT_ROOT

        root = PROJECT_ROOT
    for name in ("cast/lock.json", "cast/cast.json"):
        path = root / name if root.name != "cast" else root.parent / name
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            chars = data.get("characters") or {}
            if isinstance(chars, dict):
                return set(chars)
    return None


def run_command(config, ws, args) -> int:  # noqa: ANN001 - 由 cli 传入
    manuscript = getattr(args, "manuscript", None)
    if not manuscript:
        print("用法：lvs check <拍摄稿.md>")
        return 2

    src = Path(manuscript)
    if not src.is_file():
        print(f"拍摄稿不存在：{src}")
        return 2

    text = src.read_text(encoding="utf-8")
    result = check_manuscript(text, known_cast=collect_known_cast(config=config))

    stats = result.stats
    print(f"校验：{src.name}")
    print(
        f"  段 {stats.get('segments', 0)}｜旁白 {stats.get('narration_chars', 0)} 字｜"
        f"画面位 {stats.get('visual_marks', 0)} 条"
        + (f"（{stats['chars_per_mark']} 字/条）" if stats.get("chars_per_mark") else "")
        + f"｜文末清单 {stats.get('mark_table_rows', 0)} 条"
    )

    findings = result.sorted_findings()
    if not findings:
        print("  ✓ 全部通过")
        return 0

    for f in findings:
        print(f.render())

    n_err, n_warn = len(result.errors), len(result.warns)
    print(f"\n结论：{n_err} 个 error，{n_warn} 个 warn。")
    if n_err:
        print("  error 必须先修 —— 它们一定会产出错图或错音。")
        return 1
    print("  可以进 `lvs parse`，warn 建议一并处理。")
    return 0
