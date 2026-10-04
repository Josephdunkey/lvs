"""项目接入（`lvs init`）—— 让"下一本书 / 下一个题材"只需要一条命令。

## 为什么要有它

这套流水线要服务的**不止一本书**。但接一本新书原本需要：

1. 手工建素材库目录树（`00-设定/` `05-拍摄稿/` `_cast/` ……）
2. 手写一份 config：`[paths].lib`、`[cast].dir`、`[cast].card`、`[shots].style`、
   `[qc].face_model`、`[tts]` 音色……**每一项填错都要等跑到那一步才报错**
3. 猜目录名对不对（各书文件名不完全一致）

这套动作做第二次就开始烦人，做第五次一定会漏 —— 而漏掉的是"定妆库路径写错"
这类**静默**问题（跑出来才发现锚定没生效）。

`lvs init` 把 1 和 2 变成一次命令，并把 3 变成一次体检（`— 缺什么、下一步做什么`）。

## 它做什么 / 不做什么

**做**：建目录骨架、写项目风格文件骨架、生成一份可直接用的项目 config、体检并列出待办。

**不做**：不碰原著、不写拍摄稿、不建定妆卡 —— 那些是**创作**，不是脚手架能代劳的
（拍摄稿由 `newmuyu` 拆书技能产出；定妆卡是人写的）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from lvs.config import EXAMPLE_FILENAME, PROJECT_ROOT
from lvs.styles import project_style_file, scaffold_style_file


# 素材库骨架。编号沿用拆书技能（newmuyu）的约定，**不要改号** ——
# 改了号，技能产出与流水线读的就不是同一棵树了。
LIB_DIRS: tuple[tuple[str, str], ...] = (
    ("00-设定", "风格 / 定妆卡 / 音色（人写的设定，变动最少）"),
    ("01-素材卡", "每期的讲稿与内容素材包（拆书技能的产物）"),
    ("04-图片素材", "分镜图（lvs assets 的产物落这里或其镜像）"),
    ("05-拍摄稿", "拍摄稿；迁移产物落 05-拍摄稿/_v2/"),
    ("06-分镜提示词", "分镜总表与提示词清单"),
    ("07-成片", "成片交付目录"),
    ("_cast", "定妆库（跨集共享，跟着书走）"),
)


@dataclass
class InitReport:
    lib: Path
    created: list[Path] = field(default_factory=list)
    existing: list[Path] = field(default_factory=list)
    config_path: Path | None = None
    todos: list[str] = field(default_factory=list)


def scaffold_lib(lib: Path) -> tuple[list[Path], list[Path]]:
    """建素材库骨架。返回 `(新建的, 本来就有的)` —— 已存在的一律不动。"""
    created: list[Path] = []
    existing: list[Path] = []
    for name, _why in LIB_DIRS:
        p = lib / name
        if p.is_dir():
            existing.append(p)
        else:
            p.mkdir(parents=True, exist_ok=True)
            created.append(p)
    return created, existing


CONFIG_TEMPLATE = '''# {title} · 项目配置
#
# 用法（两种都行）：
#   LVS_CONFIG={filename} lvs <命令>
#   lvs --config {filename} <命令>
#
# 本文件由 `lvs init` 生成，只填了**跟这本书绑定**的路径与风格。

[base]
# ★ 机器相关的项（API key / ComfyUI 地址 / TTS 服务 / 人脸权重）在这里**继承**主配置。
#   留空 = 不继承（本文件必须自带全部键）。
#   不选「把密钥复制过来」是因为 `config.toml` 在 .gitignore 里，而本文件不在 —— 复制等于提交密钥。
config = "config.toml"

[paths]
# 本项目素材库根。**所有项目内路径都从它派生**，改这一个就够。
lib = "{lib}"

[shots]
# 本书的画面风格（`lvs styles` 可看全部预设；也可以在这里填自定义名字）
style = "{style}"
# 素材来源策略。**注释掉 = 继承主 config 的 [shots].source_mode**。
# 本书想另用一套就在这里覆盖。最常改的一种：画风要统一、不想混实拍素材时设 local。
#   source_mode = "local"
# 为什么值得显式写：主 config 常留着 source_mode = "auto"，
# 而 auto 会把一部分镜判给 Pexels —— 没配 `[pexels].api_key` 时那些镜必然失败
# （逐镜隔离，不会连累其余镜，但你会白丢一批图）。

# ★ 「图」的粒度 —— **必选，本文件故意不给默认值**。
#   shot = 每镜一张（保留「远景→近景→特写」的镜头推进）
#   beat = 同一画面位共用一张（省算力，观感更「静」）
# 有画面位跨了多个镜时，`lvs assets` / `lvs run` 会停下问你（退出码 2）。
#   image_granularity = "shot"
#   image_granularity = "beat"

[cast]
# 定妆库与定妆卡：留空 = 自动从 [paths].lib 推导
#   定妆库 → <lib>/_cast
#   定妆卡 → <lib>/00-设定/*定妆卡*.md（只有一份时才自动认；多份会提示你指定）
dir = ""
card = ""
workflow = "zimage_cast.json"
width = 1024
height = 1024
seed = {seed}

[pipeline]
enforce = true

# ---- 以下项从主 config.toml 继承（见上面的 [base]）；需要覆盖时再放开 ----
# [comfyui]  base_url / model / workflow / width / height
# [tts]      mode / ref_audio / instruct / base_url
# [pexels]   api_key
# [library]  dirs（通用素材库，跨书共用）
# [qc]       face_model（人脸权重，跨书共用）
# [cast]     style_suffix（★ 定妆照风格后缀，**必须与场景图不同**：
#            场景是戏剧光 + 16:9，定妆是「平光胸像 + 方图」，两者写在一起会语义打架）
'''


def write_project_config(
    lib: Path,
    *,
    name: str,
    style: str,
    out: Path | None = None,
    seed: int = 7777,
) -> Path:
    """写一份项目 config。已存在**不覆盖**（里面可能有手工调过的音色与密钥）。"""
    target = out or (PROJECT_ROOT / f"config.{name}.toml")
    if target.is_file():
        return target
    target.write_text(
        CONFIG_TEMPLATE.format(
            title=name,
            lib=str(lib).replace("\\", "/"),
            style=style,
            seed=seed,
            filename=target.name,
        ),
        encoding="utf-8",
    )
    return target


def audit_lib(lib: Path, config_path: Path | None = None) -> list[str]:
    """体检：列出现在还缺什么、以及下一步该敲什么命令。

    刻意**不自动补**缺失的创作产物（定妆卡、拍摄稿）—— 那些是人写的。
    脚手架只负责把"该做什么"说清楚。
    """
    todos: list[str] = []

    missing_dirs = [name for name, _ in LIB_DIRS if not (lib / name).is_dir()]
    if missing_dirs:
        todos.append(f"缺目录：{'、'.join(missing_dirs)} → 重跑 `lvs init \"{lib}\"` 补上")

    setting = lib / "00-设定"
    cards = []
    if setting.is_dir():
        cards = sorted(p for p in setting.glob("*定妆*.md") if p.is_file())
    if not cards:
        todos.append(
            "还没有定妆卡（`00-设定/00-风格与人物定妆卡.md`）—— "
            "**这是人物一致性的唯一来源**，必须人写。可先用 `lvs cast --lint` 校验写法。"
        )
    elif len(cards) > 1:
        todos.append(
            f"00-设定 下有 {len(cards)} 份像定妆卡的文件 → 在 config 的 `[cast].card` 指定用哪一份"
        )

    sf = project_style_file(lib)
    if not sf.is_file():
        todos.append("还没有项目风格文件 → `lvs init` 会生成骨架，填好 suffix 即可")
    else:
        from lvs.styles import _parse_file  # noqa: PLC2701 - 同包内部工具

        got, _dflt, err = _parse_file(sf)
        if err:
            todos.append(f"风格文件有问题：{err}")
        elif not got:
            todos.append("风格文件里还没有可用的风格条目（`[styles.<名字>] suffix = \"…\"`）")

    shots = sorted(lib.glob("05-拍摄稿/*.md"))
    shots = [p for p in shots if "迁移报告" not in p.name and p.parent.name != "_v2"]
    if not shots:
        todos.append(
            "还没有拍摄稿 → 用 `newmuyu` 拆书技能产出到 `05-拍摄稿/`，"
            "格式契约见 `docs/拍摄稿格式契约.md`"
        )

    if config_path is not None and not config_path.is_file():
        todos.append(f"项目 config 不存在：{config_path}")

    return todos


def run_init(args) -> int:  # noqa: ANN001
    raw = getattr(args, "lib", None)
    if not raw:
        print("用法：lvs init <素材库路径> [--name 项目名] [--style 风格名]")
        print("  例：lvs init \"D:/素材库/某本书/10-语料/知识视频素材库\" --name 项目名")
        return 2

    lib = Path(str(raw)).expanduser()
    name = str(getattr(args, "name", None) or lib.parent.parent.name or lib.name)
    style = str(getattr(args, "style", None) or "").strip()

    print(f"接入项目：{name}")
    print(f"  素材库：{lib}")
    print()

    # ---- 1) 目录骨架 ----
    created, existing = scaffold_lib(lib)
    for p in created:
        print(f"  ✓ 新建 {p.name}")
    if existing:
        print(f"  · 已存在 {len(existing)} 个目录（未改动）")

    # ---- 2) 风格 ----
    #
    # ★ 这里必须用**指向本项目 lib 的 config** 去建 registry。
    #
    # 原先传的是 `build_registry(None)` —— 它推不出 lib，于是**只读内置预设**，
    # 拿到的 default 是内置默认（`historical-documentary`）。
    # 但项目风格文件里可能已经声明了本书的默认风格，两者一撞，
    # `config [shots].style` 会**覆盖**项目声明（`styles.py` 的优先级：
    # `[shots].style` > 文件里的 default）—— 等于 init 把用户手工调好的风格
    # **悄悄换掉**了。
    #
    # 实测踩到：雨月物语的项目风格文件写着 `default = "ukiyo-e-woodblock"`
    # （浮世绘·怪谈夜色，还为本作专门收紧了后缀），而 `lvs init` 往 config 里
    # 写了 `historical-documentary`（历史纪录片）→ 一跑 shots 风格整个跑偏。
    #
    # 修法不是"再写一份 default 判断"，而是**让 init 与出图走同一个真源**：
    # 给 registry 一个能推出 lib 的 config，它自己就会读项目风格文件，
    # 拿到的 `default` 就是项目声明的那一个。校验自定义风格名也一并正确了
    # （原先项目里自定义的风格会被误判成"不存在"）。
    reg = None
    try:
        from lvs.config import Config as _Cfg
        from lvs.styles import build_registry

        reg = build_registry(_Cfg({"paths": {"lib": str(lib)}}, None))
    except Exception:  # noqa: BLE001 - 风格文件坏了不该让 init 整个失败
        reg = None

    if not style:
        style = reg.default if reg is not None else "historical-documentary"
    if reg is not None:
        try:
            reg.get(style)
        except Exception as exc:  # noqa: BLE001
            print()
            print(str(exc))
            return 2

    sf = project_style_file(lib)
    if sf.is_file():
        print(f"  · 风格文件已存在，未覆盖：{sf}")
        if style == getattr(reg, "default", None):
            print(f"      → 采用项目里声明的默认风格：{style}")
    else:
        scaffold_style_file(lib)
        print(f"  ✓ 风格骨架 {sf}")
        print("      （填好 `[styles.<名字>] suffix = …` 就能用；不填就用内置预设）")

    # ---- 3) 项目 config ----
    out = Path(str(getattr(args, "config_out", ""))).expanduser() if getattr(args, "config_out", None) else None
    cfg_path = write_project_config(
        lib, name=name, style=style, out=out, seed=int(getattr(args, "seed", 7777) or 7777)
    )
    print(f"  ✓ 项目配置 {cfg_path}")

    # ---- 4) 体检 ----
    print()
    todos = audit_lib(lib, cfg_path)
    if todos:
        print(f"还差 {len(todos)} 步：")
        for t in todos:
            print(f"  · {t}")
    else:
        print("  ✅ 骨架就绪。")

    print()
    print("接下来：")
    print(f"  1) 把原著与设定放好（定妆卡写进 {lib / '00-设定'}）")
    print(f"  2) lvs styles --config \"{cfg_path.name}\"          # 选定画面风格")
    print(f"  3) lvs cast --task <任务> --lint --config \"{cfg_path.name}\"   # 校验锚定描述")
    print(f"  4) lvs gate --task <任务> --config \"{cfg_path.name}\"          # 看门禁到哪了")
    print()
    print("每个项目的 config 是独立的 —— 书之间互不干扰（风格 / 定妆库 / 任务名都跟着走）。")
    print(f"通用项（ComfyUI / TTS / Pexels / 人脸权重）仍在 {PROJECT_ROOT / EXAMPLE_FILENAME} 对应的主配置里。")
    return 0
