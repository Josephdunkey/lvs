#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把《挪威的森林》P5（讲书档）的 67 条 beat 钉死到具体图片上。

背景：`lvs shots` 会把拍摄稿的每条 `[画面位]` 按 `→` 拆成 beat，逐镜分配。
但**画面是从哪儿来的**它判不了 —— 手头的 75 张第一章分镜图是按「逐字朗读版」的
时序出的，与讲书版的镜序不是一一对应，靠关键词翻库必然错配。
所以这里用一张**人工核过**的映射表，逐 beat 钉 `library_asset`（D14 手动优先）。

映射表 `BEAT_MAP` 里：
  - `001/H/NNN.png`      = 复用第一章已有的 75 张分镜图
  - `001P1/H/NNN.png` / `002/H/NNN.png` / `003/H/NNN.png` / `004/H/NNN.png` = 前四期已出的图
  - `005/H/NNN.png`      = `gen_images_005.py` 本期新补的图

用法：
    python scripts/pin_nw05_beats.py            # 预览
    python scripts/pin_nw05_beats.py --write    # 写回 shots.json
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
TASK = "NW05"

# 冷开场（片头那段「这片草地上，有一口井」）没有画面位，单独钉一张：井的母题图
COLD_OPEN_ASSET = "005/H/002.png"

# ---- beat 映射（拍摄稿 beat 原文 → 图）-----------------------------------
# key 由 .work/NW02/parse.json 原样导出；value 由 _make_pin005.py 按 beat 序号直接给出。
# 005/H = gen_images_005.py 新补（48 张）；004/H、003/H、002/H、001P1/H、001/H = 复用前四期已出的图。
BEAT_MAP: dict[str, str] = {
    "台灯下的一封长信：夜间书桌一角，一盏台灯照着一叠摊开的信纸，纸边被翻得起了毛，旁边斜放着一支笔。零文字。": "003/H/039.png",
    "B45 淡红色的信封：桌面一角，一只淡红色的信封，正反两面都写满极工整的小字，字小得几乎要贴上脸才看得清。零文字。": "005/H/001.png",
    "画面收黑：整幅画面收黑，只留极淡的颗粒。": "001P1/H/023.png",
    "系列报幕卡：一张空着的桌球台，红白四个球已经收进球袋，台面上方那盏灯已经熄灭。零文字。": "002/H/024.png",
    "车库里的红色小轿车：清晨的车库内，一辆红色老式小轿车停着，旁边的白墙上什么都没有。零文字。": "002/H/002.png",
    "一封短信与京都山里的疗养院：一张桌上放着一封短信和一张地图，窗外是山里的屋顶。零文字。": "003/H/040.png",
    "敞开的窗与扔出去的空盒子：室内靠窗一角，窗扇向外敞开，窗台上倒放着一只空的奶油色纸盒，窗外一片过曝的亮。零文字。": "004/H/001.png",
    "B47 阿美寮远景：群山环抱的盆地，几座低矮房子散在田边，晾衣竿上挂着衣服，远处升起一缕白烟。零文字。": "005/H/002.png",
    "摊在桌上的七页信纸：桌面俯视，七页信纸一页压着一页摊开，最上面一页只有开头几行，其余一片空白。零文字。": "003/H/039.png",
    "B48 宿舍窗边的背影：宿舍床边，一个人坐在床沿，上衣脱下来搭在腿上，窗户开着，窗帘被风吹起。只给后背与肩，不给脸。零文字。": "005/H/003.png",
    "B49 二楼窗口外的鸽舍：从窗口往外看，对面的屋顶上立着一座木鸽舍，几只鸽子停在上面，午后的光很平。零文字。": "005/H/004.png",
    "B51 屋顶之上的一片星：低矮的屋顶轮廓之上，一片干净的夜空，星星密而亮。零文字。": "005/H/005.png",
    "B52 写到一半的信与笔：桌面近景，一张写了一半的信纸，字迹工整，笔搁在纸上，旁边一杯水。零文字。": "005/H/006.png",
    "B55 信封背后的三个字：信封背面近景，一小行端正到近乎刻板的字迹，其余地方空着。零文字。": "005/H/007.png",
    "B56 法语词典打开的一页：桌上一本摊开的旧词典，纸页发黄，边上放着一只铅笔。零文字。": "005/H/008.png",
    "B59 抽象示意·三把长度不同的尺：三把木尺并排放在白纸上，长短不一，刻度却都没有标数字。零文字。": "005/H/009.png",
    "B61 抽象示意·裂开又对上的两块：一块从中间裂成两半的木片，两半贴合在一起，裂缝还在，只是没有分开。零文字。": "005/H/010.png",
    "B62 抽象示意·一个人缩进壳里：画面中央是一个蜷着的背影轮廓，轮廓外面包着一层壳状的空隙，壳的内侧干干净净。零文字。": "005/H/011.png",
    "B65 午后的草地与几张躺椅：一大片起伏的草地，草地上散放着两三张空躺椅，远处的树影被拉得很长。零文字。": "005/H/012.png",
    "B66 球场边的篮球：一个露天的球场边，一颗篮球停在水泥地上，篮架很高，场上没有人。零文字。": "005/H/013.png",
    "B68 温室与一整排菜垄：一座塑料薄膜的温室，前面是整整齐齐的菜垄，土是刚翻过的深色。零文字。": "005/H/014.png",
    "B70 瓜田里的三颗西瓜：贴着地面长着的三颗西瓜，藤叶压在上面，其中一颗明显比另外两颗大。零文字。": "005/H/015.png",
    "B73 厨房秤上的一只碗：老式厨房里，一台指针秤上放着一只陶碗，指针停在中间偏右的位置。零文字。": "005/H/016.png",
    "图书馆的斜阳与外文书：图书馆靠窗的一张长桌，斜光落在摊开的外文书上，书架向深处退去。零文字。": "003/H/017.png",
    "一张唱片封套与一条绸带：几张大唱片封套叠放在桌边，最上面的一张系着一条绸带。零文字。": "003/H/014.png",
    "B75 医生办公室的一角：一张旧木桌，桌上一只茶杯和一叠纸，窗外的树影投在墙上，房间里没有人。零文字。": "005/H/017.png",
    "B77 泥地上的一串脚印：泥地上一串往前的脚印，两只脚的角度略有不同，深一脚浅一脚。零文字。": "005/H/018.png",
    "B80 一间安静的公共房间：一间大屋子里摆着几张矮桌和椅子，几个人各自坐着，谁也不看谁，光线很淡。零文字。": "005/H/019.png",
    "B81 街上走着的许多人（背影）：一条街上，密密麻麻全是走动的背影，全都朝一个方向，没有一张脸转过来。零文字。": "005/H/020.png",
    "B84 一扇从里面闩上的门：室内一侧的木门，门闩插着，门缝下面透进一线外面的亮。零文字。": "005/H/021.png",
    "B87 空房间窗口的一把椅子：空房间里，一把椅子正对着窗口，窗外是模糊的树，椅子上没有人。零文字。": "005/H/022.png",
    "B88 折起的一页信：桌上一页信纸被折成三折，折痕很清楚，旁边压着一只信封。零文字。": "005/H/023.png",
    "B90 长桌前并排的三把空椅子：一张长桌前并排摆着三把椅子，中间那把靠得稍近一些，全都没有人。零文字。": "005/H/024.png",
    "B93 随信寄来的那张地图：一张手绘地图摊在桌上，一条线从车站画到山里，尽头画着一个方块，旁边是一小段字。零文字。": "005/H/025.png",
    "B94 走廊尽头的公用电话：宿舍走廊尽头，一台旧式公用电话挂在墙上，听筒挂着，旁边没有人。零文字。": "005/H/026.png",
    "星期天东京的旧商业街：一条旧式商业街的街口，卷闸门半开，招牌都虚着，行人很少。零文字。": "004/H/027.png",
    "B97 靠在墙边打电话的侧影：宿舍走廊里，一个人靠在墙边打电话，听筒贴着耳朵，墙上挂着一只钟。只给侧影。零文字。": "005/H/027.png",
    "夜里摊开的一本旧书与一只小碗：夜里桌上摊开着一本翻旧的书，旁边放着一只小碗，别的什么都没有。零文字。": "004/H/032.png",
    "雨后车站的站台：一个旧式车站的站台，雨刚停，地面反着光，站台上人很少。零文字。": "003/H/025.png",
    "车窗上两个看不清的侧影：列车车窗上，映着两个看不清的侧影，窗外是掠过的街景。零文字。": "002/H/020.png",
    "B100 候车亭凳子上的地图：一排候车亭的空凳子，一个人坐在最边上，膝上摊着地图，包放在脚边。零文字。": "005/H/028.png",
    "等高线密到发黑的地图：一张摊开的地图，等高线密到几乎发黑，纸上只有线和空白。零文字。": "002/H/011.png",
    "B103 山路与握着方向盘的双手：车厢内视角，一双紧握方向盘的手，前方是不断拐弯的山路。零文字。": "005/H/029.png",
    "B104 杉树林里的公路：一条窄路穿进笔直高耸的杉树林，树干密密麻麻，光几乎被挡没，路面发暗。零文字。": "005/H/030.png",
    "B106 盆地里的田与一缕白烟：四面环山的盆地，青色的田一直铺到山脚，远处一缕细白的烟直直地升上去。零文字。": "005/H/031.png",
    "晾衣绳上的白衬衣与黄昏：一条晾衣绳上挂着白衬衣，光偏黄，背景是屋顶与树。零文字。": "003/H/033.png",
    "B107 柴堆上睡觉的猫：房檐下堆到高处的烧柴，一只花猫趴在最上面睡着，耳朵塌着。零文字。": "005/H/032.png",
    "B108 山间只有站牌的停靠点：一个只有站牌的停车点，站牌立在路边，四周是田和山，一个人也没有。零文字。": "005/H/033.png",
    "B109 从山顶望下去的京都：从山顶望向远处，京都市区铺在低处，雾蒙蒙一片，近处是草坡。零文字。": "005/H/034.png",
    "B111 山顶的会车：窄得几乎只能过一辆车的山顶路段，两辆公共汽车一前一后停着，路下就是坡。零文字。": "005/H/035.png",
    "武藏野的人工渠与空长椅：一条安静的水渠斜着穿过画面，岸边是修剪整齐的草坡和一行树。零文字。": "003/H/006.png",
    "B113 岔路口立着的木牌：林间岔路口，一块木牌立在路边，牌面朝路，木头的棱角已经磨圆。零文字。": "005/H/036.png",
    "B115 白色石墙与敞开的黑铁门：一道只有人高的白色矮石墙，中间是两扇铁铸的黑门，门完全开着，里面是树。零文字。": "005/H/037.png",
    "B116 空门卫室的桌面：门卫室窗内，一张小桌上摆着烟灰缸、茶杯和一台小收音机，杯子里的茶还剩一半。零文字。": "005/H/038.png",
    "B117 停车场里的深蓝色轿车：树下的停车场上只停着三辆车，其中一辆深蓝色轿车最靠里，车身上有一层薄灰。零文字。": "005/H/039.png",
    "B120 林中的转盘式交叉路口：林中的一个小转盘路口，几条路从这里散开，中心是一小片草地，路上没有人。零文字。": "005/H/040.png",
    "B121 旧别墅院子里的石灯笼：一座老式别墅的院子里，一座石雕灯笼立在草间，旁边的树被修剪得很齐整。零文字。": "005/H/041.png",
    "B122 凹地里的三层楼房：一栋简练的三层水泥楼建在凹下去的场地里，四周是树林，屋顶与树顶几乎齐平。零文字。": "005/H/042.png",
    "名古屋式的医院走廊：一条长而干净的室内走廊，光从一侧的窗照进来，走廊尽头没有人。零文字。": "003/H/022.png",
    "B123 大厅里的褐色沙发与帆布包：一间明亮的室内大厅，褐色的沙发上放着一只帆布包，旁边的地板上映着倒影。零文字。": "005/H/043.png",
    "B124 地板上鞋的倒影：擦得发亮的地板上，清楚地映着一双鞋的倒影，地板缝线笔直。零文字。": "005/H/044.png",
    "B126 一双正在握手的手：画面里只有两只握在一起的手，其中一只正被另一只轻轻转过来看。零文字。": "005/H/045.png",
    "树影下的一张空长椅：树影落在一张空长椅上，长椅背后是空着的路。零文字。": "003/H/024.png",
    "B129 抽象示意·留着的那一处弯：一根铁丝上有一处弯，别的部分都笔直，只有那处弯没有被掰动。零文字。": "005/H/046.png",
    "B130 背着包上缓坡的背影：一条山道上，一个背着帆布包的人正往上走，只给背影，两侧是树。零文字。": "005/H/047.png",
    "B133 白墙前站得很小的一个人：一堵很高的白墙前，一个人站得很小，影子贴在墙根。零文字。": "005/H/048.png",
    "纯白的空画面：整幅画面接近纯白，只有极淡的纸的纹理，别的什么都没有。零文字。": "004/H/055.png",
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
