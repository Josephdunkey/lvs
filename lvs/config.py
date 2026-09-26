"""配置加载。

约定：配置来自**仓库根的** `config.toml`（模板见 `config.example.toml`）。
`config.toml` 含密钥，已在 `.gitignore` 中。

查找顺序（`find_config`）：
1. 命令行 `--config <path>`
2. 环境变量 `LVS_CONFIG`
3. 当前工作目录的 `config.toml`
4. 仓库根的 `config.toml`
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

# 仓库根 = lvs 包的上一级目录（本文件位于 <root>/lvs/config.py）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILENAME = "config.toml"
EXAMPLE_FILENAME = "config.example.toml"


class ConfigError(Exception):
    """配置缺失或字段非法。消息面向用户，必须说清"缺哪个字段、去哪填"。"""


def find_config(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """按约定顺序定位配置文件，找不到返回 None。"""
    if explicit:
        return Path(explicit)

    env = os.environ.get("LVS_CONFIG")
    if env:
        return Path(env)

    for candidate in (Path.cwd() / CONFIG_FILENAME, PROJECT_ROOT / CONFIG_FILENAME):
        if candidate.is_file():
            return candidate

    return None


def _dotted_get(data: dict[str, Any], dotted: str) -> Any:
    """按 `a.b.c` 取嵌套值，任一层缺失返回 `_MISSING`。"""
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


class _Missing:
    """哨兵：区分"配置里是空字符串"与"配置项不存在"。"""

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<missing>"


_MISSING = _Missing()


class Config:
    """已加载的配置。字段按需 `get` / `require`，不在导入期强制校验。"""

    def __init__(self, data: dict[str, Any], path: Path | None) -> None:
        self._data = data
        self.path = path

    # ---- 构造 -------------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "Config":
        """从文件加载；文件不存在则抛 `ConfigError`（面向用户的提示）。"""
        if not path.is_file():
            example = PROJECT_ROOT / EXAMPLE_FILENAME
            raise ConfigError(
                f"未找到配置文件：{path}\n"
                f"  请复制模板并填写：{example}  →  {path}"
            )
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:  # 语法错误
            raise ConfigError(f"配置文件格式有误：{path}\n  {exc}") from exc
        return cls(data, path)

    @classmethod
    def empty(cls) -> "Config":
        """空的配置对象（供 `lvs doctor` 在无配置时也能体检）。"""
        return cls({}, None)

    # ---- 读取 -------------------------------------------------------------

    def get(self, dotted: str, default: Any = None) -> Any:
        value = _dotted_get(self._data, dotted)
        return default if value is _MISSING else value

    def has(self, dotted: str) -> bool:
        """字段存在**且非空**（空字符串 / 空列表视为未填）。"""
        value = self.get(dotted, _MISSING)
        if value is _MISSING:
            return False
        return not (isinstance(value, (str, list, dict)) and len(value) == 0)

    def require(self, dotted: str, hint: str = "") -> Any:
        """取必填项；缺失或为空时抛 `ConfigError`，消息含字段名与填写位置。"""
        if not self.has(dotted):
            where = f"{self.path}" if self.path else f"{PROJECT_ROOT / CONFIG_FILENAME}"
            tail = f"\n  说明：{hint}" if hint else ""
            raise ConfigError(
                f"缺少配置项 `{dotted}`，请在 {where} 中填写。" + tail
            )
        return self.get(dotted)
