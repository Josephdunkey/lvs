#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把《挪威的森林》P3（讲书档）的 51 条 beat 钉死到具体图片上。

背景：`lvs shots` 会把拍摄稿的每条 `[画面位]` 按 `→` 拆成 beat，逐镜分配。
但**画面是从哪儿来的**它判不了 —— 手头的 75 张第一章分镜图是按「逐字朗读版」的
时序出的，与讲书版的镜序不是一一对应，靠关键词翻库必然错配。
所以这里用一张**人工核过**的映射表，逐 beat 钉 `library_asset`（D14 手动优先）。

映射表 `BEAT_MAP` 里：
  - `001/H/NNN.png`      = 复用第一章已有的 75 张分镜图
  - `002/H/NNN.png` / `001P1/H/NNN.png` = 前两期已出的图
  - `003/H/NNN.png`      = `gen_images_003.py` 本期新补的图

用法：
    python scripts/pin_nw03_beats.py            # 预览
    python scripts/pin_nw03_beats.py --write    # 写回 shots.json
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
TASK = "NW03"

# 冷开场（片头那段「这片草地上，有一口井」）没有画面位，单独钉一张：井的母题图
COLD_OPEN_ASSET = "003/H/001.png"

# ---- beat 映射（拍摄稿 beat 原文 → 图）-----------------------------------
# key 由 .work/NW02/parse.json 原样导出；value 由 _make_pin003.py 按 beat 序号直接给出。
# 003/H = gen_images_003.py 新补（40 张）；002/H、001P1/H、001/H = 复用前两期已出的图。
BEAT_MAP: dict[str, str] = {
    "B24 供水塔与天台：夜。天台上空无一人，一根晾衣绳上挂着一件忘收的白衬衣，被风吹得鼓起来，活像一个空壳；画面一角是圆筒形供水塔的铁梯。右侧远处是新宿的灯，左侧是池袋的灯。零文字。": "003/H/001.png",
    "B23 速溶咖啡瓶与萤火虫：特写。一只玻璃空瓶，瓶盖上几个针孔，瓶底铺着几片草叶和一点水，草叶旁有一个极小的、几乎看不清的淡黄光点。背景全黑，唯一光源就是那点光。零文字。": "003/H/002.png",
    "萤火虫与螺栓帽：极近特写。一只小小的萤火虫停在供水塔边缘一颗漆皮剥落的螺栓帽上，一动不动；疤痕一样凸起的旧漆，背景全黑，只有它腹部一点若隐若现的光。零文字。": "003/H/003.png",
    "萤光轨迹：纯黑画面中，一道极淡的、断断续续的萤光弧线，从画面右下方向左上方滑去，越远越淡，最后几乎看不见。除此之外什么都没有。零文字。": "003/H/004.png",
    "画面收黑：整幅画面收黑，只留极淡的颗粒。": "001P1/H/023.png",
    "系列报幕卡：一张桌球台的空台，四个球已收进球袋，头顶那盏台灯是灭的（画面零文字，期数后期叠加）。": "002/H/024.png",
    "B15 一米距离的背影：秋日的郊外小径，一名穿深色大衣的女孩走在前面，背影修长，头发笔直垂到肩胛；她身后半步之外，一个男生的半个肩膀刚刚入画。前景是被虚化的树影，光斑落在两人之间的地面上。女孩露出右侧的耳朵。零文字。": "003/H/005.png",
    "武藏野的人工渠与土堰：秋日午后，一条狭窄的人工渠沿着土堰流过，水面很平，两岸是修剪过的草坡和一排树。画面里没有人，只有一只空长椅。光很软。零文字。": "003/H/006.png",
    "女大学生公寓的房间：极简。榻榻米上只有一张矮桌、一个坐垫、一只小书架；窗台一角晾着一双长筒袜，窗玻璃干净到能映出对面房子的屋顶。冷白的天光。零文字。": "003/H/007.png",
    "榻榻米上的小桌：两副碗筷、一碟腌菜、一锅还冒着热气的米饭，桌角放着一只没有图案的粗瓷碗。斜光从窗子进来，没有人入画。零文字。": "003/H/008.png",
    "直子的后侧特写：只给侧面。黑色长发在右耳上方被一枚素色发卡别住，右耳完全露出来；背景虚化成一片浅灰。零文字。": "003/H/009.png",
    "结构示意：眼睛与透明的空处：画面中央只有一双眼睛的局部，眼白是过曝的白，瞳孔里什么倒影都没有；眼睛之外是一片完全空白的浅灰。抽象示意，零文字。": "003/H/010.png",
    "冬夜街头的手：夜里的东京街头，一件深色大衣的袖子上，搭着另一只戴着手套的手；两个人都只入画一半，看不见脸。背景是虚化的路灯与湿的路面。零文字。": "003/H/011.png",
    "一间拉上窗帘的房间，桌边一只空椅子。": "001P1/H/003.png",
    "S5-烟灰缸特写：一只空烟灰缸，一只手在反复拨弄它，桌面上反着吊灯的光。浅景深。": "002/H/026.png",
    "B13 阿姆斯特丹运河摄影画：挂在宿舍灰墙上的黑白摄影画，画面是运河、石桥与一排山形屋顶，河面有细碎的波纹。画框细白边，墙面上没有任何其他文字。": "002/H/010.png",
    "B13 态2 被揭掉摄影画的墙：宿舍的灰墙上只剩一枚空钉，画框留下的四角胶痕还在，墙面发暗。画面里没有任何文字，也没有人。零文字。": "003/H/012.png",
    "B6 地图：桌上摊开一张过于详尽的地形图，等高线密到近乎全黑，颜色是旧纸的黄；一只手在图上缓慢移动。图面零文字。": "002/H/011.png",
    "小鹿图案的毛衣：一件红黑相间、织着驯鹿图案的毛衣挂在一只旧衣架上，背后是宿舍房间的灰墙。画面里没有人。零文字。": "003/H/013.png",
    "B17 红绸带与唱片封套：一只正方形的唱片封套，用牛皮纸包着，上面系着一根红色绸带打的礼品结；桌面是旧木头的颜色。封套上没有任何文字。零文字。": "003/H/014.png",
    "手织毛线手套：两只戴着毛线手套的手，拇指明显短了一截，针脚不太均匀；背景是深色大衣的粗花呢。只给手，不给脸。零文字。": "003/H/015.png",
    "新年暖灯下的杂煮锅：榻榻米上的一只小砂锅正在冒热气，旁边摆着两块烤过的年糕和两只粗瓷碗；一盏暖黄的灯把锅沿照出一圈光。没有人入画。零文字。": "003/H/016.png",
    "B20 东大图书馆的斜阳与外文书：傍晚的图书馆长窗，斜阳落在摊开的一本硬壳外文书上，书页上布满细密的西文；桌角放着一支钢笔和一个空水杯。书页上的文字全部虚化成线条，不可辨认任何具体字词。": "003/H/017.png",
    "食堂窗边两个并排的位置：宿舍食堂靠窗的一张长桌，两个位置并排，左边摊着一本书，右边放着一只还没来得及碰的汤碗；窗外是灰色的院子。没有人入画。零文字。": "003/H/018.png",
    "宿舍走廊的对峙：夜里的宿舍走廊，几个人的剪影挤在画面里，其中一个手里横着一根木刀的轮廓；光线来自走廊尽头的一盏灯，脸全部看不清。零文字。": "003/H/037.png",
    "B22 盐水杯与空杯：桌面特写。一只装满盐水的玻璃杯，杯壁挂着水珠，旁边还放着三只空杯。冷白的光，桌面是旧木。没有人入画。零文字。": "003/H/019.png",
    "涩谷深夜的霓虹光河：深夜的涩谷街头，湿的路面上倒映着大片模糊的彩色光团，远处是层叠的招牌形状——全部虚化成无字色块，一个可辨认的字都没有。画面里只有一两个远处的背影。": "003/H/020.png",
    "B21 情人旅馆的早上：室内，厚重的暗红窗帘拉着，床灯还亮着，一股隔夜酒气的感觉；镜子前有一个正在化妆的女孩的侧影，只给轮廓，不给脸。床头柜上放着烟和一只空杯子。零文字。": "003/H/021.png",
    "名古屋式医院的走廊：一条很长、很干净、很空的医院走廊，地面反光，两侧是关着的门。没有人。冷白的顶光。零文字。": "003/H/022.png",
    "初美的侧影：一名年轻女性站在窗边，穿素色高领衫与长裙，姿态挺拔安静，只给半身侧影，脸在逆光里看不清；窗外是阴天的灰色天空。零文字。": "003/H/023.png",
    "树影下的一张长椅：四月的公园，一张空长椅，头顶是刚长出嫩叶的树，光斑落在椅面上。画面里没有人，椅子的另一头空着。零文字。": "003/H/024.png",
    "雨天国分寺车站站台：傍晚的郊外小站，站台被雨淋得发亮，长椅上没有人，远处是湿的铁轨和昏黄的车灯。零文字。": "003/H/025.png",
    "电车里被挤塌的蛋糕盒：一只用细绳系着的白色纸盒被拎在手里，盒身已经被挤得歪斜，一角凹进去。背景是虚化的车厢内景。零文字。": "003/H/026.png",
    "B18 二十支蜡烛与塌了的蛋糕：黑暗的房间里，桌上那盒塌成圆形剧场的蛋糕上插着二十支还在燃的小蜡烛，烛光把桌面照出一小圈暖黄，房间其余部分全是黑的。零文字。": "003/H/027.png",
    "B19 雨窗与唱片套上的泪痕：雨夜，窗玻璃上全是流动的水痕，室内很暗；地毯上摊着一张唱片套，套面上有几滴明显的水渍。零文字。": "003/H/028.png",
    "散乱的唱片套与酒杯：榻榻米上散乱地放着几张唱片套、两只玻璃杯、一只葡萄酒瓶和一只干净的烟灰缸；桌上剩着半盒变形的生日蛋糕，像时间在这里突然停住了。没有人入画。零文字。": "003/H/029.png",
    "半截话的示意：抽象示意。一句话被从中间剪断，前半段化作一条实线，后半段的线开始碎成小段、渐渐消散在空中。除此之外整幅画面是空的。零文字。": "003/H/030.png",
    "B11 薄雾：室内或空镜，一层极淡的白雾刚刚凝出一点模糊的边界，像是有东西正在从雾里显形，但还看不出是什么。低对比，大幅留白。": "002/H/025.png",
    "一双手托着一个空房间的模型，屋里的家具正在一件件消失。": "001P1/H/020.png",
    "空白的年历与便笺：书桌前的墙上贴着一张既无摄影又无绘画的年历，整张纸一片洁白、上面只有格子；书桌上放着一张摊开的便笺和一支笔。零文字。": "003/H/038.png",
    "门上被撤掉姓名卡片的公寓门：一扇木板套窗关得严严实实的公寓门，门上原本贴姓名卡片的位置只剩一小块颜色不同的空白，胶痕还在。走廊里没有人。零文字。": "003/H/031.png",
    "台灯下的一封长信：夜里的书桌，一盏旧台灯压低照着一叠写满字的信纸（字迹全部虚化成不可辨认的灰线），旁边放着一支笔和一枚信封。零文字。": "003/H/039.png",
    "M2 井态2：承接第一期的草地井口，井沿的石头长着苔，井里正有一层黑雾缓慢升起，已经漫到井口边缘。天空是灰的，草被风压低。": "001/H/040.png",
    "一封短信与京都山里的疗养院：画面下方是一张只有几行字的短信（字迹虚化成灰线），画面上方是远处山谷里的一栋白色建筑轮廓，被松林围住；整幅是灰白的雾。零文字。": "003/H/040.png",
    "罢课后空无一人的阶梯教室：五月底的大学教室，黑板上一片空白（零文字），阶梯座椅全部空着，斜光从高窗照进来，灰尘在光柱里浮动。零文字。": "003/H/032.png",
    "1969 东京校园街景：戴安全帽的学生人群、拉起的绳索、无字旗面，远景是校门与阶梯教室。": "001P1/H/004.png",
    "天台晾衣绳上的白衬衣：宿舍天台，一根晾衣绳上挂着一件忘收的白衬衣，被晚风吹得鼓起来，衣摆在空中摇荡，像一个没有身体的空壳。背景是灰蓝的暮色。零文字。": "003/H/033.png",
    "B25 记忆中的旧式水门与萤火：夜。一座青砖砌的旧式水门立在一条小河边，岸边的水草几乎盖满了河面；水门上方的积水潭上，几百只萤火虫交织成一片，像正在燃烧的火星。画面里没有一个人，四周是很深的黑。零文字。": "003/H/034.png",
    "速溶咖啡瓶里的萤火虫：玻璃瓶特写，瓶底几片草叶、薄薄一层水，瓶壁上有一个正在往上爬又滑落的小虫的影子；瓶底透出一点极微弱的淡黄光，瓶盖的针孔透着外面的夜色。零文字。": "003/H/035.png",
    "供水塔与残缺的月亮：仰视或平视，一座圆筒形的旧供水塔立在屋顶天台上，栏杆生锈；天上一轮略微残缺的苍白月亮，塔身反着一层很淡的光。没有人。零文字。": "003/H/036.png",
    "M2 井态3 / M1 草地：吞没一切的黑，与草地空镜回扣第一期收尾。": "001/H/075.png",
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
