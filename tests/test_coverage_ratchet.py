"""覆盖率棘轮：关键模块的覆盖率**只许升不许降**（M03）。

为什么全局 `fail_under` 不够：全局数字可以被"给简单模块补测试"填平，
而真正会静默坏掉的是编排入口、配音、生图、LLM 这几条主路径。

用法（先产出覆盖率数据，再跑本文件）：
    ./.venv/Scripts/python.exe -m coverage run -m pytest -m "not slow and not gpu"
    ./.venv/Scripts/python.exe -m coverage json -o .work/tmp/cov.json
    ./.venv/Scripts/python.exe -m pytest tests/test_coverage_ratchet.py

没有 `.work/tmp/cov.json` 时整体 skip —— 日常 `pytest -m "not slow and not gpu"`
不会因为缺数据而变红（门禁是选做的一条命令，不是每次跑测试的固定开销）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COV_JSON = ROOT / ".work" / "tmp" / "cov.json"
COVERAGERC = ROOT / ".coveragerc"

#: 模块 → 水位（2026-10-05 实测值向下取 3 个点，留余量）。
#: 改主路径后**只许往上抬**：实测 → 减 3 → 写回这里。
RATCHET: dict[str, float] = {
    "lvs/llm.py": 95.0,        # 实测 98.3（M01 修完：原 26%）
    "lvs/studio.py": 37.0,     # 实测 40.4
    "lvs/tts.py": 55.0,        # 实测 58.3
    "lvs/cli.py": 53.0,        # 实测 56.8
    "lvs/run.py": 60.0,        # 实测 63.4
    "lvs/imagegen.py": 61.0,   # 实测 64.4
    "lvs/assets.py": 61.0,     # 实测 64.5
    "lvs/cast.py": 65.0,       # 实测 68.5
    "lvs/gui/app.py": 56.0,    # 实测 59.2
    "lvs/publish.py": 76.0,    # 实测 79.3
}


def _load() -> dict:
    data = json.loads(COV_JSON.read_text(encoding="utf-8"))
    # Windows 上 key 是 `lvs\studio.py`，统一成 `/` 再比。
    return {k.replace("\\", "/"): v for k, v in data["files"].items()}


def _total() -> float:
    data = json.loads(COV_JSON.read_text(encoding="utf-8"))
    return float(data["totals"]["percent_covered"])


def _fail_under() -> float:
    text = COVERAGERC.read_text(encoding="utf-8")
    return float(re.search(r"^fail_under\s*=\s*([\d.]+)", text, re.M).group(1))


@pytest.mark.skipif(not COV_JSON.exists(),
                    reason="需要先跑 coverage run + coverage json（见本文件顶部用法）")
def test_key_modules_do_not_regress() -> None:
    files = _load()
    bad: list[str] = []
    for mod, floor in RATCHET.items():
        entry = files.get(mod)
        if entry is None:
            bad.append(f"{mod}: 覆盖率数据里没有这个文件（改名了？）")
            continue
        got = float(entry["summary"]["percent_covered"])
        if got + 1e-9 < floor:
            bad.append(f"{mod}: {got:.1f}% < 水位 {floor:.1f}%")
    assert bad == [], "覆盖率退步：\n  " + "\n  ".join(bad)


@pytest.mark.skipif(not COV_JSON.exists(), reason="同 test_key_modules_do_not_regress")
def test_total_stays_above_the_configured_floor() -> None:
    """`.coveragerc` 的 fail_under 与实测总覆盖率不许脱节。"""
    assert _total() + 1e-9 >= _fail_under()