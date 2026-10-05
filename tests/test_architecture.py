"""架构约束测试 —— 把"这次整理出来的秩序"变成**跑得起来的assert**。

## 为什么需要这一组

本项目的历史是：**同一个东西在 2–5 处各写一遍**，靠"人肉同步"维持一致。
每一处都写过注释说"要与 XX 保持一致"（`gui/jobs.py` 里甚至写着
"与 cli.py 的子命令参数一一对应，不许出现这里没有的东西"）——
但注释拦不住任何人，因为**不一致不会报错**。

实测过的静默失效：改一个 CLI 参数名 → 编排器 `getattr(args, "旧名", None)`
拿到 `None` → 那个开关被悄悄忽略 → 跑完才发现"我明明加了 `--no-llm`"。

所以这里把三条架构约束变成断言：

| 约束 | 一旦破了会怎样 |
|---|---|
| 阶段定义只有 `lvs/stage.py` 一处 | 参数在某一处漏掉 → 静默失效 |
| 只有一个地方伪造 `argparse.Namespace` | CLI 的形状渗进核心，程序内无法组合阶段 |
| 所有 `*Error` 继承 `LvsError` | 上层只能枚举 `except`，加一个错一个 |

这类测试的价值不在于"现在是对的"，而在于**下一次有人破坏它时会红**。
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re
import sys

import pytest

from lvs import errors as errors_mod
from lvs import pipeline as pipeline_mod
from lvs import stage as stage_mod

LVSDIR = pathlib.Path(stage_mod.__file__).parent


#: 入口点，不算模块。它 **必须** import `cli`（那就是它的职责），
#: 且 import 它会把 `main()` 真的跑一遍 —— 所以从"遍历模块"里排除。
_ENTRYPOINTS = {"__init__", "__main__"}


def _modules() -> list[pathlib.Path]:
    """**所有**模块，含 `gui/` 子包。

    ★ 原先只 glob `lvs/*.py`，于是 `gui/` 成了**架构约束的盲区** ——
    `gui/store.py` 里藏着的**第 7 份**阶段顺序副本，以及 `GuiError` /
    `ConfigIOError` 没继承统一基类，全都因此没被这条测试守住。
    「约束只覆盖它碰巧扫到的文件」= 约束本身是假的。
    """
    return sorted(p for p in LVSDIR.rglob("*.py") if p.stem not in _ENTRYPOINTS)


def _source(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


# ---- 约束 1：阶段定义只有一处 ------------------------------------------------


def test_stage_table_is_the_single_source_of_truth():
    """`STAGES` 的顺序、标题、产物、门禁都必须**只在这里**定义一次。"""
    assert stage_mod.ORDER == ("parse", "shots", "assets", "voice", "build")
    for st in stage_mod.STAGES:
        assert st.title, f"{st.name} 缺 title"
        assert st.memo, f"{st.name} 缺 memo（一句话职责）"
        assert st.artifacts, f"{st.name} 缺 artifacts（断点续跑与门禁都靠它）"
        assert st.impl.startswith("lvs."), f"{st.name} 的 impl 写错了：{st.impl}"


def test_drivers_are_separate_from_pipeline_stages():
    """`run` / `studio` 是编排入口，不该混进流水线顺序里。"""
    assert [s.name for s in stage_mod.DRIVERS] == ["run", "studio", "publish"]
    assert all(s.kind == "driver" for s in stage_mod.DRIVERS)
    for name in ("run", "studio"):
        assert name not in stage_mod.ORDER


def test_stage_gates_agree_with_the_gate_table():
    """★ 阶段表说"这一步被 G1 守"，门禁表也必须这么说 —— 两边不许各说各的。

    注意门禁是**多值**的：`assets` 同时被 G1（分镜/提示词）与 G2（定妆）守。
    第一版把 `gate` 写成单值，这条测试当场就抓到了（G1 被 G2 覆盖掉）。
    """
    by_stage: dict[str, set[str]] = {}
    for gate in pipeline_mod.GATES:
        if gate.stage:
            by_stage.setdefault(gate.stage, set()).add(gate.id)
    for st in stage_mod.STAGES:
        assert set(st.gates) == by_stage.get(st.name, set()), (
            f"{st.name}：阶段表说 {sorted(st.gates)}，门禁表说 {sorted(by_stage.get(st.name, set()))}"
        )


def test_every_gated_stage_exists():
    """门禁表里引用的阶段名必须真实存在（防改名后留下悬空引用）。"""
    for gate in pipeline_mod.GATES:
        if gate.stage:
            assert gate.stage in stage_mod.STAGE_NAMES, f"{gate.id} 指向不存在的阶段 {gate.stage}"


def test_stage_needs_only_reference_declared_stages():
    for st in stage_mod.STAGES:
        for need in st.needs:
            assert need in stage_mod.STAGE_NAMES, f"{st.name} 依赖了不存在的阶段 {need}"


def test_stage_order_respects_needs():
    """`needs` 必须排在前面 —— 否则血缘与断点续跑都会错。"""
    pos = {name: i for i, name in enumerate(stage_mod.ORDER)}
    for st in stage_mod.STAGES:
        for need in st.needs:
            assert pos[need] < pos[st.name], f"{st.name} 依赖 {need}，但 {need} 排在后面"


def test_gui_stage_spec_is_a_view_not_a_copy():
    """★ 界面的阶段表必须是 `stage.py` 的**视图**，不能再抄一份。

    这条是本项目最贵的老毛病：`gui/jobs.py` 抄了一份参数表，
    与 `cli.py` 靠人肉同步，抄漏一个 → 界面上少一个开关，且不报错。
    """
    from lvs.gui import jobs

    assert set(jobs.STAGE_SPEC) == set(stage_mod.BY_NAME)
    for name, stage in stage_mod.BY_NAME.items():
        assert jobs.STAGE_SPEC[name] == stage.options, f"{name} 的开关表与阶段表不一致"
    assert jobs.STAGE_POSITIONAL == {
        s.name: s.positional for s in stage_mod.ALL if s.positional
    }


def test_gui_does_not_redeclare_option_class():
    """`Option` 的类定义只该有一处（`stage.py`）。"""
    jobs_src = _source(LVSDIR / "gui" / "jobs.py")
    assert "class Option" not in jobs_src, "gui/jobs.py 又自己定义了一份 Option"
    assert "from lvs.stage import" in jobs_src


# ---- 约束 2：只有一个地方伪造 Namespace --------------------------------------


def test_only_stage_context_fakes_a_namespace():
    """★ 全仓只有 `StageContext.as_namespace` 一处构造 `argparse.Namespace`。

    多一处，CLI 的形状就多渗一点进核心；而且每处都要自己记住字段名 ——
    写错只会静默失效（`getattr` 拿 None）。
    """
    offenders: list[str] = []
    for path in list(_modules()) + sorted((LVSDIR / "gui").glob("*.py")):
        src = _source(path)
        if path.name == "stage.py":
            continue
        if "argparse.Namespace(" in src:
            offenders.append(path.name)
    assert offenders == [], f"这些文件还在自己伪造 Namespace：{offenders}"


def test_orchestrators_go_through_the_stage_table():
    """`run.py` / `studio.py` 必须按阶段表跑，不许自己写死阶段顺序。"""
    for name in ("run.py", "studio.py"):
        src = _source(LVSDIR / name)
        assert "stage_mod" in src or "from lvs import stage" in src, f"{name} 没接阶段表"
    run_src = _source(LVSDIR / "run.py")
    assert '("shots", "assets", "voice", "build")' not in run_src, (
        "run.py 又出现了硬编码的阶段顺序"
    )


# 一条能命中**任何**写法的判据：括号内同时出现三个阶段名，就说明有人在列顺序。
_STAGE_ORDER_LITERAL = re.compile(
    r'[(\[{][^)\]}]*"shots"[^)\]}]*"assets"[^)\]}]*"voice"[^)\]}]*[)\]}]'
)


def test_no_module_hardcodes_the_stage_order():
    """★ 阶段顺序只许来自 `stage.ORDER` —— 不许任何模块再抄一份。

    这条判据是**补上来的**：第一版只查了 `run.py` 里的那个字面量，
    结果漏掉了 `board.py` 的 `STAGE_ORDER = ("parse","shots","assets","voice","build")`
    —— 那是**第 6 份**副本（cli / gui-jobs / run / pipeline / workspace 之外）。
    教训：判据要按"形状"写，不要按"某一个已知写法"写。
    """
    offenders: list[str] = []
    for path in _modules():
        if path.stem in ("stage", "cli", "pipeline"):
            continue  # stage 是真源；cli 是壳（它的子命令名就是阶段名）；pipeline 用 GATE.stage
        if _STAGE_ORDER_LITERAL.search(_source(path)):
            offenders.append(path.name)
    assert offenders == [], (
        f"这些文件硬编码了阶段顺序：{offenders}\n"
        f"  请改成 `from lvs.stage import ORDER`。"
    )


# ---- 约束 3：统一错误基类 ----------------------------------------------------


def _module_name_of(path: pathlib.Path) -> str:
    """文件路径 → 模块名（`lvs/gui/app.py` → `lvs.gui.app`）。

    ★ 不能拼 `lvs.<stem>` —— 子目录会变成 `lvs.app`（不存在）。
    这个坑正是"扫描范围一扩大就暴露"的那类。
    """
    rel = path.relative_to(LVSDIR.parent).with_suffix("")
    return ".".join(rel.parts)


def test_all_error_classes_inherit_lvs_error():
    """★ 所有 `*Error` / `*Conflict` 都要继承 `LvsError`。

    不然上层想说"这是输入问题，别重试"就得枚举 `except A, B, C…` ——
    加一个新错误类就漏一个。
    """
    import importlib

    for path in _modules():
        importlib.import_module(_module_name_of(path))

    offenders: list[str] = []
    for mod_name, mod in list(sys.modules.items()):
        if not mod_name.startswith("lvs") or mod is None:
            continue
        for attr, obj in vars(mod).items():
            if not inspect.isclass(obj) or not attr.endswith(("Error", "Conflict")):
                continue
            if getattr(obj, "__module__", "") != mod_name:
                continue
            if not issubclass(obj, errors_mod.LvsError):
                offenders.append(f"{mod_name}.{attr}")
    assert offenders == [], f"这些错误类没继承 LvsError：{offenders}"


def test_error_base_keeps_old_bases_for_compatibility():
    """多重继承必须保留原基类 —— 否则既有的 `except RuntimeError` 会失效。"""
    from lvs.guard import GPUConflict
    from lvs.styles import StyleError

    assert issubclass(GPUConflict, RuntimeError)
    assert issubclass(StyleError, KeyError)


def test_exit_codes_are_distinguishable():
    """`Blocked`（等人决定）与 `Usage`（改了再来）必须是**不同的码** ——
    混起来自动化脚本就没法区分"该等人"和"该修环境"。"""
    from lvs.pipeline import GateBlocked

    assert errors_mod.EXIT_BLOCKED != errors_mod.EXIT_USAGE
    assert GateBlocked.exit_code == errors_mod.EXIT_BLOCKED
    assert errors_mod.UsageError().exit_code == errors_mod.EXIT_USAGE
    assert errors_mod.BlockedError().exit_code == errors_mod.EXIT_BLOCKED
    for code in (0, 1, 2, 3):
        assert code in errors_mod.EXIT_MEANING, f"退出码 {code} 没有写进语义表"


def test_exit_code_for_is_total():
    """任意异常都要能映射出退出码（未知异常归为失败，**不是** usage）。"""
    assert errors_mod.exit_code_for(errors_mod.BlockedError("x")) == errors_mod.EXIT_BLOCKED
    assert errors_mod.exit_code_for(ValueError("x")) == errors_mod.EXIT_FAILED
    assert errors_mod.exit_code_for(KeyboardInterrupt()) == errors_mod.EXIT_FAILED


# ---- 约束 4：产物指纹只有一处实现 --------------------------------------------


#: 允许自己用 `hashlib` 的文件 + **必须写清理由**。想加新条目就得先想明白
#: "它跟 `artifact.fingerprint` / `artifact.signature` 到底差在哪"。
_HASH_ALLOWLIST: dict[str, str] = {
    "artifact.py": "签名与指纹的**唯一**实现处",
    "assets.py": "`_file_digest` 是**内容哈希**（真读字节），比指纹严格 —— 缓存命中要求字节一致",
    "llm_cache.py": "LLM 内容寻址缓存的键（sha256(模型+地址+消息+参数)）：要的是**请求内容**的指纹，"
                    "与 `artifact` 的「文件变没变」（size+mtime）不是一回事，也不该共用",
}


def test_hashing_lives_in_the_allowlist():
    """★ 求签名/指纹这件事只该有几个明确的实现处，每个都有理由。

    为什么管这个：同一段 `join → sha1 → 截断` 原先在 `build` / `assets` / `library`
    里**字面上重复了三遍**，而三份各自演化过（有的用 `|`、有的用 `\x1f`，有的截断有的不截）。
    这正是本项目反复吃亏的"同一件事写多遍" —— 不报错，只是慢慢分叉。
    """
    offenders: list[str] = []
    for path in _modules():
        if path.name in _HASH_ALLOWLIST:
            continue
        if "hashlib" in _source(path):
            offenders.append(path.name)
    assert offenders == [], (
        f"这些文件自己算了哈希：{offenders}\n"
        f"  求签名请用 `artifact.signature(...)`；文件集指纹请用 `artifact.fingerprint(...)`。\n"
        f"  确有不同用途，就在 `_HASH_ALLOWLIST` 里加一条**并写清理由**。"
    )


def test_parameter_signatures_go_through_the_shared_helper():
    """参数签名（`join→sha1→截断`）不许再各写一份。"""
    for name in ("build.py", "assets.py", "library.py"):
        src = _source(LVSDIR / name)
        assert "artifact.signature(" in src, f"{name} 没用共用的参数签名助手"


# ---- 约束 5：分层方向 --------------------------------------------------------


def test_no_module_imports_the_cli():
    """核心模块不许 import `cli`（那是壳）—— 否则就是反向依赖。"""
    offenders = [
        p.name for p in _modules()
        if p.stem != "cli" and "lvs.cli" in _lvs_imports_of(p)
    ]
    assert offenders == [], f"这些模块反向依赖了 cli：{offenders}"


#: `stage.py` 允许 import 的 lvs 模块 —— 前提是它们**自己零 lvs 依赖**（真正的叶子）。
#: 规则的**目的**是"不成环、能被任意一方 import"，所以判据该写"只允许叶子"，
#: 而不是"一个都不许" —— 后者会把安全的东西也禁掉，逼人绕路（第一版就是这么被自己绊了一下）。
#:
#: `lvs.runlog` 是 2026-10-03 加的：`stage.note_stage_end` 要记轨迹。
#: 它只依赖 json/datetime/pathlib/typing —— **确为叶子**（下面的断言会验）。
_LEAF_ALLOWED = {"lvs.errors", "lvs.runlog"}


def _lvs_imports_of(path: pathlib.Path) -> list[str]:
    """列出这个文件引入的 lvs 模块（**全名**）。

    ★ `from lvs import errors` 的 `node.module` 是 `"lvs"`，不是 `"lvs.errors"` ——
    只取 `node.module` 会把这一大类导入**全部漏掉**。
    （这不是假想：第一版就漏了，判据因此对着空的列表通过了。）
    """
    tree = ast.parse(_source(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("lvs"):
            base = node.module
            if base == "lvs":                      # `from lvs import x, y`
                found += [f"lvs.{a.name}" for a in node.names]
            else:                                   # `from lvs.x import y`
                found.append(base)
        elif isinstance(node, ast.Import):          # `import lvs.x`
            found += [a.name for a in node.names if a.name.startswith("lvs")]
    return found


def test_stage_module_only_depends_on_leaves():
    """`stage.py` 只能依赖**零依赖的叶子模块**，这样它永远不成环、谁都能 import。

    （阶段的实现用**字符串** `"lvs.shots:run_command"` 延迟解析，正是为了不 import 它们。）
    """
    imports = _lvs_imports_of(LVSDIR / "stage.py")
    extra = [m for m in imports if m not in _LEAF_ALLOWED]
    assert extra == [], f"stage.py 引入了非叶子模块：{extra}"

    # 被允许的那些，必须**真的**是叶子 —— 否则"允许"就成了后门
    for mod in imports:
        if mod not in _LEAF_ALLOWED:
            continue
        path = LVSDIR / f"{mod.split('.')[-1]}.py"
        assert path.is_file(), f"{mod} 不存在"
        assert _lvs_imports_of(path) == [], f"{mod} 自己又依赖了别的 lvs 模块，不算叶子"


#: 允许读 `shots.json` 但**不做契约校验**的文件 + 理由。
#: 加新条目要写清"为什么它读了却不验"。
_SHOTS_READ_EXEMPT: dict[str, str] = {
    "shots.py": "它自己**产出** shots.json（校验在写之前做）",
    "cast.py": "只取人物槽位，容错读；取不到最多少注入锚定，不产出错产物",
    "run.py": "只是判断要不要检查 GPU 守卫，用 try_load_shots 容错",
    "studio.py": "内部操作（货源翻转），真正的消费在它调用的 assets 里（那里已校验）",
    "store.py": "界面只读显示，且用 try_load_shots 容错",
}


def test_every_shots_consumer_validates_the_contract():
    """★ 凡是**严格读** `shots.json`（`load_shots`）的下游，都必须做契约校验。

    判据：文件里**调用**了 `load_shots(`（不是定义它），就必须同时出现
    `handoff.check_shots_or_message`。

    为什么用"架构级判据"而不是逐个写用例：上游产物坏了是**静默**的（不报错，
    只产出错的成片），而"漏一个下游"这件事**逐个测是测不出来的** ——
    上一轮就漏了 `qc`（它决定 G3 门禁放不放行）。

    `try_load_shots`（容错读）不在判据内：那是"没有就没有"的语义，各类豁免见上表。
    """
    # `(?<!def )` 排除定义行 —— `workspace.py` 是读取语义的 owner（定义 `load_shots`），
    # 不做校验是对的；校验是**消费方**的事。（第一版判据没排除，它当场报了 workspace.py。）
    call = re.compile(r"(?<!def )\bload_shots\(")
    offenders: list[str] = []
    for path in _modules():
        src = _source(path)
        if not call.search(src):
            continue
        if path.name in _SHOTS_READ_EXEMPT:
            continue
        if "handoff.check_shots_or_message" not in src:
            offenders.append(path.name)
    assert offenders == [], (
        f"这些下游严格读了 shots.json 却没做契约校验：{offenders}\n"
        f"  请加 `handoff.check_shots_or_message(...)`；"
        f"确属容错读，就在 `_SHOTS_READ_EXEMPT` 里加一条**并写清理由**。"
    )


def test_shots_read_exemptions_are_justified():
    """豁免表必须每条都有非空理由 —— 空理由等于没写。"""
    for name, why in _SHOTS_READ_EXEMPT.items():
        assert why.strip(), f"{name} 的豁免理由为空"


def test_every_work_command_supports_json():
    """★ **每个工作命令都要有 `--json`**，否则 agent 那条路就断在这儿。

    为什么用判据而不是逐个补：`结果契约` 的价值全在**覆盖完整** ——
    漏一个命令，agent 就得为它单独写一套解析，或者干脆正则中文输出
    （而措辞一改就断）。这跟"下游必须验契约"是同一类判据。

    `gate` / `styles` 有自己的 `--json`（形状不同），也算通过。
    """
    import importlib

    cli = importlib.import_module("lvs.cli")
    parser = cli.build_parser()

    subs: dict[str, object] = {}
    for action in parser._actions:  # noqa: SLF001 - 没有别的办法枚举子命令
        if getattr(action, "choices", None) and isinstance(action.choices, dict):
            subs.update(action.choices)

    want = {"parse", "shots", "assets", "voice", "build", "run", "publish", "qc", "gate", "styles",
            "resume", "cost"}
    missing_cmd: list[str] = []
    missing_flag: list[str] = []
    for name in sorted(want):
        if name not in subs:
            missing_cmd.append(name)
            continue
        flags: set[str] = set()
        for a in subs[name]._actions:  # noqa: SLF001
            flags |= set(a.option_strings)
        if "--json" not in flags:
            missing_flag.append(name)

    assert missing_cmd == [], f"这些工作命令不存在了：{missing_cmd}"
    assert missing_flag == [], (
        f"这些工作命令没有 `--json`：{missing_flag}\n"
        f"  结果契约要求工作命令都能产出统一信封（见 `lvs/result.py`）。"
    )


# ---- 约束 6：大产物必须配侧车索引（P1 / T1）--------------------------------


def test_large_artifacts_have_sidecar_index():
    """★ 任何 > 64 KB 的产物都必须配 `<名字>.index.json`。

    为什么是架构判据：`shots.json` 半兆字节，agent 一读就常驻重发（老病根）。
    "先读小表"这件事靠纪律会退化，靠判据不会。扫描对象是**真实任务目录**
    （`.work/*/`）；干净 clone（没有 `.work/`）时跳过。
    """
    import importlib

    workspace_mod = importlib.import_module("lvs.workspace")
    work = LVSDIR.parent / ".work"
    if not work.is_dir():
        pytest.skip("没有 .work/（干净 clone），跳过真实产物扫描")

    offenders: list[str] = []
    for task_dir in sorted(p for p in work.glob("*") if p.is_dir()):
        for source_name, index_name in workspace_mod.INDEX_SIDECARS.items():
            source = task_dir / source_name
            if not source.is_file():
                continue
            if source.stat().st_size <= workspace_mod.INDEX_MIN_SOURCE_BYTES:
                continue
            if not (task_dir / index_name).is_file():
                offenders.append(source.relative_to(work).as_posix())
    assert offenders == [], (
        f"这些大产物没有侧车索引：{offenders}\n"
        "  修复：重跑 `lvs shots --task <任务>`（会同步写索引），"
        "或跑 `.work/tools/backfill_shots_index.py`。"
    )


def test_sidecar_registry_names_are_consistent():
    """登记表的命名规则与 `shots_index_name` 必须一致（否则判据查错了文件）。"""
    import importlib

    workspace_mod = importlib.import_module("lvs.workspace")
    for source_name, index_name in workspace_mod.INDEX_SIDECARS.items():
        assert workspace_mod.shots_index_name(source_name) == index_name


def test_sidecar_threshold_is_the_documented_64kb():
    import importlib

    workspace_mod = importlib.import_module("lvs.workspace")
    assert workspace_mod.INDEX_MIN_SOURCE_BYTES == 64 * 1024


#: 契约模块必须自己也是叶子 —— 产出方（shots）与消费方（tts/assets/build）都要引它，
#: 它一旦依赖别人就可能成环。`breaker` 同理（各阶段都引它）。
_LEAF_MODULES = ("handoff.py", "breaker.py")


@pytest.mark.parametrize("fname", _LEAF_MODULES)
def test_shared_contract_modules_are_leaves(fname: str):
    """`handoff` / `breaker` 必须零 lvs 依赖 —— 它们要被上下游**同时**引入。"""
    path = LVSDIR / fname
    assert path.is_file(), f"{fname} 不存在"
    imported = _lvs_imports_of(path)
    assert imported == [], (
        f"{fname} 引入了 lvs 模块：{imported}\n"
        f"  它是「阶段间共享的叶子」，一旦依赖别人就可能与调用方成环。"
    )


def test_handoff_source_set_matches_the_real_sources():
    """★ 两端要有「同名测试」：`handoff.VALID_SOURCES` 是硬编码的，
    必须与真源（`sources.MODES` 去掉 auto ∪ `prompting.KIND_GRAPHIC`）一致 ——
    否则契约会允许一个下游处理不了的 source，或拒绝一个合法的。
    """
    import importlib

    handoff = importlib.import_module("lvs.handoff")
    sources = importlib.import_module("lvs.sources")
    prompting = importlib.import_module("lvs.prompting")

    expected = (set(sources.MODES) - {sources.MODE_AUTO}) | {prompting.KIND_GRAPHIC}
    assert set(handoff.VALID_SOURCES) == expected, (
        f"handoff.VALID_SOURCES={sorted(handoff.VALID_SOURCES)}，"
        f"真源={sorted(expected)} —— 两边必须一致"
    )


@pytest.mark.parametrize("name", ["parse", "shots", "assets", "voice", "build"])
def test_each_stage_declares_what_it_produces(name: str):
    """每个阶段的产物必须写在阶段表里 —— 门禁与断点续跑都读它。"""
    st = stage_mod.BY_NAME[name]
    assert st.artifacts, f"{name} 没声明产物"
    assert st.impl, f"{name} 没声明实现"
