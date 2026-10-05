"""`lvs config check` —— 配置体检：**必填在不在 / 类型对不对 / 有没有写错的键 / 各段认得出吗**。

## 为什么需要它（提案 §3 B2 的同族问题）

`config.toml` 是本项目唯一的"接错就静默"的入口：

- 键名写错（`[shots] style_name = …`）→ 代码读不到就走**默认值**，不报错，只是画面变了；
- 类型写错（`zoom_amount = "0.15"`）→ 各阶段的 `float(...)` 会炸在很深的地方；
- 段名写错（`[provider]` 少个 s）→ 整个路由静默失效，全走远程（花钱）。

所以这里把"配置像不像样"做成一条**能先跑、能进 CI** 的命令，与
`schemas/config.schema.json` 对着干 —— 契约在那边，报告在这边。

## 语义（很关键）

- **校验的是"合并后的配置"**（`[base] config = …` 已展开、环境变量已叠加）——
  也就是**流水线真正看到的**那份。只看本文件的键，会把继承来的段漏报成"缺失"。
- **未知键只警告，不算错**：代码里有动态键（`bgm.<key>` / `comfyui.<key>` /
  `[styles.<名字>]`），一刀切会误报；而"误报会让人不再信任校验器"。
- **必填很少**（`paths.lib` / `shots.style|visual_mode|source_mode`）：只列"缺了以后
  下游会用默认值继续、且默认值一定不对"的那几个。宁可少报，也不要让人因为瞎报而忽略它。
- 退出码：`1` = 有 error（改了再来）；`0` = 通过（warn 不拦）；`2` = 根本没找到配置文件。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from lvs import config as config_mod
from lvs import schemas
from lvs.errors import EXIT_FAILED, EXIT_OK, EXIT_USAGE

LEVEL_ERROR = "error"
LEVEL_WARN = "warn"
LEVEL_INFO = "info"

#: 已知段 → 一句话。识别到的段在报告里点名；缺失的段进 `sections.missing`。
SECTION_NOTES: dict[str, str] = {
    "app": "LLM 连接（密钥也可走环境变量）",
    "paths": "项目素材库根",
    "pexels": "Pexels 实拍素材",
    "tts": "配音后端",
    "voice": "配音节奏与字幕",
    "shots": "分镜与画面风格",
    "build": "成片合成",
    "bgm": "背景音乐（可选；`lvs bgm` 可本地生成）",
    "gpu": "显存守卫",
    "library": "本地素材库",
    "comfyui": "本地生图",
    "cast": "定妆库",
    "qc": "生图巡检",
    "pipeline": "流水线门禁",
    "budget": "LLM 成本闸门（observe / warn / cap）",
    "providers": "LLM 路由（本地 ollama / 远程）",
    "publish": "投稿物料",
    "styles": "装机级内联风格",
}

#: 报告里最多列几条（363 条同样的错没有意义）。
SHOW_LIMIT = 10


@dataclass(frozen=True)
class Finding:
    """一条体检结论。`key` 是点分键（找不到具体键时是空串）。"""

    level: str
    message: str
    key: str = ""

    def render(self) -> str:
        mark = {"error": "✗", "warn": "⚠", "info": "·"}.get(self.level, "·")
        where = f"`{self.key}`：" if self.key else ""
        return f"  {mark} {where}{self.message}"


def _unknown_keys(data: Any, table: dict[str, frozenset[str]], prefix: str = "") -> list[str]:
    """配置里有、契约里没声明的键（递归；**自由表不查**，见 `schemas.known_keys`）。"""
    props = table.get(prefix)
    if props is None or not isinstance(data, dict):
        return []
    out: list[str] = []
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if key not in props:
            out.append(path)
            continue
        if isinstance(value, dict):
            out.extend(_unknown_keys(value, table, path))
    return out


def _section_lines(config: config_mod.Config) -> list[Finding]:
    """段识别：`[providers]` / `[budget]` / `[bgm]` 三段的**当前生效语义**。"""
    from lvs import llm_provider

    data = config.as_dict()
    out: list[Finding] = []

    mode = llm_provider.mode(config)
    routes = llm_provider.route_table(config)
    local = "、".join(s for s, t in routes.items() if t == "local") or "（无）"
    remote = "、".join(s for s, t in routes.items() if t == "remote") or "（无）"
    out.append(Finding(LEVEL_INFO, f"mode={mode}｜local: {local}｜remote: {remote}", "providers"))

    budget_mode = str(data.get("budget", {}).get("mode", "") or "").strip() or "observe"
    cap = float(data.get("budget", {}).get("cap_usd", 0) or 0)
    tail = f"，cap ${cap:g}" if cap else ""
    out.append(Finding(LEVEL_INFO, f"mode={budget_mode}（缺省 observe：只记不拦）{tail}", "budget"))

    bgm_file = str(data.get("bgm", {}).get("file", "") or "").strip()
    note = f"file={bgm_file}（build 会混进去）" if bgm_file else "未配 .file → 不混 BGM（`lvs bgm` 可本地生成）"
    out.append(Finding(LEVEL_INFO, note, "bgm"))
    return out


def _finalize(findings: list[Finding], payload: dict[str, Any]) -> tuple[list[Finding], dict[str, Any]]:
    """补齐 payload 的 `findings` / `counts` / `ok` —— **早退分支也要有**（`--json` 的形状必须稳定）。"""
    payload["findings"] = [{"level": f.level, "key": f.key, "message": f.message} for f in findings]
    payload["counts"] = {
        LEVEL_ERROR: sum(1 for f in findings if f.level == LEVEL_ERROR),
        LEVEL_WARN: sum(1 for f in findings if f.level == LEVEL_WARN),
        LEVEL_INFO: sum(1 for f in findings if f.level == LEVEL_INFO),
    }
    payload["ok"] = payload["counts"][LEVEL_ERROR] == 0
    return findings, payload


def check(path: Path | str | None) -> tuple[list[Finding], dict[str, Any]]:
    """体检一份配置文件。返回 `(findings, payload)`；`payload` 给 `--json` 用。"""
    from lvs.errors import LvsError

    findings: list[Finding] = []
    payload: dict[str, Any] = {"path": str(path or ""), "sections": {"present": [], "missing": []}}

    if not path or not Path(path).is_file():
        findings.append(Finding(LEVEL_ERROR, f"找不到配置文件：{path or '（未指定）'}"
                                            f"\n     复制模板：{config_mod.PROJECT_ROOT / config_mod.EXAMPLE_FILENAME}"
                                            f"  →  {config_mod.PROJECT_ROOT / config_mod.CONFIG_FILENAME}"))
        return _finalize(findings, payload)

    src = Path(path)
    try:
        with open(src, "rb") as fh:
            tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        findings.append(Finding(LEVEL_ERROR, f"TOML 语法有误：{exc}"))
        return _finalize(findings, payload)
    except OSError as exc:
        findings.append(Finding(LEVEL_ERROR, f"读不了这个文件：{exc}"))
        return _finalize(findings, payload)

    try:
        config = config_mod.Config.load(src)
    except LvsError as exc:
        findings.append(Finding(LEVEL_ERROR, f"配置加载失败：{exc}"))
        return _finalize(findings, payload)
    except Exception as exc:  # noqa: BLE001 - 体检报告不该被自己的读取炸掉
        findings.append(Finding(LEVEL_ERROR, f"配置加载异常：{type(exc).__name__}: {exc}"))
        return _finalize(findings, payload)

    data = config.as_dict()
    payload["env_keys"] = sorted(config.env_keys)

    # 1) 契约：必填 / 类型（未知键不在契约里报，见下）
    for problem in schemas.validate("config", data):
        key = problem.split("：", 1)[0].lstrip("$.")
        findings.append(Finding(LEVEL_ERROR, problem.split("：", 1)[-1], key))

    # 2) 未知键：只警告
    for key in sorted(set(_unknown_keys(data, schemas.known_keys("config")))):
        section = key.split(".")[0]
        known = schemas.known_keys("config").get(section) or frozenset()
        hint = "、".join(sorted(known)[:8]) if known else ""
        findings.append(Finding(
            LEVEL_WARN,
            f"未知键（代码里没人读它 —— 写错名字会**静默走默认值**）"
            + (f"；该段已知：{hint}" if hint else ""),
            key,
        ))

    # 3) 段识别
    present = [name for name in SECTION_NOTES if name in data]
    missing = [name for name in SECTION_NOTES if name not in data]
    payload["sections"] = {"present": present, "missing": missing}
    findings.extend(_section_lines(config))

    if config.env_keys:
        findings.append(Finding(
            LEVEL_INFO,
            "这些键当前**来自环境变量**（值不打印；要改就改环境变量）："
            + "、".join(sorted(config.env_keys)),
        ))

    return _finalize(findings, payload)


def run_command(config_path: Path | str | None, args: Any) -> int:  # noqa: ANN401 - 由 cli 传入
    """`lvs config check`。退出码：0 通过 / 1 有 error / 2 找不到配置文件。"""
    findings, payload = check(config_path)
    as_json = bool(getattr(args, "json", False))
    missing_file = not (config_path and Path(str(config_path)).is_file())
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if missing_file:
            return EXIT_USAGE
        return EXIT_OK if payload.get("ok") else EXIT_FAILED

    errors = [f for f in findings if f.level == LEVEL_ERROR]
    warns = [f for f in findings if f.level == LEVEL_WARN]
    infos = [f for f in findings if f.level == LEVEL_INFO]

    print(f"配置体检：{config_path or '（未找到）'}")
    if payload.get("sections", {}).get("present") or payload.get("sections", {}).get("missing"):
        present = "、".join(payload["sections"]["present"])
        missing = "、".join(payload["sections"]["missing"])
        print(f"  段 {len(payload['sections']['present'])}/{len(SECTION_NOTES)}：{present}")
        if missing:
            print(f"  未配（走内置默认）：{missing}")
    for f in infos:
        print(f.render())

    for level, group in (("error", errors), ("warn", warns)):
        if not group:
            continue
        print(f"\n{'✗' if level == 'error' else '⚠'} {len(group)} 个 {level}：")
        for f in group[:SHOW_LIMIT]:
            print(f.render())
        if len(group) > SHOW_LIMIT:
            print(f"  …（其余 {len(group) - SHOW_LIMIT} 条省略）")

    if missing_file:
        return EXIT_USAGE
    if errors:
        print(f"\n结论：{len(errors)} 个 error，{len(warns)} 个 warn。error 必须改 —— "
              "它们要么让下游炸在深处，要么让某个设置**静默失效**。")
        return EXIT_FAILED
    print(f"\n结论：✓ 通过（{len(warns)} 个 warn 建议一并看看）。")
    return EXIT_OK
