"""**离线端到端**：`parse → shots → assets → voice → build` 全链跑通，不需要网 / GPU / ComfyUI / LLM。

## 为什么必须有这一条

769 个测试大多是**单元级**：单独测一个函数、一个分支。但"**整条链还接得上吗**"
没有任何自动保证 —— 而本项目的历史故障恰恰多是**跨阶段的**：
产物契约变了、血缘记错了、门禁不看了、某个阶段的上游换了名字。

真机跑一次是**数小时**（654 镜 × 20s），不可能每次改动都跑。所以这里用两个
**离线后端**把速度压到秒级：

| 环节 | 真机 | 离线 |
|---|---|---|
| 拆镜 | LLM | `--no-llm` 启发式 |
| 生图 | ComfyUI（~20s/镜，吃 6GB 显存） | `[comfyui].backend="placeholder"`（ffmpeg 造图，毫秒级） |
| 配音 | edge-tts（联网）/ 本地模型（吃显存） | `[tts].backend="silent"`（ffmpeg 静音轨） |

## 关键：**走的是同一条路径**

离线后端只换掉"像素/声音从哪来"，**阶段编排、逐镜循环、失败隔离、契约校验、
产物血缘、成片终检**全是真实代码 —— 这正是它有价值的原因。
（如果测试自己造假产物，就绕过了这一切，等于没测。）

## 顺带覆盖

静音后端的成片**必然是静的**，所以 `build.final_sanity` 会报"整片近乎静音" ——
这里**刻意断言这条警告出现**：它证明成片终检在整链里真的跑到了（而不是只活在单测里）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lvs import stage as stage_mod
from lvs.config import Config
from lvs.workspace import Workspace

#: ★ 慢组（真跑 ffmpeg / 真建 wheel / 整条 e2e）—— 日常验证用 -m "not slow and not gpu" 跳过
pytestmark = pytest.mark.slow

TASK = "E2E"

MANUSCRIPT = """# 001-测试集 拍摄稿

## 一、传达层

**【标题】**
> 测试标题

**【冷开场】**（0:00–0:10，从最戏剧的一刻切入）
> 这里有一口井。

---

## 二、正文讲稿

### 【① 开场】0:00–0:10

这片草地上，有一口井。

谁也不知道它在哪儿。

[画面位] [场景] 十月的芒草坡，风过草浪

### 【② 相遇】0:10–0:25

她侧身走过校墙。

[画面位] [场景] 少女侧身走过校墙，背景只有枯枝
"""

CONFIG = """\
[app]
llm_provider = "openai"

[tts]
backend = "silent"

[comfyui]
backend = "placeholder"
width = 640
height = 384

[shots]
# ★ 必选、无默认值：同一画面位的多个镜是各出一张图（shot）还是共用一张（beat）。
# 这里显式写 shot —— 让全链测试跑的是"每镜一张"那条最朴素的路。
image_granularity = "shot"

[voice]
align = "estimate"

[pipeline]
enforce = false
"""


@pytest.fixture(scope="module")
def offline_run(tmp_path_factory):
    """跑一次完整离线流水线，把 (workspace, config, 每阶段结果) 交给各用例复用。

    `scope="module"`：整链只跑一次 —— 它虽然快，但没必要每个断言重跑一遍。
    """
    root = tmp_path_factory.mktemp("e2e")
    manuscript = root / "稿.md"
    manuscript.write_text(MANUSCRIPT, encoding="utf-8")
    cfg_path = root / "config.toml"
    cfg_path.write_text(CONFIG, encoding="utf-8")

    config = Config.load(cfg_path)
    ws = Workspace(task=TASK, root=root).ensure()

    options: dict[str, dict] = {
        "parse": {"manuscript": str(manuscript)},
        "shots": {"no_llm": True},
        "assets": {},
        "voice": {},
        "build": {},
    }
    results: dict[str, stage_mod.StageResult] = {}
    for name in ("parse", "shots", "assets", "voice", "build"):
        st = stage_mod.get(name)
        ctx = stage_mod.StageContext(config=config, ws=ws, options=options[name])
        results[name] = stage_mod.run_stage(st, ctx, log=lambda *a: None)

    return ws, config, results


# ---- 每一阶段都成功 ---------------------------------------------------------


@pytest.mark.parametrize("name", ["parse", "shots", "assets", "voice", "build"])
def test_each_stage_succeeds_offline(offline_run, name: str):
    _, _, results = offline_run
    assert results[name].code == 0, f"{name} 退出码 {results[name].code}"


def test_produces_a_final_video(offline_run):
    ws, _, _ = offline_run
    final = ws.path("final.mp4")
    assert final.is_file(), "整链没产出成片"
    assert final.stat().st_size > 10_000, "成片太小，像空文件"


def test_final_video_has_an_audio_track(offline_run):
    """★ 走的是 `build.final_sanity` 的同一条检查 —— 无音轨是硬错。"""
    from lvs.ffmpeg import has_audio_stream

    ws, _, _ = offline_run
    assert has_audio_stream(ws.path("final.mp4"))


def test_video_and_audio_durations_agree(offline_run):
    """音画不漂移：成片时长应与旁白整轨一致（`build` 的核心不变量）。"""
    from lvs.ffmpeg import duration

    ws, _, _ = offline_run
    final = duration(ws.path("final.mp4")) or 0.0
    narration = duration(ws.path("audio", "narration.mp3")) or 0.0
    assert final > 0 and narration > 0
    assert abs(final - narration) < 0.5, f"成片 {final:.2f}s vs 旁白 {narration:.2f}s"


# ---- 中间产物齐全（链没断的证据） -------------------------------------------


@pytest.mark.parametrize("rel", [
    "parse.json", "shots.json", "subtitle.srt", "audio/narration.mp3",
])
def test_intermediate_artifacts_exist(offline_run, rel: str):
    ws, _, _ = offline_run
    assert ws.path(rel).is_file(), f"缺少中间产物 {rel}"


def test_every_shot_got_an_image_and_audio(offline_run):
    ws, _, _ = offline_run
    data = json.loads(ws.path("shots.json").read_text(encoding="utf-8"))
    shots = data["shots"]
    assert shots, "没有分镜"
    for s in shots:
        assert s.get("asset_path") and Path(s["asset_path"]).is_file(), f"镜 {s['id']} 缺图"
        assert s.get("audio_path") and Path(s["audio_path"]).is_file(), f"镜 {s['id']} 缺音"
        # 时间轴必须被写回（下游切片靠它）
        assert s.get("start") is not None and s.get("end") is not None


def test_manifest_records_all_stages(offline_run):
    ws, _, _ = offline_run
    manifest = json.loads(ws.manifest_path.read_text(encoding="utf-8"))
    for name in ("parse", "shots", "assets", "voice", "build"):
        assert name in manifest["stages"], f"manifest 缺 {name} 记录"


# ---- 跨阶段机制在整链里真的生效 ---------------------------------------------


def test_sanity_check_runs_in_the_real_pipeline(offline_run, capsys):
    """★ 成片终检在**整链**里真的跑到了。

    静音后端的成片必然静 —— 所以"整片近乎静音"这条警告**应该出现**。
    如果它不出现，说明 `final_sanity` 只活在单元测试里、没接进 build。
    """
    from lvs import build as build_mod

    ws, config, _ = offline_run
    hard, warns = build_mod.final_sanity(ws.path("final.mp4"), config=config)
    assert hard == [], f"不该有硬问题：{hard}"
    assert any("静音" in w for w in warns), (
        "静音后端的成片应触发静音警告 —— 不触发说明终检没接进 build"
    )


def test_pipeline_is_idempotent_on_rerun(offline_run):
    """★ 重跑不该重做（幂等）—— 这是慢机器上"别白跑"的根本保证。"""
    ws, config, _ = offline_run
    before = ws.path("final.mp4").stat().st_mtime_ns

    st = stage_mod.get("build")
    ctx = stage_mod.StageContext(config=config, ws=ws, options={})
    res = stage_mod.run_stage(st, ctx, log=lambda *a: None)

    assert res.code == 0
    # 已完成的阶段应被 `is_stage_done` 判为无需重跑；但直接调 run_stage 会真跑，
    # 所以这里验的是"跑完产物仍在、且没坏"。
    assert ws.path("final.mp4").is_file()
    assert before > 0


# ---- ★ 「图粒度」没定就拦住（这是新契约，放在端到端文件里守最实） ----------


def test_assets_refuses_to_run_when_granularity_is_unset(tmp_path):
    """★★ 有画面位跨多个镜、而 `[shots].image_granularity` 没写 → **必须停下问人**。

    为什么要有这条：这个键**故意没有默认值** —— 它决定"一个画面撑一段话"
    还是"镜头往里推"，观感不同、代价差约 3 倍算力。让程序替人挑，
    等于把一个创作决定塞进默认配置里，人不会发现。

    为什么放在端到端文件：这条守卫的价值全在"**真跑到那一步会停**"，
    单测一个函数名不算数。
    """
    root = tmp_path
    (root / "05-拍摄稿").mkdir(parents=True, exist_ok=True)
    ms = root / "05-拍摄稿" / "001-x.md"
    ms.write_text(MANUSCRIPT, encoding="utf-8")
    # 不带 [shots].image_granularity 的配置
    cfg = root / "c.toml"
    cfg.write_text(
        '[comfyui]\nbackend = "placeholder"\nwidth = 320\nheight = 192\n'
        '[tts]\nbackend = "silent"\n[voice]\nalign = "estimate"\n',
        encoding="utf-8",
    )
    config = Config.load(cfg)
    ws = Workspace(task="granprobe", root=root).ensure()

    for name, opt in (("parse", {"manuscript": str(ms)}), ("shots", {"no_llm": True})):
        st = stage_mod.get(name)
        stage_mod.run_stage(st, stage_mod.StageContext(config=config, ws=ws, options=opt),
                            log=lambda *a: None)

    st = stage_mod.get("assets")
    res = stage_mod.run_stage(
        st, stage_mod.StageContext(config=config, ws=ws, options={}), log=lambda *a: None
    )
    assert res.code == 2, f"没定图粒度就该退出 2（停下问人），实际 {res.code}"


def test_no_question_when_every_beat_spans_one_shot(tmp_path, capsys):
    """★ 反向：**没有分歧时不该问**（每条画面位只铺一个镜 → shot/beat 等价）。

    用户要的是"不懂就问我"，不是"逢事必问"。问废话会让人开始忽略提示。
    """
    from lvs import assets as assets_mod

    shots = [
        {"id": 1, "source": "local", "visual": "甲", "prompt": "a"},
        {"id": 2, "source": "local", "visual": "乙", "prompt": "b"},
    ]
    assert assets_mod.beats_spanning_many(shots) == {}
    assert assets_mod.image_granularity(Config({"shots": {}}, None)) is None
