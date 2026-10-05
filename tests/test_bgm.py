"""BGM 测试：① `lvs/build.py` 的老混音路径（bgm_source / _bgm_audio_filter / finalize）；② `lvs/bgm.py`（P5：生成 + 压低闪避混音）。

三条设计合同：
1. **没配不是错，配了但文件不在必须说出来**（不许静默当成"没配"）；
2. **BGM 出任何问题都不许毁掉成片** —— 自动退回无 BGM 版本（参考 NarratoAI）；
3. **amix 必须 `normalize=0`** —— ffmpeg ≥4.4 默认把各输入各除以输入数，
   会把旁白直接砍半（"加了 BGM 旁白变小声"的典型事故）。

前三组用桩（快）；最后一组真跑 ffmpeg，端到端验证"加了 BGM 音量确实上去、
时长不漂移"——那是 normalize=0 漏掉时最先暴露的地方。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from lvs import bgm
from lvs import build
from lvs import llm
from lvs.config import Config
from lvs.ffmpeg import FFmpegError, duration as ff_duration, mean_volume_db, tools
from lvs.workspace import Workspace


# ---- bgm_source 三态 ---------------------------------------------------------


def test_not_configured_is_not_an_error():
    """BGM 是可选项：没配 = (None, 空原因)，不报警。"""
    path, why = build.bgm_source(Config({}, None))
    assert path is None and why == ""


def test_configured_but_missing_file_reports_reason(tmp_path):
    """★ 配了但文件不在是配置错误 —— 原因必须说出来，不许静默。"""
    cfg = Config({"bgm": {"file": str(tmp_path / "nope.mp3")}}, None)
    path, why = build.bgm_source(cfg)
    assert path is None
    assert why and "不存在" in why and "[bgm].file" in why


def test_existing_file_is_returned(tmp_path):
    bgm = tmp_path / "bgm.mp3"
    bgm.write_bytes(b"x")
    path, why = build.bgm_source(Config({"bgm": {"file": str(bgm)}}, None))
    assert path == bgm and why == ""


def test_blank_string_treated_as_not_configured():
    path, why = build.bgm_source(Config({"bgm": {"file": "   "}}, None))
    assert path is None and why == ""


# ---- _bgm_audio_filter（滤镜串）---------------------------------------------


def _cfg(**bgm) -> Config:
    return Config({"bgm": bgm}, None)


def test_default_volume_and_fades():
    f = build._bgm_audio_filter(0.0, 100.0, _cfg())
    assert "volume=0.250" in f
    assert "afade=t=in:d=2.00" in f
    assert "afade=t=out:st=97.00:d=3.00" in f   # total - fade_out


def test_fade_out_start_is_total_minus_fade_out():
    f = build._bgm_audio_filter(0.0, 60.0, _cfg(fade_out=5.0))
    assert "st=55.00:d=5.00" in f


def test_no_fade_out_when_total_shorter_than_fade():
    """总时长比淡出还短时，st 会算成负数（非法）→ 干脆不加淡出。"""
    f = build._bgm_audio_filter(0.0, 2.0, _cfg(fade_out=3.0))
    assert "afade=t=out" not in f
    assert "afade=t=in" in f


def test_zero_fades_disabled():
    f = build._bgm_audio_filter(0.0, 100.0, _cfg(fade_in=0.0, fade_out=0.0))
    assert "afade" not in f and "volume=" in f


def test_volume_clamped_to_one():
    f = build._bgm_audio_filter(0.0, 100.0, _cfg(volume=5.0))
    assert "volume=1.000" in f


def test_bad_values_fall_back_to_defaults():
    f = build._bgm_audio_filter(0.0, 100.0, _cfg(volume="loud", fade_in="x"))
    assert "volume=0.250" in f and "afade=t=in:d=2.00" in f


# ---- finalize 的退回外壳 ------------------------------------------------------


def _run_finalize(monkeypatch, tmp_path, cfg: Config):
    """把 `_finalize` 换成桩，返回 (调用记录, finalize 返回值)。"""
    ws = Workspace(task="t", root=tmp_path).ensure()
    calls: list[Path | None] = []

    def fake(final_ws, video_track, narration, srt, config, *, bgm=None):
        calls.append(bgm)
        if bgm is not None:
            raise FFmpegError("fake: corrupt bgm")
        return ws.path("final.mp4"), False

    monkeypatch.setattr(build, "_finalize", fake)
    result = build.finalize(ws, Path("v.mp4"), Path("n.mp3"), Path("s.srt"), cfg)
    return calls, result


def test_bgm_failure_falls_back_to_no_bgm(monkeypatch, tmp_path):
    """★ 配乐坏了不许毁掉成片：第一次带 BGM 炸 → 第二次无 BGM 成功返回。"""
    bgm = tmp_path / "bgm.mp3"
    bgm.write_bytes(b"x")
    cfg = Config({"bgm": {"file": str(bgm)}}, None)
    calls, result = _run_finalize(monkeypatch, tmp_path, cfg)
    assert calls == [bgm, None], "应先试带 BGM，失败后退回无 BGM"
    assert result[0].name == "final.mp4"


def test_no_bgm_config_calls_finalize_once(monkeypatch, tmp_path):
    calls, _ = _run_finalize(monkeypatch, tmp_path, Config({}, None))
    assert calls == [None]


def test_missing_bgm_file_skips_retry(monkeypatch, tmp_path):
    """配了但文件不在：说明原因后直接走无 BGM，不该先失败一次。"""
    cfg = Config({"bgm": {"file": str(tmp_path / "gone.mp3")}}, None)
    calls, _ = _run_finalize(monkeypatch, tmp_path, cfg)
    assert calls == [None]


# ---- _finalize 的命令构造（normalize=0 是硬合同）-----------------------------


def _capture_finalize(monkeypatch, tmp_path, cfg: Config, *, bgm: Path | None):
    ws = Workspace(task="t", root=tmp_path).ensure()
    srt = ws.path("subtitle.srt")
    srt.write_text("", encoding="utf-8")   # 空 srt → 不烧字幕，走纯混音路径
    captured: list[list[str]] = []
    monkeypatch.setattr(build, "tools", lambda: ("ffmpeg", ""))
    monkeypatch.setattr(build, "ff_duration", lambda p: 100.0)
    def fake_run(args, cwd=None):      # noqa: ANN001, ANN202
        # ★ 桩也要「成功 = 产物真的存在」：早先只记 args、不落文件，
        #   于是掩盖了「ffmpeg 返回 0 却没产物」这条真故障（本轮 #2 原子化时被它绊到）。
        captured.append(list(args))
        assert str(args[-1]).endswith("final.mp4.part"), "必须先写 .part 再归位"
        Path(args[-1]).write_bytes(b"stub")

    monkeypatch.setattr(build, "ff_run", fake_run)
    build._finalize(ws, Path("v.mp4"), Path("n.mp3"), srt, cfg, bgm=bgm)
    assert captured, "ff_run 没被调用"
    return captured[0]


def test_bgm_command_loops_and_never_normalizes(monkeypatch, tmp_path):
    """★ amix 缺 normalize=0 → 旁白被砍半；-stream_loop -1 才能铺满短 BGM。"""
    bgm = tmp_path / "bgm.mp3"
    bgm.write_bytes(b"x")
    args = _capture_finalize(monkeypatch, tmp_path, _cfg(), bgm=bgm)
    filters = args[args.index("-filter_complex") + 1]
    assert "normalize=0" in filters, "amix 不写 normalize=0，旁白会被砍半"
    assert "amix=inputs=2" in filters and "alimiter" in filters
    assert "-stream_loop" in args, "BGM 短于成片时不循环就中途没声"


def test_no_bgm_command_unchanged(monkeypatch, tmp_path):
    """没配 BGM 时命令与从前完全一致：两个输入、无 filter_complex。"""
    args = _capture_finalize(monkeypatch, tmp_path, _cfg(), bgm=None)
    assert "-filter_complex" not in args
    assert args.count("-i") == 2
    assert "amix" not in " ".join(args)


def test_burn_subtitles_path_also_carries_bgm(monkeypatch, tmp_path):
    """烧字幕分支同样要接 BGM（两条路径当年是复制粘贴的，最容易漏一条）。"""
    ws = Workspace(task="t", root=tmp_path).ensure()
    srt = ws.path("subtitle.srt")
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
    bgm = tmp_path / "bgm.mp3"
    bgm.write_bytes(b"x")
    captured: list[list[str]] = []
    monkeypatch.setattr(build, "tools", lambda: ("ffmpeg", ""))
    monkeypatch.setattr(build, "ff_duration", lambda p: 100.0)
    def fake_run(args, cwd=None):      # noqa: ANN001, ANN202
        # ★ 桩也要「成功 = 产物真的存在」：早先只记 args、不落文件，
        #   于是掩盖了「ffmpeg 返回 0 却没产物」这条真故障（本轮 #2 原子化时被它绊到）。
        captured.append(list(args))
        assert str(args[-1]).endswith("final.mp4.part"), "必须先写 .part 再归位"
        Path(args[-1]).write_bytes(b"stub")

    monkeypatch.setattr(build, "ff_run", fake_run)
    build._finalize(ws, Path("v.mp4"), Path("n.mp3"), srt, _cfg(), bgm=bgm)
    filters = captured[0][captured[0].index("-filter_complex") + 1]
    assert "subtitles=" in filters and "amix=inputs=2" in filters
    assert "normalize=0" in filters


# ---- 真跑 ffmpeg 的端到端 ------------------------------------------------------


def _have_ffmpeg() -> bool:
    try:
        tools()
        return True
    except FFmpegError:
        return False


@pytest.mark.slow   # ★ 慢组：TestRealFfmpegBgm
class TestRealFfmpegBgm:
    """端到端：真的混一段音，验证 BGM 把音量抬上去、时长不漂移。

    这是 `normalize=0` 漏掉时最先暴露的地方 —— 漏了它，旁白砍半，
    "加了 BGM 的成片"反而比不加更轻。
    """

    @pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg 不可用")
    def test_bgm_raises_volume_without_changing_duration(self, tmp_path):
        ff, _ = tools()
        video = tmp_path / "v.mp4"
        narr = tmp_path / "n.m4a"
        bgm = tmp_path / "bgm.mp3"
        # 3s 静音画面
        subprocess.run(
            [ff, "-y", "-f", "lavfi", "-i", "color=c=black:s=320x180:r=25",
             "-t", "3", "-c:v", "libx264", "-preset", "ultrafast", str(video)],
            check=True, capture_output=True,
        )
        # 3s 旁白：0.3 幅度的 440Hz（约 -10.5 dBFS）
        subprocess.run(
            [ff, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
             "-af", "volume=0.3", str(narr)],
            check=True, capture_output=True,
        )
        # 1s BGM（会被循环铺满 3s）：满幅度 220Hz
        subprocess.run(
            [ff, "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=1",
             "-af", "volume=1.0", str(bgm)],
            check=True, capture_output=True,
        )

        ws = Workspace(task="bgm", root=tmp_path).ensure()
        srt = ws.path("subtitle.srt")
        srt.write_text("", encoding="utf-8")

        base_db = mean_volume_db(narr)
        out, _ = build._finalize(
            ws, video, narr, srt, _cfg(volume=0.6, fade_in=0.0, fade_out=0.0), bgm=bgm
        )
        mix_db = mean_volume_db(out)
        got_dur = ff_duration(out)

        assert base_db is not None and mix_db is not None
        # BGM 满幅度 ×0.6 叠在 0.3 幅度旁白上，平均功率必然明显上升；
        # 若 amix 把旁白砍半（normalize=0 漏掉），这里反而会下降。
        assert mix_db > base_db + 3.0, (
            f"加 BGM 后音量没上去（{base_db:.1f} → {mix_db:.1f} dB）——"
            "怀疑 amix normalize=0 丢失导致旁白被砍半"
        )
        assert got_dur is not None and abs(got_dur - 3.0) < 0.35, (
            f"成片时长 {got_dur}s 与旁白 3s 不符（BGM 输入不该拉长/缩短成片）"
        )

    @pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg 不可用")
    def test_corrupt_bgm_file_falls_back_end_to_end(self, tmp_path):
        """真实的坏 BGM（非音频文件）→ finalize 退回无 BGM 版本，成片照出。"""
        ff, _ = tools()
        video = tmp_path / "v.mp4"
        narr = tmp_path / "n.m4a"
        subprocess.run(
            [ff, "-y", "-f", "lavfi", "-i", "color=c=black:s=320x180:r=25",
             "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", str(video)],
            check=True, capture_output=True,
        )
        subprocess.run(
            [ff, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(narr)],
            check=True, capture_output=True,
        )
        bad = tmp_path / "bad.mp3"
        bad.write_bytes(b"this is not audio data" * 10)

        ws = Workspace(task="bgmfail", root=tmp_path).ensure()
        ws.path("subtitle.srt").write_text("", encoding="utf-8")
        cfg = Config({"bgm": {"file": str(bad)}}, None)

        out, _ = build.finalize(ws, video, narr, ws.path("subtitle.srt"), cfg)
        assert out.is_file() and (out.stat().st_size > 0)
        assert ff_duration(out) is not None and ff_duration(out) > 1.5
        assert mean_volume_db(out) is not None, "退回的无 BGM 版本必须有音轨"


# ============================================================================
# P5：`lvs bgm` —— 本地生成可商用 BGM（自适应文案风格）+ 压低闪避混音
# 全部离线：不联网、不加载模型、不跑 ffmpeg（唯一真跑 ffmpeg 的在最后 slow 组）。
# ============================================================================


HISTORY_TEXT = "王朝更替的岁月里，武士与将军在战乱中守着朝廷的旧事，千百年过去。"
HEALING_TEXT = "母亲在温暖的家乡陪着我，童年的温柔像治愈的光，那是幸福的味道。"
SUSPENSE_TEXT = "凶手把尸体藏在密室，阴谋与失踪的惨案，血迹诡异得让人不安。"
HEROIC_TEXT = "他热血冲锋，为复仇决战到底，不屈的英雄逆袭成传说。"
DAILY_TEXT = "市集上很热闹，酒馆里闲聊俏皮，轻松幽默又有趣的日常生活。"
SAD_TEXT = "离别时她满眼眼泪，孤独与思念让人叹息，只剩凄凉与遗憾。"
JA_SAD_TEXT = "她的怨与恨压了几十年，泪都干了，只剩苦。"
JA_MYSTERY_TEXT = "妖怪与怨灵在异界的梦里现身，带着一股咒。"
JA_SOLEMN_TEXT = "将军与武士的旧事，幕府和寺院走过千百年。"



# ---- 规则兜底：文案 → 风格/情绪/BPM -----------------------------------------


def test_history_text_maps_to_classical_strings():
    m = bgm.match_style(HISTORY_TEXT)
    assert m.rule.name == "solemn_history"
    assert "classical" in m.rule.genre
    assert "strings" in m.rule.instruments
    assert m.rule.bpm == 72
    assert m.keywords, "命中的关键词必须留下来（bgm.json 要可追溯）"


def test_healing_text_maps_to_piano():
    m = bgm.match_style(HEALING_TEXT)
    assert m.rule.name == "healing"
    assert "piano" in m.rule.instruments
    assert m.rule.bpm == 68


def test_suspense_text_maps_to_dark_electronic():
    m = bgm.match_style(SUSPENSE_TEXT)
    assert m.rule.name == "suspense"
    assert "electronic" in m.rule.genre
    assert "tense" in m.rule.mood


def test_heroic_text_maps_to_orchestral():
    m = bgm.match_style(HEROIC_TEXT)
    assert m.rule.name == "heroic"
    assert "orchestral" in m.rule.genre
    assert m.rule.bpm == 110


def test_daily_and_sad_texts_pick_their_own_rules():
    assert bgm.match_style(DAILY_TEXT).rule.name == "light_daily"
    assert bgm.match_style(SAD_TEXT).rule.name == "melancholy"


def test_repeated_keywords_beat_single_mention():
    """★ 计次数：提一次"家"不该把满篇"尸体/凶"的惊悚片定成治愈。"""
    text = "家里很安静。" + "尸体" * 3 + "凶手又来了一次。"
    assert bgm.match_style(text).rule.name == "suspense"


# ---- 配乐池：整本书钉在「日本古风」（`[bgm].style_pool`） ----------------------


def test_pool_rules_are_only_that_pool():
    names = [r.name for r in bgm.pool_rules("japanese_ancient")]
    assert names == [
        "ja_ancient_melancholy", "ja_ancient_mystery",
        "ja_ancient_solemn", "ja_ancient_calm",
    ]
    assert all(r.pool == "japanese_ancient" for r in bgm.pool_rules("japanese_ancient"))


def test_empty_pool_keeps_the_generic_rules():
    """★ 防回归：没写 `style_pool` 的书必须和以前**一模一样**（前 5 期口径）。"""
    assert bgm.pool_rules("") == tuple(r for r in bgm.STYLE_RULES if not r.pool)
    assert bgm.match_style(HISTORY_TEXT).rule.name == "solemn_history"


def test_pool_picks_variant_by_keywords():
    assert bgm.match_style(JA_SAD_TEXT, "japanese_ancient").rule.name == "ja_ancient_melancholy"
    assert bgm.match_style(JA_MYSTERY_TEXT, "japanese_ancient").rule.name == "ja_ancient_mystery"
    assert bgm.match_style(JA_SOLEMN_TEXT, "japanese_ancient").rule.name == "ja_ancient_solemn"


def test_pool_falls_back_inside_the_pool_not_to_neutral():
    """★ 池内一条都没命中时用**池内底**，不许掉回通用池的中性钢琴。"""
    m = bgm.match_style("今天下午三点，我们出发去往下一个地点。", "japanese_ancient")
    assert m.rule.name == "ja_ancient_calm"
    assert m.keywords == () and not m.matched
    assert "koto" in m.rule.instruments


def test_pool_never_leaks_western_rules():
    for text in (HISTORY_TEXT, HEALING_TEXT, SUSPENSE_TEXT, HEROIC_TEXT, DAILY_TEXT, SAD_TEXT):
        rule = bgm.match_style(text, "japanese_ancient").rule
        assert rule.pool == "japanese_ancient", rule.name


def test_unknown_pool_is_a_usage_error():
    """池名写错要当场报错，不许静默退回通用池（那会让"本书用和风"无声失效）。"""
    with pytest.raises(bgm.BgmUsageError):
        bgm.match_style(HISTORY_TEXT, "no_such_pool")


def test_style_pool_is_read_from_config():
    plan = bgm.build_prompt(SAD_TEXT, _cfg(style_pool="japanese_ancient"), use_llm=False)
    assert plan.pool == "japanese_ancient"
    assert plan.style["name"] == "ja_ancient_melancholy"
    assert plan.style["pool"] == "japanese_ancient"
    assert plan.as_dict()["pool"] == "japanese_ancient"
    assert "[japanese_ancient]" in plan.describe()


def test_no_keyword_falls_back_to_neutral_soft():
    m = bgm.match_style("今天下午三点，我们出发去往下一个地点。")
    assert m.rule.name == "neutral_soft"
    assert m.keywords == ()
    assert not m.matched


def test_different_texts_give_different_prompts():
    """★ 自适应（要求①）：不同文案必须产出**不同**的提示词。"""
    history = bgm.style_prompt(bgm.match_style(HISTORY_TEXT).rule)
    healing = bgm.style_prompt(bgm.match_style(HEALING_TEXT).rule)
    assert history != healing
    assert "classical" in history and "warm acoustic" in healing


def test_prompt_asks_for_instrumental_no_vocals_and_loop():
    prompt = bgm.style_prompt(bgm.match_style(HISTORY_TEXT).rule)
    assert "no vocals" in prompt
    assert "loop" in prompt
    assert "72 bpm" in prompt


# ---- build_prompt：LLM 路径 + 离线兜底 --------------------------------------


class _Resp:
    """OpenAI 兼容响应替身（只实现 chat 用到的 status_code / text / json）。"""

    def __init__(self, content: str) -> None:
        self.status_code = 200
        self.text = content
        self._payload = {"choices": [{"message": {"content": content}}]}

    def json(self):
        return self._payload


def _transport(items: list):
    """假传输层：依次吐出 `_Resp` 或异常（不联网）。"""
    calls: list[dict] = []

    def post(url, **kw):  # noqa: ANN001, ANN202
        calls.append({"url": url, **kw})
        item = items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    post.calls = calls  # type: ignore[attr-defined]
    return post


@pytest.fixture
def _clean_transport():
    llm.set_transport(None)
    yield
    llm.set_transport(None)


def _llm_cfg() -> Config:
    return Config(
        {"app": {"openai_api_key": "test-key", "openai_model_name": "test-model"}},
        None,
    )


def test_build_prompt_without_key_uses_rules():
    plan = bgm.build_prompt(HISTORY_TEXT, _cfg(), duration=60.0)
    assert plan.source == "rules"
    assert plan.warnings == []
    assert plan.style["name"] == "solemn_history"
    assert plan.prompt == bgm.style_prompt(bgm.match_style(HISTORY_TEXT).rule)


def test_build_prompt_use_llm_false_skips_llm_even_with_key(_clean_transport):
    llm.set_transport(_transport([_Resp("should not be called")]))
    plan = bgm.build_prompt(HISTORY_TEXT, _llm_cfg(), use_llm=False)
    assert plan.source == "rules"


def test_build_prompt_uses_llm_when_key_present(_clean_transport):
    """★ LLM 路径：讲稿主题/情绪由 LLM 翻成提示词（离线假传输层）。"""
    payload = {
        "genre": "noir jazz", "instruments": "double bass, brushed drums",
        "mood": "sultry", "bpm": 88,
        "prompt": "slow noir jazz, double bass, brushed drums, no vocals",
    }
    llm.set_transport(_transport([_Resp(json.dumps(payload))]))
    plan = bgm.build_prompt(HISTORY_TEXT, _llm_cfg(), duration=90.0)
    assert plan.source == "llm"
    assert plan.style["genre"] == "noir jazz"
    assert plan.style["bpm"] == 88
    assert plan.prompt == payload["prompt"]
    assert plan.warnings == []


def test_llm_partial_json_is_filled_from_rules(_clean_transport):
    """LLM 只回一半字段也别丢：缺的用规则兜底补上。"""
    llm.set_transport(_transport([_Resp(json.dumps({"genre": "chiptune"}))]))
    plan = bgm.build_prompt(HISTORY_TEXT, _llm_cfg())
    assert plan.source == "llm"
    assert plan.style["genre"] == "chiptune"
    assert plan.style["bpm"] == 72                      # 规则兜底（历史=72）
    assert "strings" in plan.style["instruments"]


def test_llm_failure_falls_back_to_rules_with_warning(_clean_transport):
    """★ LLM 401/超时/坏 JSON 都不许让整片没有 BGM。"""
    llm.set_transport(_transport([RuntimeError("boom")]))
    plan = bgm.build_prompt(SUSPENSE_TEXT, _llm_cfg())
    assert plan.source == "rules"
    assert plan.style["name"] == "suspense"
    assert plan.warnings and "退回" in plan.warnings[0]


def test_llm_bad_json_falls_back_to_rules(_clean_transport):
    llm.set_transport(_transport([_Resp("这不是 JSON")]))
    plan = bgm.build_prompt(SUSPENSE_TEXT, _llm_cfg())
    assert plan.source == "rules"
    assert plan.warnings


def test_llm_bpm_is_clamped(_clean_transport):
    payload = {"bpm": 900, "genre": "x", "instruments": "y", "mood": "z", "prompt": "p"}
    llm.set_transport(_transport([_Resp(json.dumps(payload))]))
    plan = bgm.build_prompt(HISTORY_TEXT, _llm_cfg())
    assert plan.style["bpm"] == 180

# ---- 文案读取 / 时长 --------------------------------------------------------


def _ws_with_shots(tmp_path, text="王朝与武士的历史。", title="测试稿"):
    ws = Workspace(task="bgmt", root=tmp_path).ensure()
    ws.write_shots({
        "title": title,
        "shots": [{"id": 1, "narration": text, "visual": "夜色",
                   "segment_heading": "第一节"}],
    })
    return ws


def test_collect_text_reads_title_headings_narration(tmp_path):
    ws = _ws_with_shots(tmp_path, "王朝与武士的历史。")
    text, title = bgm.collect_text(ws)
    assert title == "测试稿"
    assert "王朝与武士的历史" in text
    assert "第一节" in text and "夜色" in text


def test_collect_text_respects_limit(tmp_path):
    ws = _ws_with_shots(tmp_path, "历史" * 500)
    text, _ = bgm.collect_text(ws, limit=40)
    assert len(text) == 40


def test_collect_text_without_shots_is_usage_error(tmp_path):
    ws = Workspace(task="empty", root=tmp_path).ensure()
    with pytest.raises(bgm.BgmUsageError) as excinfo:
        bgm.collect_text(ws)
    assert "lvs shots" in str(excinfo.value)


def test_resolve_duration_config_then_clamp(tmp_path):
    ws = Workspace(task="d", root=tmp_path).ensure()
    assert bgm.resolve_duration(ws, _cfg(duration=30)) == 30.0
    assert bgm.resolve_duration(ws, _cfg(duration=600)) == bgm.MAX_DURATION_S
    assert bgm.resolve_duration(ws, _cfg(duration=3)) == bgm.MIN_DURATION_S
    assert bgm.resolve_duration(ws, _cfg(), 45.0) == 45.0          # CLI 优先
    assert bgm.resolve_duration(ws, _cfg()) == bgm.DEFAULT_DURATION_S


# ---- 元信息 / wav -----------------------------------------------------------


def test_write_meta_structure_and_license(tmp_path):
    """★ bgm.json 必须带许可说明（可商用是这条流水线的硬前提）。"""
    path = tmp_path / "bgm.json"
    bgm.write_meta(path, {"task": "t", "prompt": "p", "license": dict(bgm.LICENSE_INFO)})
    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert "\\u" not in raw, "中文不许被转义（与 shots.json 同约定）"
    meta = json.loads(raw)
    assert meta["license"]["name"] == "Stability AI Community License"
    assert "100 万" in meta["license"]["commercial_use"]
    assert "归使用者" in meta["license"]["output_ownership"]
    assert "Gemma" in meta["license"]["bundled_terms"]
    # 商用需注册 / 分发才署名（照 LICENSE.md 原文，别把"输出归你"说成"任何情况都无需署名"）
    assert "注册" in meta["license"]["commercial_use"]
    assert "Powered by Stability AI" in meta["license"]["distribution_attribution"]
    assert "LICENSE.md" in meta["license"]["authoritative"]


def test_write_wav_is_16bit_pcm(tmp_path):
    path = tmp_path / "a.wav"
    samples = [0.0, 0.5, -0.5, 0.25] * 100
    bgm.write_wav(path, samples, 8000)
    with wave.open(str(path), "rb") as handle:
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 8000
        assert handle.getnchannels() == 1
        assert handle.getnframes() == len(samples)


def test_write_wav_normalizes_overdrive(tmp_path):
    """>1.0 的样本按峰值归一，别削顶成方波。"""
    path = tmp_path / "loud.wav"
    bgm.write_wav(path, [0.0, 2.0, -2.0], 8000)
    with wave.open(str(path), "rb") as handle:
        frames = handle.readframes(3)
    import struct

    values = struct.unpack("<3h", frames)
    assert max(values) == 32767 and min(values) == -32767


def test_write_wav_leaves_no_part_file(tmp_path):
    bgm.write_wav(tmp_path / "b.wav", [0.1] * 10, 8000)
    assert not list(tmp_path.glob("*.part"))


# ---- 权重状态 / 降级报错 ----------------------------------------------------


def test_weights_state_missing_dir(tmp_path):
    ok, why = bgm.weights_state(Config({"bgm": {"model_dir": str(tmp_path / "nope")}}, None))
    assert not ok and "不存在" in why


def test_weights_state_missing_files(tmp_path):
    ok, why = bgm.weights_state(Config({"bgm": {"model_dir": str(tmp_path)}}, None))
    assert not ok and "model_config.json" in why


def test_weights_state_ready(tmp_path):
    (tmp_path / "model_config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "model.safetensors").write_bytes(b"x")
    ok, why = bgm.weights_state(Config({"bgm": {"model_dir": str(tmp_path)}}, None))
    assert ok and why == ""


def test_model_dir_default_and_relative(tmp_path):
    default = bgm.model_dir(Config({}, None))
    assert default.name == bgm.MODEL_DIRNAME and default.parent.name == "models"
    relative = bgm.model_dir(Config({"bgm": {"model_dir": "models/x"}}, None))
    assert relative.name == "x"


def test_generate_without_weights_explains_next_step(tmp_path):
    """★ 降级：权重没下载时必须说清"下一步敲什么"，而不是 traceback。"""
    ws = _ws_with_shots(tmp_path)
    cfg = Config({"bgm": {"model_dir": str(tmp_path / "none")}}, None)
    with pytest.raises(bgm.BgmError) as excinfo:
        bgm.generate(ws, cfg)
    message = str(excinfo.value)
    assert "权重未下载" in message
    assert "lvs bgm download" in message
    assert excinfo.value.exit_code == 1


def test_run_command_generate_reports_failure_code(tmp_path, capsys):
    ws = _ws_with_shots(tmp_path)
    cfg = Config({"bgm": {"model_dir": str(tmp_path / "none")}}, None)

    class _Args:
        action = None
        duration = 30.0
        seed = None
        steps = None
        cfg_scale = None
        prompt = None
        no_llm = True
        force = False
        json = False

    assert bgm.run_command(cfg, ws, _Args()) == 1
    assert "权重未下载" in capsys.readouterr().out


def _bed(ws: Workspace, data: bytes = b"bed") -> Path:
    """往 `bgm/bgm.wav` 写测试用的假 BGM（`bgm/` 不在 Workspace 骨架里，先建目录）。"""
    path = ws.path("bgm", "bgm.wav")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_generate_writes_wav_and_meta_with_fake_model(tmp_path, monkeypatch, capsys):
    """★ 生成主路径（**不真调模型**）：CPU + seed 可复现 + bgm.json 全字段 + 幂等复用。"""
    import numpy as np

    ws = _ws_with_shots(tmp_path, HISTORY_TEXT)
    seen: dict = {}

    def fake_load(config, *, device="cpu"):  # noqa: ANN001, ANN202
        seen["load_device"] = device
        return object(), {"sample_rate": 8000}

    def fake_generate(model, model_config, **kwargs):  # noqa: ANN001, ANN202
        seen.update(kwargs)
        frames = int(float(kwargs["duration"]) * 8000)
        return np.zeros((1, 2, frames), dtype="float32"), 8000

    monkeypatch.setattr(bgm, "load_model", fake_load)
    monkeypatch.setattr(bgm, "generate_audio", fake_generate)

    meta = bgm.generate(ws, _cfg(), duration=20, use_llm=False)

    assert seen["load_device"] == "cpu" == seen["device"], "推理必须走 CPU（显存让给 ComfyUI/TTS）"
    assert seen["seed"] == bgm.DEFAULT_SEED and seen["steps"] == bgm.DEFAULT_STEPS
    assert seen["sampler_type"] == bgm.DEFAULT_SAMPLER
    assert "sigma_min" not in seen and "sigma_max" not in seen, "SA3 走 flow，别塞 v-diffusion 的 sigma"
    assert (ws.path("bgm", "bgm.wav")).is_file()
    assert "记账失败" not in capsys.readouterr().out, "bgm 阶段该记进 manifest"

    on_disk = json.loads(ws.path("bgm", "bgm.json").read_text(encoding="utf-8"))
    assert on_disk["prompt_source"] == "rules" and on_disk["reused"] is False
    assert on_disk["duration_s"] == 20.0 and on_disk["sample_rate"] == 8000
    assert on_disk["license"]["name"] == bgm.LICENSE_INFO["name"]
    assert "no vocals" in meta["prompt"]
    assert on_disk["style_pool"] == "", "没配池 → 通用池，报告里必须是空串"

    again = bgm.generate(ws, _cfg(), duration=20, use_llm=False)
    assert again["reused"] is True, "盘上已有产物就别再烧一遍 CPU"


def test_sa3_defaults_follow_the_model_card():
    """★ SA3 是 rectified flow：默认 steps=8 / cfg=1.0 / pingpong（模型卡原值）。
    用 SA2 那套（100 步 / cfg 6.0 / dpmpp-3m-sde）会走错采样分支且 CPU 上慢十倍。"""
    assert (bgm.DEFAULT_STEPS, bgm.DEFAULT_CFG_SCALE, bgm.DEFAULT_SAMPLER) == (8, 1.0, "pingpong")


def _fake_sat(monkeypatch, names: tuple[str, ...]):
    """把 stable-audio-tools 换成只有这几个推理函数的假模块（**不 import 真 torch**）。"""
    import sys
    import types

    root = types.ModuleType("stable_audio_tools")
    root.__path__ = []  # type: ignore[attr-defined]
    inference = types.ModuleType("stable_audio_tools.inference")
    inference.__path__ = []  # type: ignore[attr-defined]
    generation = types.ModuleType("stable_audio_tools.inference.generation")
    for name in names:
        setattr(generation, name, lambda *a, **k: None)
        getattr(generation, name).__name__ = name
    inference.generation = generation  # type: ignore[attr-defined]
    root.inference = inference  # type: ignore[attr-defined]
    for module in (root, inference, generation):
        monkeypatch.setitem(sys.modules, module.__name__, module)


def test_generators_prefers_the_inpaint_entry(monkeypatch):
    """模型卡用的是 `generate_diffusion_cond_inpaint`；老版本 SAT 只有 `generate_diffusion_cond`。"""
    _fake_sat(monkeypatch, ("generate_diffusion_cond", "generate_diffusion_cond_inpaint"))
    assert [fn.__name__ for fn in bgm._generators()] == [
        "generate_diffusion_cond_inpaint", "generate_diffusion_cond",
    ]

    _fake_sat(monkeypatch, ("generate_diffusion_cond",))
    assert [fn.__name__ for fn in bgm._generators()] == ["generate_diffusion_cond"]


def test_generate_reads_steps_and_sampler_from_config(tmp_path, monkeypatch):
    """`[bgm].steps / cfg_scale / sampler` 也能改（CLI 优先）。"""
    import numpy as np

    ws = _ws_with_shots(tmp_path, HISTORY_TEXT)
    seen: dict = {}
    monkeypatch.setattr(bgm, "load_model", lambda config, *, device="cpu": (object(), {"sample_rate": 8000}))
    monkeypatch.setattr(
        bgm, "generate_audio",
        lambda model, mc, **kw: (seen.update(kw) or np.zeros((1, 2, 8), dtype="float32"), 8000),
    )

    bgm.generate(ws, _cfg(steps=16, cfg_scale=2.5, sampler="euler"), duration=20, use_llm=False)
    assert seen["steps"] == 16 and seen["cfg_scale"] == 2.5 and seen["sampler_type"] == "euler"


# ---- 混音参数（要求②：压低 + 闪避） ----------------------------------------


def test_mix_defaults_are_conservative():
    params = bgm.mix_params(_cfg())
    assert params.volume_db == -16.0
    assert params.duck is True
    assert params.duck_threshold == 0.03
    assert params.duck_ratio == 8.0


def test_mix_params_from_config_and_cli_overrides():
    cfg = _cfg(volume_db=-22.5, duck_ratio=4, duck=False)
    params = bgm.mix_params(cfg)
    assert params.volume_db == -22.5 and params.duck_ratio == 4.0 and params.duck is False
    params = bgm.mix_params(cfg, volume_db=-12.0, duck=False)
    assert params.volume_db == -12.0
    params = bgm.mix_params(_cfg(), volume_db=None)
    assert params.volume_db == -16.0, "None = 没传，不许覆盖配置默认"


def test_mix_params_bad_value_falls_back():
    assert bgm.mix_params(_cfg(volume_db="loud")).volume_db == -16.0


def test_mix_filter_ducks_bgm_with_voice_as_key():
    """★ 要求②：BGM 压低 + 以旁白([0:a])为 key 的 sidechaincompress + normalize=0。"""
    f = bgm.mix_filter(bgm.mix_params(_cfg()), total=100.0)
    assert "volume=-16.00dB" in f
    assert "afade=t=in:d=2.00" in f
    assert "afade=t=out:st=97.00:d=3.00" in f
    assert "[bg][0:a]sidechaincompress=" in f
    assert "threshold=0.030" in f and "ratio=8" in f and "detection=rms" in f
    assert "amix=inputs=2:duration=first:dropout_transition=0:normalize=0" in f
    assert "alimiter=limit=0.98" in f


def test_mix_filter_without_duck_keeps_volume_low():
    f = bgm.mix_filter(bgm.mix_params(_cfg(duck=False)), total=100.0)
    assert "sidechaincompress" not in f
    assert "volume=-16.00dB" in f
    assert "[0:a][bg]amix=inputs=2" in f


def test_mix_filter_fade_out_skipped_when_too_short():
    f = bgm.mix_filter(bgm.mix_params(_cfg()), total=2.0)
    assert "afade=t=out" not in f and "afade=t=in" in f


def test_mix_filter_zero_fades_disabled():
    f = bgm.mix_filter(bgm.mix_params(_cfg(fade_in=0.0, fade_out=0.0)), total=100.0)
    assert "afade" not in f and "volume=" in f


def test_mix_command_shape():
    args = bgm.mix_command(
        "ffmpeg", Path("final.mp4"), Path("bgm.wav"), Path("bgm_final.mp4.part"),
        params=bgm.mix_params(_cfg()), total=100.0,
    )
    assert args[0] == "ffmpeg"
    assert "-nostdin" in args, "非交互 shell 里 ffmpeg 不能去读 stdin（会挂住）"
    assert args[args.index("-stream_loop") + 1] == "-1", "BGM 要循环铺满成片"
    assert "sidechaincompress" in args[args.index("-filter_complex") + 1]
    assert args[args.index("-map") + 1] == "0:v"
    assert "copy" in args and "-shortest" in args
    assert args[-1].endswith("bgm_final.mp4.part")
    assert args[args.index("-f") + 1] == "mp4"


def test_mix_writes_bgm_prefixed_file_and_keeps_original(tmp_path, monkeypatch):
    """★ 产物是 `bgm_<原片名>`，原片一字不动。"""
    ws = Workspace(task="bgmmix", root=tmp_path).ensure()
    ws.path("final.mp4").write_bytes(b"original")
    _bed(ws)
    captured: list[list[str]] = []

    def fake_run(args, cwd=None):  # noqa: ANN001, ANN202
        captured.append(list(args))
        assert str(args[-1]).endswith(".part")
        Path(args[-1]).write_bytes(b"mixed")

    monkeypatch.setattr(bgm, "ff_run", fake_run)
    monkeypatch.setattr(bgm, "ff_duration", lambda p: 100.0)
    out = bgm.mix(ws, _cfg())

    assert out.name == "bgm_final.mp4"
    assert ws.path("final.mp4").read_bytes() == b"original"
    assert out.read_bytes() == b"mixed"
    assert not list(out.parent.glob("*.part"))
    assert "sidechaincompress" in captured[0][captured[0].index("-filter_complex") + 1]


def test_mix_missing_inputs_are_usage_errors(tmp_path):
    ws = Workspace(task="bgmmissing", root=tmp_path).ensure()
    with pytest.raises(bgm.BgmUsageError) as excinfo:
        bgm.mix(ws, _cfg())
    assert "lvs build" in str(excinfo.value)

    ws.path("final.mp4").write_bytes(b"v")
    with pytest.raises(bgm.BgmUsageError) as excinfo:
        bgm.mix(ws, _cfg())
    assert "lvs bgm" in str(excinfo.value)


def test_mix_refuses_to_overwrite_original(tmp_path):
    ws = Workspace(task="bgmoverwrite", root=tmp_path).ensure()
    video = ws.path("final.mp4")
    video.write_bytes(b"v")
    _bed(ws)
    with pytest.raises(bgm.BgmUsageError) as excinfo:
        bgm.mix(ws, _cfg(), out=video)
    assert "覆盖" in str(excinfo.value)


# ---- CLI 接线 ---------------------------------------------------------------


def test_cli_registers_bgm_mix():
    from lvs.cli import build_parser

    args = build_parser().parse_args(["bgm", "mix", "--task", "t", "--volume-db", "-20"])
    assert args.command == "bgm" and args.action == "mix"
    assert args.volume_db == -20.0

    args = build_parser().parse_args(["bgm", "prompt", "--task", "t", "--no-llm"])
    assert args.action == "prompt" and args.no_llm is True

    args = build_parser().parse_args(["bgm", "--task", "t"])
    assert args.action is None, "不给动作 = 生成（默认）"


def test_cli_bgm_prompt_is_readonly(tmp_path, monkeypatch):
    """★ `lvs bgm prompt` 只读：不替当前目录建任务目录（票 23 同款契约）。"""
    from lvs import cli

    name = "_bgm-prompt-probe"
    task_dir = Path(__file__).resolve().parent.parent / ".work" / name
    shutil.rmtree(task_dir, ignore_errors=True)
    monkeypatch.setattr(bgm, "run_command", lambda config, ws, args: seen.append(ws) or 0)
    seen: list = []
    cfg = tmp_path / "config.toml"
    cfg.write_text("[app]\n", encoding="utf-8")

    assert cli.main(["bgm", "prompt", "--task", name, "--config", str(cfg)]) == 0
    assert seen, "bgm prompt 该把 workspace 交给 run_command"
    assert not seen[0].dir.exists(), "只读动作不该建 .work/<task>/"
    assert not task_dir.exists()


def test_print_plan_offline(tmp_path, capsys):
    ws = _ws_with_shots(tmp_path, HISTORY_TEXT)
    assert bgm.run_command(_cfg(), ws, _plan_args()) == 0
    out = capsys.readouterr().out
    assert "rules" in out and "no vocals" in out


def _plan_args():
    class _Args:
        action = "prompt"
        duration = 30.0
        no_llm = True

    return _Args()

# ---- 真跑 ffmpeg：闪避真的发生了吗（slow 组） -------------------------------


def _band_level(ff: str, path: Path, start: float, dur: float) -> float:
    """低通 + bandreject 把人声挖掉，只量 BGM 频段的 mean_volume（dB）。"""
    import re

    proc = subprocess.run(
        [ff, "-nostdin", "-hide_banner", "-nostats", "-ss", str(start), "-t", str(dur),
         "-i", str(path), "-af", "bandreject=f=1000:width_type=h:w=250,lowpass=f=400,volumedetect",
         "-f", "null", "-"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300,
    )
    match = re.search(r"mean_volume:\s*(-?[\d.]+)", proc.stderr or "")
    assert match, f"volumedetect 没输出：{proc.stderr[-400:]}"
    return float(match.group(1))


@pytest.mark.slow
class TestRealFfmpegBgmDuck:
    """端到端验证要求②：人声一开口，BGM 真的被 sidechaincompress 压下去。

    做法：旁白用 1 kHz（0–2 s 有声、2–4 s 静音），BGM 用 120 Hz 常驻；
    量的时候先 `bandreject=f=1000` + `lowpass=f=400` 把旁白挖掉，
    再看「人声段」与「静音段」的 BGM 电平差。duck=False 时这个差应当 ≈ 0。
    """

    @pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg 不可用")
    def test_duck_lowers_bgm_while_voice_plays(self, tmp_path):
        ff, _ = tools()
        voice = tmp_path / "voice.wav"
        bed = tmp_path / "bed.wav"
        subprocess.run(
            [ff, "-nostdin", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:duration=2",
             "-af", "volume=0.6,apad=whole_dur=4", str(voice)],
            check=True, capture_output=True, stdin=subprocess.DEVNULL,
        )
        subprocess.run(
            [ff, "-nostdin", "-y", "-f", "lavfi", "-i", "sine=frequency=120:duration=4",
             "-af", "volume=0.5", str(bed)],
            check=True, capture_output=True, stdin=subprocess.DEVNULL,
        )

        measured: dict[str, float] = {}
        for label, params in (
            ("off", bgm.MixParams(duck=False, fade_in=0.0, fade_out=0.0)),
            ("on", bgm.MixParams(fade_in=0.0, fade_out=0.0)),
        ):
            out = tmp_path / f"mix_{label}.wav"
            subprocess.run(
                [ff, "-nostdin", "-y", "-i", str(voice), "-stream_loop", "-1", "-i", str(bed),
                 "-filter_complex", bgm.mix_filter(params, total=4.0), "-map", "[a]",
                 "-c:a", "pcm_s16le", "-shortest", str(out)],
                check=True, capture_output=True, stdin=subprocess.DEVNULL,
            )
            with_voice = _band_level(ff, out, 0.5, 1.0)
            no_voice = _band_level(ff, out, 2.5, 1.0)
            measured[label] = no_voice - with_voice   # >0 = 人声段被压低了

        assert abs(measured["off"]) < 1.0, (
            f"没开闪避时 BGM 电平不该随人声变化（实测 {measured['off']:+.2f} dB）"
        )
        assert measured["on"] > 3.0, (
            f"开了闪避，人声段的 BGM 应明显更低（实测只差 {measured['on']:+.2f} dB）"
            "——检查 sidechaincompress 的输入顺序/key 是不是旁白"
        )


# ---- 权重来源：ModelScope 下载 + 离线文本编码器 ----------------------------


def test_weights_state_missing_text_encoder_is_not_ready(tmp_path):
    """★ 下载被打断（缺 t5gemma 子目录）必须先拦下，别读完 2.3 GB 主权重才炸。"""
    (tmp_path / "model_config.json").write_text(
        json.dumps({"model": {"conditioning": {"configs": [
            {"id": "prompt", "type": "t5gemma",
             "config": {"repo_id": "stabilityai/stable-audio-3-small-music",
                        "subfolder": "t5gemma-b-b-ul2"}},
        ]}}}),
        encoding="utf-8",
    )
    (tmp_path / "model.safetensors").write_bytes(b"x")

    ok, why = bgm.weights_state(_cfg(model_dir=str(tmp_path)))
    assert not ok and "t5gemma-b-b-ul2" in why and "下载不完整" in why

    (tmp_path / "t5gemma-b-b-ul2").mkdir()
    ok, why = bgm.weights_state(_cfg(model_dir=str(tmp_path)))
    assert ok and why == ""


def test_localize_text_encoders_points_at_local_dir(tmp_path):
    """★ 离线加载的关键：conditioner 的 repo_id/subfolder → 本地 model_path。"""
    (tmp_path / "t5gemma-b-b-ul2").mkdir()
    model_config = {"model": {"conditioning": {"configs": [
        {"id": "prompt", "type": "t5gemma",
         "config": {"max_length": 256, "padding_mode": "learned",
                    "repo_id": "stabilityai/stable-audio-3-small-music",
                    "subfolder": "t5gemma-b-b-ul2"}},
        {"id": "seconds_total", "type": "number", "config": {}},
    ]}}}

    assert bgm._localize_text_encoders(model_config, tmp_path) == 1
    prompt = model_config["model"]["conditioning"]["configs"][0]["config"]
    assert prompt["model_path"] == str(tmp_path / "t5gemma-b-b-ul2")
    assert "repo_id" not in prompt and "subfolder" not in prompt
    assert prompt["max_length"] == 256, "改写不许动其它字段"
    assert model_config["model"]["conditioning"]["configs"][1]["config"] == {}
    assert bgm._localize_text_encoders(model_config, tmp_path) == 0, "幂等"


def test_localize_text_encoders_keeps_repo_id_when_local_dir_missing(tmp_path):
    """本地没这个编码器时别把路改死 —— 保留 repo_id，让它照旧联网（失败会报人话）。"""
    model_config = {"configs": [{"config": {"repo_id": "some/repo", "subfolder": "missing"}}]}
    assert bgm._localize_text_encoders(model_config, tmp_path) == 0
    assert model_config["configs"][0]["config"]["repo_id"] == "some/repo"


def test_ms_urls_follow_config():
    assert bgm.ms_endpoint(Config({}, None)) == bgm.MS_ENDPOINT
    assert bgm.ms_repo(Config({}, None)) == bgm.MS_REPO
    assert bgm.ms_endpoint(_cfg(ms_endpoint="https://example.test")) == "https://example.test"
    assert bgm.ms_repo(_cfg(ms_repo="someone/else")) == "someone/else"
    assert bgm._ms_files_url("https://x", "a/b").endswith("/api/v1/models/a/b/repo/files"
                                                         "?Revision=master&Recursive=True")
    assert bgm._ms_file_url("https://x", "a/b", "t5gemma-b-b-ul2/config.json").endswith(
        "FilePath=t5gemma-b-b-ul2/config.json")


def test_download_prefers_modelscope(tmp_path, monkeypatch):
    """默认走 ModelScope（非门控）；成功了就不碰 hf-mirror。"""
    seen: dict = {}

    def fake_ms(config, directory):  # noqa: ANN001, ANN202
        seen["ms"] = directory

    def boom(config, directory):  # noqa: ANN001, ANN202
        raise AssertionError("ModelScope 成功时不该碰 hf-mirror")

    monkeypatch.setattr(bgm, "_download_modelscope", fake_ms)
    monkeypatch.setattr(bgm, "_download_hf_mirror", boom)
    cfg = _cfg(model_dir=str(tmp_path / "ms-ok"))
    assert bgm.download(cfg) == tmp_path / "ms-ok"
    assert seen["ms"] == tmp_path / "ms-ok"


def test_download_falls_back_to_hf_mirror_when_modelscope_fails(tmp_path, monkeypatch, capsys):
    """ModelScope 不通要退回 hf-mirror，而不是直接失败。"""
    seen: dict = {}

    def fail(config, directory):  # noqa: ANN001, ANN202
        raise bgm.BgmError("HTTP 502")

    def fake_hf(config, directory):  # noqa: ANN001, ANN202
        seen["hf"] = directory

    monkeypatch.setattr(bgm, "_download_modelscope", fail)
    monkeypatch.setattr(bgm, "_download_hf_mirror", fake_hf)
    cfg = _cfg(model_dir=str(tmp_path / "fallback"))
    bgm.download(cfg)
    assert seen["hf"] == tmp_path / "fallback"
    assert "退回 hf-mirror" in capsys.readouterr().out


def test_download_is_idempotent_when_weights_ready(tmp_path, monkeypatch):
    """权重齐了就别再下（`lvs bgm download` 该是幂等的）。"""
    (tmp_path / "model_config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "model.safetensors").write_bytes(b"x")
    monkeypatch.setattr(bgm, "_download_modelscope",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该再下")))
    assert bgm.download(_cfg(model_dir=str(tmp_path))) == tmp_path


def test_ms_fetch_one_resumes_from_part_file(tmp_path, monkeypatch):
    """★ 断点续传：有 `.part` 就带 Range 从断点接着下，字节数对上才原子归位。"""
    import sys
    import types

    payload = b"0123456789"
    part = tmp_path / "w.bin.part"
    part.write_bytes(payload[:4])
    seen: dict = {}

    class _Resp:
        status_code = 206

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *exc):  # noqa: ANN002, ANN204
            return False

        def iter_content(self, chunk):  # noqa: ANN001, ANN201
            yield payload[4:]

    def fake_get(url, headers=None, stream=False, timeout=None):  # noqa: ANN001, ANN202
        seen["url"] = url
        seen["headers"] = headers or {}
        return _Resp()

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=fake_get))
    bgm._ms_fetch_one("https://x", "a/b", tmp_path, "w.bin", len(payload))

    assert seen["headers"]["Range"] == "bytes=4-"
    assert seen["url"].endswith("FilePath=w.bin")
    assert (tmp_path / "w.bin").read_bytes() == payload
    assert not part.exists(), "归位后不该留 .part"


def test_ms_fetch_one_gives_up_with_human_message(tmp_path, monkeypatch):
    """HTTP 一直 500：重试若干次后抛 BgmError（带文件名），不许写半截文件。"""
    import sys
    import types

    class _Resp:
        status_code = 500

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *exc):  # noqa: ANN002, ANN204
            return False

        def iter_content(self, chunk):  # noqa: ANN001, ANN201
            return iter(())

    monkeypatch.setitem(sys.modules, "requests",
                        types.SimpleNamespace(get=lambda *a, **k: _Resp()))
    with pytest.raises(bgm.BgmError) as excinfo:
        bgm._ms_fetch_one("https://x", "a/b", tmp_path, "w.bin", 10, attempts=2)
    assert "w.bin" in str(excinfo.value)
    assert not (tmp_path / "w.bin").exists()


# ---- T5Gemma 掩码兼容层（torch<2.6，见 lvs/bgm.py 顶部那段注释） -------------


def test_t5gemma_encoder_masks_bidirectional_and_sliding():
    """掩码语义：full 层双向（只挡 padding），sliding 层再叠 |q-kv| < window。"""
    torch = pytest.importorskip("torch")

    masks = bgm._t5gemma_encoder_masks(
        torch.ones(1, 4, dtype=torch.bool), 4, 2, torch.device("cpu"))
    assert set(masks) == {"full_attention", "sliding_attention"}
    full = masks["full_attention"]
    assert full.dtype is torch.bool and tuple(full.shape) == (1, 1, 4, 4)
    assert masks["sliding_attention"][0, 0].tolist() == [
        [True, True, False, False],
        [True, True, True, False],
        [False, True, True, True],
        [False, False, True, True],
    ], "窗口 2：|q-kv| < 2"

    padded = bgm._t5gemma_encoder_masks(
        torch.tensor([[True, True, False, False]]), 4, None, torch.device("cpu"))
    assert padded["full_attention"][0, 0].tolist() == [[True, True, False, False]] * 4, (
        "full 层双向 + 挡掉 padding"
    )


def test_t5gemma_encoder_masks_pad_slice_and_none():
    """2D 掩码比 q 短就补 True、长就切片；没给掩码就全 True（不挡任何 token）。"""
    torch = pytest.importorskip("torch")

    masks = bgm._t5gemma_encoder_masks(None, 3, None, torch.device("cpu"))
    assert tuple(masks["full_attention"].shape) == (1, 1, 3, 3)
    assert bool(masks["full_attention"].all())
    assert masks["sliding_attention"] is masks["full_attention"], "不滑动时两层共用一张掩码"

    short = bgm._t5gemma_encoder_masks(
        torch.tensor([[True, False]]), 3, 4096, torch.device("cpu"))
    assert short["full_attention"][0, 0].tolist() == [
        [True, False, True], [True, False, True], [True, False, True]]

    long = bgm._t5gemma_encoder_masks(
        torch.ones(1, 6, dtype=torch.bool), 2, None, torch.device("cpu"))
    assert tuple(long["full_attention"].shape) == (1, 1, 2, 2)


def _fake_t5gemma(monkeypatch, calls: dict):
    """假 transformers 模块树：只放一个会记录入参的 T5GemmaEncoderModel。"""
    import sys
    import types

    modeling = types.ModuleType("transformers.models.t5gemma.modeling_t5gemma")

    class FakeEncoderModel:
        config = types.SimpleNamespace(sliding_window=4096)

        def forward(self, input_ids=None, attention_mask=None, position_ids=None,
                    inputs_embeds=None, **kwargs):
            calls["attention_mask"] = attention_mask
            return "out"

    modeling.T5GemmaEncoderModel = FakeEncoderModel  # type: ignore[attr-defined]
    for name in ("transformers", "transformers.models", "transformers.models.t5gemma"):
        module = types.ModuleType(name)
        module.__path__ = []  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setitem(sys.modules, modeling.__name__, modeling)
    sys.modules["transformers.models.t5gemma"].modeling_t5gemma = modeling  # type: ignore[attr-defined]
    return modeling


def test_t5gemma_shim_wraps_forward_with_prebuilt_masks(monkeypatch):
    """补丁只做一件事：把 2D 掩码折成 4D dict 再交回 transformers 自己的 forward。"""
    import types

    calls: dict = {}
    modeling = _fake_t5gemma(monkeypatch, calls)
    monkeypatch.setattr(bgm, "_torch_before", lambda *a: True)
    monkeypatch.setattr(bgm, "_t5gemma_encoder_masks",
                        lambda mask, q_len, window, device: {"full_attention": (q_len, window)})
    bgm._T5GEMMA_SHIM.clear()

    assert bgm._install_t5gemma_encoder_mask_shim() == "patched"
    assert bgm._install_t5gemma_encoder_mask_shim() == "already", "装两次也只该包一层"

    model = modeling.T5GemmaEncoderModel  # type: ignore[attr-defined]
    out = model().forward(input_ids=types.SimpleNamespace(shape=(1, 4)),
                          attention_mask=types.SimpleNamespace(device="cpu"))
    assert out == "out"
    assert calls["attention_mask"] == {"full_attention": (4, 4096)}
    bgm._T5GEMMA_SHIM.clear()


def test_t5gemma_shim_steps_aside_on_new_torch(monkeypatch):
    """torch>=2.6 时整段不生效（走官方路径）—— 升级后不是技术债。"""
    monkeypatch.setattr(bgm, "_torch_before", lambda *a: False)
    bgm._T5GEMMA_SHIM.clear()
    assert bgm._install_t5gemma_encoder_mask_shim().startswith("skip:")
    assert "forward" not in bgm._T5GEMMA_SHIM


def test_torch_before_parses_local_version(monkeypatch):
    """`2.5.1+cu121` 这种带后缀的版本号也要能比大小。"""
    import sys
    import types

    fake = types.ModuleType("torch")
    monkeypatch.setitem(sys.modules, "torch", fake)
    for version, expected in (("2.5.1+cu121", True), ("2.4.9", True),
                              ("2.6.0", False), ("2.10.0", False)):
        fake.__version__ = version  # type: ignore[attr-defined]
        assert bgm._torch_before(2, 6) is expected, version
