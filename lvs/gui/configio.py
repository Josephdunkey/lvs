"""配置编辑（票据 37）。

`config.toml` 里的注释每一行都是说明，**改它绝不能把注释抹掉**。
所以回写用 `tomlkit`（保留注释 / 顺序 / 格式），不用 `tomllib`+重新序列化。

边界（上一轮定的是"配置只读"，这一轮用户明确要能改素材库位置等）：
- 只开放**白名单里的键**；其余（如 `build.subtitle_style`）一律不可改。
- 密钥只读时**打码**，改的时候填新值（留空 = 不改）。
- 一次写失败（磁盘/权限）要抛错，不能让界面误以为保存成功。
"""

from __future__ import annotations
from lvs.errors import UsageError

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit


class ConfigIOError(UsageError, ValueError):
    """配置读写的可预期失败（文件没了 / 值不合法 / 写不进）。

    归类为 `UsageError`（退出码 2）：它的解药是"改一下配置再来"，
    不是"重试"也不是"等人审批"。多重继承 `ValueError` 是为了兼容既有调用点。
    """


@dataclass(frozen=True)
class Field:
    key: str          # 点分路径，如 "library.dirs"
    label: str
    kind: str         # str | int | bool | choice | dirs
    choices: tuple[str, ...] = ()
    secret: bool = False
    hint: str = ""


EDITABLE: tuple[Field, ...] = (
    Field("library.dirs", "素材库目录", "dirs",
          hint="每行一个目录；留空 = 关闭「翻库优先」"),
    Field("library.min_score", "关键词命中阈值", "int"),
    Field("app.openai_api_key", "LLM Key（拆镜必填）", "str", secret=True),
    Field("app.openai_base_url", "LLM 地址", "str"),
    Field("app.openai_model_name", "LLM 模型", "str"),
    Field("pexels.api_key", "Pexels Key", "str", secret=True),
    Field("shots.visual_mode", "画面模式", "choice", ("graphic", "photo")),
    Field("build.kenburns", "运镜", "choice", ("zoom-in", "zoom-out", "pan-right", "pan-up", "none")),
    Field("build.kenburns_supersample", "抗抖超采样倍数", "int"),
    Field("comfyui.base_url", "ComfyUI 地址", "str"),
)

_SECRET_SUFFIXES = ("api_key", "key", "token", "secret", "password")


def is_secret_key(key: str) -> bool:
    """这个键名看着像密钥吗 —— 只有这一处判定（设置页的「全部配置」表也用它）。"""
    return any(suffix in (key or "").lower() for suffix in _SECRET_SUFFIXES)


def mask(value: Any) -> str:
    """密钥值打码：长值只露头尾，短值只说「已设置」，空值给空串。"""
    text = str(value or "")
    if not text:
        return ""
    if len(text) > 10:
        return text[:4] + "…" + text[-4:]
    return "已设置"


_MASK_CHAR = "…"           # 必须与 mask() 用的那个字符一致
_SET_SENTINEL = "已设置"    # mask() 对短值的产物


def looks_masked(value: Any) -> bool:
    """这个值是不是 `read_editable()` 自己吐出去的掩码？

    ★ 为什么必须在**后端**挡（2026-10-05 审查 S01）：设置页把掩码填进了
      `value=`，用户点一次「保存」就把 `sk-1…cdef` 当真值写回 —— 而它是一串
      **永远不可能通过校验**的字符串，等于把真 key 静默删掉，且不可恢复
      （原值不在任何地方留副本）。触发概率极高：只是想改一下 `library.dirs`
      也会踩到。前端只要哪天忘了判空（或浏览器自动填充、或有人直接 curl 打
      `/api/config`），掩码就会被当成"用户填的新密钥"。后端是最后一道，
      也是唯一**不依赖前端正确性**的那道。
    """
    text = str(value or "")
    return _MASK_CHAR in text or text == _SET_SENTINEL


_BY_KEY = {f.key: f for f in EDITABLE}


def _coerce(field: Field, raw: Any) -> Any:
    if field.kind == "dirs":
        if isinstance(raw, str):
            items = [line.strip() for line in raw.splitlines() if line.strip()]
        elif isinstance(raw, (list, tuple)):
            items = [str(x).strip() for x in raw if str(x).strip()]
        else:
            raise ValueError("素材库目录得是一行一个的文本或列表")
        return items

    if field.kind == "int":
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"`{field.key}` 需要整数") from None
        if value < 0:
            raise ValueError(f"`{field.key}` 不能为负")
        return value

    if field.kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    text = str(raw).strip()
    if field.kind == "choice":
        if text not in field.choices:
            raise ValueError(f"`{field.key}` 只接受：{'/'.join(field.choices)}（收到 {text!r}）")
        return text
    return text


def _set(doc: Any, dotted: str, value: Any) -> None:
    *sections, key = dotted.split(".")
    node = doc
    for section in sections:
        if section not in node:
            node[section] = tomlkit.table()
        node = node[section]
    if isinstance(value, list):
        node[key] = tomlkit.array().multiline(False)
        for item in value:
            node[key].append(item)
    else:
        node[key] = value


def _is_secret(key: str) -> bool:
    return any(suffix in key.lower() for suffix in _SECRET_SUFFIXES)


def read_editable(path: Path | str) -> dict[str, dict[str, Any]]:
    """把白名单键的当前值读出来（密钥打码），供设置页渲染表单。"""
    path = Path(path)
    if not path.is_file():
        raise ConfigIOError(f"找不到配置文件：{path}")
    try:
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    except Exception as exc:                        # noqa: BLE001 - tomlkit 的解析异常
        raise ConfigIOError(f"配置解析失败：{exc}") from exc

    out: dict[str, dict[str, Any]] = {}
    for field in EDITABLE:
        node: Any = doc
        for section in field.key.split(".")[:-1]:
            node = node.get(section, {}) if isinstance(node, dict) else {}
        leaf = field.key.split(".")[-1]
        value = node.get(leaf) if isinstance(node, dict) else None
        shown = mask(value) if field.secret else value
        out[field.key] = {
            "label": field.label,
            "kind": field.kind,
            "choices": list(field.choices),
            "secret": field.secret,
            "hint": field.hint,
            "value": shown,
            # ★ "这个键有值、但回显的是掩码"。前端据此**不把掩码填进输入框**
            #   （S01 的另一半：后端挡写回，前端从源头不产生掩码提交）。
            "masked": bool(field.secret and looks_masked(shown)),
        }
    return out


def apply(path: Path | str, changes: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """校验 + 回写 `changes`，返回写回后的 `read_editable`。

    `changes` 只包含用户**改过**的键（密钥留空 = 不改，前端不提交）。
    """
    path = Path(path)
    if not path.is_file():
        raise ConfigIOError(f"找不到配置文件：{path}")
    if not changes:
        return read_editable(path)

    bad = sorted(set(changes) - set(_BY_KEY))
    if bad:
        raise ConfigIOError(f"这些键不允许在界面里改：{bad}")

    doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    coerced: dict[str, Any] = {}
    for key, raw in changes.items():
        try:
            coerced[key] = _coerce(_BY_KEY[key], raw)
        except ValueError as exc:
            raise ConfigIOError(str(exc)) from exc

    for key, value in coerced.items():
        # ★ 掩码在**这里**被挡住（最后一道，也是不依赖前端的那道）。见 looks_masked()。
        if _BY_KEY[key].secret and looks_masked(value):
            raise ConfigIOError(
                f"拒绝写入 `{key}`：提交的是设置页回显的**掩码**（{value!r}），不是真密钥。\n"
                "  要换密钥请填完整的新值；不想改就别动那个输入框（留空 = 不改）。"
            )
        _set(doc, key, value)

    # 原子写：先写临时文件再替换，避免写到一半断电坏文件
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        raise ConfigIOError(f"写配置失败：{exc}") from exc
    return read_editable(path)
