#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把《挪威的森林》P4（讲书档）的 70 条 beat 钉死到具体图片上。

背景：`lvs shots` 会把拍摄稿的每条 `[画面位]` 按 `→` 拆成 beat，逐镜分配。
但**画面是从哪儿来的**它判不了 —— 手头的 75 张第一章分镜图是按「逐字朗读版」的
时序出的，与讲书版的镜序不是一一对应，靠关键词翻库必然错配。
所以这里用一张**人工核过**的映射表，逐 beat 钉 `library_asset`（D14 手动优先）。

映射表 `BEAT_MAP` 里：
  - `001/H/NNN.png`      = 复用第一章已有的 75 张分镜图
  - `002/H/NNN.png` / `003/H/NNN.png` / `001P1/H/NNN.png` = 前三期已出的图
  - `004/H/NNN.png`      = `gen_images_004.py` 本期新补的图

用法：
    python scripts/pin_nw04_beats.py            # 预览
    python scripts/pin_nw04_beats.py --write    # 写回 shots.json
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
TASK = "NW04"

# 冷开场（片头那段「这片草地上，有一口井」）没有画面位，单独钉一张：井的母题图
COLD_OPEN_ASSET = "004/H/001.png"

# ---- beat 映射（拍摄稿 beat 原文 → 图）-----------------------------------
# key 由 .work/NW02/parse.json 原样导出；value 由 _make_pin004.py 按 beat 序号直接给出。
# 004/H = gen_images_004.py 新补（55 张）；003/H、002/H、001P1/H、001/H = 复用前三期已出的图。
BEAT_MAP: dict[str, str] = {
    "B42 抽象示意·草莓蛋糕与窗口：室内靠窗一角，窗扇向外敞开，窗台上倒放着一只空的奶油色纸盒，盒盖被风掀起一角；窗外一片过曝的亮。没有人。零文字。": "004/H/001.png",
    "画面收黑：整幅画面收黑，只留极淡的颗粒。": "001P1/H/023.png",
    "系列报幕卡：一张桌球台的空台，红白四个球已经收进球袋，台面上一盏灯已经熄灭。零文字。": "002/H/024.png",
    "1969 东京校园街景：复课后的大学正门与银杏道，人不多，斜光拉得很长。零文字。": "001P1/H/004.png",
    "B30 小餐馆窗边空着的对面座位：窗边的一张桌子，桌上一份吃完的煎蛋与青豌豆色拉留下的空盘，对面那把椅子拉开着，没有人坐。零文字。": "004/H/002.png",
    "B29b 逆光里的短发肩线：从侧后方拍的一个人，头发短到露出后颈，穿白棉布连衣裙，肩上一层很亮的光。只给背影与肩线，不给脸。零文字。": "004/H/003.png",
    "罢课后空无一人的阶梯教室：大学教室，黑板上一片空白，阶梯座椅全都空着，斜光从高窗照进来，灰尘在光柱里浮动。零文字。": "003/H/032.png",
    "复课后的阶梯教室：同一间阶梯教室，人坐得七零八落，前排只有三四个人，后排空出一大片，黑板仍旧空白。零文字。": "004/H/004.png",
    "一栏姓名卡片只剩一张：宿舍门旁的金属姓名卡槽，别人的卡片都空了，只剩一张还插在槽里，纸角有点卷。零文字。": "004/H/005.png",
    "B28 图书馆的桌面与课表：图书馆靠窗的一张长桌，桌上摊着一本厚书和几本笔记，窗外是秋天下午的院子。零文字。": "004/H/006.png",
    "空书桌上的积灰与收音机：宿舍一角，一张书桌落着薄灰，桌角摆着一台旧收音机，搁物架上整齐地放着塑料杯、牙刷和一只茶筒。零文字。": "004/H/007.png",
    "管理主任室的窗口：一个半开的小窗口，窗台上放着一本翻开的登记簿，簿面空白，窗口后面没有人。零文字。": "004/H/008.png",
    "空房间的模型：一双手托着一个空房间的模型，房间里有床、有桌、有一盏灯，没有人。零文字。": "001P1/H/020.png",
    "B5 煎蛋与青豌豆色拉的餐盘：白瓷盘里一只煎蛋、一份青豌豆色拉和几个蘑菇，旁边一把叉子，桌面是旧木。零文字。": "004/H/009.png",
    "B29 桌面上的深色太阳镜：一只手把深色太阳镜放在餐馆桌面上，镜片朝上，旁边是咖啡杯与糖匙。只给手与桌面，不给脸。零文字。": "004/H/010.png",
    "短到露出后颈的发梢：从侧后方看，头发短到露出整段后颈与耳廓，发梢因为长出来而微微有些不齐。只给上半身的侧后。零文字。": "004/H/011.png",
    "侧脸静止的五秒：一个女孩的侧脸静止不动，鼻梁和下颌线很干净，耳垂上什么都没有，背后是一扇半亮的窗。零文字。": "004/H/012.png",
    "窗边的两人位：餐馆靠窗的座位，桌上两份餐具，窗外是一小段安静的街道，阳光把窗格投在桌面上。零文字。": "004/H/013.png",
    "桌面上的太阳镜与手：一只摘下来的太阳镜放在桌面，旁边是托着腮的一只手，桌上还有一杯没喝完的咖啡。零文字。": "004/H/014.png",
    "电话亭与街角：一个旧式电话亭立在街角，玻璃上有一点反光，里面的听筒挂回了叉簧。零文字。": "004/H/015.png",
    "盒饭专卖店的红漆方盒：桌面上两只四方形红漆盒子，盒盖打开着，里面一格一格装着饭和小菜，旁边一碗汤。零文字。": "004/H/016.png",
    "树影下的一张空长椅：一棵大树的树影落在一张空长椅上，长椅背后是一段矮墙，光斑在椅子上慢慢移动。零文字。": "003/H/024.png",
    "B32 那缕笔直的白烟与鸽子：一棵大树的旁边，一缕很直的淡烟从地面升起，烟柱细而稳，屋脊上有两只鸽子，背景是晴朗的天。零文字。": "004/H/017.png",
    "B31 公园长椅与爬满常春藤的旧校舍：一张公园长椅正对着远处的旧校舍，校舍的墙面被常春藤爬满，屋檐下有一排小窗。零文字。": "004/H/018.png",
    "旧木桌上的奖状与旧辞典：一张旧木桌上放着一卷证书和一本厚厚的旧辞典，纸边有些发黄，桌面上没有别的字。零文字。": "004/H/019.png",
    "杂志架的一排排封面：书店门口一排杂志架，每一本的封面都是空白的色块。零文字。": "004/H/020.png",
    "B6 地图：一张摊开的老地图，纸面是旧黄的颜色，路网清晰，所有的地名与标注都是空白的。零文字。": "002/H/011.png",
    "食堂长桌的对坐：一张长条食堂桌，两个人对面坐着，桌上两只餐盘、一只汤碗。只给背影与桌面的手。零文字。": "004/H/021.png",
    "食堂窗边两个并排的位置：食堂窗边两个并排的座位，桌上是两份没动过的餐盘，窗外是秋天的树。零文字。": "003/H/018.png",
    "隔着走道的两个座位：图书馆里隔着一条走道的两个座位，一个上面放着一本合着的书，另一个空着。零文字。": "004/H/022.png",
    "B34 深夜酒吧的吧台与两只空杯：吧台上一盏昏黄的灯，灯下并排两只空杯，吧台后面的酒架一格一格，标签都空着。零文字。": "004/H/023.png",
    "涩谷深夜的霓虹光河：深夜街道上的招牌与霓虹全部虚化成一片白光，路面上没有行人，只有两三个模糊的影子。零文字。": "003/H/020.png",
    "红脑袋蜻蜓与院子：初秋的院子，一群红脑袋的蜻蜓在半空里悬停，下面是一堵矮墙和几盆花草，一个人也没有。零文字。": "004/H/024.png",
    "B37 水仙花插在高玻璃杯里：一只细高的玻璃杯里插着十来枝白色水仙，杯子放在旧木桌面上，光线从右边照过来。零文字。": "004/H/025.png",
    "B35 都营电车与古旧房屋：电车紧贴着一排低矮的旧房屋行驶，房檐几乎够到车窗，屋顶上晾着东西，天上没有云。零文字。": "004/H/026.png",
    "大冢站前的旧商业街：一条老街，两侧都是低矮的旧铺面，招牌空白，路面笼着一层灰蒙蒙的薄雾似的脏。零文字。": "004/H/027.png",
    "B36 小林书店落到底的卷闸门：一间旧书店的门脸，卷闸门一落到底，门上的招牌空白，旁边按着一只电铃。零文字。": "004/H/028.png",
    "B39 二楼的陡梯与昏黄客厅：从昏暗的客厅往上看，一道又窄又陡的木楼梯通向二楼，楼梯口透出一线昏黄的亮光。零文字。": "004/H/029.png",
    "B38 厨房里做饭的背影与明晃晃的窗光：一个短发的人背对镜头站在灶台前，一手拿锅一手拿铲，旁边的菜板上放着几样切好的菜，灶台上方窗户的光很亮。只给背影。零文字。": "004/H/030.png",
    "B38b 一桌关西风味的饭菜：一张矮桌上摆着六七个碟子，有鱼片、荷包蛋、炖菜、汤和一碗饭，碟子都很素净，光线从窗口照进来。零文字。": "004/H/031.png",
    "一本翻旧了的食谱：一本翻开在桌上的旧食谱，纸页上全是空白，书角卷起，旁边放着一只小碗。零文字。": "004/H/032.png",
    "晾在绳上的一副浅色衣物：阳台的绳上晾着两件浅色的贴身衣物，风很小，衣角垂着，背景是一段旧墙。零文字。": "004/H/033.png",
    "名古屋式医院的走廊：一条长走廊，尽头有一扇亮着的窗，两边是关着的门，门牌空白，走廊上没有人。零文字。": "003/H/022.png",
    "B6 地图：一张摊开的老地图，纸面旧黄，路网和海岸线很清楚，所有的地名标注都是空白的。零文字。": "002/H/011.png",
    "客厅的旧沙发与茶几：一间昏黄的客厅，旧沙发上没有人，茶几上放着一只空茶杯和一份摊开的报纸，报纸上空白。零文字。": "004/H/034.png",
    "二楼窗口望向街道：从一个旧屋的二楼窗口往外看，下面是空荡荡的商业街，卷闸门一排排落着。零文字。": "004/H/035.png",
    "B40 晾衣台与远处升起的浓烟：一处比周围屋脊高出很多的晾衣台，栏杆很细，远处隔三四座房子的地方，一团浓烟正腾空而起。零文字。": "004/H/036.png",
    "浓烟与看热闹的人群：远景，一团黑烟在低矮的屋顶后面翻涌，前面站着几个很小的人影，全都背对着画面。零文字。": "004/H/037.png",
    "初秋午后的肩线与手：两个人并坐在栏杆前的肩线，只给肩、手臂和搭在栏杆上的手，看不见脸。零文字。": "004/H/038.png",
    "B41 坐垫、四瓶啤酒与一把吉他：晾衣台的地上放着两张坐垫，旁边是四瓶啤酒和一把木吉他，栏杆外是屋顶与天空。零文字。": "004/H/039.png",
    "空灶台与一只锅：厨房的灶台上放着一口空锅，灶眼是冷的，旁边什么都没有。零文字。": "004/H/040.png",
    "斜靠的肩膀与栏杆：两个人的肩线上，一颗很短的头发靠过来，外面是屋顶和渐渐暗下来的天。只给肩与头发的轮廓。零文字。": "004/H/041.png",
    "晾衣台栏杆外的黄昏：晾衣台的栏杆外，天色开始发灰，屋脊一层一层远处融在一起。零文字。": "004/H/042.png",
    "一间拉上窗帘的房间：一间关着窗帘的房间，光线很暗，只有一条亮的缝，地上有一只空椅子的腿。零文字。": "001P1/H/003.png",
    "B42 抽象示意·被推开的碗：一只装着饭的碗被一只手从桌子中间往边上推开，碗里的饭一点没动，桌面素净。零文字。": "004/H/043.png",
    "B42b 抽象示意·窗口与一双手：一个人站在窗口，两只手扶在窗框上，窗外一片过曝的亮，只给手的背面与窗框。零文字。": "004/H/044.png",
    "B42 抽象示意·从窗口扔出的盒子：一扇开着的窗，一只奶白色的盒子刚被抛到窗外，在半空中翻着。零文字。": "004/H/045.png",
    "空着的两只手：一双空着的手在桌面上摊开，掌心朝上，手里什么都没有。零文字。": "004/H/046.png",
    "桌上单独放着一样很小的东西：桌面上只放着一只素净的玻璃杯，旁边空无一物。零文字。": "004/H/047.png",
    "B43 抽象示意·被阴影慢慢盖住的房间：一个房间内部，光从一侧照进来，房间的另一侧正被一大片深色慢慢覆盖过去，家具只剩轮廓。零文字。": "004/H/048.png",
    "电线杆上的两只乌鸦：两根电线杆的顶上停着两只乌鸦，下面的街道已经空了，只剩下一点水迹。零文字。": "004/H/049.png",
    "B43b 初秋午后的吻与睫毛的影子：两个人侧面靠近的剪影，只给轮廓与贴在一起的肩线，阳光把睫毛的影子投在脸上。零文字。": "004/H/050.png",
    "两个人之间的一小段空栏杆：晾衣台的栏杆上，两只手各自搭在一处，中间隔着一小段空栏杆。零文字。": "004/H/051.png",
    "校园午休的院子：一片校园的空地，远处是教学楼，近处有几张长椅，三三两两的人影都很小。零文字。": "004/H/052.png",
    "阳光下的院子与远处的门：明亮的院子，几个人影在远处走动，一个人独自坐在近处的长椅上，只给背影。零文字。": "004/H/053.png",
    "B9 桌球台态2：一张空着的桌球台，红白四个球已经收进球袋，台面上方那盏灯已经关掉。零文字。": "002/H/024.png",
    "B44 新宿清晨的自动售货机与席地而坐：天刚亮的西口广场，一台自动售货机亮着灯，地上坐着两个人的剪影，旁边是几个酒瓶。零文字。": "004/H/054.png",
    "纯白的空画面：整幅画面接近纯白，只有极淡的纸的纹理，别的什么都没有。零文字。": "004/H/055.png",
    "B26 光的轨迹（回收 P3）：纯黑的画面里，一道极淡的光的轨迹从一侧划过，越远越弱，最后看不见。零文字。": "003/H/004.png",
    "一封短信与京都山里的疗养院：桌上一封摊开的短信，纸面空白，窗外是山里的树影。零文字。": "003/H/040.png",
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
