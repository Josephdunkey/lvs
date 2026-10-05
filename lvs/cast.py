"""定妆（`lvs cast`）—— 出图前的**人物一致性闸门**。

## 为什么需要这一层

调研过 ArcReel / Drama Skills / Toonflow 三个同类项目后，它们的结论完全一致：

> 角色一致性不能靠提示词祈祷。要**先生成并冻结角色参考图**，之后所有分镜都引用它。
> ArcReel 的原话是「pipeline **won't** move to storyboards until the character and clue
> assets exist」—— 把这件事做成**结构性约束**，而不是一句建议。

我们的现状正好是反面：定妆卡只是一段英文锚定描述，靠人肉在每镜提示词里复制。
代价是实测过的 —— 直子被画成男人、跨镜脸在漂、整批 479 张图作废。

## 三态（照搬 Drama Skills 的 IMG / PLAN / REF）

| state | 含义 | 能否绑进生成 |
|---|---|---|
| `IMG`  | 只有提示词，还没有图 | ✗ |
| `PLAN` | 你答应要提供参考图，但还没给 | ✗ |
| `REF`  | 图已存在、且**已核验** | ✓ **只有它能** |

`assets` 阶段会调 `gate_missing()`：只要有 `state != REF` 的人物出现在本集镜头里，
**直接拒绝执行**并打印怎么批准。这是本节存在的意义 —— 没有冻结的脸，生成的分镜图全都要作废。

## 落点

定妆库放在**书级**（跨集共享），不是一个任务里：

    <书素材库>/_cast/
    ├── cast.json          人物登记表（提取自拍摄稿 + 定妆卡）
    ├── lock.json          ★ 冻结参考：只有 approved 的才写进来
    ├── NAOKO/
    │   ├── ref-01-neutral.png      批准后的正式参考（可多张）
    │   ├── v1/ v2/                 每轮候选
    │   └── _rejected/v2/           被打回的，带 note
    └── _contact-sheet-NAOKO.png    五景验收表

路径由 `config.toml [cast].dir` 指定；未配置时用仓库根 `cast/`。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from lvs import imagegen
from lvs.config import PROJECT_ROOT
from lvs.errors import LvsError, EXIT_USAGE

STATE_IMG = "IMG"
STATE_PLAN = "PLAN"
STATE_REF = "REF"
STATES = (STATE_IMG, STATE_PLAN, STATE_REF)

# 可绑进生成的唯一状态
BINDABLE = {STATE_REF}

DEFAULT_CAST_DIRNAME = "cast"
# 素材库里的定妆库目录名。和仓库根的 `cast/` **刻意不同**：
# 素材库里以 `_` 开头的目录表示"工具/库"，与 `00-设定` `05-拍摄稿` 这些内容目录分开。
# ★ 必须与 `lvs init` 建的目录名一致 —— 否则 init 建 `_cast`、cast 读 `cast`，
#   定妆库会"凭空多出一个空目录"，而且不报错。
LIB_CAST_DIRNAME = "_cast"

# 人物槽位：`{NAOKO}` 或 `{渡边彻}`。
# **允许中日文**：定妆卡里的角色有些没有拉丁名（`渡边彻`、`突撃隊`），
# 强行要求英文 ID 会逼着人去改定妆卡 —— 而"改格式"是最不该收的成本。
# 以字母或汉字开头，避免吞掉 `{}` / `{1}` 这类占位符。
SLOT = re.compile(r"\{([A-Za-z\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]{0,15})\}")

# 定妆卡里的角色小节标题：`### 直子（なおこ / Naoko）`
_CARD_CHAR_HEAD = re.compile(
    r"^#{3,4}\s*(?P<name>[^#\s（(/／]+)"
    r"(?P<extra>[^#（(]*)"
    r"(?:[（(](?P<alias>[^）)]*)[）)])?\s*$"
)

# 定妆卡里的锚定描述标签
_CARD_ANCHOR_LABEL = re.compile(r"锚定描述|形象锚定|定妆描述")
_CARD_PRONOUN_LABEL = re.compile(r"^[-*\s]*代词\s*[：:]")

_FENCE = re.compile(r"^```")

# 任何 Markdown 标题，连同它的层级。小节边界靠**层级**判定，不是靠"标题长得像不像人名"。
_RE_ANY_HEAD = re.compile(r"^(#{1,6})\s*(.*)$")


def _heading_level(line: str) -> int:
    """标题层级；不是标题返回 0。"""
    m = _RE_ANY_HEAD.match(line.strip())
    return len(m.group(1)) if m else 0

# 定妆卡里的机器真源 JSON 块（村上春树线的约定：```json 里含 characters）
_RE_JSON_BLOCK = re.compile(r"```json\s*\n(?P<body>.*?)\n```", re.DOTALL)


class CastError(LvsError, RuntimeError):
    """定妆库相关错误。消息面向用户。"""

    exit_code = EXIT_USAGE


# ---- 数据模型 --------------------------------------------------------------


@dataclass
class CastEntry:
    """一个人物的定妆记录。"""

    display: str = ""
    anchor: str = ""
    state: str = STATE_PLAN
    refs: list[str] = field(default_factory=list)
    source: str = ""
    negative_notes: list[str] = field(default_factory=list)
    approved_at: str = ""
    approved_by: str = ""
    rejected: list[dict[str, Any]] = field(default_factory=list)
    # 同一个人的多个锚定变体（定妆卡里写了两段，如渡边彻的「青年期」与「37 岁」）。
    # `anchor` 永远是 `anchor_variants[0]`；其余留在这里供人拆成独立条目 ——
    # 硬塞进一条会让"19 岁的脸"和"37 岁的脸"争同一个槽位（这书里真发生过）。
    anchor_variants: list[str] = field(default_factory=list)
    # 定妆卡括号里的别名与俗称（`突撃隊（"敢死队"，渡边的宿舍室友）`）。
    # 正文里出现的往往是**俗称**而不是角色名 —— 只按 display 搜会漏人，
    # 漏人的后果是"本集该出定妆照的没出"。
    aliases: list[str] = field(default_factory=list)
    # 定妆卡里声明的代词（`- 代词：我、他`）。拍摄稿的画面位大量用代词指人
    # （"直子的左手握住他的手"），只换姓名不换代词，那一镜就只注入了一个人的锚定。
    # 由**人**在定妆卡里声明代词，机器才敢换 —— 代词是人写的判断，不该由代码猜。
    pronouns: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CastEntry":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def bindable(self) -> bool:
        """能否安全地绑进生成：state 已批 **且** 参考图文件**真的在**。

        第二半不能省 —— 状态说 REF 但文件被删/路径变了，是最难查的一类故障：
        闸门放行、生成照跑、结果用了个不存在的参考。宁可在闸门处多一次 `is_file()`。
        """
        if self.state not in BINDABLE or not self.refs:
            return False
        return any(Path(r).expanduser().is_file() for r in self.refs)

    @property
    def primary(self) -> str:
        """首选参考图路径（空串表示还没有）。"""
        return self.refs[0] if self.refs else ""


@dataclass
class CastLock:
    """定妆库：人物 + 场景 + 道具。"""

    characters: dict[str, CastEntry] = field(default_factory=dict)
    locations: dict[str, CastEntry] = field(default_factory=dict)
    props: dict[str, CastEntry] = field(default_factory=dict)
    style: str = ""
    version: int = 1

    def all_entries(self) -> dict[str, CastEntry]:
        return {**self.characters, **self.locations, **self.props}

    def bindable_characters(self) -> set[str]:
        return {k for k, v in self.characters.items() if v.bindable}

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "style": self.style,
            "characters": {k: v.to_dict() for k, v in self.characters.items()},
            "locations": {k: v.to_dict() for k, v in self.locations.items()},
            "props": {k: v.to_dict() for k, v in self.props.items()},
        }


# ---- 读写 ------------------------------------------------------------------


def cast_dir(config=None, override: str | Path | None = None) -> Path:  # noqa: ANN001
    """定妆库目录：显式 override > `[cast].dir` > `<素材库>/_cast` > 仓库根 `cast/`。

    中间那一层是给"多本书"用的：配了 `[paths].lib` 之后不用再逐个填路径，
    定妆库自然落在**那本书的素材库**里（跨集共享，跟着书走）。
    """
    if override:
        return Path(override).expanduser()
    if config is not None:
        value = config.get("cast.dir")
        if value:
            return Path(str(value)).expanduser()
        from lvs.config import lib_dir

        lib = lib_dir(config)
        if lib is not None:
            return lib / LIB_CAST_DIRNAME
    return PROJECT_ROOT / DEFAULT_CAST_DIRNAME


def find_card(config) -> Path | None:  # noqa: ANN001
    """定妆卡路径：`[cast].card` > `<素材库>/00-设定/*定妆卡*.md`。

    自动探测是因为各书的文件名不完全一致（`00-风格与人物定妆卡.md`、
    `00-人物定妆卡.md`…），要求用户填全路径是多余的成本 —— 但只要**探测到多个**
    就明确不猜（宁可报错让人指定，也不要静默挑错一份，那会污染整个定妆库）。
    """
    if config is None:
        return None
    explicit = config.get("cast.card")
    if explicit:
        p = Path(str(explicit)).expanduser()
        return p if p.is_file() else None

    from lvs.config import lib_dir

    lib = lib_dir(config)
    if lib is None:
        return None
    setting = lib / "00-设定"
    if not setting.is_dir():
        return None

    hits: list[Path] = []
    for pattern in ("*定妆卡*.md", "*定妆*.md"):
        hits.extend(sorted(p for p in setting.glob(pattern) if p.is_file()))
    seen: list[Path] = []
    for p in hits:
        if p not in seen:
            seen.append(p)
    if len(seen) == 1:
        return seen[0]
    if len(seen) > 1:
        print(
            "[定妆卡] 00-设定 下有多份像定妆卡的文件，**不猜**："
            + "、".join(p.name for p in seen)
            + "\n        请用 `[cast].card` 指定用哪一份。"
        )
    return None


def lock_path(directory: Path) -> Path:
    return directory / "lock.json"


def registry_path(directory: Path) -> Path:
    return directory / "cast.json"


def load_lock(directory: Path) -> CastLock:
    """读定妆库；不存在返回空库（不抛 —— 调用方按"什么都没批"处理）。"""
    path = lock_path(directory)
    if not path.is_file():
        return CastLock()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CastError(f"定妆库损坏，无法解析：{path}\n  {exc}") from exc
    return CastLock(
        characters={k: CastEntry.from_dict(v) for k, v in (data.get("characters") or {}).items()},
        locations={k: CastEntry.from_dict(v) for k, v in (data.get("locations") or {}).items()},
        props={k: CastEntry.from_dict(v) for k, v in (data.get("props") or {}).items()},
        style=str(data.get("style") or ""),
        version=int(data.get("version") or 1),
    )


def save_lock(directory: Path, lock: CastLock) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = lock_path(directory)
    path.write_text(
        json.dumps(lock.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def save_registry(directory: Path, entries: dict[str, CastEntry]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = registry_path(directory)
    payload = {
        "version": 1,
        "updated_at": _now(),
        "characters": {k: v.to_dict() for k, v in entries.items()},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_registry(directory: Path) -> dict[str, CastEntry]:
    path = registry_path(directory)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: CastEntry.from_dict(v) for k, v in (data.get("characters") or {}).items()}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---- 提取 ------------------------------------------------------------------


def extract_slots(text: str) -> set[str]:
    """从拍摄稿里抓所有 `{NAME}` 人物槽位。"""
    return set(SLOT.findall(text or ""))


def slots_of_shots(shots: Iterable[dict[str, Any]]) -> dict[str, list[int]]:
    """逐镜抓人物槽位 → `{NAME: [镜号…]}`（按镜号排序）。

    同时看 `visual` 与 `prompt` —— 前者是拍摄稿原话，后者可能被 LLM 改写后保留/丢失槽位，
    两处取并集才不会漏。
    """
    found: dict[str, set[int]] = {}
    for shot in shots or []:
        sid = int(shot.get("id") or 0)
        blob = f"{shot.get('visual') or ''} {shot.get('prompt') or ''} {shot.get('scene') or ''}"
        for name in SLOT.findall(blob):
            found.setdefault(name, set()).add(sid)
    return {k: sorted(v) for k, v in sorted(found.items())}


def slots_of_task(ws, config=None) -> list[str]:  # noqa: ANN001
    """本集用到的槽位名（已排序）。`shots.json` 优先，退到拍摄稿 `source.md`。

    为什么两处都看：拆镜后 `shots.json` 才是"真的会出图的画面"，
    但拆镜之前（G0 之后、G1 之前）只有拍摄稿 —— 那时也得能判"本集要哪些人"。
    """
    names: set[str] = set()
    shots = _shots_of(ws)
    if shots:
        names.update(slots_of_shots(shots))
    if not names and ws is not None:
        src = ws.path("source.md")
        if src.is_file():
            names.update(extract_slots(src.read_text(encoding="utf-8", errors="replace")))
    return sorted(names)


def slots_fingerprint(ws, config=None) -> tuple[str, int]:  # noqa: ANN001
    """G2（定妆门）的指纹：**只算本集槽位**，返回 `(指纹, 槽位数)`。

    ★ 为什么不指纹全库 `_cast/lock.json`（2026-10-06 实测的坑）：

    `pipeline.subjects()` 对 G2 返回的是**全库共享**的 `_cast/lock.json` 一个文件，
    而 `artifact.fingerprint()` 只看 stat、不看内容。于是**任意一集**增删人物都会改写它 ——
    给 UGE08 加 4 人、给 UGE09 加 2 人，各触发一轮 UGE01-UGE09「定妆已失效」，
    逼人对九集做九次毫无意义的重新批准。而"批准"唯一的成本就是**批到最后没人再看**。

    本集真正该管的只有两件事：**这一集用到哪些人**、**这些人的锚定与参考图变了没有**。
    所以指纹 = 本集槽位名 + 该人 state + 锚定文本 + 参考图 stat 签名 + 禁项。

    边界：
      · 有拍摄稿/分镜表但**一个槽位都没有**（纯空镜、纯图文卡集）→ 返回哨兵指纹，
        让"批准"仍能进行（这类集的 G2 本来就是走过场）；
      · 连拍摄稿都没有 → 返回空串 + 0，调用方据此**拒绝批准** ——
        免得批出一个"本集有什么人都还不知道"的定妆门。
    """
    from lvs.artifact import entries, signature

    names = slots_of_task(ws, config)
    if not names:
        has_manuscript = ws is not None and (
            ws.path("source.md").is_file() or ws.path("shots.json").is_file()
        )
        return (signature("<no-slots>"), 1) if has_manuscript else ("", 0)

    lock = load_lock(cast_dir(config))
    parts: list[str] = []
    for name in names:
        entry = lock.characters.get(name)
        if entry is None:
            parts.append(f"{name}|<未登记>")
            continue
        refs = "、".join(entries([Path(r) for r in entry.refs]))
        notes = "、".join(entry.negative_notes or [])
        parts.append(f"{name}|{entry.state}|{entry.anchor.strip()}|{refs}|{notes}")
    return signature(*sorted(parts)), len(parts)

def parse_card(text: str) -> dict[str, CastEntry]:
    """从定妆卡 Markdown 抽出人物锚定描述。

    支持两种写法（我们的两条线各用了一种）：

    1. **JSON 块**：```json 里含 `characters` / `locations` / `props`
    2. **小节 + 代码块**：`### 角色名（…）` 标题下，
       `锚定描述：` 标签后的第一个 ``` 块就是锚定文本

    两种都认，因为两种写法都已有成品定妆卡 —— 要求用户改格式是不可接受的成本。
    """
    entries: dict[str, CastEntry] = {}

    # --- 写法 1：JSON 块 ---
    for m in _RE_JSON_BLOCK.finditer(text or ""):
        try:
            data = json.loads(m.group("body"))
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        chars = data.get("characters")
        if isinstance(chars, dict):
            for key, val in chars.items():
                if isinstance(val, dict):
                    entry = CastEntry.from_dict(val)
                    entry.display = entry.display or str(val.get("display") or val.get("name") or key)
                    entries.setdefault(str(key), entry)
                elif isinstance(val, str) and val.strip():
                    entries.setdefault(str(key), CastEntry(display=str(key), anchor=val.strip(),
                                                           state=STATE_IMG))

    # --- 写法 2：小节 + 代码块 ---
    lines = (text or "").splitlines()
    i = 0
    while i < len(lines):
        m = _CARD_CHAR_HEAD.match(lines[i].strip())
        if not m:
            i += 1
            continue
        display = m.group("name").strip()
        extra = (m.group("extra") or "").strip()
        alias = (m.group("alias") or "").strip()
        if extra:
            # ★ `### 屋秋津 / 海盗（{PIRATE}）` 这类标题：斜杠后的常用名必须收成别名。
            # 不收的话「本集出现」判定搜不到「海盗」二字（标题解析又早退），
            # 整节人物在库里凭空消失 —— 正是本函数上一次踩的坑的下一层。
            alias = f"{extra} / {alias}" if alias else extra
        level = _heading_level(lines[i])

        # 收集本小节。★ 边界按**标题层级**判：遇到同级或更高级的标题就停。
        # 早期版本只认"长得像人名"的标题，于是 `### 反廉价感` 会一路穿到
        # `## 三、人物定妆卡` 下面的引文里，把 `> 用法：…把「锚定描述」整段照抄…`
        # 当成锚定描述，凭空造出一个叫 MOTIF 的人物（真机上就这么飘了）。
        j = i + 1
        anchors: list[str] = []
        pronouns: list[str] = []
        saw_label = False
        pending_label = False
        while j < len(lines):
            nxt = lines[j].strip()
            nxt_level = _heading_level(nxt)
            if nxt_level and nxt_level <= level:
                break
            # 引文行是**说明文字**，不是字段。`> 用法：…锚定描述…` 这种
            # 提到关键词的引文不能算"这一段有锚定描述"。
            if nxt.startswith(">"):
                j += 1
                continue
            if _CARD_PRONOUN_LABEL.match(nxt):
                pronouns = _split_pronouns(re.sub(r"^[^：:]*[：:]\s*", "", nxt))
                j += 1
                continue
            if _CARD_ANCHOR_LABEL.search(nxt):
                saw_label = True
                pending_label = True
                # 标签同一行后面可能还有内容（`锚定描述：待核对（第 6 章）`）
                after = re.sub(r"^[^：:]*[：:]\s*", "", nxt)
                if after and not after.startswith("`"):
                    anchors.append(after)
                j += 1
                continue
            if _FENCE.match(nxt) and pending_label:
                j += 1
                buf: list[str] = []
                while j < len(lines) and not _FENCE.match(lines[j].strip()):
                    buf.append(lines[j])
                    j += 1
                block = "\n".join(buf).strip()
                if block:
                    anchors.append(block)
                pending_label = False
                continue
            j += 1
        i = j if j > i else i + 1

        # ★ 只认**写了锚定描述**的小节。定妆卡里还有 `### 风格前缀` / `### A 类（递进轨）`
        # 这类同为三级的说明节 —— 不排除的话它们会变成没有锚定的"人物"，污染定妆库。
        if not display or not saw_label or display in entries:
            continue
        key = _id_from_display(display, alias)
        entries.setdefault(
            key,
            CastEntry(
                display=display,
                anchor=anchors[0] if anchors else "",
                anchor_variants=anchors,
                aliases=_split_aliases(alias),
                pronouns=pronouns,
                state=STATE_IMG if anchors else STATE_PLAN,
            ),
        )
    return entries


def _split_pronouns(text: str) -> list[str]:
    """`我、他` → `["我", "他"]`。只收 1–2 字的词，且去重。

    先剥掉括号里的**说明**再做判断 —— 定妆卡里自然会写成
    `- 代词：我（第一人称叙述者，全书唯一）`，不剥的话整串长度超标、
    结果一个代词都收不进来（第一次跑就是这么静默失败的）。
    """
    cleaned = re.sub(r"[（(][^）)]*[）)]", "", text or "")
    out: list[str] = []
    for piece in re.split(r"[、,，/／;；|·\s]+", cleaned):
        token = piece.strip().strip("\"'“”「」《》()（）")
        if 1 <= len(token) <= 2 and token not in out:
            out.append(token)
    return out


def pronoun_map(
    registry: dict[str, CastEntry],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """把"代词 → 人物"分成两堆：**能唯一确定的** 与 **有歧义的**。

    返回 `(resolved, ambiguous)`：
    - `resolved["他"] = "渡边彻"` —— 只有一个人声明了 `他`，机器可以放心换
    - `ambiguous["他"] = ["渡边彻", "木月"]` —— 两个人都有可能是"他"，**不能猜**

    为什么不在有歧义时"挑一个最可能的"：代词是人写的指代，挑错等于给那一镜
    注入**另一个人的脸**。这种错不会报错、不会崩，只会悄悄产出错图 —— 正是
    本项目最怕的一类故障。所以有歧义一律交给人（列进迁移报告）。

    这条规矩的例外只有一个：**自称**（`我`/`我...`）在同一篇稿子里通常唯一。
    但也不特殊照顾 —— 一样按"唯一才换"处理。
    """
    claims: dict[str, list[str]] = {}
    for name, entry in registry.items():
        for p in entry.pronouns:
            if p and name not in claims.setdefault(p, []):
                claims[p].append(name)
    resolved = {p: ids[0] for p, ids in claims.items() if len(ids) == 1}
    ambiguous = {p: ids for p, ids in claims.items() if len(ids) > 1}
    return resolved, ambiguous


# ---- 锚定描述体检（`lvs cast --lint`） ---------------------------------------

# 为什么要有它：锚定描述是"跨镜不变的那张脸"的唯一来源。它写得不可画，
# 后面几百张图就一起歪 —— 而这类问题**看代码看不出来，看定妆照也不一定看得出**
# （四张候选各不相同，人只会觉得"随机性真大"，不会想到是锚定写法的问题）。
#
# 判据全部来自本项目实机踩过的坑，不是凭空想的。
ANCHOR_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "A-measure",
        re.compile(
            r"\b(?:about|around|roughly|approximately|some|nearly|just)?\s*"
            r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
            r"(?:\s*(?:-|–|to|or)\s*(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten))?"
            r"\s*(?:cm|centimetres?|centimeters?|mm|inches?|inch|feet|foot|metres?|meters?|"
            r"kilos?|kg|pounds?|lbs?)\b",
            re.IGNORECASE,
        ),
        "用量度数字描述外观 —— 模型画不出「四五公分」。"
        "改成**可画的物理属性**：「耳朵与后颈完全露出」而不是「剪到四五公分」。",
    ),
    (
        "A-procedure",
        re.compile(
            r"\bhair\s+(?:is\s+|was\s+)?(?:cut|trimmed|shaved|cropped|grown)\b"
            r"|\b(?:had|has|having)\s+(?:his|her|their)\s+hair\b",
            re.IGNORECASE,
        ),
        "用「剪发/蓄发动作」描述外观 —— 模型看到的是动作，不是结果。"
        "改成结果态：`very short cropped hair, ears and nape fully exposed`。",
    ),
    (
        "A-simile",
        re.compile(r"\bas if\b|\bas though\b|\bseem(?:s|ed)? to\b|\blook(?:s|ed)? like\b", re.IGNORECASE),
        "抽象比喻/从句 —— cfg=1.0 的模型按字面画。锚定描述里只留**能画出来的名词与形容词**，"
        "比喻留给评述文案。",
    ),
)

# 「表情/情绪词」：它们**不该**出现在锚定描述里。
# 为什么：锚定描述承诺的是"跨镜不变的那张脸"，而表情是**逐镜变化**的
# （这一镜笑、下一镜哭，是正常分镜）。表情词混进锚定，模型会把表情和五官
# 放在同一层重新采样 —— 于是同一段锚定，四张候选的四张脸、四种表情一起飘。
# 木月就是这么飘的：锚定里有 `a faintly cool smile`，而出定妆照用的构图是
# 「中性胸像、平光」，两者在语义上打架，模型只能每张各解一次。
_EXPRESSION_WORDS = re.compile(
    r"(?<![A-Za-z])(?:smil(?:e|es|ing)|smirk(?:ing)?|grin(?:s|ning)?|laugh(?:s|ing)?|"
    r"frown(?:s|ing)?|cry(?:ing)?|weep(?:ing)?|tearful|"
    r"angry|sad|sadness|happy|cheerful|joyful|gloomy|melancholy|"
    r"worried|anxious|nervous|scared|frightened|surprised|puzzled|confused|"
    r"glaring|staring|scowling|pouting|pensive|expressionless|"
    r"expression|mood|demeanou?r|temperament|微笑|冷笑|苦笑|笑容|大笑|含笑|哭泣|流泪|眼泪|泪痕|皱眉|蹙眉|愤怒|悲伤|忧伤|忧郁|忧愁|开心|高兴|愉快|兴奋|紧张|焦虑|害怕|惊恐|惊讶|困惑|茫然|发呆|神情|表情|神色|神态|情绪|气色)(?![A-Za-z])",
    re.IGNORECASE,
)

# 词数上限：超过它，模型会把这些属性**逐条独立采样**，
# 于是同一个锚定出四张不同的脸（木月就是这么飘的：7 条互相独立的属性）。
ANCHOR_MAX_WORDS = 55

# 「服装/道具词」：不该占锚定描述的地盘。
# 为什么：锚定的职责是**锁脸**。服装与道具是**逐镜可变**的（同一人换三套衣服很正常），
# 写进锚定等于让模型把采样预算分给衬衫和书包 —— 脸就飘了，而且**构图也会被带跑**
# （突撃隊的锚定里有 5 个服装道具词，出图直接变成全身站姿，与"中性胸像"的定妆构图打架）。
#
# ★ 这条规则曾经被我删过一次，理由是"它在木月上不触发" —— 那是**测错了对象**。
#   它抓不到木月（木月的问题是表情词），但它正好抓住突撃隊。
#   一条规则不命中某个案例，不等于规则不成立。
_PROP_WORDS = re.compile(
    r"(?<![A-Za-z])(?:shirt|t-shirt|tshirt|sweater|jumper|cardigan|jacket|coat|blazer|"
    r"overcoat|trousers|pants|jeans|shorts|skirt|dress|uniform|suit|tie|scarf|belt|"
    r"gloves?|hat|cap|shoes?|sneakers?|boots?|socks?|bag|backpack|umbrella|watch|ring|"
    r"earrings?|necklace|bracelet|glasses|sunglasses|cigarette|pipe|book|cup|glass|bottle|衬衫|衬衣|毛衣|针织衫|外套|大衣|风衣|西装|夹克|长裙|短裙|连衣裙|长裤|短裤|牛仔裤|校服|制服|和服|旗袍|围巾|领带|手套|帽子|皮鞋|球鞋|运动鞋|靴子|袜子|书包|背包|手提包|雨伞|手表|戒指|耳环|项链|手链|墨镜|香烟|烟斗|杯子|玻璃杯|瓶子)"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)

# 「人称名词」：锚定描述里**必须至少有一个**。
# 为什么这是硬判据：把特征前置时很容易顺手把主语丢掉 ——
# 第一版 MIDORI 改写成 "a very short cropped haircut with the ears ... exposed,
# a small neat oval face ..., a slight slender build ..., tomboyish rather than girlish"，
# 全句没有一个「girl / woman / person」。模型于是自己决定画谁，给出了一个**男性**。
# 这是"为了修一个问题而引入另一个"的典型，所以用一条规则把它钉死。
_PERSON_WORDS = re.compile(
    r"(?<![A-Za-z])(?:man|men|woman|women|boy|girl|person|people|youth|teen|teenager|"
    r"student|male|female|guy|lady|gentleman|figure|child|kid|adult|elderly|senior|"
    # ★ 2026-10-06 补：身份类名词（nobleman / courtier / monk …）。缺了它们，
    # `{KIYOYUKI}`「a Japanese nobleman in his sixties with …」与 `{SORIN}`
    # 「a Japanese courtier in his thirties with …」会被误报 A-nosubject ——
    # 而它们主语齐全、完全可画。**假阳性比漏报更贵**：报警多了人就学会无视。
    # 只收「只能当名词」的词；official / noble 这类兼作形容词的一律不收
    # （收了会把「official robes」这种真丢主语的写法放过）。
    r"nobleman|noblewoman|courtier|monk|nun|priest|scholar|warrior|samurai|soldier|"
    r"merchant|servant|attendant|retainer|aristocrat|peasant|farmer|fisherman|sailor|"
    r"hunter|thief|beggar|lad|lass|maiden|squire|craftsman|blacksmith|painter|poet|"
    r"widow|orphan|nurse|teacher|"
    r"男人|男子|男生|男孩|女人|女子|女生|女孩|少年|少女|青年|壮年|中年|年轻人|老人|老者|长者|人影|身影|人物|学生|孩子|孩童|儿童|妇人|姑娘|老头|老太|僧人|和尚|武士|商人|学者|贵族|公卿|侍从|农夫|渔夫|猎人|盗贼|乞丐|文士|画师|尼姑)"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)

# 「骨相词」：出现在**第一小句**里，才算"把可画的特征放在最前"。
# 为什么第一小句特别重要：cfg=1.0 的模型对提示词**前段**的权重显著更高。
# 若开头是 "a lively Japanese college girl around 19" 这类身份交代，
# 模型会把采样预算花在"活泼的大学生"这种抽象气质上，而不是"什么发型什么脸"。
# 这是 MIDORI 那一版头发盖住耳朵的根因（锚定卡把发型写在第二小句）。
_FACE_WORD_LIST: tuple[str, ...] = (
    "hair", "haircut", "bangs", "fringe", "ponytail", "braid", "curls?", "bald", "shaved", "face",
    "eyes?", "eyebrows?", "brows?", "jaw", "chin", "cheek(?:bone)?s?", "nose", "mouth", "lips?",
    "skin", "complexion", "build", "height", "tall", "short", "slim", "slender", "lean", "stocky",
    "lanky", "beard", "moustache", "scar", "mole", "freckles?", "glasses", "wrinkles?", "发型", "头发",
    "发丝", "刘海", "鬓角", "辫子", "马尾", "卷发", "光头", "寸头", "平头", "短发", "长发", "脸型", "脸庞", "面庞", "面容", "面孔",
    "鹅蛋脸", "方脸", "圆脸", "瘦脸", "五官", "眼睛", "双眼", "眼眸", "瞳色", "眉毛", "浓眉", "下颌", "下巴", "颧骨", "脸颊", "鼻梁",
    "鼻子", "嘴巴", "嘴唇", "肤色", "皮肤", "身形", "身材", "体格", "体型", "个子", "皱纹", "痣", "疤痕", "伤疤", "胡须", "络腮胡",
    "胡子", "shaven"
)

def _face_words(*, derive: bool) -> re.Pattern[str]:
    """由**同一份**词表编出两个正则；不要各自维护一份（这项目吃过这亏）。

    两者的差别只在**尾后缀**与**边界**：

    - `derive=False`（`_FACE_WORDS`，计数用）：词根本身，边界等于 `\b`。
    - `derive=True`（`_FACE_WORDS_FIRST`，判「特征有没有放最前」）：额外认
      `-ed/-en/-s` 派生词与**连字符复合词**，并且边界改成「两侧不是拉丁字母」。

    ★ 为什么必须多这一档（2026-10-06 踩坑）：`{JUJI}` 的锚定写成
      `a shaven-headed Japanese man in his forties, hollow-cheeked, ...` ——
      特征明明在最前，但 `shaven-headed`/`hollow-cheeked` 是**连字符复合词**，
      `\bshaved\b`、`\bcheek\b` 一个都不命中，`lint` 于是误报 `A-order`
      （"第一小句里没有可画的外貌特征"）。**特征前置的正确写法反而被报警**，
      报警多了人就学会无视它 —— 那比不报更糟。

    ★ 顺带修掉的第二个坑：`\b` 对中文**基本不成立**（汉字本身是 word 字符，
      `\b脸型` 只在行首或标点后才命中）。所以中文锚定里"脸型/眼睛/浓眉"这些词
      原先几乎一律数不到。两侧改成「非拉丁字母」后中文才算得进来。
    """
    joined = "|".join(sorted(_FACE_WORD_LIST, key=len, reverse=True))
    tail = r"(?:s|es|ed|en|d)?" if derive else ""
    return re.compile(rf"(?<![A-Za-z])(?:{joined}){tail}(?![A-Za-z])", re.IGNORECASE)


_FACE_WORDS = _face_words(derive=False)
_FACE_WORDS_FIRST = _face_words(derive=True)



def lint_anchor(anchor: str, *, max_words: int = ANCHOR_MAX_WORDS) -> list[dict[str, str]]:
    """体检一段锚定描述。返回 `[{code, snippet, why}]`（空列表 = 通过）。

    只报告、不自动改写 —— 改法要看上下文（"剪到四五公分"改成"耳朵露出"还是
    "紧贴头皮"，取决于这个角色长什么样）。机器给判据，人来定夺。
    """
    from lvs import prompting

    text = (anchor or "").strip()
    if not text:
        return [{"code": "A-empty", "snippet": "", "why": "锚定描述是空的 —— 这个角色无法冻结参考。"}]

    out: list[dict[str, str]] = []
    for code, pattern, why in ANCHOR_RULES:
        m = pattern.search(text)
        if m:
            out.append({"code": code, "snippet": m.group(0), "why": why})

    # 特征是否放在最前（判据：第一小句里有没有可画的骨相词）
    first = re.split(r"[,;，；]", text, maxsplit=1)[0]
    if not _FACE_WORDS_FIRST.search(first):
        out.append({
            "code": "A-order",
            "snippet": first.strip()[:40],
            "why": "第一小句里没有可画的外貌特征。cfg=1.0 的模型对**前段**权重更高，"
                   "开头放身份交代（「一个活泼的大学生」）会把采样预算花在气质而不是脸上。"
                   "把最**可画、最能区分**的那条特征提到最前（发型 / 脸型 / 眼睛 / 体型），"
                   "再补身份。",
        })

    # 有没有「人」这个名词（丢了主语 = 模型自己决定画谁）
    if not _PERSON_WORDS.search(text):
        out.append({
            "code": "A-nosubject",
            "snippet": text[:40],
            "why": "整段锚定描述里没有任何人称名词（girl / woman / man / student…）。"
                   "把特征前置时很容易连主语一起丢掉 —— 模型于是自己决定画谁"
                   "（第一版 MIDORI 被画成了男性）。写法：**主语 + with + 特征**，"
                   "如 `a Japanese college girl around 19 with a very short cropped haircut, "
                   "ears and nape fully exposed`。",
        })

    # 表情/情绪词混进锚定（锚定承诺"跨镜不变"，表情逐镜变化）
    m = _EXPRESSION_WORDS.search(text)
    if m:
        out.append({
            "code": "A-expression",
            "snippet": m.group(0),
            "why": "锚定描述里出现了表情/情绪词。表情是**逐镜变化**的（这一镜笑、下一镜凝重，"
                   "本来就是正常分镜），写进锚定会让模型把表情和五官放在同一层重新采样 —— "
                   "同一段锚定能出四张脸、四种表情。定妆照用的构图是「中性胸像、平光」，"
                   "和表情词语义打架，模型只能每张各解一次。"
                   "删掉表情，只留不变的五官与轮廓；表情交给分镜描述。",
        })

    # 服装/道具词是否盖过骨相词（锚定要锁脸，不是写穿搭）
    face_hits = len(_FACE_WORDS.findall(text))
    prop_hits = len(_PROP_WORDS.findall(text))
    if prop_hits and prop_hits >= face_hits:
        out.append({
            "code": "A-diluted",
            "snippet": f"骨相词 {face_hits} vs 服装/道具词 {prop_hits}",
            "why": "服装与道具的词不少于骨相词 —— 它们会被模型独立采样，后果有两层："
                   "① 同一段锚定能出四张不同的脸；"
                   "② **构图被带跑**（突撃隊的锚定列了衬衫/长裤/毛衣/皮鞋/书包 5 件衣物，"
                   "出图直接变成全身站姿，和「中性胸像」的定妆构图打架）。"
                   "锚定的职责是锁脸；服装与道具是逐镜可变的，移到分镜描述里写。",
        })

    # 否定式：与 `lvs check` / `lvs migrate` **同源**（prompting.NEGATION_RULES）。
    # 两处各写一套必然漂移，用户就不知道该信哪个。
    for code, snippet, why in prompting.find_negations(text):
        out.append({"code": f"A-neg:{code}", "snippet": snippet,
                    "why": f"否定式（{why}）—— 锚定描述里只能写**画面里有什么**。"})

    words = len(text.split())
    if words > max_words:
        out.append({
            "code": "A-bloated",
            "snippet": f"{words} words",
            "why": f"属性太多（上限 {max_words}）。这些属性会被**逐条独立采样**，"
                   "同一个锚定能出四张不同的脸。挑 4–6 条**骨相级**特征"
                   "（脸型 / 眼 / 发 / 轮廓 / 体型）留下，服装与道具交给分镜描述。",
        })
    return out



def _split_aliases(alias: str) -> list[str]:
    """把括号里的别名串拆成可用作检索的词：`"敢死队"，渡边的宿舍室友` → `["敢死队"]`。

    只留 2–8 字、**不含「的」**的片段。为什么用「的」当判据：括号里混着
    "渡边的宿舍室友"这类**说明**（不是别名），它们永远搜不到，反而掩盖真别名。
    早期版本用"以「渡边」开头就丢"当判据 —— 结果连真别名 `渡边`（渡边彻的简称）
    也一起丢了。而画面位里大量用的正是简称，丢掉的后果是那一镜**根本不注入锚定**，
    且不报错（槽位少一个不会让任何人察觉）。
    """
    if not alias:
        return []
    raw = re.split(r"[/／，,、;；|]", alias)
    out: list[str] = []
    for piece in raw:
        token = piece.strip().strip('"\'“”「」《》()（）{}')
        if not token:
            continue
        if 2 <= len(token) <= 8 and "的" not in token:
            if token not in out:
                out.append(token)
    return out


def entry_matches_text(entry: CastEntry, text: str) -> bool:
    """该人物是否出现在这段正文里（按显示名与别名分别搜）。"""
    if not text:
        return False
    names = [entry.display, *entry.aliases]
    return any(n and n in text for n in names)


def _id_from_display(display: str, alias: str = "") -> str:
    """给人读的显示名推一个 ID。

    优先级：**括号里显式写下的 `{SLOT}`** → 拉丁别名 → 显示名本身。

    ★ 2026-10-06 修的坑：早期版本只认拉丁别名，于是定妆卡里明明白白写着的
    `{SORIN}` / `{KOMACHI}` / `{KIYOYUKI}` 被同一个括号里的罗马字盖掉 ——
    `良岑宗贞（… / Munesada / 遍昭 / 僧正遍昭 / {SORIN}）` 登记成了 `MUNESADA`。
    后果是**静默**的：拍摄稿里的 `{SORIN}` 变成一个库里没有的幽灵槽位
    （PLAN、无锚定），`apply_slots` 绑定不上、`gate_missing` 报缺人、
    定妆候选一张也渲染不出来，而错因只看得到"锚定描述为空"。
    槽位名是作者显式声明的契约，不该由罗马字去猜。
    """
    for src in (alias, display):
        m = SLOT.search(src or "")
        if m:
            return m.group(1)
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}", alias or ""):
        low = token.lower()
        if low in {"san", "kun", "chan", "the"}:
            continue
        return token.upper()
    latin = re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}", display or "")
    if latin:
        return latin[-1].upper()
    return display.strip()


# ---- 注入与闸门 ------------------------------------------------------------


def apply_slots(text: str, lock: CastLock) -> str:
    """把 `{NAME}` 替换成冻结的锚定描述。未登记的名字原样保留（让错误显形）。"""
    if not text:
        return text

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        entry = lock.characters.get(name)
        if entry and entry.anchor:
            return entry.anchor.strip()
        return m.group(0)

    return SLOT.sub(sub, text)


def gate_missing(shots: Iterable[dict[str, Any]], lock: CastLock) -> dict[str, list[int]]:
    """返回「出现在本集但**没能绑定**的人物」→ 镜号清单。

    空 dict = 闸门放行。`assets` 靠它决定跑不跑。
    """
    missing: dict[str, list[int]] = {}
    for name, ids in slots_of_shots(shots).items():
        entry = lock.characters.get(name)
        if entry is None or not entry.bindable:
            missing[name] = ids
    return missing


def gate_message(missing: dict[str, list[int]], lock: CastLock, directory: Path) -> str:
    """未通过闸门时的用户提示：说清**怎么修**，而不是只报错。

    命令里写**真实的人物 ID**而不是 `<NAME>` 占位符 —— 用户要的是能直接粘贴的东西。
    """
    names = sorted(missing)
    lines = [
        "✗ 出图闸门未通过：以下人物还没有冻结的定妆参考，生成的分镜图会变脸。",
        "",
    ]
    for name in names:
        ids = missing[name]
        entry = lock.characters.get(name)
        state = entry.state if entry else "未登记"
        shown = "、".join(str(i) for i in ids[:12]) + (" …" if len(ids) > 12 else "")
        lines.append(
            f"  · {name}（{entry.display if entry else '—'}）state={state}"
            f"  出现在 {len(ids)} 镜：{shown}"
        )
    lines += [
        "",
        f"  定妆库：{directory}",
        "  下一步：",
        "    1) lvs cast --task <任务名> --extract        # 从拍摄稿/定妆卡提取人物",
        f"    2) lvs cast --render {','.join(names)}        # 出候选定妆照",
        f"    3) lvs cast --approve {','.join(names)}       # 审阅通过后冻结",
        "",
        "  为什么必须这样：没有冻结的脸，本集图全部作废（2026-10-01 实测 479 张）。",
        "  确要跳过（例如纯空镜集）：给 `lvs assets --no-cast-gate`。",
    ]
    return "\n".join(lines)


# ---- 出候选定妆照 ----------------------------------------------------------


@dataclass
class RenderResult:
    character: str
    version: int
    files: list[Path] = field(default_factory=list)
    failed: int = 0
    errors: list[str] = field(default_factory=list)


# 画幅词：定妆照的画幅由 `[cast].width/height`（默认 1024×1024 方图）决定，
# 而风格后缀是从**场景图**预设继承来的，里面往往写着 `16:9`。
# 两者在提示词里同时出现 = 语义打架，模型只能猜 —— 定妆照不需要这个不确定性。
_ASPECT_TOKEN = re.compile(r"(?<![\w:])[1-9]\d?\s*[:：]\s*(?:[1-9]\d?)(?![\w:])")


def strip_aspect(text: str) -> str:
    """去掉 `16:9` / `9:16` / `1:1` 这类画幅词，并收拾干净留下的残渣。

    残渣要收拾，是因为这些词夹在逗号之间（`grayscale, 16:9`），
    直接删会留下 `grayscale, `、`grayscale, .` 这种断句 —— 提示词里的破标点
    会让模型对"这句还没完"产生歧义。删干净比留着好。
    """
    out = _ASPECT_TOKEN.sub("", text or "")
    out = re.sub(r"\s{2,}", " ", out)                 # 折叠多空格
    out = re.sub(r"\s+([,;.、，；])", lambda m: m.group(1), out)  # 标点前不留空格
    out = re.sub(r"([,;、，；])\s*(?=[,;.、，；]|$)", "", out)  # 空标点成对吃掉
    out = re.sub(r"\s{2,}", " ", out)
    return out.strip(" ,;、，；.")


def cast_prompt(entry: CastEntry, config=None, style_suffix: str = "") -> str:  # noqa: ANN001
    """定妆照提示词：锚定描述 + 定妆专用构图 + 风格后缀。

    定妆照与场景图**刻意用不同的构图**：平光、正视、素背景 —— 目标是"看清楚这张脸"，
    而不是"好看"。风格的戏剧光留到场景图再用。
    """
    core = (entry.anchor or entry.display or "").strip().rstrip(".")
    framing = config.get("cast.framing") if config is not None else None
    framing = framing or (
        "neutral chest-up portrait, even frontal lighting falling from both sides, "
        "smooth seamless studio backdrop, sharp focus on the facial features, "
        "soft even illumination across the whole face"
    )
    suffix = strip_aspect(style_suffix)
    parts = [core, framing]
    if suffix:
        parts.append(suffix)
    return ". ".join(p for p in parts if p) + "."


def render_candidates(
    name: str,
    entry: CastEntry,
    directory: Path,
    *,
    config,  # noqa: ANN001
    count: int = 4,
    seed: int | None = None,
    style_suffix: str = "",
    width: int | None = None,
    height: int | None = None,
    variant: int = 1,
    client: imagegen.ComfyClient | None = None,
) -> RenderResult:
    """出 `count` 张候选，落 `cast/<NAME>/v<n>/cand-NN.png`。失败隔离：单张失败不影响其余。

    `variant` 选第几段锚定描述（1-based）。定妆卡里一个人可能有两段
    （渡边彻的「青年期」与「37 岁」）—— 两段是两张不同的脸，要分开出、分开审。
    """
    char_dir = directory / name
    version = next_version(char_dir)
    out_dir = char_dir / f"v{version}"
    out_dir.mkdir(parents=True, exist_ok=True)

    anchor = _pick_anchor(entry, variant)
    prompt = cast_prompt(CastEntry(display=entry.display, anchor=anchor), config, style_suffix)
    base_seed = seed
    if base_seed is None:
        base_seed = int(config.get("cast.seed", 7777)) if config is not None else 7777

    cast_workflow = None
    if config is not None:
        cast_workflow = config.get("cast.workflow")
    workflow = cast_workflow or config.get("comfyui.workflow", "zimage_turbo.json")
    model = config.get("comfyui.model", "z_image_turbo_int8_convrot.safetensors")
    base_url = config.get("comfyui.base_url", "http://127.0.0.1:8188")

    extra: dict[str, Any] = {}
    w = width or (int(config.get("cast.width")) if config.has("cast.width") else None)
    h = height or (int(config.get("cast.height")) if config.has("cast.height") else None)
    if w:
        extra["WIDTH"] = w
    if h:
        extra["HEIGHT"] = h
    for cfg_key, token in (("clip", "CLIP"), ("vae", "VAE"), ("negative", "NEGATIVE")):
        val = config.get(f"comfyui.{cfg_key}") if config is not None else None
        if val is not None:
            extra[token] = val

    result = RenderResult(character=name, version=version)
    cli = client or imagegen.ComfyClient(base_url)
    if not cli.health():
        result.errors.append(f"ComfyUI 不可达：{base_url}")
        return result

    for i in range(max(1, count)):
        out = out_dir / f"cand-{i + 1:02d}.png"
        try:
            imagegen.generate(
                prompt,
                out,
                base_url=base_url,
                workflow=workflow,
                model=model,
                seed=int(base_seed) + i,
                extra=extra or None,
                client=cli,
            )
        except imagegen.ComfyError as exc:
            result.failed += 1
            result.errors.append(str(exc).splitlines()[0])
            continue
        result.files.append(out)

    # 候选元数据落盘：seed 与提示词都要留痕，不然复现不了
    meta = {
        "character": name,
        "version": version,
        "variant": variant,
        "at": _now(),
        "prompt": prompt,
        "workflow": workflow,
        "model": model,
        "seeds": [int(base_seed) + i for i in range(max(1, count))][: len(result.files)],
        "files": [str(p) for p in result.files],
    }
    (out_dir / "_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def _pick_anchor(entry: CastEntry, variant: int) -> str:
    """取第 `variant` 段锚定描述（1-based，越界回落到第一段）。"""
    variants = entry.anchor_variants or ([entry.anchor] if entry.anchor else [])
    if not variants:
        return entry.anchor or ""
    idx = max(1, int(variant)) - 1
    return variants[idx] if idx < len(variants) else variants[0]


def next_version(char_dir: Path) -> int:
    """下一个候选版本号 = 已有 `v<n>` 的最大 n + 1。"""
    if not char_dir.is_dir():
        return 1
    nums = []
    for p in char_dir.iterdir():
        m = re.fullmatch(r"v(\d+)", p.name)
        if m and p.is_dir():
            nums.append(int(m.group(1)))
    return max(nums, default=0) + 1


def latest_version(char_dir: Path) -> Path | None:
    """最新的候选版本目录。"""
    if not char_dir.is_dir():
        return None
    best: tuple[int, Path] | None = None
    for p in char_dir.iterdir():
        m = re.fullmatch(r"v(\d+)", p.name)
        if m and p.is_dir():
            n = int(m.group(1))
            if best is None or n > best[0]:
                best = (n, p)
    return best[1] if best else None


# ---- 批准 / 打回 -----------------------------------------------------------


def approve(
    directory: Path,
    lock: CastLock,
    name: str,
    *,
    source: Path | None = None,
    version: int | None = None,
    kind: str = "characters",
    by: str = "user",
) -> list[Path]:
    """把某个候选（或指定文件）提升为正式参考，写入 lock.json。

    `source` 给了就用它；否则取 `version`（缺省取最新）目录里的全部候选。
    复制而非移动 —— 候选目录保留原样，方便回看"当时还有哪些别的样子"。
    """
    table: dict[str, CastEntry] = getattr(lock, kind, lock.characters)
    entry = table.get(name)
    if entry is None:
        entry = CastEntry(display=name, state=STATE_PLAN)
        table[name] = entry

    char_dir = directory / name
    char_dir.mkdir(parents=True, exist_ok=True)

    picked: list[Path] = []
    if source is not None:
        if not Path(source).is_file():
            raise CastError(f"指定的参考图不存在：{source}")
        picked = [Path(source)]
    else:
        ver_dir = char_dir / f"v{version}" if version else latest_version(char_dir)
        if ver_dir is None or not ver_dir.is_dir():
            raise CastError(
                f"{name} 还没有候选定妆照。先跑：lvs cast --render {name}"
            )
        picked = sorted(p for p in ver_dir.glob("*.png") if p.is_file())
        if not picked:
            raise CastError(f"{ver_dir} 里没有 png 候选，先重出：lvs cast --render {name}")

    refs: list[str] = []
    for idx, src in enumerate(picked, start=1):
        dst = char_dir / f"ref-{idx:02d}{src.suffix.lower() or '.png'}"
        shutil.copy2(src, dst)
        refs.append(str(dst))

    entry.refs = refs
    entry.state = STATE_REF
    entry.approved_at = _now()
    entry.approved_by = by
    save_lock(directory, lock)
    return [Path(r) for r in refs]


def reject(
    directory: Path,
    lock: CastLock,
    name: str,
    *,
    version: int | None = None,
    note: str = "",
    kind: str = "characters",
) -> Path:
    """把某轮候选移进 `_rejected/v<n>/` 并记原因。不删 —— 失败样本是下次调参的依据。"""
    table: dict[str, CastEntry] = getattr(lock, kind, lock.characters)
    entry = table.setdefault(name, CastEntry(display=name, state=STATE_PLAN))

    char_dir = directory / name
    ver_dir = char_dir / f"v{version}" if version else latest_version(char_dir)
    if ver_dir is None or not ver_dir.is_dir():
        raise CastError(f"{name} 没有可打回的候选版本")

    dest = char_dir / "_rejected" / ver_dir.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(ver_dir), str(dest))

    entry.rejected.append({"version": ver_dir.name, "note": note, "at": _now()})
    if not entry.refs:
        entry.state = STATE_PLAN
    save_lock(directory, lock)
    return dest


# ---- 审阅表（contact sheet） -----------------------------------------------


def _cjk_font(size: int):
    """找一款系统中文字体。找不到返回 None（退化成英文标签，不阻断）。"""
    try:
        from PIL import ImageFont  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc", "NotoSansCJK-Regular.ttc"):
        path = Path("C:/Windows/Fonts") / name
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size)
            except Exception:  # noqa: BLE001
                continue
    return None


def build_contact_sheet(
    directory: Path,
    *,
    out_path: Path | None = None,
    cell: int = 300,
    latest_only: bool = False,
) -> Path | None:
    """把每个角色每一轮的候选拼成一张审阅表。**一人一版一行**，逐张编号。

    **为什么要有它**：定妆的验收动作是"横向比"。让用户挨个打开 6 个目录、25 张图，
    等于把审阅成本推给最该省力的那一步；一张图看完才是能坚持的流程。

    `latest_only=False` 是默认：同一个人的多段锚定（渡边彻 19 岁 / 37 岁）是**两张不同的脸**，
    只看最新一轮会把前一段直接藏起来 —— 而"两段都在"恰恰是它需要被审的原因。
    """
    try:
        from PIL import Image, ImageDraw  # type: ignore
    except Exception:  # noqa: BLE001
        return None

    chars = sorted([d for d in directory.iterdir() if d.is_dir() and not d.name.startswith("_")])
    rows: list[tuple[str, list[Path]]] = []
    for char_dir in chars:
        versions = sorted(
            (d for d in char_dir.iterdir() if d.is_dir() and re.fullmatch(r"v\d+", d.name)),
            key=lambda d: int(d.name[1:]),
        )
        if not versions:
            continue
        picked_versions = versions[-1:] if latest_only else versions
        for ver in picked_versions:
            files = sorted(ver.glob("*.png"))
            if not files:
                continue
            variant = 1
            meta = ver / "_meta.json"
            if meta.is_file():
                try:
                    variant = int(json.loads(meta.read_text(encoding="utf-8")).get("variant") or 1)
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    variant = 1
            label = f"{char_dir.name}  {ver.name}"
            if len(versions) > 1 or variant > 1:
                label += f"（第 {variant} 段锚定）"
            rows.append((label, files))

    if not rows:
        return None

    cols = max(len(files) for _, files in rows)
    pad, header = 8, 34
    width = max(pad * 2 + 420, pad + cols * (cell + pad))
    height = header + len(rows) * (cell + header)
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    font = _cjk_font(15) or None
    small = _cjk_font(12) or None

    draw.text(
        (pad, 8),
        f"定妆候选审阅表 · {len(rows)} 轮 / {sum(len(f) for _, f in rows)} 张   "
        f"批准：lvs cast --approve <ID>[=vN]  打回：lvs cast --reject <ID>=vN --note \"原因\"",
        fill=(0, 0, 0), font=font,
    )

    for r, (label, files) in enumerate(rows):
        top = header + r * (cell + header)
        draw.text((pad, top - 22), label, fill=(0, 0, 0), font=font)
        for c, f in enumerate(files):
            try:
                with Image.open(f) as img:
                    thumb = img.convert("RGB").resize((cell, cell))
            except Exception:  # noqa: BLE001
                continue
            x = pad + c * (cell + pad)
            canvas.paste(thumb, (x, top))
            draw.rectangle([x, top, x + cell - 1, top + cell - 1], outline=(160, 160, 160))
            draw.text((x + 4, top + 4), f.name, fill=(120, 120, 120), font=small)

    target = out_path or (directory / "_审阅表.png")
    canvas.save(target)
    return target


# ---- 锚定体检的命令前端 ------------------------------------------------------

_CONVENTION = """  好锚定的四条约定（判据来自本项目实机踩坑）：
    1. **最可画的特征放最前** —— 模型对提示词前段权重更高（发型 / 脸型 / 眼睛 / 体型）
    2. **写结果，不写动作** —— `ears exposed` 而不是 `hair cut short`
    3. **写物理属性，不写度量** —— `very short cropped hair` 而不是 `four or five centimetres`
    4. **只写能画的** —— 比喻、气质、"似乎是"一律删；服装与道具交给分镜描述
"""


def _run_lint(lock: CastLock, registry: dict[str, CastEntry], which: str) -> int:
    """`lvs cast --lint [IDS]`：体检锚定描述。返回 0 = 全通过，1 = 有问题。"""
    entries: dict[str, CastEntry] = {**registry, **lock.characters}
    if not entries:
        print("定妆库里没有人物。先跑 `lvs cast --task <任务> --extract`。")
        return 2

    wanted: set[str] | None = None
    token = (which or "").strip()
    if token and token.upper() != "ALL":
        wanted = set()
        for t in (x.strip() for x in token.split(",") if x.strip()):
            if t in entries:
                wanted.add(t)
                continue
            hit = next((k for k, e in entries.items() if e.display == t), None)
            if hit:
                wanted.add(hit)
            else:
                print(f"  · {t}：库里没有这个 ID（跳过）")

    rows = sorted(entries) if wanted is None else sorted(wanted)
    print(f"锚定描述体检 · {len(rows)} 个人物")
    print()
    bad_chars = 0
    bad_items = 0
    for name in rows:
        entry = entries.get(name)
        if entry is None:
            continue
        variants = entry.anchor_variants or ([entry.anchor] if entry.anchor else [])
        if not variants:
            print(f"  ⚠ {name}（{entry.display}）：没有锚定描述（state={entry.state}）")
            bad_chars += 1
            continue
        char_ok = True
        for no, anchor in enumerate(variants, 1):
            findings = lint_anchor(anchor)
            label = f"{name}"
            if len(variants) > 1:
                label += f"（第 {no} 段锚定）"
            if not findings:
                print(f"  ✅ {label}")
                continue
            char_ok = False
            bad_items += len(findings)
            print(f"  ⚠ {label}")
            for f in findings:
                snip = f"「{f['snippet']}」 " if f["snippet"] else ""
                print(f"      · [{f['code']}] {snip}")
                print(f"        {f['why']}")
        if not char_ok:
            bad_chars += 1

    print()
    if bad_chars == 0:
        print("  ✅ 全部通过。可以出候选定妆照了。")
        return 0
    if bad_items == 0:
        # 有锚定的全过了，剩下的只是"还没写锚定"的后续章节角色 —— 说清楚，
        # 别让人以为有 2 条问题要修。
        print(f"  有锚定描述的全部通过；另有 {bad_chars} 个人物还没写锚定（state=PLAN，后续章再用）。")
        return 0
    print(f"  {bad_chars} 个人物 / {bad_items} 条问题需要在**定妆卡**里改（改完重跑 --extract）。")
    print()
    print(_CONVENTION)
    print("  为什么值得停下来改：锚定描述是「跨镜不变的那张脸」的唯一来源。")
    print("  它写得不可画，后面几百张图会一起歪 —— 而这类问题看代码看不出来、")
    print("  看四张候选也只会觉得「随机性真大」，不会想到是锚定写法的问题。")
    return 1


# ---- 命令入口 --------------------------------------------------------------


def _print_table(
    lock: CastLock,
    registry: dict[str, CastEntry],
    missing: dict[str, list[int]],
    *,
    appears: set[str] | None = None,
) -> None:
    """打印定妆库现状。`appears` 是"本集出现的人"，与"已冻结"分开显示 —— 两者不是一回事。"""
    rows = sorted(set(registry) | set(lock.characters) | set(missing) | (appears or set()))
    if not rows:
        print("  定妆库里还没有人物。先跑 `lvs cast --task <任务名> --extract`。")
        return
    print(f"  {'ID':<12}{'显示名':<10}{'state':<7}{'参考图':<7}{'本集':<6}{'变体':<6}锚定描述")
    for name in rows:
        entry = lock.characters.get(name) or registry.get(name) or CastEntry()
        refs = len(entry.refs)
        shots = missing.get(name) or []
        in_ep = str(len(shots)) + " 镜" if shots else ("是" if appears and name in appears else "—")
        variants = len(entry.anchor_variants) or (1 if entry.anchor else 0)
        anchor = (entry.anchor or "").replace("\n", " ")[:34]
        print(
            f"  {name:<12}{entry.display or '—':<10}{entry.state:<7}{refs:<7}"
            f"{in_ep:<6}{variants:<6}{anchor}"
        )


def run_command(config, ws, args) -> int:  # noqa: ANN001 - 由 cli 传入
    directory = cast_dir(config, getattr(args, "dir", None))

    lock = load_lock(directory)
    registry = load_registry(directory)

    # ---- 提取 ----
    if getattr(args, "extract", False):
        text = ""
        if ws is not None:
            src = ws.path("source.md")
            if not src.is_file():
                src = ws.path("parse.json")
            if src.is_file():
                text = src.read_text(encoding="utf-8")

        slots = extract_slots(text) if text else set()

        card_path = find_card(config) or config.get("cast.card")
        card_entries: dict[str, CastEntry] = {}
        if card_path and Path(str(card_path)).is_file():
            card_entries = parse_card(Path(str(card_path)).read_text(encoding="utf-8"))

        # 定妆卡是**书级真源**：即便本集一个槽位都没写，人物也要先登记进库 ——
        # "给谁出定妆照"是一本书一次的事，不该每集重来一遍。
        added = sum(
            1 for n in slots if n not in registry and n not in card_entries
        )
        # ★ 合并优先级：**定妆卡赢**。它是描述的真源；cast.json 只是个镜像。
        # 反过来的话，改了定妆卡却读不到新描述（旧镜像把它盖住）——
        # 那正是"改了锚定却还是老脸"这类幽灵故障的来源。
        registry = {**registry, **card_entries}
        for name in sorted(slots):
            registry.setdefault(name, CastEntry(display=name, state=STATE_PLAN))

        # lock 里只保留**审批状态**（state / refs / 审批记录 / 打回记录），
        # 描述字段一律以定妆卡为准刷新。
        for name, entry in registry.items():
            old = lock.characters.get(name)
            if old is None:
                lock.characters[name] = CastEntry(
                    display=entry.display, anchor=entry.anchor, state=entry.state,
                    anchor_variants=list(entry.anchor_variants), source=entry.source,
                    aliases=list(entry.aliases),
                )
            else:
                old.display = entry.display or old.display
                old.anchor = entry.anchor or old.anchor
                old.anchor_variants = list(entry.anchor_variants) or old.anchor_variants
                old.aliases = list(entry.aliases) or old.aliases
                if entry.source:
                    old.source = entry.source

        # 标出"本集出现"：拍摄稿还没上槽位时，只能拿显示名在正文里搜
        appears: set[str] = set(slots)
        if text:
            for key, entry in registry.items():
                if entry_matches_text(entry, text):
                    appears.add(key)

        save_registry(directory, registry)
        save_lock(directory, lock)

        print(f"定妆库：{directory}")
        print(
            f"  本集槽位 {len(slots)} 个｜定妆卡读到 {len(card_entries)} 人｜"
            f"库内合计 {len(lock.characters)} 人（本次新增 {added}）"
        )
        if not card_path:
            print("  提示：config.toml 的 `[cast].card` 没配，锚定描述需要手填。")
        elif not card_entries:
            print(f"  提示：定妆卡 {card_path} 里没解析出人物小节，检查标题格式。")
        if not slots:
            print("  ⚠ 本集拍摄稿里**没有** `{NAME}` 槽位 —— 人物一致性注入不会生效。")
            print("    迁移写法：[画面位] [场景] {角色名} 侧身走过校墙，背景只有枯枝")
        print()
        _print_table(lock, registry, slots_of_shots(_shots_of(ws)), appears=appears)
        print("\n  下一步：lvs cast --render <ID> 出候选 → lvs cast --approve <ID> 冻结")
        return 0

    # ---- 出候选 ----
    targets = getattr(args, "render", None)
    if targets:
        names = [n.strip() for n in str(targets).split(",") if n.strip()] or ["ALL"]
        entries = {**lock.characters, **registry}
        if names == ["ALL"]:
            names = sorted(n for n, e in entries.items() if e.anchor)
        if not names:
            print("没有可出图的人物（锚定描述为空）。先跑 --extract 并补锚定描述。")
            return 2
        style_suffix = _style_suffix(config)
        failures = 0
        for name in names:
            entry = entries.get(name)
            if entry is None or not entry.anchor:
                print(f"  跳过 {name}：库里没有这个人物，或锚定描述为空")
                failures += 1
                continue
            print(f"出候选定妆照：{name}（{args.count} 张）…")
            res = render_candidates(
                name, entry, directory, config=config,
                count=int(getattr(args, "count", 4) or 4),
                seed=getattr(args, "seed", None),
                style_suffix=style_suffix,
                variant=int(getattr(args, "variant", 1) or 1),
            )
            if res.errors:
                for err in res.errors[:2]:
                    print(f"  [失败] {err}")
            if res.files:
                print(f"  → v{res.version}：{len(res.files)} 张，{res.files[0].parent}")
            else:
                failures += 1

        try:
            sheet = build_contact_sheet(directory)
            if sheet:
                print(f"\n  审阅表：{sheet}")
                print('  看完再决定：lvs cast --approve <ID> / --reject <ID> --note "原因"')
        except Exception as exc:  # noqa: BLE001 - 审阅表只是锦上添花，失败不影响出图
            print(f"  [审阅表] 生成失败，不影响候选：{exc}")
        return 1 if failures else 0

    # ---- 打回 ----
    rejects = getattr(args, "reject", None)
    if rejects:
        for spec in str(rejects).split(","):
            spec = spec.strip()
            if not spec:
                continue
            if "=" in spec:
                name, _, ver = spec.partition("=")
                version = int(ver) if ver.strip().isdigit() else None
            else:
                name, version = spec, None
            name = name.strip()
            try:
                dest = reject(directory, lock, name, version=version, note=str(getattr(args, "note", "") or ""))
            except CastError as exc:
                print(f"  [失败] {exc}")
                continue
            print(f"  已打回 {name} → {dest}")
        return 0

    # ---- 批准 ----
    approves = getattr(args, "approve", None)
    if approves:
        for spec in str(approves).split(","):
            spec = spec.strip()
            if not spec:
                continue
            if "=" in spec:
                name, _, ver = spec.partition("=")
                version = int(ver) if ver.strip().isdigit() else None
            else:
                name, version = spec, None
            name = name.strip()
            try:
                refs = approve(directory, lock, name, version=version,
                               source=Path(getattr(args, "from_file")) if getattr(args, "from_file", None) else None)
            except CastError as exc:
                print(f"  [失败] {exc}")
                continue
            print(f"  ✓ {name} 已冻结：{len(refs)} 张参考图")
            for r in refs:
                print(f"      {r}")
        print(f"\n定妆库：{lock_path(directory)}")
        return 0

    # ---- 锚定描述体检 ----
    lint_arg = getattr(args, "lint", None)
    if lint_arg is not None:
        return _run_lint(lock, registry, lint_arg)

    # ---- 只重出审阅表 ----
    # 注意判据用 `is not None`：`--sheet` 是 `nargs="?"` + `const=""`，
    # 不带值时拿到的是**空字符串**（falsy）—— 用真值判断会直接漏进下面的状态分支。
    sheet_arg = getattr(args, "sheet", None)
    if sheet_arg is not None:
        target = build_contact_sheet(directory, out_path=Path(sheet_arg) if sheet_arg else None)
        if target:
            print(f"审阅表：{target}")
            return 0
        print("没有可拼的候选图。先跑 `lvs cast --render <ID>`。")
        return 2

    # ---- 默认：看状态 ----
    shots = _shots_of(ws)
    used = slots_of_shots(shots)
    print(f"定妆库：{directory}")
    _print_table(lock, registry, used)

    missing = gate_missing(shots, lock)
    if missing:
        print()
        print(gate_message(missing, lock, directory))
        return 1
    if not shots:
        # 没有分镜时"没缺人"是**废话**，不是通过 —— 必须说清楚，
        # 否则用户会以为一致性保护已经生效（其实连稿子都还没解析）。
        print()
        print("  · 本任务还没有 shots.json，无法判断闸门 —— 先跑 `lvs parse` 再 `lvs shots`。")
        return 0
    if not used:
        print()
        print("  · 本集没有任何 `{NAME}` 人物槽位：闸门**无事可做**，人物一致性不会生效。")
        print("    迁移写法：[画面位] [场景] {角色名} 侧身走过校墙 —— 或跑 `lvs migrate`。")
        return 0
    print(f"\n  ✓ 闸门通过：本集用到的 {len(used)} 个人物（{', '.join(sorted(used))}）都已冻结。")
    return 0


def _shots_of(ws) -> list[dict[str, Any]]:  # noqa: ANN001
    if ws is None:
        return []
    data = ws.try_load_shots() if hasattr(ws, "try_load_shots") else {}
    return data.get("shots", []) or []


def _style_suffix(config) -> str:  # noqa: ANN001
    """取定妆照的风格后缀。

    `[cast].style_suffix` 优先 —— 定妆照是"看清脸"的图，风格常与场景图不同
    （场景图要戏剧光，定妆照要平光素背景）。给了就用给的，省得为一本书去改全局预设。
    """
    override = config.get("cast.style_suffix") if config is not None else None
    if override:
        return str(override)
    try:
        # 走**注册表**而不是内置表：项目素材库里自定义的风格也要能用于定妆照
        # （否则会出现"场景图是水墨、定妆照是漫画"的错配）。
        from lvs.styles import build_registry

        reg = build_registry(config)
        return reg.get(config.get("shots.style") if config is not None else None).suffix
    except Exception:  # noqa: BLE001 - 风格取不到不该阻断定妆
        return ""
