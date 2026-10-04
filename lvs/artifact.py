"""产物指纹 —— 整个项目判断"这个东西变没变"的**唯一**实现。

## 为什么单独成一个模块

同一个需求在两个地方出现过，而且各写了一遍：

- `pipeline.py` 的门禁要判"批准之后产物被改了没有"
- `workspace.py` 的阶段要判"上游产物变了没有（该不该重跑）"

两套判据各写一遍必然漂移（先写的宽、后写的窄），然后**没人知道该信哪个** ——
这正是本项目在别处反复踩过的坑（否定式规则、次数统计口径）。

所以它落在这里：零依赖、纯函数、被两边共用。

## 指纹用什么算

`size + mtime_ns`，**不读内容**。取舍：

- 门禁的对象可能是几百张图，逐张读内容要几秒；只 `stat` 是毫秒级
- 任何一次真实写入都会改 `mtime_ns` —— 覆盖、追加、替换全部拦得住
- 唯一的漏网：把内容改回完全相同的大小、同时把 mtime 也改回原值（需要刻意伪造）
- 代价：复制/恢复文件会更新 mtime → 判为"变了" → 要求重审/重跑。
  这是**安全方向**的误报，可以接受（多说一句话，不会放过错产物）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


def _stat_sig(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return "unreadable"
    return f"{st.st_size}:{st.st_mtime_ns}"


def entries(paths: Iterable[Path]) -> list[str]:
    """把一组路径展开成"条目"列表：目录会递归展开，不存在的路径**不进列表**。

    不存在的路径不进列表是有意的：否则"还没生成"会成为签名的一部分，
    一生成签名就变了，下游会被无端判为失效。
    """
    out: list[str] = []
    for p in paths:
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if not f.is_file():
                    continue
                try:
                    rel = f.relative_to(p)
                except ValueError:  # pragma: no cover - 理论不可达
                    rel = f
                out.append(f"{p.name}/{rel.as_posix()}|{_stat_sig(f)}")
        elif p.is_file():
            out.append(f"{p.name}|{_stat_sig(p)}")
    return out


def fingerprint(paths: Iterable[Path]) -> str:
    """一组路径的指纹；空集返回空串（调用方据此判"产物还没生成"）。"""
    items = entries(paths)
    if not items:
        return ""
    items.sort()
    h = hashlib.sha1()
    for item in items:
        h.update(item.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()[:16]


def count_files(paths: Iterable[Path]) -> int:
    n = 0
    for p in paths:
        if p.is_dir():
            n += sum(1 for f in p.rglob("*") if f.is_file())
        elif p.is_file():
            n += 1
    return n


SEP = "\x1f"  # 单元分隔符：比 `|` 更不容易与内容撞车


def signature(*parts: object, length: int = 16) -> str:
    """**参数签名** —— 把"决定某个产物长什么样"的参数元组压成一个短串。

    与 `fingerprint()` 的区别：那个的输入是**文件**（只看 stat，不读内容）；
    这个的输入是**参数值**（`\x1f` 连接后 sha1）。

    为什么要抽出来：这个 "join → sha1 → 截断" 的写法在 `build`（片段复用键）、
    `assets`（取素材的请求键）、`library`（索引缓存键）里**字面上重复了三遍**。
    三份各自演化过（有的用 `|`、有的用 `\x1f`，有的截 16 位有的不截）——
    正是本项目反复吃亏的"同一件事写多遍"。

    注意：**内容哈希**（真读文件字节）不在此列 —— `assets._file_digest` 是有意
    更严格的另一件事（缓存命中要求字节一致），它继续自己实现。
    """
    payload = SEP.join(str(p) for p in parts).encode("utf-8")
    digest = hashlib.sha1(payload).hexdigest()
    return digest[:length] if length else digest


@dataclass(frozen=True)
class ArtifactRef:
    """记进 manifest 的产物引用：路径 + 当时的签名。

    `is_dir=True` 表示这是一个**目录型产物**（如 `assets`、`audio`）——
    它的签名不能用单个 `stat`（目录的 mtime 不会被子文件的内容改动触发），
    而是**递归指纹**（`fingerprint()` 展开所有文件逐个 stat）。
    """

    path: str
    sig: str = ""
    is_dir: bool = False

    @classmethod
    def of(cls, path: Path) -> "ArtifactRef":
        if path.is_dir():
            return cls(path=str(path), sig=fingerprint([path]), is_dir=True)
        return cls(path=str(path), sig=_stat_sig(path))

    def to_dict(self) -> dict[str, str]:
        data = {"path": self.path, "sig": self.sig}
        if self.is_dir:
            data["is_dir"] = "1"
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ArtifactRef":
        return cls(
            path=str(data.get("path", "")),
            sig=str(data.get("sig", "")),
            is_dir=str(data.get("is_dir", "")) == "1",
        )

    def unchanged(self) -> bool:
        """当时记的签名与现在是否一致。路径已不存在也判"变了"。

        目录型产物重算**递归指纹**，文件型用单个 stat —— 两者判据不同，
        因为目录的 `mtime` 不反映子文件内容变化。
        """
        if self.is_dir:
            return self.sig == fingerprint([Path(self.path)])
        return self.sig == _stat_sig(Path(self.path))


def refs(paths: Iterable[Path]) -> list[ArtifactRef]:
    """把一组路径转成可入库的产物引用。**文件与目录都收**。

    上一版只收文件（`if p.is_file()`），导致目录型上游产物（`assets` / `audio`）
    被静默跳过 —— `voice` 的上游是 `assets` 目录，血缘记不到，于是
    **改了图、`voice` 仍判"已完成"**，拿旧图继续配音合成，全程不报错。
    """
    return [ArtifactRef.of(p) for p in paths if p.is_file() or p.is_dir()]
