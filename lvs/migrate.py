"""拍摄稿 v2 迁移（`lvs migrate`）—— 把已有稿子变成"能过闸门"的稿子。

## 为什么需要它

v2 的两道闸门都要求拍摄稿里有机器能读的东西：

| 闸门 | 要求 | 没有会怎样 |
|---|---|---|
| G2 定妆 | 画面位里要有 `{NAME}` 人物槽位 | 定妆库白建，一致性注入一点不生效 |
| G1 提示词体检 | 画面位要标 `[场景]` / `[图表]` | 解析器靠关键词猜，猜错的进 uncertain |

已有的旧稿子这两样都没有。要求作者回头重写全部旧稿是**不可接受的成本**。

## 本命令做什么 / 不做什么

**做（机械的，可自动）**：
1. 给每条 `[画面位]` 补 `[场景]` / `[图表]` 标注（判据取自 `prompting.resolve_kind`，与生图分流同一套）
2. 把画面位里提到的人物名换成 `{NAME}` 槽位（名单取自 `[cast].card` 的定妆库）
3. 顺手把已经拆坏的地方（多余空格、重复标注）归一

**不做（需要判断的，列成工作清单交人）**：
4. 否定式描述（`无字` / `空无一人` / `不给五官`）—— 改法要看上下文，
   机器硬改会把"旗面空白"改成"旗面平整的单色织物"还是"旗面被风灌满"全凭猜。
   本命令把它们**逐条列出来**，并给出"为什么必须改"，由人或 Agent 定夺。

**绝不覆盖原稿**：结果写到 `--out` 指定目录（默认原目录下 `_v2/`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from lvs import cast as cast_mod
from lvs import parse as parse_mod
from lvs import prompting

# 画面位行
_RE_MARK = parse_mod._RE_VISUAL_MARK  # noqa: SLF001 - 同包内部约定

# 已带显式标注就不重复加
_RE_TAG = re.compile(r"^[\[【]\s*(场景|实拍|画面|图表|图示|图表卡)\s*[\]】]")

DEFAULT_OUTDIRNAME = "_v2"


@dataclass
class Change:
    line: int
    before: str
    after: str
    why: str


@dataclass
class WorklistItem:
    line: int
    text: str
    why: str


@dataclass
class MigrateReport:
    total_marks: int = 0
    tagged: int = 0
    slotted: int = 0
    pronoun_slotted: int = 0
    # 有歧义的代词 → 候选人物。机器不敢猜，列出来交给人。
    ambiguous: dict[str, list[str]] = field(default_factory=dict)
    # 需要人工复核的槽位化（目前只有"疑似比较提及"）。已按出场处理，但请人过一眼。
    review: list[tuple[int, str, str]] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    worklist: list[WorklistItem] = field(default_factory=list)

    def to_markdown(self, title: str) -> str:
        lines = [
            f"# 拍摄稿 v2 迁移报告 · {title}",
            "",
            f"- 画面位共 {self.total_marks} 条",
            f"- 补 `[场景]/[图表]` 标注 **{self.tagged}** 条",
            f"- 补 `{{NAME}}` 人物槽位 **{self.slotted}** 条",
            f"- 补代词槽位 **{self.pronoun_slotted}** 条",
            f"- 待人工判断 **{len(self.worklist) + len(self.ambiguous)}** 条（本命令不敢硬改）",
            f"- 待人工**复核** **{len(self.review)}** 条（已按出场处理，请过一眼）",
            "",
        ]
        if self.changes:
            lines += ["## 改了什么", "", "| 行 | 原 | 改后 | 为什么 |", "|---|---|---|---|"]
            for c in self.changes:
                lines.append(
                    f"| {c.line} | `{_clip(c.before)}` | `{_clip(c.after)}` | {c.why} |"
                )
            lines.append("")
        if self.ambiguous:
            lines += [
                "## ⚠ 代词有歧义（**必须人工改写**）",
                "",
                "同一个代词被**多个**人物声明，机器猜错就等于给那一镜注入另一个人的脸 —— "
                "不报错、不崩，只是悄悄出错图。所以一律留给人：",
                "",
                "| 代词 | 候选人 | 改法 |",
                "|---|---|---|",
            ]
            for p, names in sorted(self.ambiguous.items()):
                lines.append(
                    f"| `{p}` | {'、'.join(names)} | 把画面位里的「{p}」写成 `{{{names[0]}}}` "
                    f"（或对应那一位的 ID） |"
                )
            lines += [
                "",
                "或者，如果这本书里这个代词确实只指一个人，就在定妆卡的该人物小节里补一行",
                "`- 代词：他` —— 机器下次就能唯一确定，不用再人工过。",
                "",
            ]
        if self.review:
            lines += [
                "## 🔍 待复核：疑似「比较提及」（已按出场槽位化）",
                "",
                "中文里「与渡边同龄」和「与渡边并肩」句式相同，但前者只是拿他作参照、",
                "**他并不在场**。机器分不清，默认按出场处理（多数情况对）。",
                "请扫一眼下面几条，若确是比较提及，把那一处的 `{ID}` 改回中文即可。",
                "",
                "| 行 | 说明 |",
                "|---|---|",
            ]
            for ln, _text, why in self.review:
                lines.append(f"| {ln} | {why} |")
            lines.append("")
        if self.worklist:
            lines += [
                "## ⚠ 待人工判断（必须处理，否则出错图）",
                "",
                "这些是**否定式描述**：Z-Image Turbo 是 cfg=1.0 蒸馏模型，没有负向引导，",
                "否定句里的名词会被**原样画出来**。改法要看上下文，所以留给人。",
                "",
                "| 行 | 原文 | 为什么要改 |",
                "|---|---|---|",
            ]
            for w in self.worklist:
                lines.append(f"| {w.line} | `{_clip(w.text)}` | {w.why} |")
            lines.append("")
        return "\n".join(lines) + "\n"


def _clip(text: str, n: int = 70) -> str:
    text = text.replace("|", "\\|")
    return text if len(text) <= n else text[: n - 1] + "…"


# ---- 单行处理 --------------------------------------------------------------


def tag_line(body: str) -> tuple[str, bool]:
    """给画面位正文补 `[场景]` / `[图表]` 标注。返回 (新正文, 是否改过)。"""
    if _RE_TAG.match(body.strip()):
        return body, False
    beats = prompting.split_beats(body)
    kinds = [prompting.classify(b.text, b.kind) for b in beats]
    # 一条画面位里混了场景与图表时，用**第一个** beat 的判定（它就是这镜的主画面）
    kind = kinds[0] if kinds else prompting.KIND_SCENE
    tag = "场景" if kind == prompting.KIND_SCENE else "图表"
    return f"[{tag}] {body.lstrip()}", True


# 已经写进文本的槽位：`{NAOKO}`。它的**内部**不该再被当成待替换的文本。
# 为什么必须有这个守卫：简称与全名会互相包含（`渡边` ⊂ `渡边彻`），
# 先换全名得到 `{渡边彻}`，再用简称去搜就会命中方括号里的「渡边」，
# 产出 `{{渡边彻}彻}` 这种坏槽位。用交替式一次扫完，槽位优先吃掉，
# 剩下的文本才会被拿去匹配 —— 单次遍历，不靠多次重扫。
_SLOT_OR = re.compile(r"(\{[^{}]*\})")


def _first_outside_slot(text: str, token: str) -> re.Match[str] | None:
    """在**不在 `{}` 槽位内**的位置，找 `token`（允许后跟一个「的」）。"""
    pattern = re.compile(_SLOT_OR.pattern + r"|(" + re.escape(token) + r"的?)")
    for m in pattern.finditer(text):
        if m.group(1) is None:  # 命中的不是槽位，就是我们要的 token
            return m
    return None


def slot_line(
    body: str,
    registry: dict[str, cast_mod.CastEntry],
    pronouns: dict[str, str] | None = None,
) -> tuple[str, int, int, list[str]]:
    """把画面位里的人物名与**代词**换成 `{ID}` 槽位。

    返回 `(新正文, 换了几个姓名, 换了几个代词)`。

    ---

    **姓名**：只换**第一次出现**的每个名字。同一条画面位里反复出现同一个人很常见
    （"直子的侧脸…直子的手"），换两遍只会让提示词里塞两段同样的锚定描述。
    换第二个之后出现的那个名字保留中文 —— 它是"同一个人的第二次提及"，
    留着反而帮模型理解画面里有两处她。

    **代词**：只在**该人本行还没出现过**时换（同一个人由姓名换过一次，就不再由代词换）。
    这一步是补盲区：`直子的左手握住他的右手` 原先只换出一个 `{NAOKO}`，
    渡边的锚定一点没注入 —— 那一镜的"他"会被模型自由采样成**任何一张脸**。

    代词必须由定妆卡里的人**声明**（`- 代词：他`），且**唯一**才换。
    有歧义时 `pronouns` 里不会有它（见 `cast.pronoun_map`），这里自然跳过。

    第四个返回值 `flags` 是**给人工复核的提醒**（不是错误）：中文里
    「与渡边同龄」「比直子高」这种**比较提及**与被比较的人同框的句子长得几乎一样
    （「与直子并肩」就是同框）。机器分不清，就一律**照常槽位化、同时列出来** ——
    槽位化是多数情况下的正确答案，而列出来让人有机会推翻它。
    """
    out = body
    n_names = 0
    n_pronouns = 0
    used: set[str] = set()
    flags: list[str] = []

    for name, entry in sorted(registry.items(), key=lambda kv: -len(kv[0])):
        # 长名优先（`渡边彻` 先于 `渡边`），否则简称会把全名截断成 `{渡边}彻`
        for token in sorted([entry.display, *entry.aliases], key=len, reverse=True):
            if not token or token not in out:
                continue
            # `直子的` → `{NAOKO} `（吃掉"的"，避免英文锚定后面挂个中文"的"的别扭）
            m = _first_outside_slot(out, token)
            if not m:
                continue
            start = m.start(2)
            marker = out[start - 1] if start > 0 else ""
            if marker and marker in _COMPARE_BEFORE:
                flags.append(
                    f"「{out[start: m.end(2)]}」前面是「{marker}」—— 可能是**比较提及**"
                    f"（「与{token}同龄」）而不是出场（「与{token}并肩」）。已按出场槽位化，请复核。"
                )
            out = out[:start] + "{" + name + "} " + out[m.end(2):]
            used.add(name)
            n_names += 1
            break

    for pron, name in sorted((pronouns or {}).items(), key=lambda kv: -len(kv[0])):
        if name in used:
            continue  # 同一个人的锚定不注入两遍
        m = _first_pronoun_outside_slot(out, pron)
        if not m:
            continue
        out = out[: m.start(2)] + "{" + name + "} " + out[m.end(2):]
        used.add(name)
        n_pronouns += 1

    return out, n_names, n_pronouns, flags


# 代词误伤的防呆：中文里"他/我"大量出现在**复合词**里（其他、他们、自我、我们…），
# 一律换掉会把"其他"变成"其{渡边彻} "。这两个字符集是前后边界判据 ——
# 只挡最常见的几种，不追求完备：剩下的靠迁移报告逐条复核（报告是兜底）。
# 比较连接词：出现在姓名**前一个字**时，这一处可能是"拿他作参照"而不是"他在场"。
# 只报告、不跳过 —— 「与直子并肩」和「与直子同龄」句式相同，机器分不清，
# 但前者该槽位化、后者不该。默认按出场处理（多数情况对），把判断留给人。
_COMPARE_BEFORE = "与同像比似"

_PRONOUN_BLOCK_BEFORE = "其无任等乃自"
_PRONOUN_BLOCK_AFTER = "们人乡日年月处方"


def _first_pronoun_outside_slot(text: str, pron: str) -> re.Match[str] | None:
    """找第一个「不在槽位内、且前后边界合法」的代词。

    ★ 边界必须拿**原文**判，不能拿孤立出来的那个词判。
    踩过的坑：先用 lookbehind 写成 `(?<!其)他(?!们)`，然后对截出来的 `"他"` 做
    `fullmatch` —— `"他"` 前面什么都没有，lookbehind 必然通过，
    于是「其他人都走了」被换成「其{渡边彻} 人都走了」。判据必须贴着真实上下文。

    ★ 两种语言的边界判据不同，**不能用同一套**：
      - **中文**：没有词间空格，`他` 会落在 `其他 / 他们 / 自我` 这类复合词里，
        要用「前后一个字是不是构词成分」来挡。
      - **英文**：`he` 是 `the / her / when` 的子串，必须用单词边界
        `(?<![A-Za-z])…(?![A-Za-z])`。拿中文那套去判英文等于没判 ——
        `he` 会把「the」里的两个字母替掉（`the man` → `t{X} man`）。
    """
    is_ascii = bool(pron) and all(ord(c) < 128 for c in pron)
    body = (
        r"(?<![A-Za-z])" + re.escape(pron) + r"(?![A-Za-z])"
        if is_ascii
        else re.escape(pron) + r"的?"
    )
    # 英文忽略大小写（`He` 与 `he` 是同一个人）；中文没有大小写，不需要
    flags = re.IGNORECASE if is_ascii else 0
    pattern = re.compile(_SLOT_OR.pattern + r"|(" + body + r")", flags)
    for m in pattern.finditer(text):
        if m.group(1) is not None:
            continue  # 命中槽位，跳过
        if is_ascii:
            return m  # 单词边界已经把复合词挡在外面了
        start, end = m.start(2), m.end(2)
        before = text[start - 1] if start > 0 else ""
        after = text[end] if end < len(text) else ""
        # ★ `before and …`：空串 `in` 任何字符串都为 True —— 少了这个短路，
        # **句首的代词会被全部挡掉**（"她钻出松林"里的"她"前面什么都没有，
        # 于是被判成"在复合词里"）。这类 bug 不报错，只是悄悄少注入一个锚定。
        if (before and before in _PRONOUN_BLOCK_BEFORE) or (
            after and after in _PRONOUN_BLOCK_AFTER
        ):
            continue
        return m
    return None


# 需要人工判断的写法：判据**不在本文件**，而是复用 `prompting.NEGATION_RULES`。
# 两处各写一套必然漂移（先写的窄、后写的宽），用户就不知道该信哪个。


# 拍摄稿里可选的**集级代词声明**行：`代词：她={NAOKO}，他={渡边彻}`。
#
# 为什么声明写在**拍摄稿**里而不是定妆卡里：代词的作用域是"这一集讲谁"，
# 不是"这本书有谁"。一本小说里可能有多个女性角色，`她` 在**书级**是歧义的
# （例如四个人都符合），但在某一集里只有一个女性出场 —— 歧义是**集级**消失的。
# 定妆卡是书级真源，装不下集级信息；拍摄稿本来就是集级的产物。
_PRONOUN_LINE = re.compile(r"^\s*[>*\-]*\s*代词(?:映射)?\s*[：:]\s*(?P<body>.+?)\s*$")
_PRONOUN_PAIR = re.compile(r"(?P<pron>[^\s=＝,，、;；]+?)\s*[=＝]\s*\{?(?P<name>[^}\]\s,，、;；]+)\}?")


def read_pronoun_line(text: str) -> dict[str, str]:
    """从拍摄稿头部读可选的 `代词：她=NAOKO，他=渡边彻` 行。读不到返回空 dict。

    只认**第一条**匹配行：一条声明就够，多条只会让人怀疑该信哪条。
    """
    for raw in (text or "").splitlines():
        m = _PRONOUN_LINE.match(raw)
        if not m:
            continue
        out: dict[str, str] = {}
        for pair in _PRONOUN_PAIR.finditer(m.group("body")):
            pron = pair.group("pron").strip()
            name = pair.group("name").strip()
            if pron and name:
                out[pron] = name
        return out
    return {}


def scan_worklist(lines: list[str]) -> list[WorklistItem]:
    """找出所有需要人工判断的写法（只扫画面位行）。"""
    items: list[WorklistItem] = []
    for i, raw in enumerate(lines, start=1):
        m = _RE_MARK.match(raw.strip())
        if not m:
            continue
        body = m.group(1)
        hits = prompting.find_negations(body)
        if hits:
            code, snippet, why = hits[0]
            items.append(WorklistItem(line=i, text=raw.strip(), why=f"{why}（命中「{snippet}」）"))
    return items


# ---- 主流程 ----------------------------------------------------------------


def migrate_text(
    text: str,
    registry: dict[str, cast_mod.CastEntry] | None = None,
    pronouns: dict[str, str] | None = None,
) -> tuple[str, MigrateReport]:
    """迁移一篇拍摄稿的正文。返回 (新文本, 报告)。"""
    registry = registry or {}
    pronouns = pronouns or {}
    lines = text.splitlines()
    report = MigrateReport()
    out: list[str] = []

    for i, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        m = _RE_MARK.match(stripped)
        if not m:
            out.append(raw)
            continue

        report.total_marks += 1
        body = m.group(1)
        before = stripped

        body, tagged = tag_line(body)
        if tagged:
            report.tagged += 1

        n_slot = 0
        n_pron = 0
        if registry or pronouns:
            body, n_slot, n_pron, flags = slot_line(body, registry, pronouns)
            for f in flags:
                report.review.append((i, body, f))

        why: list[str] = []
        if n_slot:
            why.append(f"补 {n_slot} 处人物槽位")
            report.slotted += 1
        if n_pron:
            why.append(f"补 {n_pron} 处代词槽位")
            report.pronoun_slotted += 1

        after = f"[画面位] {body}"
        if why:
            report.changes.append(Change(i, before, after, "、".join(why)))
        elif tagged:
            report.changes.append(Change(i, before, after, "补 kind 标注"))
        out.append(after)

    report.worklist = scan_worklist(lines)
    return "\n".join(out) + ("\n" if text.endswith("\n") else ""), report


def run_command(config, ws, args) -> int:  # noqa: ANN001 - 由 cli 传入
    manuscript = getattr(args, "manuscript", None)
    if not manuscript:
        print("用法：lvs migrate <拍摄稿.md> [--out DIR]")
        return 2

    src = Path(manuscript)
    if not src.is_file():
        print(f"拍摄稿不存在：{src}")
        return 2

    # 人物名单：优先用 `--cast-dir` / config 的定妆库；没有就退到定妆卡
    registry: dict[str, cast_mod.CastEntry] = {}
    directory = cast_mod.cast_dir(config, getattr(args, "cast_dir", None))
    registry = cast_mod.load_registry(directory) or cast_mod.load_lock(directory).characters
    if not registry:
        card = cast_mod.find_card(config) or config.get("cast.card")
        if card and Path(str(card)).is_file():
            registry = cast_mod.parse_card(Path(str(card)).read_text(encoding="utf-8"))
    if not registry:
        print("提示：定妆库与定妆卡都没读到人物，本次只补 kind 标注、不补人物槽位。")

    text = src.read_text(encoding="utf-8")
    resolved, ambiguous = cast_mod.pronoun_map(registry)

    # 优先级（后写覆盖先写）：定妆卡声明 < 拍摄稿的 `代词：` 行 < `--pronoun`
    #
    # 为什么要三层而不是一层：三处的"该信谁"不同 ——
    #   - 定妆卡是**书级**：`我` 全书只指渡边，写在这里最稳
    #   - 拍摄稿 `代词：` 行是**集级**：`她` 在 P1 只指直子，但书级是歧义的
    #   - `--pronoun` 是**本次**：人工在命令行上的一次性判断
    from_script = read_pronoun_line(text)
    for pron, name in from_script.items():
        resolved[pron] = name
        ambiguous.pop(pron, None)
    if from_script:
        print(f"  集级代词声明（来自拍摄稿）：{'、'.join(f'{p}→{n}' for p, n in from_script.items())}")

    for spec in getattr(args, "pronoun", None) or []:
        if "=" not in spec:
            print(f"  · 忽略格式不对的 --pronoun：`{spec}`（要写 `代词=人物ID`）")
            continue
        pron, name = (x.strip() for x in spec.split("=", 1))
        if not pron or not name:
            continue
        resolved[pron] = name
        ambiguous.pop(pron, None)

    new_text, report = migrate_text(text, registry, resolved)
    report.ambiguous = ambiguous

    out_dir = Path(getattr(args, "out", None) or (src.parent / DEFAULT_OUTDIRNAME))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / src.name
    out_file.write_text(new_text, encoding="utf-8")
    report_file = out_dir / f"{src.stem}-迁移报告.md"
    report_file.write_text(report.to_markdown(src.name), encoding="utf-8")

    print(f"迁移完成：{src.name}")
    print(
        f"  画面位 {report.total_marks} 条；补 kind 标注 {report.tagged}；"
        f"补人物槽位 {report.slotted}；补代词槽位 {report.pronoun_slotted}"
    )
    if resolved:
        print(f"  代词映射（唯一，已自动换）：{'、'.join(f'{p}→{n}' for p, n in resolved.items())}")
    print(f"  产物：{out_file}")
    print(f"  报告：{report_file}")
    if ambiguous:
        print()
        print(f"  ⚠ {len(ambiguous)} 个代词有歧义，机器不敢换（换错=注入另一个人的脸）：")
        for p, names in sorted(ambiguous.items()):
            print(f"     「{p}」可能是 {'、'.join(names)} —— 请在画面位里写明 {{{names[0]}}}")
        print("     或在定妆卡该人物小节补一行 `- 代词：…` 让机器下次唯一确定。")
    if report.worklist:
        print()
        print(f"  ⚠ 还有 {len(report.worklist)} 条需要人工判断（否定式描述），例如：")
        for w in report.worklist[:5]:
            print(f"     L{w.line} {_clip(w.text, 60)}")
        print("     全部明细见报告；改完再跑 `lvs check` 复验。")
    print()
    print("  原稿未被改动。确认无误后自行替换，或直接对 _v2/ 里的稿子跑 parse。")
    return 0
