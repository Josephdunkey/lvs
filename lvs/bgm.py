"""BGM（背景音乐）：本地生成可商用配乐 + 与人声混音。

为什么要这个模块（重构提案之外的独立功能 P5）：成片过去只有旁白，静得像"有声书"；
而**可商用**的曲子在不同平台（B 站 / 抖音 / YouTube）都要单独找、单独买，换一本书还得重来。
这里把"配乐"当成流水线的一步：**本地生成**（版权干净、零成本、可复现）+ **压低闪避**
（旁白永远是主角）。

三条设计合同
------------
1. **自适应文案**（要求①）—— 讲稿什么调子，配乐就什么调子。双层策略：
   a. **LLM 路径**：config 填了 `[app].openai_api_key` 时，用 `lvs.llm.chat_json`
      把讲稿主题/情绪翻成 BGM 提示词（genre / instruments / mood / BPM）；
   b. **离线规则兜底**：没有 key（或调用失败）也能出片 —— 内置关键词→风格映射表，
      例如 历史/庄重→古典弦乐、治愈/温情→钢琴、悬疑/惊悚→低沉电子、激昂/热血→管弦乐、
      轻松/生活→轻快木管；一条都没命中就用中性柔和的保底风格。
2. **不打扰人声**（要求②）—— BGM 基础音量压到 `[bgm].volume_db`（默认 -16 dB），
   再以旁白为 key 走 ffmpeg `sidechaincompress` **闪避**：人一开口，BGM 自动再降几 dB。
   混音器是 `amix=normalize=0`（ffmpeg ≥4.4 默认会各除输入数，把旁白砍半 —— 见 build.py 的注释）。
3. **失败要说人话** —— 权重没下载 / 模型调不起来 / ffmpeg 挂了，都给出"下一步敲什么"，
   绝不留一个空文件冒充配乐。

环境约束（本机事实，别改写）
---------------------------
- **推理走 CPU**（`device="cpu"`）：8 GB 单卡被 ComfyUI(:8188) 与 Qwen3-TTS(:8100) 串行占用，
  BGM 不许抢显存。
- **权重走 ModelScope 国内镜像**（`MS_ENDPOINT` / `MS_REPO`）：HF 上这个仓库是 **gated**
  （要申请访问 + `HF_TOKEN`，hf-mirror 回 403 GatedRepoError），而 ModelScope 的镜像仓库
  **非门控、国内直连可下、不需要任何令牌** —— `lvs bgm download` 先走它，挂了才退回 hf-mirror。
- **加载完全离线**：权重齐了（`model_config.json` + `model.safetensors` + `t5gemma-b-b-ul2/`
  文本编码器）就**不查 HF、不要 HF_TOKEN** —— `load_model` 把 conditioner 里的 `repo_id`
  改写成本地目录再建模型（见 `_localize_text_encoders`）。
- **推理参数照模型卡**：权重目录里的 `README.md`（= HF 上那份 model card）是 SA3 的用法原文，
  默认值取它那套（`generate_diffusion_cond_inpaint` + steps 8 / cfg_scale 1.0 / sampler
  "pingpong"）。SA2 / SA-Open 的 100 步那套是 v-diffusion 的，会走错采样分支 —— 见 `DEFAULT_STEPS`。

许可（Stable Audio 3.0 Small-Music，Stability AI Community License）
-------------------------------------------------------------------
个人 / 组织年收入 < 100 万美元可商用；**输出归使用者**、无需署名；权重自带 T5Gemma
文本编码器（附 Gemma Terms of Use）。这段写进 `bgm.json.license`，也写在 docs 里。
"""

from __future__ import annotations

import json
import os
import wave
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from lvs import artifact
from lvs.config import PROJECT_ROOT, Config
from lvs.errors import EXIT_FAILED, LvsError, UsageError
from lvs.ffmpeg import AUDIO_AR, AUDIO_BR, FFmpegError
from lvs.ffmpeg import duration as ff_duration
from lvs.ffmpeg import run as ff_run
from lvs.ffmpeg import tools
from lvs.workspace import Workspace


class BgmError(LvsError, RuntimeError):
    """BGM 生成 / 下载 / 混音失败。消息里必须带"下一步敲什么"。"""

    exit_code = EXIT_FAILED


class BgmUsageError(UsageError):
    """BGM 的输入或前置条件不对（缺成片、缺 shots.json、缺 --duration）—— 改了再来。"""


#: 选定模型（Stability AI Community License）。改它 = 换许可证，README/docs 一起改。
MODEL_ID = "stabilityai/stable-audio-3-small-music"
MODEL_DIRNAME = "stable-audio-3-small-music"
#: HuggingFace 直连不通、hf-mirror 可达（HTTP 200）—— 只是 **备选** 下载源（要 HF_TOKEN）。
HF_MIRROR = "https://hf-mirror.com"
#: 首选下载源：ModelScope 上的同内容镜像仓（**非门控**、国内直连、不要令牌）。
MS_ENDPOINT = "https://modelscope.cn"
MS_REPO = "stabilityai/stable-audio-3-small-music"
MS_REVISION = "master"
#: 文本编码器（T5Gemma，1.2 GB）在权重目录里的子目录；缺它就没法离线加载。
TEXT_ENCODER_SUBDIR = "t5gemma-b-b-ul2"
WEIGHT_FILES = ("model.safetensors", "model.ckpt")
CONFIG_FILENAME = "model_config.json"

#: 时长策略：CPU 上生成 1 分钟音频要几分钟，**不要**按成片时长生成 10 分钟。
#: 默认生成一段 60 秒的 bed，混音时用 `-stream_loop -1` 循环铺满整片。
DEFAULT_DURATION_S = 60.0
MIN_DURATION_S = 15.0
MAX_DURATION_S = 180.0
#: 默认推理参数 = **模型卡给 SA3 的原值**（models/stable-audio-3-small-music/README.md 的
#: `### Using with stable-audio-tools` 段）：steps=8 / cfg_scale=1.0 / sampler_type="pingpong"。
#: ★ 踩过的坑：SA2 / SA-Open 的默认（100 步 / cfg 6.0 / dpmpp-3m-sde）是给 **v-diffusion** 的；
#: SA3 是 **rectified flow**，`sampling.py` 按模型类型分派采样器 —— 传 v-diffusion 的名字会走错
#: 分支，而 100 步在 CPU 上还要慢十倍以上。要换回 SA-Open：`--steps 100 --cfg-scale 6`。
DEFAULT_STEPS = 8
DEFAULT_CFG_SCALE = 1.0
DEFAULT_SEED = 20261005
DEFAULT_SAMPLER = "pingpong"
#: 全篇抽多少字判风格/喂 LLM —— 几千字足够，没必要把 516 KB 的 shots.json 全塞进去。
TEXT_LIMIT = 4000

#: 逐字校对过权重自带的 `LICENSE.md`（下载后落在 model_dir/LICENSE.md）——
#: §III 商用免费额度 + 商用需注册、§III(iv) 输出归使用者、§IV(a) 分发才要署名。
LICENSE_INFO: dict[str, Any] = {
    "model": MODEL_ID,
    "name": "Stability AI Community License",
    "commercial_use": "个人 / 组织年收入 < 100 万美元可商用（商用前需在 stability.ai/community-license 注册）",
    "output_ownership": "生成音频（输出）归使用者所有 —— 用输出不必署名",
    "distribution_attribution": (
        "分发模型 / Derivative Work（含内嵌它的产品）时才要：随附本协议 + 保留 Notice + "
        "显著标注 “Powered by Stability AI”"
    ),
    "bundled_terms": "权重自带 T5Gemma 文本编码器（附 Gemma Terms of Use）",
    "url": "https://stability.ai/community-license-agreement",
    "authoritative": "以权重自带的 LICENSE.md 为准（models/<模型>/LICENSE.md）",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---- ① 自适应风格：关键词 → 风格/情绪/BPM（离线兜底） ------------------------


@dataclass(frozen=True)
class StyleRule:
    """一条"关键词 → 配乐风格"规则。加新风格只要往 `STYLE_RULES` 里追加一条。"""

    name: str
    label: str
    keywords: tuple[str, ...]
    genre: str
    instruments: str
    mood: str
    bpm: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "genre": self.genre,
            "instruments": self.instruments,
            "mood": self.mood,
            "bpm": self.bpm,
        }


#: 规则表顺序 = 平票时的优先级。默认保底风格放最后（它命中不了任何关键词）。
STYLE_RULES: tuple[StyleRule, ...] = (
    StyleRule(
        "solemn_history", "历史·庄重",
        ("历史", "王朝", "朝代", "帝王", "将军", "武士", "战乱", "朝廷", "岁月",
         "千百年", "古事", "家谱", "名门", "旧事", "幕府", "寺院"),
        "cinematic classical", "strings, timpani, low brass", "solemn, dignified", 72,
    ),
    StyleRule(
        "heroic", "激昂·热血",
        ("激昂", "热血", "战斗", "英雄", "复仇", "冲锋", "决斗", "不屈", "决战",
         "拼命", "怒吼", "逆袭", "奋斗"),
        "epic orchestral", "full orchestra, horns, taiko drums", "triumphant, driving", 110,
    ),
    StyleRule(
        "suspense", "悬疑·惊悚",
        ("悬疑", "惊悚", "恐怖", "诡异", "凶手", "尸体", "阴谋", "密谋",
         "失踪", "惨案", "血迹", "死亡", "诡异", "凶"),
        "dark ambient electronic", "low drone, sub pulse, sparse percussion", "tense, ominous", 90,
    ),
    StyleRule(
        "mystery", "神秘·奇幻",
        ("神秘", "奇幻", "妖怪", "幽灵", "神话", "传说", "咒", "妖", "幻", "梦",
         "灵", "异界", "玄"),
        "ethereal ambient", "pads, celesta, breathy choir", "mysterious, dreamlike", 76,
    ),
    StyleRule(
        "melancholy", "悲伤·离别",
        ("悲伤", "离别", "孤独", "思念", "眼泪", "凄凉", "遗憾", "逝去",
         "叹息", "悲哀", "哀", "苦", "痛"),
        "melancholic chamber", "cello, felt piano, sparse strings", "sad, reflective", 64,
    ),
    StyleRule(
        "healing", "治愈·温情",
        ("治愈", "温情", "温暖", "母亲", "家乡", "童年", "陪伴", "温柔", "善意",
         "亲情", "幸福", "团圆", "喜欢"),
        "warm acoustic", "solo piano, soft strings, light guitar", "tender, hopeful", 68,
    ),
    StyleRule(
        "light_daily", "轻松·日常",
        ("轻松", "日常", "市集", "酒馆", "闲聊", "俏皮", "幽默", "有趣", "热闹",
         "生活", "玩笑", "闲话", "酒"),
        "light acoustic", "woodwinds, pizzicato, ukulele", "playful, easygoing", 104,
    ),
)

#: 一条关键词都没命中的保底：中性、柔和、绝不抢戏。
FALLBACK_STYLE = StyleRule(
    "neutral_soft", "中性·柔和",
    (),
    "ambient piano", "felt piano, warm pad, soft strings", "calm, unobtrusive", 70,
)


@dataclass(frozen=True)
class StyleMatch:
    """规则匹配结果：命中的风格 + 命中的关键词（`keywords` 进 bgm.json，可追溯）。"""

    rule: StyleRule
    keywords: tuple[str, ...] = ()

    @property
    def matched(self) -> bool:
        return bool(self.keywords)


def match_style(text: str) -> StyleMatch:
    """从文案里挑风格：按命中关键词**次数**计分，平票取 `STYLE_RULES` 里靠前的。

    为什么要计次数而不是"命中即选"：一段讲稿反复出现"死/凶/尸体"显然是惊悚，
    而只提一次"家"不该把整片定成治愈。次数是最便宜也最可解释的权重。
    """
    haystack = text or ""
    best: StyleRule | None = None
    best_score = 0
    best_hits: tuple[str, ...] = ()
    for rule in STYLE_RULES:
        hits = [kw for kw in rule.keywords if kw in haystack]
        score = sum(haystack.count(kw) for kw in hits)
        if score > best_score:
            best, best_score, best_hits = rule, score, tuple(hits)
    if best is None:
        return StyleMatch(FALLBACK_STYLE, ())
    return StyleMatch(best, best_hits)


def tempo_word(bpm: int) -> str:
    if bpm < 70:
        return "slow"
    if bpm < 95:
        return "moderate"
    return "upbeat"


def style_prompt(style: StyleRule) -> str:
    """风格 → 英文提示词（模型文本编码器对英文更稳）。

    刻意把"无人声 / 不抢戏 / 可循环"写进去：这是**配乐**不是单曲，
    旁白才是主角（要求②在提示词层面也压一道）。
    """
    return (
        f"instrumental {style.genre} background music, {style.instruments}, "
        f"{style.mood}, {tempo_word(style.bpm)} tempo around {style.bpm} bpm, "
        "soft dynamics, sparse arrangement, no vocals, no lead melody, "
        "seamless loop, background underscore for spoken narration"
    )

@dataclass
class PromptPlan:
    """一段文案定了什么配乐：提示词 + 结构化的风格 + 它从哪来。"""

    prompt: str
    style: dict[str, Any]
    source: str                      # "llm" | "rules" | "manual"
    keywords: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)
    text_chars: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "style": self.style,
            "source": self.source,
            "keywords": list(self.keywords),
            "text_chars": self.text_chars,
            "warnings": list(self.warnings),
        }

    def describe(self) -> str:
        style = self.style
        hit = f"（命中：{'/'.join(self.keywords)}）" if self.keywords else ""
        return (
            f"{style['label']}{hit} → {style['genre']} · {style['instruments']} · "
            f"{style['mood']} · {style['bpm']} BPM"
        )


LLM_SYSTEM_PROMPT = (
    "你是长视频的配乐指导。读一段讲稿摘录，为它挑一段**纯器乐**背景音乐。"
    "只输出 JSON（不要围栏、不要解释），字段："
    '{"genre": "英文风格名", "instruments": "英文乐器（逗号分隔）", '
    '"mood": "英文情绪", "bpm": 整数, "prompt": "一句英文配乐提示词"}。'
    "硬要求：无人声、旋律不抢旁白、可无缝循环；BPM 在 40–180 之间。"
)


def _style_from_llm(data: Any, fallback: StyleRule) -> StyleRule:
    """把 LLM 的 JSON 收敛成一条 `StyleRule`；缺字段的用规则兜底值补齐。

    为什么要"逐个字段兜底"而不是"整体不信"：LLM 常常只回一半字段（比如没给 bpm），
    整体丢弃等于白花一次调用；逐字段兜底既保住了它的判断，又不至于让缺字段炸掉。
    """
    if not isinstance(data, dict):
        raise BgmError("LLM 没有返回 JSON 对象")

    def _text(key: str, default: str) -> str:
        value = data.get(key)
        text = str(value).strip() if isinstance(value, (str, int, float)) else ""
        return text or default

    try:
        bpm = int(float(data.get("bpm", fallback.bpm)))
    except (TypeError, ValueError):
        bpm = fallback.bpm
    bpm = max(40, min(180, bpm))
    return StyleRule(
        name="llm",
        label="LLM 定制",
        keywords=(),
        genre=_text("genre", fallback.genre),
        instruments=_text("instruments", fallback.instruments),
        mood=_text("mood", fallback.mood),
        bpm=bpm,
    )


def _llm_prompt_text(data: Any) -> str:
    if isinstance(data, dict):
        value = data.get("prompt")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def build_prompt(
    text: str,
    config: Config,
    *,
    use_llm: bool = True,
    duration: float = DEFAULT_DURATION_S,
) -> PromptPlan:
    """文案 → 配乐提示词（要求①的双层策略）。

    LLM 可用就用 LLM（拿它的 `prompt`，风格字段也用它）；**任何失败都退回规则**，
    并把原因塞进 `warnings` —— 配乐不该因为一次 429 就整片没有 BGM。
    """
    rules = match_style(text)
    plan = PromptPlan(
        prompt=style_prompt(rules.rule),
        style=rules.rule.as_dict(),
        source="rules",
        keywords=rules.keywords,
        text_chars=len(text or ""),
    )
    if not use_llm:
        return plan

    from lvs import llm  # 局部导入：没有 requests 的环境也要能 import 本模块

    if not llm.available(config):
        return plan
    messages = [
        {"role": "system", "content": LLM_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"讲稿摘录（可能被截断）：\n{(text or '')[:3000]}\n\n"
                f"成片时长约 {duration:.0f} 秒。"
            ),
        },
    ]
    try:
        client = llm.LLMClient.from_config(config)
        data = llm.chat_json(client, messages, retries=0, temperature=0.4)
        style = _style_from_llm(data, rules.rule)
    except Exception as exc:  # noqa: BLE001 - LLM 任何失败都必须退回规则，不许炸穿
        plan.warnings.append(f"LLM 提示词失败，已退回关键词规则：{type(exc).__name__}: {exc}")
        return plan

    plan.source = "llm"
    plan.style = style.as_dict()
    plan.prompt = _llm_prompt_text(data) or style_prompt(style)
    return plan


def collect_text(ws: Workspace, *, limit: int = TEXT_LIMIT) -> tuple[str, str]:
    """把本任务的文案摘成一段（标题 + 分镜小标题 + 旁白 + 画面描述），返回 (文本, 标题)。

    只读 `shots.json` 里的**文本字段**，不碰提示词正文；上限 `limit` 字符 ——
    风格判定用几千字足够，没必要把 516 KB 全喂给 LLM。
    """
    data = ws.try_load_shots()
    shots = data.get("shots") if isinstance(data, dict) else None
    if not shots:
        raise BgmUsageError(
            f"没有可用的文案（{ws.path('shots.json')} 不存在或没有分镜）。\n"
            f"  先跑：lvs shots --task {ws.task} --config <cfg>"
        )

    title = str(data.get("title") or "").strip()
    headings: list[str] = []
    narration: list[str] = []
    visual: list[str] = []
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        head = str(shot.get("segment_heading") or "").strip()
        if head and head not in headings:
            headings.append(head)
        line = str(shot.get("narration") or "").strip()
        if line:
            narration.append(line)
        scene = str(shot.get("visual") or "").strip()
        if scene:
            visual.append(scene)

    parts = [p for p in (title, " ".join(headings), " ".join(narration), " ".join(visual)) if p]
    return "\n".join(parts)[:limit], title

# ---- 权重：位置 / 状态 / 下载 -----------------------------------------------


def model_dir(config: Config) -> Path:
    """权重落盘目录。`[bgm].model_dir` 优先（可指到别的盘），否则 `<仓库根>/models/<模型名>`。"""
    raw = str(config.get("bgm.model_dir", "") or "").strip()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path
    return PROJECT_ROOT / "models" / MODEL_DIRNAME


def model_id(config: Config) -> str:
    return str(config.get("bgm.model_id", "") or "").strip() or MODEL_ID


def hf_endpoint(config: Config) -> str:
    return str(config.get("bgm.hf_endpoint", "") or "").strip() or HF_MIRROR


def ms_endpoint(config: Config) -> str:
    return str(config.get("bgm.ms_endpoint", "") or "").strip() or MS_ENDPOINT


def ms_repo(config: Config) -> str:
    return str(config.get("bgm.ms_repo", "") or "").strip() or MS_REPO


def _walk_dicts(node: Any):
    """深度遍历配置里的所有 dict（只认 dict/list，其它原样跳过）。"""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_dicts(item)


def _referenced_subdirs(model_config: Any) -> list[str]:
    """model_config 里 conditioner 指向的相对子目录（去重，保持出现顺序）。"""
    names: list[str] = []
    for node in _walk_dicts(model_config):
        value = node.get("subfolder")
        if isinstance(value, str) and value and value not in names:
            names.append(value)
    return names


def _localize_text_encoders(model_config: Any, directory: Path) -> int:
    """把 `repo_id` + `subfolder` 改写成本地 `model_path`（**离线加载的关键一步**）。

    为什么需要：权重自带的 `t5gemma-b-b-ul2` 文本编码器在 model_config 里是
    `{"repo_id": "stabilityai/stable-audio-3-small-music", "subfolder": "t5gemma-b-b-ul2"}`；
    `T5GemmaConditioner` 按 `model_path or repo_id or model_name` 取路径，于是**盘上有权重也照样
    去联网查 HF**。这里在建模前把 `repo_id/subfolder` 换成本地目录：
    `AutoTokenizer.from_pretrained(<本地目录>)` 直接读盘，不联网、不要 HF_TOKEN。

    本地没有这个子目录时**原样保留**（宁可让它照旧联网、失败时报人话，也别改动成死路）。
    返回改写的 conditioner 个数。
    """
    rewritten = 0
    for node in _walk_dicts(model_config):
        subdir = node.get("subfolder")
        if not (isinstance(subdir, str) and subdir) and "repo_id" not in node:
            continue
        local = directory / subdir if isinstance(subdir, str) and subdir else directory
        if not local.is_dir():
            continue
        node["model_path"] = str(local)
        node.pop("repo_id", None)
        node.pop("subfolder", None)
        rewritten += 1
    return rewritten


def weights_state(config: Config) -> tuple[bool, str]:
    """`(能不能离线加载, 原因)`。原因要能直接印给用户看。"""
    directory = model_dir(config)
    if not directory.is_dir():
        return False, f"权重目录不存在：{directory}"
    if not (directory / CONFIG_FILENAME).is_file():
        return False, f"缺 {CONFIG_FILENAME}：{directory}"
    for name in WEIGHT_FILES:
        if (directory / name).is_file():
            break
    else:
        if not list(directory.glob("*.safetensors")):
            return False, f"缺权重文件（{' / '.join(WEIGHT_FILES)}）：{directory}"
    # 文本编码器是**另一个**子目录：下载被打断时最容易缺它，缺了就必须在这里拦下，
    # 否则 2.3 GB 主权重都读完了才在建模时报错（见 MEMORY「fail-closed」那条）。
    missing = [name for name in _referenced_subdirs(_load_model_config(directory))
               if not (directory / name).is_dir()]
    if missing:
        return False, (f"缺文本编码器目录（{'、'.join(missing)}）：{directory}"
                       " —— 权重下载不完整，重跑 lvs bgm download")
    return True, ""


def _load_model_config(directory: Path) -> Any:
    """读 `model_config.json`；读不动就返回 `None`（调用方按"没这信息"处理）。"""
    try:
        return json.loads((directory / CONFIG_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


DOWNLOAD_HINT = (
    "\n  下一步：\n"
    "    1) `lvs bgm download --config <cfg>`（**走 ModelScope 国内镜像**，非门控、不要令牌）\n"
    "    2) 只有 ModelScope 挂了才退回 hf-mirror；那条路要 HF_TOKEN（HF 上仓库是 gated）\n"
    "    3) 没有权重也能先用 `lvs bgm prompt --task <T>` 看提示词与风格"
)


def download(config: Config, *, force: bool = False) -> Path:
    """把权重拉到 `model_dir`：**先 ModelScope**（非门控、国内直连），不行才退回 hf-mirror。

    已就绪时是幂等的。断点续传：每个文件写 `.part`，字节数对上才原子归位，中断了重跑接着下。
    """
    directory = model_dir(config)
    if not force:
        ready, _ = weights_state(config)
        if ready:
            print(f"  权重已就绪：{directory}（--force 可重新下载）")
            return directory

    directory.mkdir(parents=True, exist_ok=True)
    try:
        _download_modelscope(config, directory)
        return directory
    except BgmError as exc:  # noqa: BLE001 - 镜像不通就换下一个源，别把路堵死
        print(f"  ⚠ ModelScope 下载不成（{exc}）")
        print("    退回 hf-mirror（HF 上仓库是 gated，需要 HF_TOKEN）…")
    _download_hf_mirror(config, directory)
    return directory


def _ms_files_url(endpoint: str, repo: str) -> str:
    return f"{endpoint}/api/v1/models/{repo}/repo/files?Revision={MS_REVISION}&Recursive=True"


def _ms_file_url(endpoint: str, repo: str, rel: str) -> str:
    return f"{endpoint}/api/v1/models/{repo}/repo?Revision={MS_REVISION}&FilePath={rel}"


def _download_modelscope(config: Config, directory: Path) -> None:
    """从 ModelScope 镜像仓拉整份权重（公开 API + 断点续传，**不需要任何令牌**）。"""
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise BgmError(f"未安装 requests，无法走 ModelScope 下载：{exc}") from exc

    endpoint, repo = ms_endpoint(config), ms_repo(config)
    try:
        resp = requests.get(_ms_files_url(endpoint, repo), timeout=60)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 - 网络/解析失败都换成"换源"
        raise BgmError(f"列文件失败 {type(exc).__name__}: {exc}") from exc
    if payload.get("Code") != 200:
        raise BgmError(f"列文件被拒：{payload.get('Message')}")

    blobs = [(str(f.get("Path")), int(f.get("Size") or 0))
             for f in ((payload.get("Data") or {}).get("Files") or [])
             if f.get("Type") == "blob"]
    if not blobs:
        raise BgmError(f"仓库里没有文件：{repo}")
    print(f"  下载 {repo} → {directory}")
    print(f"  源：{endpoint}（ModelScope 国内镜像，非门控、不需要 HF_TOKEN）")
    for rel, size in blobs:
        _ms_fetch_one(endpoint, repo, directory, rel, size)
    print(f"  完成：{len(blobs)} 个文件 / {sum(s for _, s in blobs) / (1 << 20):.0f} MB")


def _ms_fetch_one(endpoint: str, repo: str, directory: Path, rel: str, size: int,
                  *, attempts: int = 8) -> None:
    """单文件断点续传：`.part` 累加 → 字节数对上才原子归位（中断重跑接着下）。"""
    import requests

    out = directory / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.is_file() and out.stat().st_size == size:
        print(f"  [跳过] {rel}（已就绪）")
        return
    part = out.with_name(out.name + ".part")
    url = _ms_file_url(endpoint, repo, rel)
    last: Exception | None = None
    for _ in range(max(1, attempts)):
        have = part.stat().st_size if part.is_file() else 0
        if have > size:
            part.unlink(missing_ok=True)
            have = 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with requests.get(url, headers=headers, stream=True, timeout=180) as resp:
                if have and resp.status_code == 200:
                    have = 0          # 服务端不认 Range：只能从头来
                elif resp.status_code not in (200, 206):
                    raise BgmError(f"HTTP {resp.status_code}")
                with open(part, "ab" if have else "wb") as fh:
                    for chunk in resp.iter_content(1 << 21):
                        if chunk:
                            fh.write(chunk)
        except Exception as exc:  # noqa: BLE001 - 网络抖动：下一轮接着续
            last = exc
            continue
        got = part.stat().st_size
        if got == size:
            artifact.commit_file(part, out)
            print(f"  [完成] {rel}（{size / (1 << 20):.1f} MB）")
            return
        last = BgmError(f"字节数对不上（{got} != {size}）")
    raise BgmError(f"下载 {rel} 失败：{type(last).__name__}: {last}")


def _download_hf_mirror(config: Config, directory: Path) -> Path:
    """备选：走 hf-mirror（HF 上仓库是 gated，需要 HF_TOKEN）。"""
    os.environ.setdefault("HF_ENDPOINT", hf_endpoint(config))
    directory.mkdir(parents=True, exist_ok=True)
    try:
        from huggingface_hub import snapshot_download  # 局部导入：只有这一步真需要它
    except ModuleNotFoundError as exc:
        raise BgmError("未安装 huggingface_hub，无法下载权重。请 pip install huggingface_hub") from exc

    repo = model_id(config)
    print(f"  下载 {repo} → {directory}")
    print(f"  HF_ENDPOINT={os.environ.get('HF_ENDPOINT')}（本机 HF 直连不通，必须走镜像）")
    try:
        path = snapshot_download(repo_id=repo, local_dir=str(directory))
    except Exception as exc:  # noqa: BLE001 - 网络/授权失败都要变成人话
        message = f"模型权重下载失败：{type(exc).__name__}: {exc}"
        text = str(exc).lower()
        if "gated" in text or "403" in text or "401" in text or "restricted" in text:
            message += DOWNLOAD_HINT.format(repo=repo)
        raise BgmError(message) from exc
    return Path(path)


# ---- ② 生成：模型 → wav ----------------------------------------------------


def _supported_kwargs(func: Callable[..., Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    """只保留 `func` 真正接受的参数（其余丢掉）。

    为什么需要：stable-audio-tools 各版本 `generate_diffusion_cond` 的形参不一样
    （`seed` / `sampler_type` 都是后加的）。硬传会 TypeError，而"少传一个参数"
    只会让结果稍不同 —— 对"能出片"来说，后者可接受得多。
    `**kwargs`（VAR_KEYWORD）时原样全传。
    """
    import inspect

    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):  # pragma: no cover - C 扩展函数没有签名
        return dict(kwargs)
    params = signature.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(kwargs)
    return {key: value for key, value in kwargs.items() if key in params}


# ---- T5Gemma 掩码兼容层（torch < 2.6）开始 ---------------------------------
#: transformers 4.53+ 的 T5Gemma 编码器造掩码时传 ``or_mask_function=``，而 transformers
#: 要求该参数只在 torch>=2.6 下可用（内部 vmap 依赖 dynamo 的 TransformGetItemToIndex）。
#: 本机 torch 2.5.x（8GB 卡不动 CUDA 栈）会直接抛 ``require torch>=2.6``。这里**不改
#: transformers 任何文件**，只在进程内给 ``T5GemmaEncoderModel.forward`` 包一层：把 2D
#: padding 掩码折成 T5GemmaEncoder 官方支持的 4D dict 掩码（``self_attn_mask_mapping``），
#: 从而绕开 ``create_causal_mask``。掩码语义与官方一致：full_attention 层 = 双向 + padding，
#: sliding_attention 层 = 再加 |q-kv| < sliding_window（SA3 的窗口 4096 远大于提示词长度）。
#: torch>=2.6 时整段不生效（走官方路径），升级后自然失效、不会变成技术债。
_T5GEMMA_SHIM: dict[str, Any] = {}


def _torch_before(major: int, minor: int) -> bool:
    """当前 torch 是否 < major.minor（只解析版本号数字，不引 packaging）。"""
    import re

    import torch

    nums = [int(n) for n in re.findall(r"\d+", torch.__version__.split("+")[0])]
    return tuple(nums[:2]) < (major, minor)


def _t5gemma_encoder_masks(attention_mask: Any, q_len: int, sliding_window: Any,
                           device: Any) -> dict[str, Any]:
    """2D padding 掩码（1=参与）→ 4D bool 掩码 dict，等价于官方编码器掩码。"""
    import torch

    if attention_mask is None:
        keep = torch.ones((1, q_len), dtype=torch.bool, device=device)
    else:
        keep = attention_mask.to(device=device, dtype=torch.bool)
        if keep.dim() == 1:
            keep = keep.unsqueeze(0)
        gap = q_len - int(keep.shape[-1])
        if gap > 0:
            keep = torch.nn.functional.pad(keep, (0, gap), value=True)
        elif gap < 0:
            keep = keep[:, :q_len]
    kv_len = int(keep.shape[-1])
    full = keep[:, None, None, :].expand(-1, 1, q_len, kv_len).contiguous()
    if sliding_window:
        window = int(sliding_window)
        q_idx = torch.arange(q_len, device=device).view(1, 1, q_len, 1)
        kv_idx = torch.arange(kv_len, device=device).view(1, 1, 1, kv_len)
        band = (q_idx - window < kv_idx) & (kv_idx < q_idx + window)
        return {"full_attention": full, "sliding_attention": (full & band).contiguous()}
    return {"full_attention": full, "sliding_attention": full}


def _install_t5gemma_encoder_mask_shim() -> str:
    """装 T5Gemma 掩码补丁；返回 ``patched`` / ``already`` / ``skip:...``。"""
    if not _torch_before(2, 6):
        return "skip:torch>=2.6 走官方路径"
    try:
        from transformers.models.t5gemma import modeling_t5gemma as tg
    except Exception as exc:  # noqa: BLE001 - 老版 transformers 没这个模型，照旧即可
        return f"skip:没有 T5Gemma（{type(exc).__name__}）"
    if "forward" in _T5GEMMA_SHIM:
        return "already"
    original = tg.T5GemmaEncoderModel.forward

    def forward(self, input_ids=None, attention_mask=None, position_ids=None,
                inputs_embeds=None, **kwargs):
        if not isinstance(attention_mask, dict):
            head = inputs_embeds if inputs_embeds is not None else input_ids
            q_len = int(head.shape[1])
            device = (attention_mask.device if attention_mask is not None
                      else next(self.parameters()).device)
            attention_mask = _t5gemma_encoder_masks(
                attention_mask, q_len, getattr(self.config, "sliding_window", None), device
            )
        return original(self, input_ids=input_ids, attention_mask=attention_mask,
                        position_ids=position_ids, inputs_embeds=inputs_embeds, **kwargs)

    tg.T5GemmaEncoderModel.forward = forward
    _T5GEMMA_SHIM["forward"] = original
    return "patched"


# ---- T5Gemma 掩码兼容层（torch < 2.6）结束 ---------------------------------


def load_model(config: Config, *, device: str = "cpu") -> tuple[Any, dict[str, Any]]:
    """**本地优先**加载模型：权重在盘上就完全离线（不查 HF），返回 (model, model_config)。"""
    ready, why = weights_state(config)
    if not ready:
        raise BgmError(
            f"模型权重未下载（{why}）。\n"
            f"  BGM 生成需要权重；先跑：lvs bgm download --config <cfg>"
            + DOWNLOAD_HINT.format(repo=model_id(config))
        )
    os.environ.setdefault("HF_ENDPOINT", hf_endpoint(config))
    try:
        from stable_audio_tools.models.factory import create_model_from_config
        from stable_audio_tools.models.utils import load_ckpt_state_dict
    except ModuleNotFoundError as exc:
        raise BgmError(
            "未安装 stable-audio-tools，无法生成 BGM。\n"
            "  请：pip install stable-audio-tools（或 pip install -e .[bgm]）\n"
            f"  原始错误：{exc}"
        ) from exc

    directory = model_dir(config)
    model_config = json.loads((directory / CONFIG_FILENAME).read_text(encoding="utf-8"))
    if _localize_text_encoders(model_config, directory):
        print(f"  文本编码器读本地：{directory / TEXT_ENCODER_SUBDIR}（离线，不查 HF）")
    weights = next((directory / n for n in WEIGHT_FILES if (directory / n).is_file()), None)
    if weights is None:
        candidates = sorted(directory.glob("*.safetensors"))
        if not candidates:
            raise BgmError(f"权重目录里没有可加载的权重文件：{directory}")
        weights = candidates[0]
    if _install_t5gemma_encoder_mask_shim() == "patched":
        print("  T5Gemma 掩码补丁：torch<2.6，改用预构造 4D 掩码（与官方掩码等价）")
    try:
        model = create_model_from_config(model_config)
        model.load_state_dict(load_ckpt_state_dict(str(weights)))
        model.eval()
        model.to(device)
    except Exception as exc:  # noqa: BLE001 - 模型结构不匹配也要给人话
        raise BgmError(
            f"模型加载失败（结构不匹配 / 权重损坏？）：{type(exc).__name__}: {exc}"
        ) from exc
    return model, model_config


def _generators() -> list[Callable[..., Any]]:
    """按优先级返回可用的推理函数（**两个都试，谁在就用谁**）。

    模型卡里 SA3 用的是 `generate_diffusion_cond_inpaint`（SA3 的推理/编辑共用入口，
    不给 `init_audio` / `inpaint_mask` 时就是普通生成）；老版本 SAT 只有
    `generate_diffusion_cond`。硬挑一个会在换版本时静默失效，所以列出来逐个试。
    """
    from stable_audio_tools.inference import generation

    return [
        fn for fn in (
            getattr(generation, "generate_diffusion_cond_inpaint", None),
            getattr(generation, "generate_diffusion_cond", None),
        )
        if callable(fn)
    ]


def generate_audio(
    model: Any,
    model_config: dict[str, Any],
    *,
    prompt: str,
    duration: float,
    seed: int = DEFAULT_SEED,
    steps: int = DEFAULT_STEPS,
    cfg_scale: float = DEFAULT_CFG_SCALE,
    sampler_type: str = DEFAULT_SAMPLER,
    device: str = "cpu",
) -> tuple[Any, int]:
    """跑一次生成，返回 `(audio 张量, 采样率)`。**始终 CPU**（见模块 docstring）。"""
    try:
        generators = _generators()
    except ModuleNotFoundError as exc:
        raise BgmError(
            f"未安装 stable-audio-tools 的推理模块（缺 {exc.name}），无法生成 BGM。\n"
            "  请：pip install -e .[bgm]（会带上 k-diffusion 那一串依赖）"
        ) from exc
    if not generators:
        raise BgmError("stable-audio-tools 里找不到 generate_diffusion_cond* —— 版本不对，请 pip install -U stable-audio-tools")

    sample_rate = int(model_config.get("sample_rate") or 44100)
    sample_size = max(1, int(round(duration * sample_rate)))
    # 注意**不传 sigma_min / sigma_max**：那是 v-diffusion 的参数，SA3 走 flow 分支，
    # 采样器自己有默认（sigma_max=1）；模型卡的示例也没传。
    kwargs: dict[str, Any] = {
        "conditioning": [{"prompt": prompt, "seconds_total": float(duration)}],
        "steps": int(steps),
        "cfg_scale": float(cfg_scale),
        "sample_size": sample_size,
        "sampler_type": sampler_type,
        "batch_size": 1,
        "seed": int(seed),
        "device": device,
    }
    failures: list[str] = []
    for generate in generators:
        try:
            audio = generate(model, **_supported_kwargs(generate, kwargs))
            return audio, sample_rate
        except Exception as exc:  # noqa: BLE001 - 换下一个入口再试，最后统一报人话
            failures.append(f"{generate.__name__}: {type(exc).__name__}: {exc}")
    raise BgmError("BGM 推理失败（两个入口都试过）：\n  " + "\n  ".join(failures))


def write_wav(path: Path, audio: Any, sample_rate: int) -> Path:
    """把张量写成 16-bit PCM WAV（**先写 .part 再原子归位**，不留半截文件）。

    刻意用标准库 `wave` 而不是 torchaudio/soundfile：少一个运行时依赖，
    而 BGM 是 16-bit 立体声、够用。偶发 >1.0 的样本按峰值归一，避免削顶爆音。
    """
    import numpy as np  # 局部导入：只有真写文件时才需要 numpy

    samples = (
        audio.detach().cpu().float().numpy()
        if hasattr(audio, "detach")
        else np.asarray(audio, dtype="float32")
    )
    if samples.ndim == 3:
        samples = samples[0]
    if samples.ndim == 2:
        samples = samples.T  # (通道, 采样) → (采样, 通道)
    if samples.ndim == 1:
        samples = samples[:, None]
    if samples.shape[1] > 2:
        samples = samples[:, :2]
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak > 1.0:
        samples = samples / peak
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")

    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    try:
        with wave.open(str(part), "wb") as handle:
            handle.setnchannels(int(pcm.shape[1]))
            handle.setsampwidth(2)
            handle.setframerate(int(sample_rate))
            handle.writeframes(pcm.tobytes())
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    artifact.commit_file(part, path)
    return path


def write_meta(path: Path, meta: dict[str, Any]) -> Path:
    """写 `bgm.json`（UTF-8 不转义 + 2 空格缩进 + 尾换行，与 shots.json 同一约定）。"""
    artifact.atomic_write_text(path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    return path


def resolve_duration(ws: Workspace, config: Config, override: float | None = None) -> float:
    """生成时长：`--duration` → `[bgm].duration` → 成片/旁白时长（夹在 15–180 秒）。

    为什么上限 180：CPU 上稳定音频是"分钟级/十秒级"的速度，按 10 分钟成片直出会跑到天亮。
    默认取一段 bed，混音时循环铺满整片（见 `mix`）。
    """
    value = override if override is not None else config.get("bgm.duration")
    if value:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            seconds = 0.0
        if seconds > 0:
            return max(MIN_DURATION_S, min(MAX_DURATION_S, seconds))
    for candidate in ("final.mp4", "audio/narration.mp3"):
        measured = ff_duration(ws.path(candidate))
        if measured and measured > 0:
            return max(MIN_DURATION_S, min(MAX_DURATION_S, float(measured)))
    return DEFAULT_DURATION_S

def generate(
    ws: Workspace,
    config: Config,
    *,
    duration: float | None = None,
    seed: int | None = None,
    steps: int | None = None,
    cfg_scale: float | None = None,
    prompt: str | None = None,
    use_llm: bool = True,
    force: bool = False,
    device: str = "cpu",
) -> dict[str, Any]:
    """生成 `bgm/bgm.wav` + `bgm/bgm.json`，返回元信息 dict。"""
    out = ws.path("bgm", "bgm.wav")
    meta_path = ws.path("bgm", "bgm.json")
    if out.is_file() and meta_path.is_file() and not force:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["reused"] = True
        print(f"  已有 BGM：{out}（--force 重做）")
        return meta

    seconds = resolve_duration(ws, config, duration)
    text, title = collect_text(ws)
    if prompt:
        plan = PromptPlan(
            prompt=prompt,
            style=FALLBACK_STYLE.as_dict(),
            source="manual",
            text_chars=len(text),
        )
    else:
        plan = build_prompt(text, config, use_llm=use_llm, duration=seconds)

    use_seed = DEFAULT_SEED if seed is None else int(seed)
    # 优先级：CLI → [bgm] 段 → 模型卡默认值
    use_steps = int(_num(config, "steps", DEFAULT_STEPS)) if steps is None else int(steps)
    use_cfg = _num(config, "cfg_scale", DEFAULT_CFG_SCALE) if cfg_scale is None else float(cfg_scale)
    use_sampler = str(config.get("bgm.sampler", "") or "").strip() or DEFAULT_SAMPLER

    print(f"  文案：{ws.path('shots.json')}（{title or '无标题'}，{len(text)} 字）")
    print(f"  风格：{plan.describe()}")
    print(f"  提示词（{plan.source}）：{plan.prompt}")
    for warning in plan.warnings:
        print(f"  ⚠ {warning}")
    print(f"  生成中：{seconds:.0f}s / seed {use_seed} / steps {use_steps} / cfg {use_cfg}"
          f" / {use_sampler} / {device}（CPU 上按分钟计）…")

    model, model_config = load_model(config, device=device)
    audio, sample_rate = generate_audio(
        model, model_config,
        prompt=plan.prompt, duration=seconds,
        seed=use_seed, steps=use_steps, cfg_scale=use_cfg,
        sampler_type=use_sampler, device=device,
    )
    write_wav(out, audio, sample_rate)

    meta: dict[str, Any] = {
        "task": ws.task,
        "created_at": _now(),
        "model": model_id(config),
        "model_dir": str(model_dir(config)),
        "device": device,
        "prompt": plan.prompt,
        "prompt_source": plan.source,
        "style": plan.style,
        "keywords": list(plan.keywords),
        "warnings": list(plan.warnings),
        "duration_s": round(float(seconds), 3),
        "seed": use_seed,
        "steps": use_steps,
        "cfg_scale": use_cfg,
        "sampler": use_sampler,
        "sample_rate": int(sample_rate),
        "wav": "bgm/bgm.wav",
        "meta": "bgm/bgm.json",
        "source_chars": len(text),
        "reused": False,
        "license": dict(LICENSE_INFO),
    }
    write_meta(meta_path, meta)
    print(f"  产物：{out}")
    print(f"  元信息：{meta_path}")
    print(f"  许可：{LICENSE_INFO['name']} —— {LICENSE_INFO['commercial_use']}；"
          f"{LICENSE_INFO['output_ownership']}")

    try:
        ws.mark_stage("bgm", outputs=[out, meta_path], status="done",
                      prompt_source=plan.source, style=plan.style.get("name"))
    except Exception as exc:  # noqa: BLE001 - 记账是增强，不该让生成失败
        print(f"  [注意] manifest 记账失败（不影响产物）：{exc}")
    return meta


# ---- ② 混音：压低 + 闪避 --------------------------------------------------


def _num(config: Config, key: str, default: float) -> float:
    try:
        return float(config.get(f"bgm.{key}", default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class MixParams:
    """混音参数。默认值刻意保守：**旁白不可被盖住**优先于"BGM 好听"。"""

    volume_db: float = -16.0      # BGM 基础音量（负值 = 压低）
    duck: bool = True             # 是否开启 sidechain 闪避
    duck_threshold: float = 0.03  # 线性幅度阈值（≈ -30 dBFS），旁白一过线就开始压
    duck_ratio: float = 8.0
    duck_attack: float = 20.0
    duck_release: float = 400.0
    duck_makeup: float = 1.0
    detection: str = "rms"        # rms 比 peak 平滑，不会因一个辅音就抖
    fade_in: float = 2.0
    fade_out: float = 3.0
    limiter: float = 0.98

    def as_dict(self) -> dict[str, Any]:
        return {
            "volume_db": self.volume_db,
            "duck": self.duck,
            "duck_threshold": self.duck_threshold,
            "duck_ratio": self.duck_ratio,
            "duck_attack": self.duck_attack,
            "duck_release": self.duck_release,
            "duck_makeup": self.duck_makeup,
            "detection": self.detection,
            "fade_in": self.fade_in,
            "fade_out": self.fade_out,
            "limiter": self.limiter,
        }


def mix_params(config: Config, **overrides: Any) -> MixParams:
    """从 `[bgm]` 段读混音参数，`overrides` 里的非 None 值优先（CLI 用）。"""
    base = MixParams(
        volume_db=_num(config, "volume_db", MixParams.volume_db),
        duck=config.get("bgm.duck", MixParams.duck) is not False,
        duck_threshold=_num(config, "duck_threshold", MixParams.duck_threshold),
        duck_ratio=_num(config, "duck_ratio", MixParams.duck_ratio),
        duck_attack=_num(config, "duck_attack", MixParams.duck_attack),
        duck_release=_num(config, "duck_release", MixParams.duck_release),
        duck_makeup=_num(config, "duck_makeup", MixParams.duck_makeup),
        detection=str(config.get("bgm.detection", MixParams.detection) or MixParams.detection),
        fade_in=_num(config, "fade_in", MixParams.fade_in),
        fade_out=_num(config, "fade_out", MixParams.fade_out),
        limiter=_num(config, "limiter", MixParams.limiter),
    )
    clean = {key: value for key, value in overrides.items() if value is not None}
    return replace(base, **clean) if clean else base


def mix_filter(params: MixParams, *, total: float) -> str:
    """混音滤镜串：BGM 压低 + 淡入淡出 → 以旁白为 key 闪避 → 与原音轨混合。

    三个"为什么"：
    - `volume=-16dB` 而不是线性 0.25：dB 是调音台的语言，-16 dB 一眼知道"压了多少"；
    - `sidechaincompress` 的**第一个输入是要被压的音轨、第二个是 key**（旁白）——
      人一开口，BGM 就被按 `ratio` 压下去；
    - `amix=normalize=0` **必须**：ffmpeg ≥4.4 默认各除输入数，会把旁白砍半
      （那是"加了 BGM 旁白变小"的经典事故，见 `build._finalize` 的同款注释）。
    """
    chain = [f"volume={params.volume_db:.2f}dB"]
    if params.fade_in > 0:
        chain.append(f"afade=t=in:d={params.fade_in:.2f}")
    if params.fade_out > 0 and total > params.fade_out:
        chain.append(f"afade=t=out:st={total - params.fade_out:.2f}:d={params.fade_out:.2f}")
    parts = [f"[1:a]{','.join(chain)}[bg]"]
    key = "[bg]"
    if params.duck:
        parts.append(
            f"[bg][0:a]sidechaincompress="
            f"threshold={params.duck_threshold:.3f}:ratio={params.duck_ratio:g}"
            f":attack={params.duck_attack:g}:release={params.duck_release:g}"
            f":makeup={params.duck_makeup:g}:detection={params.detection}[ducked]"
        )
        key = "[ducked]"
    parts.append(
        f"[0:a]{key}amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mixed]"
    )
    parts.append(f"[mixed]alimiter=limit={params.limiter:.2f}[a]")
    return ";".join(parts)


def mix_command(
    ffmpeg: str,
    video: Path,
    bgm: Path,
    out: Path,
    *,
    params: MixParams,
    total: float,
) -> list[str]:
    """`lvs bgm mix` 的 ffmpeg 命令（**纯函数**：测试直接断言参数，不真跑）。"""
    return [
        ffmpeg, "-nostdin", "-y",
        "-i", str(video),
        "-stream_loop", "-1", "-i", str(bgm),   # BGM 短于成片时循环铺满
        "-filter_complex", mix_filter(params, total=total),
        "-map", "0:v",                           # 画面原样 copy（字幕已烧在里面）
        "-map", "[a]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", AUDIO_BR, "-ar", str(AUDIO_AR),
        "-shortest",                             # 成片多长就多长，别被循环的 BGM 拉长
        "-movflags", "+faststart",
        "-f", "mp4",
        str(out),
    ]


def _run_to_part(args: list[str], part: Path, out: Path, *, cwd: Path | None = None) -> Path:
    """跑 ffmpeg 写 `.part`，成功后原子归位；失败清掉半成品再抛（同 build.py 的约定）。"""
    try:
        ff_run(args, cwd=cwd)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    if not part.is_file():
        raise FFmpegError(f"ffmpeg 返回成功但没有产物：{part}")
    artifact.commit_file(part, out)
    return out


def mix(
    ws: Workspace,
    config: Config,
    *,
    video: Path | None = None,
    out: Path | None = None,
    params: MixParams | None = None,
) -> Path:
    """把 `bgm/bgm.wav` 混进成片 → `bgm_<原片名>`（**原片保留**，绝不覆盖）。"""
    source = Path(video) if video else ws.path("final.mp4")
    if not source.is_file():
        raise BgmUsageError(
            f"成片不存在：{source}\n  先跑：lvs build --task {ws.task} --config <cfg>"
        )
    bed = ws.path("bgm", "bgm.wav")
    if not bed.is_file():
        raise BgmUsageError(
            f"还没有 BGM：{bed}\n  先跑：lvs bgm --task {ws.task} --config <cfg>"
        )
    resolved = MixParams() if params is None else params
    target = Path(out) if out else ws.path(f"bgm_{source.name}")
    if target.resolve() == source.resolve():
        raise BgmUsageError(f"输出不能覆盖原片：{target}")

    ffmpeg, _ = tools()
    total = ff_duration(source) or 0.0
    part = target.with_name(target.name + ".part")
    print(f"  混音：{source.name} + {bed.name} → {target.name}")
    print(
        f"  BGM 音量 {resolved.volume_db:.1f} dB"
        + (
            f"，闪避 阈值 {resolved.duck_threshold:.3f} / 比 {resolved.duck_ratio:g}"
            f" / attack {resolved.duck_attack:g}ms / release {resolved.duck_release:g}ms"
            if resolved.duck
            else "，未开闪避（--no-duck）"
        )
    )
    _run_to_part(
        mix_command(ffmpeg, source, bed, part, params=resolved, total=total),
        part, target, cwd=ws.dir,
    )
    print(f"  成片：{target}（原片保留未动）")
    try:
        ws.mark_stage("bgm_mix", outputs=[target], status="done",
                      volume_db=resolved.volume_db, duck=resolved.duck)
    except Exception as exc:  # noqa: BLE001 - 记账是增强
        print(f"  [注意] manifest 记账失败（不影响产物）：{exc}")
    return target


# ---- CLI -------------------------------------------------------------------


def _print_plan(ws: Workspace, config: Config, *, duration: float | None, use_llm: bool) -> int:
    """`lvs bgm prompt`：只出提示词与风格，**不加载模型**（秒出，便于调风格）。"""
    seconds = resolve_duration(ws, config, duration)
    text, title = collect_text(ws)
    plan = build_prompt(text, config, use_llm=use_llm, duration=seconds)
    print(f"  文案：{ws.path('shots.json')}（{title or '无标题'}，{len(text)} 字）")
    print(f"  时长：{seconds:.0f}s")
    print(f"  来源：{plan.source}")
    print(f"  风格：{plan.describe()}")
    print(f"  提示词：{plan.prompt}")
    for warning in plan.warnings:
        print(f"  ⚠ {warning}")
    return 0


def run_command(config: Config, ws: Workspace, args) -> int:  # noqa: ANN001 - 由 cli 传入
    """`lvs bgm [generate|download|prompt|mix]` 的入口。"""
    action = getattr(args, "action", None) or "generate"
    try:
        if action == "download":
            download(config, force=bool(getattr(args, "force", False)))
            return 0
        if action == "prompt":
            return _print_plan(
                ws, config,
                duration=getattr(args, "duration", None),
                use_llm=not getattr(args, "no_llm", False),
            )
        if action == "mix":
            params = mix_params(
                config,
                volume_db=getattr(args, "volume_db", None),
                duck=False if getattr(args, "no_duck", False) else None,
                duck_threshold=getattr(args, "duck_threshold", None),
                duck_ratio=getattr(args, "duck_ratio", None),
            )
            video = getattr(args, "video", None)
            out = getattr(args, "out", None)
            mix(
                ws, config,
                video=Path(video).expanduser() if video else None,
                out=Path(out).expanduser() if out else None,
                params=params,
            )
            return 0

        meta = generate(
            ws, config,
            duration=getattr(args, "duration", None),
            seed=getattr(args, "seed", None),
            steps=getattr(args, "steps", None),
            cfg_scale=getattr(args, "cfg_scale", None),
            prompt=getattr(args, "prompt", None),
            use_llm=not getattr(args, "no_llm", False),
            force=bool(getattr(args, "force", False)),
        )
        if getattr(args, "json", False):
            print(json.dumps(meta, ensure_ascii=False))
        return 0
    except (BgmError, BgmUsageError, FFmpegError) as exc:
        print(f"BGM 失败：{exc}")
        return int(getattr(exc, "exit_code", EXIT_FAILED))
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001 - 未知异常也要人话，不要 traceback
        print(f"BGM 失败：{type(exc).__name__}: {exc}")
        return EXIT_FAILED


#: 供 `lvs bgm --help` 用的动作说明（cli 注册时复用，免得两处措辞漂移）。
ACTION_HELP = {
    "generate": "（默认）按讲稿风格生成 BGM → .work/<task>/bgm/bgm.wav + bgm.json（CPU / steps 8）",
    "download": "只下载模型权重到 models/stable-audio-3-small-music（走 ModelScope 国内镜像，非门控）",
    "prompt": "只打印会用的提示词与风格（**不加载模型**，秒出）",
    "mix": "把 BGM 压低 + 闪避混进成片 → bgm_<原片名>（原片保留）",
}