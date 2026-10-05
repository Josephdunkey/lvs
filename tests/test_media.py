"""媒体扩展名白名单（`lvs/media.py`）的回归测试 —— 缺陷 B02/Q02。

## 这组测试守的是什么

1. **白名单只有一份真源**：`lvs.build` / `lvs.library` / `lvs.publish` 三个模块
   解析出来的图片扩展名集合必须**相等、且是同一个对象**（断言模块属性）。
   以前三处各写各的、靠人肉同步，谁也不知道对方漏了什么。
2. **`.tif` 必须算图片** —— 这是"永久黑帧"的直接判据：`build` 原先的白名单漏了
   `.tif/.tiff/.gif`，素材库里的 tif 会静默落成黑帧（判活与校验都不报，只有人眼看得见）。
3. **大小写不敏感**：`.PNG` / `.Tif` 也得认（Windows 上文件名大小写随手就有）。
4. **`glob_images` 一次收全**：png/jpg/*tif*/webp 要一起出现在结果里 ——
   它内部是"一次 `glob("*")` 再筛"，不许退回 `glob("*.png") + glob("*.jpg")`。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lvs import build, library, media, publish
from lvs.config import Config
from lvs.workspace import Workspace


# ---- 白名单本身 -------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "a.png", "a.PNG",                     # 大小写
    "a.jpg", "a.JPEG", "a.jpeg", "a.JpEg",
    "a.webp", "a.bmp",
    "a.gif",                              # 只有 library 原先认
    "a.tif", "a.Tif", "a.TIF", "a.tiff",  # ★ 黑帧那三个（build 原先漏的就是它们）
    r"C:\some\dir\shot-193.tif",          # Windows 绝对路径
])
def test_is_image_accepts_known_extensions(name):
    # 大小写不敏感 + 只看扩展名（文件根本不存在）
    assert media.is_image(name) is True, name


@pytest.mark.parametrize("name", [
    "a.mp4", "a.mp3", "a.txt", "a.json", "a.png.bak", "noext", "", "a.",
])
def test_is_image_rejects_everything_else(name):
    assert media.is_image(name) is False, name


def test_tif_is_in_the_whitelist_this_is_the_black_frame_regression():
    """★ 根治判据：tif/tiff/gif 必须在白名单里，否则 build 会把它们当"非图片"。"""
    assert {".tif", ".tiff", ".gif"} <= media.IMAGE_EXTS
    assert media.is_image("shot-193.tif") and media.is_image("shot-193.tiff")


def test_video_and_audio_whitelists_come_along():
    assert media.is_video("a.MP4") and media.is_video("a.mkv") and media.is_video("a.mov")
    assert not media.is_video("a.png")
    assert media.is_audio("a.mp3") and media.is_audio("a.WAV") and media.is_audio("a.flac")
    assert not media.is_audio("a.mp4")


def test_normalized_ext_is_lower_and_dotted():
    assert media.normalized_ext("A.PNG") == ".png"
    assert media.normalized_ext(Path("dir/B.JpEg")) == ".jpeg"
    assert media.normalized_ext("noext") == ""
    assert media.has_ext("x.TIF") == ".tif"      # 旧称（任务书里写作 has_ext）


# ---- glob_images ------------------------------------------------------------


def test_glob_images_collects_every_image_extension_at_once(tmp_path: Path):
    """★ 一次 glob 收全：png/jpg/**tif**/webp 都要在结果里（旧写法只 glob png+jpg）。"""
    for name in ("b.png", "a.jpg", "c.tif", "d.webp", "e.GIF"):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "note.txt").write_text("not an image", encoding="utf-8")
    (tmp_path / "sub").mkdir()                   # 不递归：子目录里的图不算
    (tmp_path / "sub" / "f.png").write_bytes(b"x")

    got = media.glob_images(tmp_path)
    assert isinstance(got, list)
    assert [p.name for p in got] == ["a.jpg", "b.png", "c.tif", "d.webp", "e.GIF"]


def test_glob_images_tolerates_a_missing_directory(tmp_path: Path):
    assert media.glob_images(tmp_path / "nope") == []
    assert media.glob_images(tmp_path) == []      # 空目录 → 空列表，不报错


def test_glob_images_returns_files_not_directories(tmp_path: Path):
    (tmp_path / "fake.png").mkdir()               # 名字像图片的目录不算
    (tmp_path / "real.png").write_bytes(b"x")
    assert [p.name for p in media.glob_images(tmp_path)] == ["real.png"]


# ---- 防漂移：三处必须解析成同一份白名单 --------------------------------------


def test_build_library_publish_share_one_image_whitelist():
    """★ 三处模块属性必须是**同一个 frozenset 对象**（别名，不是副本）。

    这条是"以后再漂移就会红"的判据：谁要是又写回一份字面量（哪怕内容一样），
    `is` 立刻不成立；内容漂移则 `==` 不成立。
    """
    assert build.IMAGE_SUFFIXES == library.IMAGE_EXTS == publish.IMAGE_EXTS == media.IMAGE_EXTS
    assert build.IMAGE_SUFFIXES is media.IMAGE_EXTS
    assert library.IMAGE_EXTS is media.IMAGE_EXTS
    assert publish.IMAGE_EXTS is media.IMAGE_EXTS
    assert library.VIDEO_EXTS is media.VIDEO_EXTS


def test_gui_suffix_lists_come_from_media_too():
    """★ GUI 侧那三处副本（票 40 的挑图 + 进度计数）也必须只认 media 这一份。

    界面原先刻意收窄（只让传 png/jpg/...），接上真源后多出来的格式是**有意为之**：
    上传的真伪由 `Image.verify()` 把关（后缀不算数），界面计数则必须与 `assets`
    阶段认得的一致 —— 否则盘上有 tif 素材，界面还显示"缺素材"。
    """
    from lvs.gui import app as gui_app
    from lvs.gui import store as gui_store

    assert gui_app.PICK_SUFFIXES is media.IMAGE_EXTS
    assert gui_store.AUDIO_SUFFIXES is media.AUDIO_EXTS
    assert gui_store.ASSET_SUFFIXES == media.IMAGE_EXTS | media.VIDEO_EXTS


def test_gui_pick_error_lists_every_media_image_format(tmp_path: Path):
    """报错信息里的允许格式要跟着 media（`is` 别名 + 排序后拼串），不是又一份字面量。"""
    from lvs.gui import app as gui_app

    with pytest.raises(gui_app.GuiError) as exc:
        gui_app.save_picked_image(tmp_path, 1, "note.txt", b"x")
    for ext in media.IMAGE_EXTS:
        assert ext in str(exc.value), ext


# ---- 端到端：publish.pick_base 以前看不见 jpeg/webp/tif ----------------------


def test_pick_base_sees_jpeg_webp_and_tif(tmp_path: Path):
    """旧实现是 `glob("*.png") + glob("*.jpg")`：一目录的 webp/tif 会被报成"没有图片"。"""
    ws = Workspace(task="t", root=tmp_path).ensure()
    cfg = Config({"publish": {}}, tmp_path / "config.toml")
    d = tmp_path / "covers"
    d.mkdir()
    for name in ("a.webp", "b.jpeg", "c.tif"):
        (d / name).write_bytes(b"x")

    got, why = publish.pick_base(ws, cfg, str(d))
    assert got is not None, why
    assert got.name == "c.tif"          # 目录里按名字排序取最后一张