# -*- coding: utf-8 -*-
"""按「正文非空白字符 / 4.5 字/秒」重算：场景时间码 + 画面位清单表 + 头部元信息"""
import re, sys, pathlib, difflib

CPS = 4.5
MAT_OVERRIDE = {
    "草叶被金风吹出层层波纹": "复用已有图（母题 M1 态1，与原 01 首拍同源）",
    "壳上凝着晨露": "复用已有图（知了壳见 28 号镜；旧书见 04 号报幕卡）",
}

def norm(s):
    return re.sub(r'[\s\[\]【】（）()，,。、：:；;]', '', s)

def clean_vis(line):
    s = line.strip()
    s = re.sub(r'^\[画面位\]\s*', '', s)
    s = re.sub(r'\[场景\]\s*|\[图表\]\s*', '', s)
    return s.strip()

def mmss(sec):
    sec = int(round(sec))
    return "%d:%02d" % (sec // 60, sec % 60)

def run(path, write=False):
    p = pathlib.Path(path)
    t = p.read_text(encoding='utf-8')
    i_body = t.find('## 二、正文讲稿')
    i_tab = t.find('## 三、画面位清单')
    assert i_body > 0 and i_tab > i_body, "章节定位失败"
    head, body, tail = t[:i_body], t[i_body:i_tab], t[i_tab:]

    lines = body.splitlines()
    secs = []            # [name, cum_start, chars, nline, vis_times[]]
    cur = 0.0
    vis_index = []       # 正文里画面位出现顺序 -> (sec_idx, time)
    si = -1
    pending = []
    # ★ 引文块**块语义**（与 lvs/parse.py 2026-10-04 修复同口径）：
    #   `> 〔引原文〕` 标记行与其后到空行/结构边界的整块都不进旁白。
    #   此前只跳标记行 → 块正文被计入字数与时间 → 时间码整体后移（003 实测虚高 784 字/约 3 分钟）。
    in_quote = False
    for raw in lines:
        l = raw.rstrip()
        m = re.match(r'^###\s*【(.+?)】\s*[\d:]+[–—~至](?:[\d:]+|结尾)', l)
        if m:
            si += 1
            secs.append({"name": m.group(1), "start": cur, "chars": 0, "lines": 0, "vis": []})
            in_quote = False
            continue
        s = l.strip()
        if not s:
            in_quote = False
            continue
        if s.startswith('[画面位]'):
            in_quote = False
            secs[si]["vis"].append(cur)
            vis_index.append((si, cur, clean_vis(s)))
            continue
        if s.startswith('>') and '〔引原文〕' in s:
            in_quote = True
            continue
        if in_quote:
            # 块正文行（无 `>` 前缀也算）；到空行或结构边界为止
            continue
        txt = s[1:].strip() if s.startswith('>') else s
        if s.startswith('#'):
            continue
        n = len(re.sub(r'\s', '', txt))
        secs[si]["chars"] += n
        secs[si]["lines"] += 1
        cur += n / CPS
    total = cur

    # ---- 1. 改写场景时间码 ----
    out = []
    si = -1
    for raw in lines:
        l = raw.rstrip()
        m = re.match(r'^(###\s*【(.+?)】)\s*[\d:]+[–—~至](?:[\d:]+|结尾)\s*$', l)
        if m:
            si += 1
            s0 = secs[si]["start"]
            s1 = secs[si+1]["start"] if si + 1 < len(secs) else total
            out.append("%s%s–%s" % (m.group(1), mmss(s0), mmss(s1)))
        else:
            out.append(l)
    body_new = "\n".join(out) + "\n\n"

    # ---- 2. 重排画面位清单表 ----
    old = []
    for l in tail.splitlines():
        if re.match(r'^\|\s*\d+\s*\|', l):
            c = [x.strip() for x in l.strip().strip('|').split('|')]
            if len(c) >= 4:
                old.append((c[1], c[2], c[3]))
    rows = []
    for si2, tsec, vis in vis_index:
        best, ratio = None, 0.0
        for o in old:
            r = difflib.SequenceMatcher(None, norm(vis), norm(o[1])).ratio()
            if r > ratio:
                best, ratio = o, r
        mat = best[2] if (best and ratio >= 0.55) else "**需补绘**"
        for key, val in MAT_OVERRIDE.items():
            if key in vis:
                mat = val
                break
        rows.append((mmss(tsec), vis, mat, ratio))
    # 未被匹配到的旧行（说明被合并或删了）
    used = set()
    for _, vis, _, _ in rows:
        for k, o in enumerate(old):
            if difflib.SequenceMatcher(None, norm(vis), norm(o[1])).ratio() >= 0.55:
                used.add(k)
    dropped = [o for k, o in enumerate(old) if k not in used]

    # ★ 只替换 `## 三、画面位清单` 后面那张表本身；表之后的章节（四、五…）必须原样保留
    _ls = tail.splitlines()
    _i = next(i for i, x in enumerate(_ls) if x.startswith('## 三、'))
    _j = _i + 1
    while _j < len(_ls) and not _ls[_j].strip().startswith('|'):
        _j += 1
    _k = _j
    while _k < len(_ls) and _ls[_k].strip().startswith('|'):
        _k += 1
    tab_pre = "\n".join(_ls[:_j])
    after = "\n".join(_ls[_k:]).lstrip("\n")
    new_tab = ["| # | 时间 | 画面 | 素材 |", "|---|---|---|---|"]
    for n, (tm, vis, mat, r) in enumerate(rows, 1):
        new_tab.append("| %02d | %s | %s | %s |" % (n, tm, vis, mat))
    tail_new = tab_pre + "\n" + "\n".join(new_tab) + "\n\n" + after

    # ---- 3. 头部元信息 ----
    nar = sum(s["chars"] for s in secs)
    # 引原文字数：紧跟 `> 〔引原文〕` 标记行之后的那一行
    q = 0
    _ls = body.splitlines()
    for k, l in enumerate(_ls):
        if l.strip().startswith('>') and '〔引原文〕' in l:
            j = k + 1
            while j < len(_ls) and not _ls[j].strip():
                j += 1
            if j < len(_ls):
                q += len(re.sub(r'\s', '', _ls[j].strip().lstrip('>').strip()))
    head_new = head
    head_new = re.sub(r'> 旁白字数[^\n]*\n',
                      '> 旁白字数 %d 字（其中引原文 %d 字，占 %.1f%%）｜估算时长 %s（4.5 字/秒估算，**待配音后回填真实时长**）\n'
                      % (nar, q, 100.0 * q / max(nar, 1), mmss(total)),
                      head_new, count=1)
    head_new = re.sub(r'> 场景[^\n]*\n',
                      '> 场景 %d 个｜画面位 %d 条｜行数 %d 行\n' % (len(secs), len(rows), 0),
                      head_new, count=1)

    result = head_new + body_new + tail_new
    result = re.sub(r'行数 \d+ 行', '行数 %d 行' % (result.count('\n') + 1), result)

    print("=" * 60)
    print(path)
    print("总时长 %s（%.0f 秒）｜旁白 %d 字｜场景 %d｜画面位 %d 条" % (mmss(total), total, nar, len(secs), len(rows)))
    for s in secs:
        print("   %-24s %s" % (s["name"], ""))
    print("--- 画面位清单预览 ---")
    for n, (tm, vis, mat, r) in enumerate(rows, 1):
        flag = "  <== 新/需人工确认" if r < 0.55 or r < 0.75 else ""
        print("  %02d %-7s %-58s %-10s (match %.2f)%s" % (n, tm, vis[:56], mat, r, flag))
    if dropped:
        print("--- 未匹配到的旧行（需人工处理） ---")
        for o in dropped:
            print("  ", o)
    if write:
        p.write_text(result, encoding='utf-8')
        print(">>> 已写入")
    return result

if __name__ == "__main__":
    run(sys.argv[1], write=("--write" in sys.argv))
