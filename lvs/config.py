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
from lvs.errors import UsageError

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


class ConfigError(UsageError, Exception):
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


def lib_dir(config: Config | None = None) -> Path | None:
    """本项目的**素材库根**（`[paths].lib`）。这是"一本书一套路径"的统一入口。

    为什么要它：`lvs` 要服务的不止一本书。素材库是"这本书的全部资料"所在
    （`00-设定/` 定妆卡与风格、`05-拍摄稿/`、`_cast/`、`04-图片素材/`……），
    结构由拆书技能（newmuyu）定义，各书同构。

    配了它之后：
      - 风格文件自动找 `<lib>/00-设定/风格预设.toml`
      - 定妆库默认落 `<lib>/_cast`
      - 定妆卡默认找 `<lib>/00-设定/*定妆卡*.md`
    于是**接入一本新书只需要填一个路径**。

    留空/None 时返回 None（退回各处的旧默认值，行为与从前一致）。
    """
    if config is None:
        return None
    value = config.get("paths.lib")
    if not value:
        return None
    return Path(str(value)).expanduser()


def resolve_under_lib(config: Config | None, *parts: str) -> Path | None:
    """拼一个素材库内的相对路径；没配 `[paths].lib` 就返回 None。"""
    lib = lib_dir(config)
    return None if lib is None else lib.joinpath(*parts)


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

# 项目 config 的继承键。为什么需要它：
# `lvs init` 生成的项目 config 只写「跟这本书绑定」的项（lib / 风格 / 定妆 / 门禁），
# 生成物里明写着「机器相关的项（ComfyUI 地址 / TTS 服务 / API key）沿用主 config.toml」——
# 但 `--config` 是**整体替换**而不是合并，于是那句话从来没兑现过：
# 跑 `lvs --config config.某书.toml shots` 会报「未配置 app.openai_api_key」，
# 静默退回启发式拆镜；ComfyUI / TTS / 人脸权重同样读不到。
#
# 不选「把密钥复制进项目 config」这条路：`config.toml` 在 .gitignore 里，而
# `config.<项目>.toml` **不在** —— 复制等于把密钥提交进版本库。
#
# 所以做成**显式继承**：项目 config 里写一行
#     [base]
#     config = "config.toml"     # 相对仓库根；也可写绝对路径
# 加载时先读它当底，再把项目 config 深合并上去（项目优先）。
# 不写 `[base]` 时行为与从前**完全一致**（临时 config、测试夹具不受影响）。
_BASE_KEY = "base"


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """`over` 深合并到 `base` 之上，返回新字典。两边同键且都是表时递归，否则 `over` 覆盖。"""
    out = dict(base)
    for key, value in over.items():
        cur = out.get(key)
        if isinstance(value, dict) and isinstance(cur, dict):
            out[key] = _deep_merge(cur, value)
        else:
            out[key] = value
    return out


def _base_target(value: Any, *, here: Path) -> Path | None:
    """解析 `[base].config`。先按**项目 config 所在目录**找，再按仓库根找。

    顺序理由：`lvs init` 默认把项目 config 写在仓库根，两种解析结果相同；
    但把手写的项目 config 放在别处（如某书的素材库里）时，相对它自己更好理解。
    """
    if not value:
        return None
    p = Path(str(value)).expanduser()
    if p.is_absolute():
        return p if p.is_file() else None
    for cand in (here / p, PROJECT_ROOT / p):
        if cand.is_file():
            return cand
    return None


class Config:
    """已加载的配置。字段按需 `get` / `require`，不在导入期强制校验。"""

    def __init__(self, data: dict[str, Any], path: Path | None) -> None:
        self._data = data
        self.path = path

    def as_dict(self) -> dict[str, Any]:
        """只读快照，供界面摊平展示用（改配置请直接改文件）。"""
        return self._data

    # ---- 构造 -------------------------------------------------------------

    @classmethod
    def load(cls, path: Path | str, *, _depth: int = 0) -> "Config":
        """从文件加载；文件不存在则抛 `ConfigError`（面向用户的提示）。

        ★ 接受 `str` 与 `Path` 两种：调用方几乎都会顺手写字符串
        （`Config.load("config.toml")`），而原先只收 `Path` 会崩成
        `AttributeError: 'str' object has no attribute 'is_file'` ——
        一句人话都没有，用户根本不知道错在哪。**宽容接受 + 报人话**。

        ★ 支持 `[base] config = "…"` 显式继承（见 `_BASE_KEY` 注释）：
        先读底配置，再把本文件深合并上去，本文件优先。
        """
        path = Path(path)
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

        # 显式继承：把 [base] 那一节从本文件里摘掉（它不是真实配置项），
        # 再逐层往上加载。`_depth` 挡住写成环的继承链（A→B→A）。
        base_section = data.get(_BASE_KEY)
        if isinstance(base_section, dict) and _depth < 5:
            target = _base_target(base_section.get("config"), here=path.parent)
            if target is not None and target.resolve() != path.resolve():
                parent = cls.load(target, _depth=_depth + 1)
                data = _deep_merge(parent.as_dict(), {k: v for k, v in data.items() if k != _BASE_KEY})
            else:
                data = {k: v for k, v in data.items() if k != _BASE_KEY}
        elif _BASE_KEY in data:
            data = {k: v for k, v in data.items() if k != _BASE_KEY}
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
