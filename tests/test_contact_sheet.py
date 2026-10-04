"""缩略图拼版（`lvs qc --sheet` 的 `qc.write_contact_sheets`）测试。

给多模态模型"一眼看一批"的产物：带镜号 + 分色状态条的缩略图拼版。
守护三件事：
1. 分页数学（纯函数，30 张/页对齐"每 30 张巡一次"）；
2. 状态条分色（error > warn > 缺图 > 通过 —— 颜色就是结论，模型只报镜号）；
3. 一张坏图不毁整版（解码失败 → 灰格，其余照拼）。
"""

from __future__ import annotations

import pytest

from lvs import qc
from lvs.workspace import Workspace

PIL = pytest.importorskip("PIL", reason="没装 Pillow")


def _mk_png(path, size=(64, 36), color=(200, 30, 30)):
    from PIL import Image

    Image.new("RGB", size, color).save(path)
    return path


# ---- 分页数学（纯函数）-------------------------------------------------------


def test_page_count_zero():
    assert qc.sheet_page_count(0) == 0


def test_page_count_fills_exactly_one_page():
    per = qc.SHEET_COLS * qc.SHEET_ROWS
    assert qc.sheet_page_count(per) == 1


def test_page_count_rolls_over():
    per = qc.SHEET_COLS * qc.SHEET_ROWS
    assert qc.sheet_page_count(per + 1) == 2
    assert qc.sheet_page_count(2 * per) == 2
    assert qc.sheet_page_count(2 * per + 1) == 3


def test_page_count_never_zero_for_positive_input():
    assert qc.sheet_page_count(1) == 1


def test_cell_metrics_are_consistent():
    w, h, label, pad = qc.cell_metrics()
    assert w > 0 and h > 0 and label > 0 and pad >= 0


# ---- 拼版生成 ----------------------------------------------------------------


@pytest.fixture
def ws(tmp_path):
    return Workspace(task="sheet", root=tmp_path).ensure()


def test_generates_one_page_for_small_batch(ws, tmp_path):
    imgs = [_mk_png(tmp_path / f"{i:03d}.png") for i in range(1, 4)]
    items = [(i, str(p)) for i, p in enumerate(imgs, start=1)]
    out = qc.write_contact_sheets(ws, items, {})
    assert len(out) == 1
    assert out[0].is_file() and out[0].name.startswith("contact-sheet-")
    from PIL import Image

    with Image.open(out[0]) as im:
        assert im.width > 0 and im.height > 0


def test_multi_page_output(ws, tmp_path):
    per = qc.SHEET_COLS * qc.SHEET_ROWS
    items = []
    for i in range(1, per + 3):          # 32 张 → 2 页
        p = _mk_png(tmp_path / f"{i:03d}.png")
        items.append((i, str(p)))
    out = qc.write_contact_sheets(ws, items, {})
    assert len(out) == 2
    assert [p.name for p in out] == ["contact-sheet-01.png", "contact-sheet-02.png"]


def test_empty_items_writes_nothing(ws):
    assert qc.write_contact_sheets(ws, [], {}) == []


def test_missing_file_gets_placeholder_cell_not_crash(ws, tmp_path):
    """缺图的镜也要占一格（灰格）—— 否则镜号错位，模型报的号全偏。"""
    real = _mk_png(tmp_path / "real.png")
    items = [(1, str(real)), (2, str(ws.dir / "nope.png")), (3, None)]
    out = qc.write_contact_sheets(ws, items, {})
    assert len(out) == 1, "缺图不该让整版失败"


def test_broken_image_is_drawn_as_placeholder(ws, tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image at all")
    items = [(1, str(bad))]
    out = qc.write_contact_sheets(ws, items, {})
    assert len(out) == 1, "解码失败的图应画成灰格，而不是毁掉整版"


def test_levels_paint_status_bar(ws, tmp_path):
    """★ 状态条颜色 = 结论。error 必须与 warn 用不同颜色（模型靠它点名）。"""
    from PIL import Image

    imgs = {sid: _mk_png(tmp_path / f"{sid:03d}.png") for sid in (1, 2, 3)}
    items = [(sid, str(p)) for sid, p in imgs.items()]
    out = qc.write_contact_sheets(
        ws, items, {1: qc.LEVEL_ERROR, 2: qc.LEVEL_WARN}
    )
    w, h, label, pad = qc.cell_metrics()
    with Image.open(out[0]) as im:
        # 第 1 格标签条（红）、第 2 格标签条（黄）、第 3 格标签条（绿）
        y = pad + h + label // 2
        c1 = im.getpixel((pad + w // 2, y))
        c2 = im.getpixel((pad + (w + pad) + w // 2, y))
        c3 = im.getpixel((pad + 2 * (w + pad) + w // 2, y))
    assert c1 == qc.COLOR_ERR, f"error 格应为红色，实际 {c1}"
    assert c2 == qc.COLOR_WARN, f"warn 格应为黄色，实际 {c2}"
    assert c3 == qc.COLOR_OK, f"通过格应为绿色，实际 {c3}"
    assert len({c1, c2, c3}) == 3, "三态颜色必须互不相同"


def test_missing_image_bar_is_gray(ws):
    from PIL import Image

    items = [(7, str(ws.dir / "gone.png"))]
    out = qc.write_contact_sheets(ws, items, {})
    w, h, label, pad = qc.cell_metrics()
    with Image.open(out[0]) as im:
        bar = im.getpixel((pad + w // 2, pad + h + label // 2))
    assert bar == qc.COLOR_MISSING


def test_items_are_sorted_by_shot_id(ws, tmp_path):
    """乱序喂入也要按镜号排 —— 拼版镜号与格位一一对应，错位=模型报错镜。"""
    from PIL import Image

    a = _mk_png(tmp_path / "a.png", color=(1, 2, 3))
    b = _mk_png(tmp_path / "b.png", color=(4, 5, 6))
    items = [(9, str(b)), (2, str(a))]      # 故意乱序
    out = qc.write_contact_sheets(ws, items, {})
    w, h, label, pad = qc.cell_metrics()
    with Image.open(out[0]) as im:
        # 缩略图贴在格子顶部居中，所以采样点取缩略图自身中心（y≈18），不是格子中心
        first = im.getpixel((pad + w // 2, pad + 18))
    assert first == (1, 2, 3), f"第一格应是镜号 002 的图，实际像素 {first}"
