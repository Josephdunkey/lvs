# 48 · 画面位归属模型与讲书稿写法相反：每段最后一条画面位被空置

Status: open
Type: bug
Severity: medium（静默浪费 ~5% 的图，不报错）

## 现象

用「讲书稿 → 拍摄稿 → `lvs parse` → 自建 shots.json」这条链跑《大师与玛格丽特》MM01 时：

- 讲稿里 `[画面位]` 共 **113** 条（按作者写法，每条都对应一张已生成的图）
- `lvs/shots.py` 的 `_sentences_with_visual()` 只把其中 **105** 条分到了镜
- 被空置的 5 条（2、81、99、106、108）**全部是每段的最后一条画面位**

也就是说：一条画面位只要落在段末，就永远分不到旁白 → 对应的图白出。

## 根因

讲书稿的画面位写法（本项目口径，见 `docs` 与 `MEMORY.md`）：

> 标记写在**所辖旁白块的末尾**，按「句后第一个画面位」归属，**等价于管它前面的句子**。

而 `lvs/shots.py::_owner_index()` 实现的是反过来的模型：

```python
def _owner_index(visuals, pos):
    owner = -1
    for i, (p, _) in enumerate(visuals):
        if p <= pos:
            owner = i
    return owner if owner >= 0 else 0
```

「归给**最近的前一条**画面位」。于是：

- 段末那条画面位**后面没有句子** → 它分不到任何旁白 → 空置
- 与讲书稿的实际语义（管它前面的块）差了一整块

两套模型在"每条画面位都紧跟一段旁白、且段末没有画面位"时**结果相同**，
所以这个 bug 只在**段末放了画面位**时才暴露 —— 而讲书稿恰恰习惯在每段末尾放一条收束镜。

## 影响

- 每段丢 1 条画面位 → 按 8 集 × 14 段估，全系列白出 **~100 张图**（约 1.5 小时 GPU）
- 更隐蔽的是：**不报错**。`lvs shots` 正常退出，只是有几条画面位从未出现在 `shots.json`
- 排查成本高：要拿讲稿的 `[画面位]` 条数和 `shots.json` 的镜数去比才会发现

## 建议修法

**首选**：把 `_owner_index` 改成"归给句后第一个画面位"（与本项目讲书稿口径一致）：

```python
def _owner_index(visuals, pos):
    """标记写在所辖旁白块的末尾 → 归给**句后第一个**画面位（管它前面的句子）。"""
    if not visuals:
        return -1
    k = bisect.bisect_right([p for p, _ in visuals], pos)
    return min(k, len(visuals) - 1)
```

**次选**（不动 lvs）：在自建 `shots.json` 的脚本里自己实现归属模型。
本次 MM01 走的就是这条 —— 见
`D:/fanshu/大师与玛格丽特/10-语料/知识视频素材库/99-工具/_make_shots_mm01.py`，
改一行后：**113 条画面位 → 110 镜，0 条空置，913/913 句全覆盖**。

> 若采纳首选，需同时确认 `.work/*/shots.json` 的既有任务是否要重跑 ——
> 归属模型反转会改变每镜的 `visual` 文本与镜序，`pin_*_beats.py` 的 BEAT_MAP key 也随之变化。
> 因此建议**只对新任务生效**，老任务不动。

## 复现

```bash
# 讲稿画面位条数
grep -c '^\[画面位\]' <讲稿.md>          # 113

# parse 后每段最后一条画面位归属
python - <<'PY'
import json
p = json.load(open(".work/MM01/parse.json", encoding="utf-8"))
for s in p["segments"]:
    flow = s.get("flow") or []
    vis = [i for i, f in enumerate(flow) if f.get("kind") == "visual"]
    if vis and vis[-1] == len(flow) - 1:
        print(s["heading"], "末条画面位在段末 → 将空置")
PY
```

## 相关

- 本项目口径原文：`MEMORY.md`「画面位归属」条
- 《挪威的森林》各期用 `pin_*_beats.py` 手工钉图，掩盖了这个问题（钉图按 beat 原文，
  不依赖归属），所以直到改用「自建 shots.json」的链路才暴露
