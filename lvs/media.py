"""媒体扩展名白名单的**唯一真源**（缺陷登记 B02 / Q02）。

## 为什么要有这个模块

"什么算一张图片"这**同一个问题**，仓库里曾经有 5 份各写各的答案：

| 位置 | 白名单 |
|---|---|
| `lvs/build.py`（旧 `IMAGE_SUFFIXES`） | png / jpg / jpeg / webp / bmp |
| `lvs/library.py`（旧 `IMAGE_EXTS`） | 上一行 + gif / tif / tiff |
| `lvs/publish.py`（旧 `pick_base`） | 只有 `glob("*.png") + glob("*.jpg")` —— 连 jpeg 都不认 |
| `lvs/gui/app.py`（`PICK_SUFFIXES`） | png / jpg / jpeg / webp / bmp |
| `lvs/gui/store.py`（`ASSET_SUFFIXES`） | png / jpg / jpeg / webp / bmp + 视频 |

各写各的**永远不会报错**，所以漂移是静默的 —— 直到素材库里真的放了一张 `.tif`：

1. `assets` 阶段照拷（素材库那一侧认 `.tif` 是图片）→ `assets/library/shot-NNN.tif`；
2. `build` 阶段用自己那份白名单判断"这是不是图片" → 认不出 `.tif`，既不进
   Ken Burns 分支、也不是视频分支 → 这一镜落成一块**永久黑帧**；
3. 判活（产物文件在不在）与校验（时长/音轨对不对）**全都通过** —— 黑帧是
   "合法的"产物，只有人眼看片才发现（实测踩过）。

所以主链路的白名单只留这一份：`build` / `library` / `publish` 都 import 它，
旧名字（`IMAGE_SUFFIXES` / `IMAGE_EXTS` / `VIDEO_EXTS`）保留为**别名** ——
别名指向同一个 `frozenset` 对象，不是第二份字面量；要支持新格式只改这里一处。

界面侧的三个名字（`lvs/gui/app.py` 的 `PICK_SUFFIXES`、`lvs/gui/store.py` 的
`ASSET_SUFFIXES` / `AUDIO_SUFFIXES`）也接了真源，并**有意跟着放宽**（多出
gif/tif/tiff，界面计数还多出 avi/m4v）：界面上拦住一个 `assets` 阶段本来就认的
格式，只会让人以为"这张素材用不上"。

★ 仍有一处没接线：`lvs/gui/static/task.js` 里
`<input accept="image/png,image/jpeg,image/webp,image/bmp">` —— 那只是浏览器的
"选择文件"过滤器（不是判据，绕过去照样能传），本次没动。

## 约定

- 一律**小写、带点**（`".png"` 而不是 `"png"`），与 `Path.suffix.lower()` 同形；
  这样调用方不用各自 `lower()` 一遍（少一处"忘了 lower"的机会）。
- 判定**只看扩展名、不碰磁盘**：`is_image()` 不打开文件、不探测内容。内容真伪
  由下游各自负责（PIL / ffprobe 读不出来会报各自的错，这里报不了也不该报）。
- `glob_images()` 是**一次 `glob("*")` 再筛**，不是
  `glob("*.png") + glob("*.jpg")` —— 后者每加一个扩展名就要多写一行，
  漏掉一行就重演上面的黑帧（`publish.pick_base` 就是这么漏掉 jpeg/webp 的）。
"""

from __future__ import annotations

from pathlib import Path

#: 图片：三处副本的**并集**。`.gif` / `.tif` / `.tiff` 原先只有 `library` 认，
#: 差分正是"黑帧"的来源（`build` 漏了它们）。
IMAGE_EXTS: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".bmp",
    ".gif", ".tif", ".tiff",
})

#: 视频：取仓库里既有列表的并集（`library.VIDEO_EXTS` 的六个）。
VIDEO_EXTS: frozenset[str] = frozenset({
    ".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v",
})

#: 音频：仓库里原先只有 `gui/store.py` 的 `AUDIO_SUFFIXES` 列过（六个）。
AUDIO_EXTS: frozenset[str] = frozenset({
    ".mp3", ".wav", ".m4a", ".aac", ".opus", ".flac",
})


def normalized_ext(path: str | Path) -> str:
    """归一化扩展名：小写、带点（`"X.JPG"` → `".jpg"`）；没有后缀 → `""`。

    比较白名单时一律走 `is_image()` / `is_video()` / `is_audio()`，
    别自己写 `path.suffix in ...` —— 那正是三份副本各自 lower 或没 lower 的起点。
    """
    return Path(str(path)).suffix.lower()


#: 旧称（原任务书里写作 `has_ext`）—— 保留别名，免得"归一化扩展名"再各写一份。
has_ext = normalized_ext


def is_image(path: str | Path) -> bool:
    """扩展名是否属于 `IMAGE_EXTS`（大小写不敏感；只看名字，不碰磁盘）。"""
    return normalized_ext(path) in IMAGE_EXTS


def is_video(path: str | Path) -> bool:
    """扩展名是否属于 `VIDEO_EXTS`（大小写不敏感；只看名字，不碰磁盘）。"""
    return normalized_ext(path) in VIDEO_EXTS


def is_audio(path: str | Path) -> bool:
    """扩展名是否属于 `AUDIO_EXTS`（大小写不敏感；只看名字，不碰磁盘）。"""
    return normalized_ext(path) in AUDIO_EXTS


def glob_images(directory: str | Path) -> list[Path]:
    """目录里的图片文件，按**名字**排序返回（不递归；目录不存在 → 空列表）。

    ★ 实现是"一次 `glob("*")` 再按 `IMAGE_EXTS` 筛"。**不要**改回
    `glob("*.png") + glob("*.jpg")` —— 那种写法是本模块要根治的病根：
    白名单里每多一个扩展名，就要记得多补一行 glob，漏了不报错，只出黑帧。
    """
    d = Path(str(directory))
    if not d.is_dir():
        return []
    return sorted((p for p in d.glob("*") if p.is_file() and is_image(p)),
                  key=lambda p: p.name)