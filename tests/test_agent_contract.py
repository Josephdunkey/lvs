"""Agent 接口契约 —— `--json` 必须是**机器真的能读**的。

## 为什么单开一组

用户的第一使用方式是 **"让 agent 直接调用这个项目"**（拍摄稿 → 自动成片）。
那么 `--json` 的输出就是**程序的接口**，不是装饰。

实测踩到（2026-10-03）：`--json` 的开关在 9 个命令上都存在，
帮助文字也写着"机器可读输出（给 Agent 用）"，但：

| 命令 | 实际 |
|---|---|
| `parse` / `shots` / `assets` / `voice` / `build` / `run` | 信封是**最后一段**（设计如此，散文在前） |
| `gate` | 干净 JSON |
| **`board` / `doctor`** | **根本没有 `--json`** —— 而 `doctor.run(as_json=…)` 早就实现了，CLI 从没传过（"定义了却从不接线"） |

于是 agent 在长任务里**没法机器可读地问"到哪了 / 环境行不行"**，
只能去解析中文散文里的 ✅⬜ 方块字 —— 最脆的那种依赖。

## 这组测试守的两条契约

1. **`board` / `doctor` 的 `--json` 只打 JSON** —— `json.loads(stdout)` 直接成功
2. **其余命令的 `--json`：末段必须是可解析的信封** —— 这是既有设计
   （docstring 明说"agent 只取最后那段 JSON 即可"），此处把它**变成判据**，
   免得哪天有人加了一句散文把它挤到中间去
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def _run(args: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, "-m", "lvs", *args],
        capture_output=True, text=True, encoding="utf-8",
        cwd=str(ROOT), timeout=timeout,
    )


def _trailing_json(text: str) -> dict:
    """按**既有契约**取最后一段 JSON 信封。

    从末尾往前找第一个能解析成 dict 的 `{` 起点 —— 这正是 agent 要做的事。
    找不到就抛，让测试红。
    """
    starts = [i for i, ch in enumerate(text) if ch == "{"]
    for i in reversed(starts):
        try:
            got = json.loads(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(got, dict):
            return got
    raise AssertionError(f"输出里没有可解析的 JSON 信封：\n{text[-400:]}")


# ---- ① board / doctor：必须**干净**的 JSON（用户最需要 agent 读这两条） ----


def test_board_json_is_pure_and_tells_the_next_stage(tmp_path: Path):
    """★ `board --json` 只打 JSON，且给出 **next_stage**。

    agent 循环靠它回答"现在到哪了、下一步该跑什么"，不用解析中文看板。
    """
    from lvs.workspace import Workspace

    ws = Workspace(task="agentprobe", root=tmp_path).ensure()
    ws.write_shots({"source_mode": "local", "shots": [
        {"id": 1, "source": "local", "narration": "a", "visual": "b"},
    ]})

    r = _run(["board", "--task", "agentprobe", "--json"])
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)          # ★ 必须**只**是 JSON，不能有散文

    assert payload["task"] == "agentprobe"
    assert "stages" in payload and isinstance(payload["stages"], list)
    assert "next_stage" in payload
    assert "shots" in payload


def test_doctor_json_is_pure_and_lists_checks():
    """★ `doctor --json` 只打 JSON，且每项带 `status` —— agent 靠它决定能不能开跑。

    `doctor.run(as_json=…)` 早就有，缺的从来是 CLI 那根线。
    """
    r = _run(["doctor", "--json"])
    payload = json.loads(r.stdout)          # 同样必须纯净

    assert isinstance(payload, list) and payload
    for c in payload:
        assert {"name", "status", "detail"} <= set(c), f"体检项字段不全：{c}"
    # 退出码语义：有"缺失"→1，否则 0（agent 据此判断环境）
    assert r.returncode in (0, 1), r.returncode


def test_doctor_json_has_no_human_prose():
    """反证：纯净性是**真的**（不能靠"恰好第一行是 {" 蒙混）。"""
    r = _run(["doctor", "--json"])
    first = r.stdout.lstrip()[:1]
    assert first == "[", f"`--json` 应当直接以 JSON 开头，实际以 {first!r} 开头"


# ---- ② 其余命令：末段信封（既有契约，这里把它变成判据） --------------------


@pytest.mark.parametrize("args", [
    ["parse", "--task", "agentprobe", "--json"],
    ["shots", "--task", "agentprobe", "--no-llm", "--json"],
    ["board", "--task", "agentprobe", "--json"],
])
def test_envelope_is_the_last_parseable_segment(tmp_path: Path, args: list[str]):
    """★ 把"agent 只取最后那段 JSON"这条**口头约定**变成可执行判据。

    必要性：散文里完全可能出现 `{SAIGYO}` 这类**花括号槽位名**
    （提示词里就有），所以"找最后一个 `{` 再解析"必须真的成立。
    哪天有人往信封**后面**加了一行输出，这条会红。
    """
    from lvs.workspace import Workspace

    Workspace(task="agentprobe", root=tmp_path).ensure()
    # 造一份带花括号槽位的 shots，逼出"散文里有 {…}"的情况
    ws = Workspace(task="agentprobe", root=tmp_path).ensure()
    ws.write_shots({"source_mode": "local", "shots": [
        {"id": 1, "source": "local", "narration": "a", "visual": "{SAIGYO} 在雪里"},
    ]})

    r = _run(args)
    assert r.returncode in (0, 1, 2, 3), r.returncode

    if args[0] in ("board",):
        json.loads(r.stdout)                 # board 是纯净契约
        return
    env = _trailing_json(r.stdout)
    assert "exit_code" in env, f"信封缺 exit_code：{env}"


# ---- ③ 契约里那几个字段：agent 的决策依据 ----------------------------------


def test_envelope_locks_the_decision_fields():
    """`result.py` 声明只锁 5 个决策字段 —— 它们必须真的在。

    agent 靠 `ok / status / exit_code / counts.failed / next_hint` 决定下一步；
    这些字段没了或改名，所有上层自动化都会静默走错分支。
    """
    from lvs import result as result_mod

    assert set(result_mod.LOCKED_FIELDS) == {
        "ok", "status", "exit_code", "counts", "next_hint",
    }


def test_exit_code_semantics_are_stable():
    """四态退出码是 agent 唯一的**跨进程**信号，语义不许漂。"""
    from lvs import errors

    assert errors.EXIT_OK == 0        # 成功
    assert errors.EXIT_FAILED == 1    # 有失败件（逐镜）
    assert errors.EXIT_USAGE == 2     # 输入/前置不对（别重试）
    assert errors.EXIT_BLOCKED == 3   # 等人审（等，不是错）
