"""依赖声明（2026-10-05 审查 E03 / 清单 #9）：**代码里 import 的第三方包必须都声明过**。

为什么必须是机器判据：原 `dependencies` 只有 `tomli` 一行，而代码里 import 了
7 个第三方包。干净环境 `pip install -e .` 之后 `lvs graphic` / `lvs gui` /
`lvs publish` 全是 ImportError —— 报的还不是"请先装 X"，是"内部错误"。

这里的判据比"人眼看一遍"硬（也不用建 wheel，所以是快组）：
- 用 **ast** 扫 `lvs/**/*.py` 的全部 import（不是 grep —— 注释与字符串不会误报）；
- 拿 `sys.stdlib_module_names` 剔掉标准库，剔掉第一方 `lvs`；
- 第三方模块名 → 发行包名有一张**显式映射表**（`PIL` → `Pillow` 这类）；
- 每个发行包必须出现在 `dependencies` 或某个 extras 里；
- **每个依赖串都必须有上界**（`<`）：下界是能力下限，上界是"别在半夜自动跳大版本"
  的保险丝。上界只挡大版本，抬上界 = 改一行 + 跑一次全量回归。
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"

#: 模块名 → 发行包名（PEP 503 归一化后比较）。只有**两者不一致**的才需要写在这里。
DIST_ALIAS = {
    "PIL": "pillow",
    "cv2": "opencv-python",
    "imageio_ffmpeg": "imageio-ffmpeg",
    "edge_tts": "edge-tts",
    "faster_whisper": "faster-whisper",
}

#: 传递依赖 / 可选探测，不算"我们欠声明"。每条都要写清理由，免得这张表变成垃圾桶。
TRANSITIVE = {
    "ctranslate2": "faster-whisper 的传递依赖；代码里只用来探测 CUDA 可用性",
}


def _normalize(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _dist_name(spec: str) -> str:
    """`Pillow>=10.0,<12` → `pillow`。"""
    return _normalize(re.split(r"[<>=!~\s\[]", spec.strip(), maxsplit=1)[0])


def _imported_third_party() -> dict[str, set[str]]:
    """`lvs/**/*.py` 里的第三方顶层 import：模块名 → 出现的文件。"""
    found: dict[str, set[str]] = {}
    for path in sorted((ROOT / "lvs").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top in sys.stdlib_module_names or top == "lvs":
                    continue
                found.setdefault(top, set()).add(path.name)
    return found


def _declared() -> dict[str, list[str]]:
    """发行包名 → 它出现在哪里（"dependencies" 或 extras 名）。"""
    with open(PYPROJECT, "rb") as fh:
        project = tomllib.load(fh)["project"]
    where: dict[str, list[str]] = {}
    for spec in project.get("dependencies", []):
        where.setdefault(_dist_name(spec), []).append("dependencies")
    for extra, specs in (project.get("optional-dependencies") or {}).items():
        for spec in specs:
            where.setdefault(_dist_name(spec), []).append(f"extras:{extra}")
    return where


class DeclaredDependenciesTest(unittest.TestCase):
    def test_every_third_party_import_is_declared(self) -> None:
        declared = _declared()
        missing: list[str] = []
        for module, files in sorted(_imported_third_party().items()):
            if module in TRANSITIVE:
                continue
            dist = DIST_ALIAS.get(module, _normalize(module))
            if dist not in declared:
                missing.append(f"{module}（{dist}）← {', '.join(sorted(files))}")
        self.assertEqual(missing, [], "有 import 没有声明：\n  " + "\n  ".join(missing))

    def test_extras_cover_the_real_workflows(self) -> None:
        """extras 名字是"装哪组能干什么"的对外承诺，删掉任何一个都是破坏契约。"""
        with open(PYPROJECT, "rb") as fh:
            extras = tomllib.load(fh)["project"]["optional-dependencies"]
        for name in ("tts", "http", "align", "image", "comfy", "gui", "qc", "all", "dev"):
            self.assertIn(name, extras, f"extras `{name}` 没了（界面/图文卡/发布靠它）")
        for need in ("pillow", "flask", "tomlkit", "numpy"):
            self.assertIn(need, {_dist_name(s) for s in extras["all"]},
                          f"`all` 少了 {need} —— 「一人开发全装」的承诺就断了")

    def test_every_dependency_has_an_upper_bound(self) -> None:
        with open(PYPROJECT, "rb") as fh:
            project = tomllib.load(fh)["project"]
        specs = list(project.get("dependencies", []))
        for extra_specs in (project.get("optional-dependencies") or {}).values():
            specs.extend(extra_specs)
        bad = [s for s in specs if "<" not in s.split(";")[0]]
        self.assertEqual(bad, [], f"这些依赖没有上界（大版本会半夜自己跳）：{bad}")

    def test_align_is_not_in_all(self) -> None:
        """`all` 故意不含 faster-whisper（GB 级重依赖）—— 别"顺手补上"。"""
        with open(PYPROJECT, "rb") as fh:
            extras = tomllib.load(fh)["project"]["optional-dependencies"]
        self.assertNotIn("faster-whisper", {_dist_name(s) for s in extras["all"]})
