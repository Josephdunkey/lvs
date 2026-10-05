"""产物契约的**外置**表达（JSON Schema）—— 让"产物长什么样"有个**代码之外**的判据。

## 为什么要有它（提案 §3 B2）

`lvs/handoff.py` 把"一份分镜表必须长什么样"写在了代码里。这够用，但有三件事它做不到：

1. **手改产物后没有独立契约可校验**：`shots.json` 是**设计成给人手改**的真相源，
   改完只能靠下游阶段撞上去才发现结构坏了（那时已经在下游深处，报的是看不懂的 `KeyError`）。
2. **外部工具 / Agent 无法自检**：别的程序想知道"这份产物合不合规"，只能去读 Python 源码。
3. **契约与实现会悄悄漂移**：判据写在代码里，就没有第二处能对它做交叉检查。

外置成 JSON Schema 之后，"同一件事"有了两个独立表达 ——
`lvs/handoff.validate_shots`（给人话与"该重跑哪条命令"）与
`schemas/shots.schema.json`（给机器、给外部工具）。
两者**互为镜像**：`tests/test_schemas.py` 用一组"人为破坏的字段"钉住
"两边必须同时变红"，任一边单飞就红。

## 分层

本模块是**只读叶子**（除了 `lvs.errors` 谁都不引）：产出方、消费方、门禁判据都能引它而不成环。
契约文件在仓库根的 `schemas/`，`pip install` 之后在顶层 `schemas/`
（`pyproject.toml` 的 package-data 把它打进去了，与 `workflows/` 同一手法）；
`LVS_SCHEMA_DIR` 是逃生阀。找不到时给的是**说得清**的错误，不是 `FileNotFoundError`。

## 边界（别把它做成"风格评判器"）

契约里只放**缺了/坏了会让下游静默出错**的判据 —— 与 `handoff` 同一条原则：
**漏报等于白做，误报会让人不再信任校验器**（这个亏本项目反复吃过）。
所以 schema 刻意**不开** `additionalProperties: false`：代码里有动态键
（`bgm.<key>` / `comfyui.<key>` / `[styles.<名字>]`），一刀切会误报。
未知键由 `lvs config check` 单独给**警告**（`known_keys()` 提供那边用的已知键表）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import jsonschema

from lvs.errors import LvsError

#: 契约名 → 文件名。**加一份就在这张表里登记**（`available()` 与测试都读它）。
SCHEMAS: dict[str, str] = {
    "shots": "shots.schema.json",
    "parse": "parse.schema.json",
    "config": "config.schema.json",
    "qc": "qc.schema.json",
}

#: 契约目录的环境变量逃生阀（打包安装 / CI 里把目录指到别处）。
ENV_SCHEMA_DIR = "LVS_SCHEMA_DIR"

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class SchemaError(LvsError, RuntimeError):
    """契约缺失或本身坏了。消息面向用户：说清"哪个文件、怎么修"。"""


def schema_dir() -> Path:
    """契约目录：`$LVS_SCHEMA_DIR` → 仓库根 `schemas/`。"""
    env = str(os.environ.get(ENV_SCHEMA_DIR, "") or "").strip()
    return Path(env) if env else PROJECT_ROOT / "schemas"


def available() -> tuple[str, ...]:
    """已登记的契约名（顺序固定，测试与 `--json` 都读它）。"""
    return tuple(sorted(SCHEMAS))


def schema_path(kind: str) -> Path:
    """某份契约的文件路径。没登记的名字抛 `SchemaError`（不静默兜底）。"""
    name = SCHEMAS.get(str(kind))
    if name is None:
        raise SchemaError(
            f"没有登记名为 {kind!r} 的契约（有：{'/'.join(available())}）。\n"
            f"  加一份契约要在 `lvs/schemas.SCHEMAS` 里登记。"
        )
    path = schema_dir() / name
    if not path.is_file():
        raise SchemaError(
            f"找不到契约文件：{path}\n"
            f"  契约目录：{schema_dir()}（可用环境变量 {ENV_SCHEMA_DIR} 指到别处）\n"
            f"  它随包分发（wheel 顶层 `schemas/`）—— 找不到就用 {ENV_SCHEMA_DIR} 指过去。"
        )
    return path


#: 编译好的 validator（`iter_errors` 每次都编译 schema 会白花几十毫秒）。
_CACHE: dict[str, jsonschema.protocols.Validator] = {}


def load(kind: str) -> dict[str, Any]:
    """读一份契约（JSON）。坏了给说得清的错误，不是 `JSONDecodeError` 堆栈。"""
    path = schema_path(kind)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SchemaError(f"契约文件不是合法 JSON：{path}\n  {exc}") from exc


def validator(kind: str) -> jsonschema.protocols.Validator:
    """某份契约的 Draft 2020-12 validator（进程内缓存）。"""
    cached = _CACHE.get(kind)
    if cached is not None:
        return cached
    schema = load(kind)
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:  # 契约自己写坏了（不是产物的问题）
        raise SchemaError(f"契约本身不是合法 JSON Schema：{schema_path(kind)}\n  {exc.message}") from exc
    made = jsonschema.Draft202012Validator(schema)
    _CACHE[kind] = made
    return made


def clear_cache() -> None:
    """清掉 validator 缓存（测试换契约目录时用）。"""
    _CACHE.clear()


# ---- 报错说人话 ---------------------------------------------------------------


def _describe(err: jsonschema.ValidationError) -> str:
    """一条校验错误 → 一行**说得清怎么修**的中文。"""
    path = err.json_path
    kind = err.validator
    if kind == "required":
        missing = err.message.split("'")[1] if "'" in err.message else err.message
        return f"{path}：缺少必填字段 `{missing}`"
    if kind == "type":
        return f"{path}：类型应为 {err.validator_value}（现在是 {type(err.instance).__name__}）"
    if kind == "enum":
        allowed = "/".join(str(x) for x in err.validator_value)
        return f"{path}：取值必须是 {allowed}（现在是 {err.instance!r}）"
    if kind == "pattern":
        return f"{path}：不能是空白串"
    if kind == "minimum":
        return f"{path}：不能小于 {err.validator_value}"
    return f"{path}：{err.message}"


def validate(kind: str, data: Any, *, limit: int = 20) -> list[str]:
    """校验一份产物。返回问题列表（空 = 通过）。

    `limit` 是**给输出封顶**（363 镜全坏时不该刷 363 行）：超出部分只在末尾说一句
    "其余 N 处省略"，与 `handoff.render_problems` 同规矩。
    """
    problems = [_describe(e) for e in validator(kind).iter_errors(data)]
    problems = sorted(set(problems))
    if limit and len(problems) > limit:
        head = problems[:limit]
        head.append(f"…（其余 {len(problems) - limit} 处省略）")
        return head
    return problems


# ---- 已知键（给 `lvs config check` 的"未知键警告"用）--------------------------


def _resolve_ref(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """把 `{"$ref": "#/$defs/x"}` 解成 `$defs.x`（只支持本文件内的 `#/` 引用）。"""
    ref = node.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return node
    cur: Any = schema
    for part in ref[2:].split("/"):
        if not isinstance(cur, dict) or part not in cur:
            return node
        cur = cur[part]
    return cur if isinstance(cur, dict) else node


def known_keys(kind: str) -> dict[str, frozenset[str]]:
    """契约里**声明过**的属性表：`点分路径 → 已知键集合`（根路径是空串）。

    只收**声明了 `properties` 的**节点 —— 像 `[styles.<名字>]` / `providers.routes`
    这类"键名由用户定"的表（只有 `additionalProperties`）刻意不收，
    免得把合法的自由表报成"未知键"。
    """
    schema = load(kind)
    table: dict[str, frozenset[str]] = {}

    def walk(node: dict[str, Any], path: str) -> None:
        props = node.get("properties")
        if not isinstance(props, dict):
            return
        table[path] = frozenset(str(k) for k in props)
        for name, sub in props.items():
            if not isinstance(sub, dict):
                continue
            child = _resolve_ref(schema, sub)
            walk(child, f"{path}.{name}" if path else str(name))

    walk(schema, "")
    return table
