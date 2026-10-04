#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把《挪威的森林》P2（讲书档）的 44 条 beat 钉死到具体图片上。

背景：`lvs shots` 会把拍摄稿的每条 `[画面位]` 按 `→` 拆成 beat，逐镜分配。
但**画面是从哪儿来的**它判不了 —— 手头的 75 张第一章分镜图是按「逐字朗读版」的
时序出的，与讲书版的 60 条 beat 不是一一对应，靠关键词翻库必然错配。
所以这里用一张**人工核过**的映射表，逐 beat 钉 `library_asset`（D14 手动优先）。

映射表 `BEAT_MAP` 里：
  - `001/H/NNN.png`      = 复用第一章已有的 75 张分镜图
  - `001P1/H/NNN.png`    = `gen_images_p1.py` 新补的图

用法：
    python scripts/pin_nw02_beats.py            # 预览
    python scripts/pin_nw02_beats.py --write    # 写回 shots.json
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
TASK = "NW02"

# 冷开场（片头那段「这片草地上，有一口井」）没有画面位，单独钉一张：井的母题图
COLD_OPEN_ASSET = "002/H/001.png"

# ---- beat 映射（拍摄稿 beat 原文 → 图）-----------------------------------
# key 由 .work/NW02/parse.json 原样导出；value 由 _make_pin002.py 的 RULES 按子串判定。
# 002/H = gen_images_002.py 新补（29 张）；001P1/H、001/H = 复用 P1 已出的图。
BEAT_MAP: dict[str, str] = {
    "B9 桌球台态1：绿绒台面、红白四球摆成菱形，俯视构图，台灯把绒面照出一圈暖光；一副球杆斜靠在墙边。零文字。": "002/H/001.png",
    "B10 红色 N360 态1：车库内景，一辆暗红小车停在正位，侧窗蒙着雾；挡风玻璃上一根雨刷夹着空白小票，仪表台上一枚收音机的刻度灯还亮着。低照度，只作结果陈述，不给过程、不给人物。": "002/H/002.png",
    "系列报幕卡：一张桌球台的空台，四个球已收进球袋，头顶那盏台灯是灭的（画面零文字，期数后期叠加）。": "002/H/024.png",
    "S1-大榉树：仰视角，一株百五十年树龄的巨榉，树冠密到把天空切成碎块，光斑落在混凝土围墙上。黑白网点质感，零文字。": "002/H/003.png",
    "S1-围墙与门柱：混凝土围墙，门柱旁一块空白的铜牌（零文字），墙头爬着春末的常春藤。禁写任何字。": "002/H/004.png",
    "1969 东京校园街景：戴安全帽的学生人群、拉起的绳索、无字旗面，远景是校门与阶梯教室。": "001P1/H/004.png",
    "B7 旗与升旗台态1：清晨的宿舍院内，一根细旗杆，旗面被风抻平（旗面完全空白、无任何图案）；地面是湿的石板，远处是宿舍楼的深色轮廓。零文字。": "002/H/005.png",
    "敢死队：光头、白衬衫、黑色背带裤、深蓝开衫毛衣，站姿笔直，正面半身。背景是宿舍走廊，逆光。一眼看去像极右学生，表情却是茫然。零文字。": "002/H/006.png",
    "S3-火柴杆抽签：桌面特写，两只手握着一把火柴，其中一根被折短；旁边是摊开的宿舍分配纸（零文字）。暖色台灯，浅景深。": "002/H/007.png",
    "S2-脏房间：宿舍房内，桌上堆着速食面袋、空啤酒罐、塞满烟头的烟灰缸，地板上散着纸屑；窗子蒙灰，光很闷。零文字。": "002/H/008.png",
    "S2-干净房间：同一户型，床铺被褥折成直角，地板反光，窗玻璃干净到能映出对面的楼；窗帘被取下叠在椅背上。冷白色硬光，与上一镜形成对照。": "002/H/009.png",
    "B13 阿姆斯特丹运河摄影画：挂在宿舍灰墙上的黑白摄影画，画面是运河、石桥与一排山形屋顶，河面有细碎的波纹。画框细白边，墙面上没有任何其他文字。": "002/H/010.png",
    "B6 地图：桌上摊开一张大幅地形图，等高线密到近乎全黑，颜色是旧纸的黄；一只手在图上缓慢移动。图面零文字（所有地名用色块代替）。": "002/H/011.png",
    "S1-升旗二人：清晨院内，高个子楼长（脖颈一道旧疤、深色运动服、白球鞋）与矮胖学生服助手（光头、提桐木箱）并肩而立，背后是旗杆。逆光剪影感，零文字。": "002/H/012.png",
    "B7 旗与升旗台态2：傍晚空旗杆，旗已收起，桐木箱放在台边；绳子在空中轻轻晃。天空是灰蓝的暮色，地面一个人也没有。": "002/H/013.png",
    "S4-收音机与体操：清晨斜光里的窗台，一台老式交流电收音机，音量旋钮拧到中间；窗外是宿舍楼的影子。地上有一双白袜的脚正在起跳，只给脚部。": "002/H/014.png",
    "B14 广播体操的跳跃：宿舍地板上的低角度，一双穿着白袜的脚起跳离地，床架的铁腿微微震动，晨光把影子拉长。零文字。": "002/H/015.png",
    "M1 草地态1：四月末的坡地，樱树新叶透光，风把草叶压出一层层浪；天光从云缝斜切。": "002/H/016.png",
    "B8 一米距离的背影：土堰上的小径，直子在前一米，背影修长，笔直黑发垂到肩胛；渡边在她身后半步之外，只入画一半肩膀。前景有虚化的樱树枝，光斑落在两人之间的地面上。": "002/H/017.png",
    "B4 直子的侧脸与耳垂黑痣：侧脸特写，长直黑发垂在脸侧，耳垂下一颗很小的黑痣；不给正面。": "002/H/018.png",
    "S5-荞麦面馆桌面：傍晚的荞麦面馆，木桌上摆着两副空碗、一杯啤酒，桌面反着吊灯的光；窗外是蓝色的暮色街景。": "002/H/019.png",
    "S5-烟灰缸特写：荞麦面馆桌上的厚玻璃烟灰缸，一只手正反复拨弄它；桌面反着吊灯的光，缸里干干净净没有烟。浅景深。": "002/H/026.png",
    "S5-结构示意：粗柱与两个我：一根很粗的深色柱子立在画面正中，两个模糊的女性轮廓绕着它一前一后地追，永远差半步。抽象结构示意，零文字。": "002/H/027.png",
    "S5-山手线车窗：夜里的电车车厢内，车窗映出两张并坐的看不清的侧影，窗外是流动的东京灯点；座位之间空着一个位置。": "002/H/020.png",
    "木月：与渡边同龄的十九岁少年，短发略乱，白色短袖衬衫，面容清瘦、眼神很亮；随手夹着一根烟，笑起来有一点冷。正面半身，背景是校门。零文字。": "002/H/021.png",
    "三人并行剪影：黄昏的路口，三个人并排走，中间那人稍稍靠前半步，两侧的人身体略向内倾；只给剪影，不给五官。构图即\"主持人—助手—客串\"的关系。": "002/H/022.png",
    "旧式咖啡店的三人卡座剪影：中间的人在说话，两侧各一个低头听的人。": "001P1/H/002.png",
    "一间拉上窗帘的房间，桌边一只空椅子。": "001P1/H/003.png",
    "B9 桌球台态1：绿绒台面与红白四球摆成菱阵，台灯照出一圈暖光。": "002/H/001.png",
    "B10 红色 N360 态1：车库里的暗红小车，雨刷夹着一张空白小票。": "002/H/002.png",
    "课桌上的白花：教室内一张空课桌，桌面上放着一小束白花，用旧报纸包着；周围都是空桌椅，午后斜光从窗户进来，灰尘在光柱里浮。零文字。": "002/H/023.png",
    "结构示意：一双手托着一个空房间的模型，屋里的家具正在一件件消失。": "001P1/H/020.png",
    "B9 桌球台态2：同一张桌球台，台面空了，四个球被收进球袋；台灯关着，绒面是暗的。与态1 同机位，只改状态。": "002/H/024.png",
    "B11 薄雾：室内或空镜，一层极淡的白雾刚刚凝出一点模糊的边界，像是有东西正在从雾里显形，但还看不出是什么。低对比，大幅留白。": "002/H/025.png",
    "结构示意：生在此侧，死在彼侧：一条平直的水平分界线把画面切成上下两半，上半是浅灰的空白，下半是浓黑的一团。抽象结构示意，零文字。": "002/H/028.png",
    "B9 桌球台态1：红白四球摆在绿绒台面上。": "002/H/001.png",
    "B6 地图：一张过于详尽的地图（旧纸与折痕，零文字）。": "002/H/011.png",
    "M2 井态2：承接第一期的草地井口，井沿的石头长着苔，井里正有一层黑雾缓慢升起，已经漫到井口边缘。天空是灰的，草被风压低。": "001/H/040.png",
    "画面收黑：整幅画面收黑，只留极淡的颗粒。": "001P1/H/023.png",
    "B9 桌球台态1：绿绒台面与红白四球。": "002/H/001.png",
    "M2 井态3 / M1 草地：吞没一切的黑，与草地空镜回扣第一期收尾。": "001/H/075.png",
    "结构示意：一个人的轮廓内部漫开的黑：一个站着的人的剪影，身体内部从胸口向外漫开一层浓黑，边界模糊。抽象结构示意，零文字。": "002/H/029.png",
    "结构示意：一双手托着一个空房间的模型，屋里的家具正在一件件消失。": "001P1/H/020.png",
    "画面收黑：整幅画面收黑。": "001P1/H/023.png",
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
            # 兜底：正文里还有没映射的 beat，落到冷开场那张（B9 桌球台态1）上
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
            f"beat 钉图（scripts/pin_nw02_beats.py）：{hits} 镜命中、"
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
