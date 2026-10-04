"""**打包契约**：`pip install` 出来的东西必须是能用的。

## 为什么这组测试存在

实测踩过：`pyproject.toml` 里写死 `packages = ["lvs"]`，把 **`lvs.gui` 整个子包漏了** ——
`pip install` 装完 `lvs gui` 直接不可用，而 `pip wheel` **不报任何错**（纯静默）。

同一个文件里 `workflows/*.json` 是**运行时要读的数据**（生图工作流模板），
不打进包就等于装完找不到模板。

这两件事都属于"**不报错、只出错**"—— 所以判据必须是"**真建一个 wheel，看里面有什么**"，
而不是"读一下配置文件，觉得没问题"。

（这是本项目的验收纪律：凡碰"机器可查"的部件，就**实测**，别靠"应该没问题"。）
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _build_wheel(tmp_path: Path) -> Path | None:
    """真建一个 wheel —— 在**一份干净的源码副本**里建。

    ## 三件实测出来的事（2026-10-05），缺一件这函数就守不住东西

    1. **必须在干净副本里建**：setuptools 的 `build/lib/` 是**增量**目录 ——
       上一次构建留下的 `build/lib/lvs/gui/templates/*.html` 不会因为
       "这次没声明 package-data"而被删掉，它会**照旧被打进 wheel**。
       直接在仓库里建，坏掉的东西会被上一次的好结果掩盖。
    2. **必须 `--no-build-isolation`**：隔离构建会去 PyPI 拿最新 setuptools
       （实测 84.0.0），而新版会自动把包目录里的数据文件塞进 wheel ——
       "漏声明 package-data"这个 bug 在隔离构建下**看不出来**。
       换成 venv 自带的 75.1.0 立刻复现：wheel 66 → 58 个条目，
        `lvs/gui/templates/*.html` 与 `lvs/gui/static/*` 全没了。
    3. **只拷运行时需要的东西**（不是整个仓库）：整个仓库含 `.work/`（6.9 GB）
       与 `.venv/`（2.2 GB），复制一遍既慢又没意义。

    建不出来（缺 setuptools / 权限）就跳过，不算失败。
    """
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        f = ROOT / name
        if f.is_file():
            shutil.copy2(f, src / name)
    for name in ("lvs", "workflows"):
        shutil.copytree(ROOT / name, src / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-build-isolation",
         "--no-deps", "-w", str(tmp_path), "-q"],
        cwd=src, capture_output=True, text=True, timeout=900,
    )
    if proc.returncode != 0:
        pytest.skip(f"pip wheel 不可用：{proc.stderr[-300:]}")
    wheels = list(tmp_path.glob("*.whl"))
    if not wheels:
        pytest.skip("没有产出 wheel")
    return wheels[0]


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> Path:
    """建一次 wheel（构建要十几秒，别重复建）。"""
    return _build_wheel(tmp_path_factory.mktemp("wheelout"))


@pytest.fixture(scope="module")
def wheel_names(wheel: Path) -> list[str]:
    return zipfile.ZipFile(wheel).namelist()


# ---- 子包不能漏 -------------------------------------------------------------


def test_wheel_contains_the_gui_subpackage(wheel_names: list[str]):
    """★ 核心回归：`lvs.gui` 必须打进 wheel。

    写死 `packages = ["lvs"]` 会静默漏掉它 —— 装完 `lvs gui` 就崩。
    """
    gui = [n for n in wheel_names if n.startswith("lvs/gui/")]
    assert gui, "wheel 里没有 lvs/gui/ —— `pip install` 之后 GUI 用不了"
    assert any(n.endswith("app.py") for n in gui), f"gui 子包不完整：{gui[:5]}"


def test_wheel_contains_every_top_level_module(wheel_names: list[str]):
    """`lvs/` 下的每个 .py 都要在 wheel 里（防"部分模块漏打"）。"""
    on_disk = {p.name for p in (ROOT / "lvs").glob("*.py")}
    packed = {Path(n).name for n in wheel_names if n.startswith("lvs/") and n.endswith(".py")}
    missing = sorted(on_disk - packed)
    assert missing == [], f"这些模块没打进 wheel：{missing}"


# ---- 运行时数据不能漏 -------------------------------------------------------


def test_wheel_contains_comfyui_workflow_templates(wheel_names: list[str]):
    """★ 生图工作流模板是**运行时要读的数据**：光有 .py 不够。

    `imagegen.template_path()` 按 `PROJECT_ROOT/workflows/` 找它；
    装到 `site-packages` 后 `PROJECT_ROOT` 就是 `site-packages`，
    所以模板必须落在 `workflows/`（顶层）。
    """
    wf = [n for n in wheel_names if n.startswith("workflows/") and n.endswith(".json")]
    assert wf, "wheel 里没有 workflows/*.json —— 装完生图找不到模板"
    on_disk = {p.name for p in (ROOT / "workflows").glob("*.json")}
    packed = {Path(n).name for n in wf}
    assert on_disk <= packed, f"这些模板没打进包：{sorted(on_disk - packed)}"


# ---- GUI 的模板与静态资源不能漏 ----------------------------------------------


def test_wheel_contains_gui_templates_and_static(wheel_names: list[str]):
    """★ GUI 的 `templates/` 与 `static/` 必须打进 wheel —— 它们是**运行时要读的数据**。

    实测（2026-10-05）：`[tool.setuptools.package-data]` 只声明
    `"lvs" = ["../workflows/*.json"]` 时，wheel 58 个条目里
    `lvs/gui/templates/*.html` 与 `lvs/gui/static/*` **一个都没有** ——
    `pip install` 之后 `lvs gui` 每个页面都 404，而 `pip wheel` 静默通过。

    为什么原来的 `test_wheel_contains_the_gui_subpackage` 没抓住：
    它只断言 `any(n.endswith("app.py") for n in gui)` ——
    **有 .py 就算过**，模板与静态资源从来不在它的检查范围内。
    所以这里改成"磁盘上有几个就必须打进几个"，不再用 `any()`。
    """
    t_dir = ROOT / "lvs" / "gui" / "templates"
    s_dir = ROOT / "lvs" / "gui" / "static"
    on_disk_t = {p.name for p in t_dir.glob("*.html")}
    on_disk_s = {p.name for p in s_dir.iterdir() if p.is_file()}
    assert on_disk_t and on_disk_s, f"源码树里就找不到 GUI 资源：{t_dir} / {s_dir}"

    packed_t = {Path(n).name for n in wheel_names if "/gui/templates/" in n}
    packed_s = {Path(n).name for n in wheel_names if "/gui/static/" in n}
    missing_t = sorted(on_disk_t - packed_t)
    missing_s = sorted(on_disk_s - packed_s)
    assert missing_t == [], f"GUI 模板没打进 wheel（装完页面 404）：{missing_t}"
    assert missing_s == [], f"GUI 静态资源没打进 wheel（装完样式/脚本 404）：{missing_s}"


# ---- 入口点 -----------------------------------------------------------------


def test_wheel_declares_the_cli_entry_point(wheel: Path, wheel_names: list[str]):
    """`lvs` 这个命令要来自 entry point —— `pip install` 之后才有 `lvs` 可敲。

    真读 wheel 里的 `entry_points.txt`（不是读 pyproject 猜），因为
    元数据可能被构建配置改掉，只有产物说了算。
    """
    entries = [n for n in wheel_names if n.endswith("entry_points.txt")]
    assert entries, "没有 entry_points.txt —— 装完不会有 `lvs` 命令"

    text = zipfile.ZipFile(wheel).read(entries[0]).decode("utf-8")
    assert "[console_scripts]" in text, f"没有 console_scripts 段：{text!r}"
    assert "lvs" in text and "lvs.cli:main" in text, f"入口点不对：{text!r}"


# ---- 配置发现：装好之后用**用户自己的** config.toml ---------------------------


def test_user_config_in_cwd_wins(tmp_path: Path, monkeypatch):
    """装到 site-packages 之后，`PROJECT_ROOT/config.toml` 是包目录里的 ——
    用户在自己的工作目录放一份 config.toml，必须**以它为准**。"""
    from lvs.config import find_config

    work = tmp_path / "myproj"
    work.mkdir()
    (work / "config.toml").write_text('[tts]\nbackend = "edge"\n', encoding="utf-8")
    monkeypatch.chdir(work)

    found = find_config(None)
    assert found is not None and found.parent == work, f"没优先用工作目录里的配置：{found}"
