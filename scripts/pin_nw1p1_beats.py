#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把《挪威的森林》P1（讲书档）的 60 条 beat 钉死到具体图片上。

背景：`lvs shots` 会把拍摄稿的每条 `[画面位]` 按 `→` 拆成 beat，逐镜分配。
但**画面是从哪儿来的**它判不了 —— 手头的 75 张第一章分镜图是按「逐字朗读版」的
时序出的，与讲书版的 60 条 beat 不是一一对应，靠关键词翻库必然错配。
所以这里用一张**人工核过**的映射表，逐 beat 钉 `library_asset`（D14 手动优先）。

映射表 `BEAT_MAP` 里：
  - `001/H/NNN.png`      = 复用第一章已有的 75 张分镜图
  - `001P1/H/NNN.png`    = `gen_images_p1.py` 新补的图

用法：
    python scripts/pin_nw1p1_beats.py            # 预览
    python scripts/pin_nw1p1_beats.py --write    # 写回 shots.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
LIB = Path(r"D:/fanshu/挪威的森林/10-语料/知识视频素材库")
IMG_DIR = LIB / "04-图片素材"
TASK = "NW01P1"

# 冷开场（片头那段「这片草地上，有一口井」）没有画面位，单独钉一张：井的母题图
COLD_OPEN_ASSET = "001/H/014.png"

# ---- beat 映射（拍摄稿 beat 原文 → 图）-----------------------------------
BEAT_MAP: dict[str, str] = {
    # ① 冷开场 · 那口井
    "十月芒草坡的草浪（母题 M1 态1）": "001/H/013.png",
    "草浪里被青草遮住的暗井口（母题 M2 态1）": "001/H/039.png",
    "弯腰俯视井内，只见浓黑": "001/H/040.png",
    # ② 报幕
    "系列报幕卡：一本摊开的旧书压在芒草上，风翻动书页，天光是全画唯一亮部（画面零文字，期数后期叠加）": "001P1/H/001.png",
    # ③ 三个人
    "三人在旧式咖啡店卡座的剪影：中间的人在说话，两侧各一个低头听的人": "001P1/H/002.png",
    "一间拉上窗帘的房间，桌上一只空椅子，窗外是五月白亮的天光": "001P1/H/003.png",
    # ④ 一九六九年
    "1969 年东京街头：封锁的校门前拉起绳子，成排的旗子（旗面一律空白、无字），远处立着扩音喇叭": "001P1/H/004.png",
    "校门前的封锁线，空无一人": "001P1/H/005.png",
    "空旷的阶梯教室，只有讲台和一地纸屑": "001P1/H/006.png",
    "一片深色泥沼，一个深陷的脚印，四下无人（无远景参照物）": "001P1/H/007.png",
    # ⑤ 书名
    "黑胶唱片放在转盘上（盘面空白无字）": "001P1/H/008.png",
    "一块浅色松木板靠在墙上，木纹清晰": "001P1/H/009.png",
    "一块被切成小块的松木板浮在草浪上方，木纹慢慢延展成一片森林的轮廓": "001P1/H/010.png",
    # ⑥ 剧情 · 机舱
    "雨云中下坠的机翼": "001/H/001.png",
    "舷窗外灰白的汉堡机场与雨衣地勤": "001/H/002.png",
    "佛兰德画派式的低饱和抑郁雨景": "001/H/036.png",
    "机舱天花板扬声器网罩特写（网罩上无字）": "001/H/003.png",
    "侧脸闭上眼的三十七岁男子": "001/H/006.png",
    "机舱金属舱壁溶解，化成同一构图的秋日草坡（转场镜）": "001/H/007.png",
    # ⑦ 剧情 · 草地
    "十月芒草坡被金风吹出层层草浪": "001/H/014.png",
    "两只火团样的小鸟腾起": "001P1/H/011.png",
    "两人并肩走远的背影剪影": "001/H/019.png",
    "同一片草坡，空无一人，草浪照旧起伏，天光却压得很低": "001/H/075.png",
    "草坡上一个模糊的、只剩轮廓的女性背影，风一吹就散成草屑": "001/H/059.png",
    # ⑧ 剧情 · 那口井
    "井壁仰视、只剩一线天光": "001/H/046.png",
    "井底散落的白骨与晃动的微光": "001/H/033.png",
    "直子的左手握住他的手（手部特写）": "001/H/048.png",
    "两只交握的手在草径上并肩移动的影子": "001/H/043.png",
    # ⑨ 剧情 · 可是行不通啊
    "直子踮起脚尖、脸颊贴上脸颊（过肩，背景虚化）": "001/H/052.png",
    "她瞳仁深处旋转的浓黑": "001/H/053.png",
    "草坡的草浪骤然炸散成碎片飘向空中（结构示意镜）": "001P1/H/012.png",
    "直子侧脸绷紧的肩线，背景草浪仍在炸散": "001/H/027.png",
    # ⑩ 剧情 · 松林里的那句话
    "松林小径的俯拍": "001/H/062.png",
    "脚下夏末知了的干壳被踩碎（极近特写）": "001/H/030.png",
    "直子的背影走向树梢间漏下的秋日光斑，画面留出大面积负空间": "001/H/068.png",
    "树梢间泻下的秋日阳光在她肩头一闪一闪": "001/H/064.png",
    "她钻出松林，快步走下缓坡": "001/H/065.png",
    # ⑪ 剧情 · 最后一句
    "一间昏暗的储藏室，地上堆着被水泡烂的纸箱，箱里的东西已经认不出形状": "001/H/071.png",
    "一张过于详尽而派不上用场的地图（旧纸与折痕）": "001P1/H/013.png",
    "图上墨迹慢慢晕开、边界不清": "001P1/H/014.png",
    "画面收黑": "001P1/H/023.png",
    # ⑫ 解读 · 那口井
    "结构示意镜：草地下方一层薄薄的空腔，像一张随时会塌的地板（母题 M2 态2）": "001P1/H/015.png",
    "两只手交握，指缝间慢慢渗进黑色的水，越握越用力": "001P1/H/016.png",
    # ⑬ 解读 · 几个意象
    "松林里一只完整却空心的知了壳（极近特写）": "001P1/H/017.png",
    "旧纸与折痕的地图，墨迹慢慢晕开": "001P1/H/014.png",
    "两只火团样的小鸟腾起的一瞬": "001P1/H/011.png",
    "直子肩头一闪一闪的秋日光斑": "001/H/064.png",
    # ⑭ 解读 · 那一代人
    "空荡的阶梯教室": "001P1/H/006.png",
    "站台上西装上班族的人流（一律背影，看不清脸）": "001/H/073.png",
    "空无一人的十月草地，杂草覆径（母题 M1 态3）": "001/H/013.png",
    # ⑮ 解读 · 村上站在哪一边
    "结构示意镜：一堵高墙与墙脚一枚裂开的蛋，光影从墙头压下（无文字）": "001P1/H/018.png",
    "清晨的佛坛前，一个老人的背影长跪（不出现正面）": "001P1/H/019.png",
    "草坡上两人并肩的剪影，其中一只手握着另一只手，天光从云缝斜切进画面": "001/H/045.png",
    # ⑯ 解读 · 为什么放在第一章
    "结构示意镜：一双手托着一个空房间的模型，屋里的家具正在一件件消失": "001P1/H/020.png",
    "空无一人的十月草地，杂草覆径，无光（母题 M1 态3）": "001/H/075.png",
    # ⑰ 收尾 · 预告（末条与 S15 那条文案完全相同，共用同一张）
    "三人并肩走远的逆光剪影": "001/H/056.png",
    "只剩一个人的背影站在草坡上": "001/H/026.png",
    "深夜的车库门缝里漏出一线暖黄的光，一辆旧式小轿车停在里面，排气管拖出一根软管": "001P1/H/021.png",
    "雨刷夹着一张被雨打湿的白色小票（纸面空白无字），挡风玻璃映着车库顶灯": "001P1/H/022.png",
}


def norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def asset(rel: str) -> Path:
    return IMG_DIR / rel


# ---- 同图相邻镜合并 --------------------------------------------------------
# 拍摄稿是「一行一口气」写的，`lvs shots` 便按行断句 → 474 镜、平均 9.8 字/镜
# （约 2.5 秒一切）。一条 beat 的图会被切成 4–8 个短镜，而 build 是**逐镜**做
# Ken Burns 的 —— 同一张图连续出现时，运镜会在每个镜头上重头再来，看起来一跳一跳。
# 合并同图的相邻镜 = 一 beat 一镜，运镜连续；顺带把节奏拉到讲书体该有的从容。
# 上限 MAX_MERGE_CHARS：一条 beat 铺得太长时仍切一刀，避免单张图停 40 秒以上。
# 130 → 200（票据 45）：130 会让 7 处"同图邻镜"因为合并后会超限而留下，
# 表现为「推到 1.15x → 跳回 1.0x → 再推」的顿挫。放宽到 200 后这 7 处也并成
# 一镜，`build` 的 Ken Burns 才是连续的（build.kenburns 是全局单值，不感知邻镜同图）。
MAX_MERGE_CHARS = 200


def positional_beats(parse_data: dict, split_sentences) -> list[tuple[int, str, str]]:
    """重算「每句旁白 → 画面位 → beat」，返回 [(段号, 旁白字面, beat 原文)]。

    与 `lvs/shots.py::_sentences_with_visual` 的两点不同（见 main() 里的注释）：
      1. 画面位归属取「位置在句子**之后**的第一个画面位」，没有则取最后一个；
      2. 段内按序平均分配 beat，保证同一条 beat 的镜是连成一段的。
    并复刻 `_merge_dangling`（残片并入后句，但保留**首句**的 beat —— 与原实现一致）。
    """
    rows: list[tuple[int, str, str]] = []
    for seg in parse_data.get("segments", []):
        idx = int(seg.get("index", 0))
        flow = seg.get("flow") or []
        marks = [(i, it.get("beats") or [{"text": it.get("text", "")}])
                 for i, it in enumerate(flow) if it.get("kind") == "visual"]
        sents: list[tuple[int, str]] = []
        for i, it in enumerate(flow):
            if it.get("kind") == "visual":
                continue
            for s in split_sentences(it.get("text", "")):
                sents.append((i, s))

        groups: list[tuple[int, list[str]]] = []
        for pos, sent in sents:
            owner = next((k for k, (p, _) in enumerate(marks) if p > pos), len(marks) - 1)
            if owner < 0:
                owner = 0
            if groups and groups[-1][0] == owner:
                groups[-1][1].append(sent)
            else:
                groups.append((owner, [sent]))

        heading = seg.get("heading") or ""
        for owner, ss in groups:
            bs = marks[owner][1] if marks else []
            n, m = len(ss), len(bs)
            for k, sent in enumerate(ss):
                beat = bs[min(m - 1, k * m // n)]["text"] if m else (heading or sent)
                rows.append((idx, sent, beat))

    merged: list[tuple[int, str, str]] = []
    i = 0
    while i < len(rows):
        seg_i, text, beat = rows[i]
        j = i + 1
        while (len(text.rstrip()) < 4 or text.rstrip().endswith(("：", ":"))) \
                and j < len(rows) and rows[j][0] == seg_i:
            text += rows[j][1]
            j += 1
        merged.append((seg_i, text, beat))
        i = j
    return merged


def merge_same_image(shots: list[dict], warn=print) -> tuple[list[dict], int]:
    """相邻且 `library_asset` 相同的镜合并成一镜（拼接旁白）。返回 (新镜表, 合并次数)。"""
    out: list[dict] = []
    merged = 0
    for shot in shots:
        prev = out[-1] if out else None
        same = (
            prev is not None
            and shot.get("library_asset")
            and shot.get("library_asset") == prev.get("library_asset")
            and shot.get("segment") == prev.get("segment")
            and len(prev["narration"]) + len(shot["narration"]) <= MAX_MERGE_CHARS
        )
        if same:
            prev["narration"] = prev["narration"] + shot["narration"]
            merged += 1
        else:
            out.append(dict(shot))
    for i, shot in enumerate(out, start=1):
        shot["id"] = i
    return out, merged


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写回 shots.json（默认只预览）")
    ap.add_argument("--task", default=TASK)
    args = ap.parse_args()

    ws = PROJECT / ".work" / args.task
    parse_data = json.loads((ws / "parse.json").read_text(encoding="utf-8"))
    shots_path = ws / "shots.json"
    # 只从 shots.json 取"版式元信息"（style / visual_mode / source_mode / title），
    # **镜表本身一律由 parse.json 重推** —— 否则本脚本不可重跑：它上一次写回的是
    # 「已合并」的镜表，条目数与骨架对不上，第二次运行就会直接退出。
    data: dict = json.loads(shots_path.read_text(encoding="utf-8")) if shots_path.is_file() else {}
    style = str(data.get("style") or "jp-youth-manga-bw")

    sys.path.insert(0, str(PROJECT))
    from lvs.shots import build_skeleton, split_sentences  # noqa: PLC0415

    # ---- beat 归属：自己在位置层面重算，不采用 `lvs shots` 的那一版 ----------
    # 两处都得纠：
    # ① `lvs/shots.py` 的 `_owner_index` 假设「画面位**管它后面**的句子」，而讲书稿的
    #    约定恰好相反 —— `[画面位]` 那一行写在它所辖旁白块的**末尾**。照原逻辑，
    #    每段最后一个画面位一个句子都分不到（60 条 beat 里 18 条从未上屏），
    #    倒数第二个画面位的图则被压到错位的旁白上。
    # ② `lvs shots` 的 LLM 提示词要求「相邻几镜尽量摊到不同 beat」，于是同一画面位下的
    #    三张图被**交替**分给相邻镜（实测段 1 出现 013→039→013→039→040 的频闪序列）。
    #    而 `A → B → C` 的本意是**先后**，不是交替。
    # 这里按「画面位跟随其后 + 段内按序平均分配」重算，并与 `build_skeleton()` 的
    # 镜序/镜数核对，对不上就报警退回。
    rows = positional_beats(parse_data, split_sentences)
    skeleton = build_skeleton(parse_data, style)
    body = [s for s in skeleton if s.get("segment") != 0]          # 去掉片头冷开场
    cold_shots = len(skeleton) - len(body)
    my_text = [r[1] for r in rows]
    skel_text = [s["narration"] for s in body]
    if my_text != skel_text or len(body) != len(rows):
        raise SystemExit(
            f"⚠️ 自算旁白序列与 build_skeleton 不一致："
            f"骨架正文 {len(body)} 镜 / 自算 {len(rows)} 镜 —— "
            "先确认 parse.json 与拍摄稿同步（重跑 `lvs parse`）。"
        )
    visuals = [r[2] for r in rows]

    beats: list[str] = []
    for seg in parse_data["segments"]:
        for item in seg.get("flow", []):
            if item.get("kind") != "visual":
                continue
            for b in item.get("beats") or [{"text": item.get("text", "")}]:
                beats.append(b["text"])

    table = {norm(k): v for k, v in BEAT_MAP.items()}
    cold = asset(COLD_OPEN_ASSET)
    hits = miss = cold_hits = 0
    dropped = cold_shots
    used: set[str] = set()
    keep: list[dict] = []

    # 片头「冷开场」是**传达层**的报幕词（拍摄稿自己标了「属包装，不计入正文字数」），
    # 而正文 ① 冷开场用的是同一段话 —— 照单全收会把这 51 个字念两遍。
    # 所以整段丢掉，让正文 ① 来承担开场（画面位 01 本来就是给这段设计的）。
    for shot, visual_raw in zip(body, visuals):
        visual = norm(visual_raw)
        rel = table.get(visual)
        if rel is None:
            # 兜底：正文里还有没映射的 beat，落到井的母题图上
            path = cold
            cold_hits += 1
        else:
            path = asset(rel)
            used.add(visual)
        if not path.exists():
            print(f"  [缺文件] 镜 {shot.get('id')}: {path}")
            miss += 1
            continue
        hits += 1
        shot = dict(shot)
        shot["library_asset"] = str(path)
        shot["visual"] = visual_raw
        shot["source"] = "library"
        shot["source_pinned"] = True
        # 全是实拍静图（不是"要读的信息"的图文卡片）→ kind 记 scene。
        # `build` 只对 `kind == graphic` 关掉运镜，None 与 "scene" 效果相同，
        # 但留痕要准：看板/素材报告都读它。
        if not shot.get("kind"):
            shot["kind"] = "scene"
        # 画面换了 → 上一轮的素材/音频/时间轴全部作废，一律清掉重来
        for f in ("asset_path", "resolved_by", "status", "error",
                  "audio_path", "audio_duration", "start", "end",
                  "generated_by"):
            shot.pop(f, None)
        shot["status"] = "pending"
        keep.append(shot)
    shots = keep

    print(f"beat 总数 {len(beats)}；映射表 {len(BEAT_MAP)} 条")
    print(f"分镜命中 {hits}（兜底 {cold_hits}）；缺文件 {miss}；丢弃冷开场重复镜头 {dropped} 个")
    unmapped = [b for b in beats if norm(b) not in table]
    if unmapped:
        print("\n⚠️ 映射表里没有的 beat：")
        for b in unmapped:
            print("   -", b)
    unused = [k for k in table if k not in used]
    if unused:
        print("\n（映射表里这些 beat 本任务没有分镜用到：）")
        for k in unused:
            print("   -", k[:44])

    if args.write:
        before = len(shots)
        shots, merged = merge_same_image(shots)
        data["shots"] = shots
        data["count"] = len(shots)
        data["by_source"] = {"library": len(shots)}
        data["by_kind"] = {"scene": len(shots)}
        data.setdefault("notes", []).append(
            f"beat 钉图（scripts/pin_nw1p1_beats.py）：{hits} 镜命中、"
            f"丢冷开场重复镜 {dropped} 个；同图相邻镜合并 {merged} 次，{before} → {len(shots)} 镜。"
            "beat 归属按位置重算（build_skeleton），不采用 LLM 的交替分配。"
        )
        shots_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n同图相邻镜合并：{before} → {len(shots)} 镜（合并 {merged} 次）")
        print(f"已写回 {shots_path}")
    return 0 if not unmapped and miss == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
