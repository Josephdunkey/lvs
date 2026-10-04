"""`lvs doctor` —— 新机器上的环境体检。

体检项：Python / ffmpeg / ffprobe / NVIDIA 驱动与显存 / LLM key / Pexels key /
TTS 后端 / ComfyUI / 本地素材库。

原则（票据 01）：
- 逐项打印 通过 / 缺失 / 警告
- **静默降级**：任何一项检测抛异常都不崩溃、也**不误报通过**（降级为"警告"）
- ffmpeg 缺失时给出 Windows 上可直接照做的安装步骤
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from lvs import __version__
from lvs import net
from lvs.config import PROJECT_ROOT, Config

PASS = "通过"
MISSING = "缺失"
WARN = "警告"

FFMPEG_HINT = (
    "Windows 安装 ffmpeg（任选其一，装完请重开终端）：\n"
    "A) winget：winget install Gyan.FFmpeg\n"
    "B) 手动：下载 https://www.gyan.dev/ffmpeg/builds/ 的 release-full，"
    "解压后把 bin 目录加入 PATH\n"
    "验证：ffmpeg -version"
)


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str
    hint: str = ""


def _run(cmd: list[str], timeout: int = 10) -> tuple[bool, str]:
    """跑一条命令，返回 (是否成功, 合并输出)。任何异常都吞掉并视为失败。"""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output.strip()


# ---- 各项检查 --------------------------------------------------------------


def check_python() -> CheckResult:
    info = sys.version_info
    version = platform.python_version()
    if info < (3, 10):
        return CheckResult(
            "Python", MISSING, f"{version}（需要 ≥ 3.10）",
            hint="安装 Python 3.11+：https://www.python.org/downloads/",
        )
    return CheckResult("Python", PASS, f"{version}  ({sys.executable})")


def check_ffmpeg() -> CheckResult:
    from lvs import ffmpeg as ff

    try:
        exe, _ = ff.tools()
    except ff.FFmpegError:
        return CheckResult("ffmpeg", MISSING, "未找到 ffmpeg", hint=FFMPEG_HINT)
    ok, out = _run([exe, "-version"])
    if not ok:
        return CheckResult("ffmpeg", WARN, f"找到 {exe} 但执行失败", hint=FFMPEG_HINT)
    first_line = out.splitlines()[0] if out else "未知版本"
    return CheckResult("ffmpeg", PASS, f"{first_line}\n        路径：{exe}")


def check_ffprobe() -> CheckResult:
    from lvs import ffmpeg as ff

    try:
        _, exe = ff.tools()
    except ff.FFmpegError:
        exe = ""
    if not exe:
        return CheckResult(
            "ffprobe", WARN,
            "未找到（时长/尺寸改由 `ffmpeg -i` 降级解析，精度足够）"
        )
    ok, out = _run([exe, "-version"])
    version = (out.splitlines()[0] if ok and out else "未知版本")
    return CheckResult("ffprobe", PASS, version)


def check_gpu() -> CheckResult:
    if not shutil.which("nvidia-smi"):
        return CheckResult("GPU", WARN, "未找到 nvidia-smi（无 NVIDIA 显卡或驱动未装）")
    ok, out = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,driver_version,memory.total,memory.used,memory.free",
            "--format=csv,noheader",
        ]
    )
    if not ok or not out.strip():
        return CheckResult("GPU", WARN, "nvidia-smi 执行失败（无法读取显存）")
    line = out.splitlines()[0]
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 5:
        return CheckResult("GPU", WARN, f"nvidia-smi 输出无法解析：{line}")
    name, driver, total, used, free = parts[:5]
    detail = f"{name} | 驱动 {driver} | 显存 {free} 可用 / {total} 总（已用 {used}）"
    return CheckResult(
        "GPU", PASS, detail,
        hint="8 GB 显存需按 spec §11 串行使用：生图与本地 TTS **不可同时驻留**",
    )


def check_llm(config: Config) -> CheckResult:
    if config.has("app.openai_api_key"):
        provider = config.get("app.llm_provider", "openai")
        model = config.get("app.openai_model_name", "")
        base = config.get("app.openai_base_url", "")
        return CheckResult("LLM key", PASS, f"已配置（{provider} / {model} / {base}）")
    return CheckResult(
        "LLM key", MISSING, "`app.openai_api_key` 未填写",
        hint="在 config.toml 的 [app] 段填写 openai_api_key（拆镜阶段必需）",
    )


def check_pexels(config: Config) -> CheckResult:
    if config.has("pexels.api_key"):
        return CheckResult("Pexels key", PASS, "已配置")
    return CheckResult(
        "Pexels key", WARN, "未配置（仅影响 source=pexels 的分镜）",
        hint="在 config.toml 的 [pexels] 段填写 api_key",
    )


def check_tts(config: Config) -> CheckResult:
    backend = config.get("tts.backend", "edge")
    if backend == "edge":
        return CheckResult("TTS 后端", PASS, f"edge（音色 {config.get('tts.voice', '默认')}）")
    if backend == "openai_speech":
        base = config.get("tts.base_url", "")
        return CheckResult("TTS 后端", PASS, f"openai_speech → {base}（二期，需自行启动服务）")
    if backend == "silent":
        # ★ 离线后端：不联网、不吃显存 —— 体检不报"注意"，但要说明它是**静音**的，
        #   免得有人以为成片会有声音。它主要供离线端到端测试与"先走通骨架"用。
        return CheckResult(
            "TTS 后端", PASS,
            "silent（**静音**占位轨，仅用于离线跑通骨架；成片不会有声音）",
        )
    from lvs.tts import SUPPORTED_BACKENDS

    return CheckResult(
        "TTS 后端", WARN,
        f"未知后端 `{backend}`（应为 {' / '.join(SUPPORTED_BACKENDS)}）",
    )


# ComfyUI 的模型可能落在这些子目录里（UNET 在 diffusion_models，SDXL 在 checkpoints，…）
_MODEL_SUBDIRS = (
    "diffusion_models", "text_encoders", "vae",
    "checkpoints", "loras", "unet", "clip", "clip_vision",
)


def _comfyui_models_dir(config: Config) -> Path | None:
    """定位 ComfyUI 的 `models/` 目录：优先 config `[comfyui].models_dir`，
    否则读 `models.lock.json` 的 `comfyui_dir`。定位不到返回 None（降级，不报错）。"""
    explicit = config.get("comfyui.models_dir")
    if explicit:
        return Path(explicit)
    try:
        lock = json.loads((PROJECT_ROOT / "models.lock.json").read_text(encoding="utf-8"))
        root = lock.get("comfyui_dir")
        if root:
            return Path(root) / "models"
    except Exception:
        pass
    return None


def _find_model(models_root: Path, filename: str) -> Path | None:
    """在 ComfyUI 的若干模型子目录里找文件；< 1MB 视为没下完/.part 残留。"""
    for sub in _MODEL_SUBDIRS:
        cand = models_root / sub / filename
        try:
            if cand.is_file() and cand.stat().st_size > 1_000_000:
                return cand
        except OSError:
            continue
    return None


def check_comfyui(config: Config) -> CheckResult:
    base = config.get("comfyui.base_url", "http://127.0.0.1:8188")
    alive = _probe_http(f"{base.rstrip('/')}/system_stats")

    # 模型文件核对：与 ComfyUI 在不在线无关，都值得查一次
    wanted = [
        n
        for n in (
            config.get("comfyui.model", "z_image_turbo_int8_convrot.safetensors"),
            config.get("comfyui.clip", "qwen_3_4b_fp8_mixed.safetensors"),
            config.get("comfyui.vae", "ae.safetensors"),
        )
        if n
    ]
    models_root = _comfyui_models_dir(config)
    missing: list[str] = []
    found = 0
    if models_root and models_root.is_dir():
        for name in wanted:
            if _find_model(models_root, name):
                found += 1
            else:
                missing.append(name)

    if not alive:
        return CheckResult(
            "ComfyUI", WARN, f"未启动：{base}（本地生图的 local 分支才需要）",
            hint=r"启动：scripts\start_comfyui.cmd（显存吃紧加 --lowvram）",
        )
    if missing:
        return CheckResult(
            "ComfyUI", WARN,
            f"在线：{base}，但缺 {len(missing)} 个模型文件：" + "、".join(missing),
            hint=r"拉模型：.venv\Scripts\python.exe scripts\fetch_models.py",
        )
    tail = f"，模型文件齐（{found}/{len(wanted)}）" if models_root else "（未定位到 models/ 目录，跳过模型核对）"
    return CheckResult("ComfyUI", PASS, f"在线：{base}{tail}")


def check_library(config: Config) -> CheckResult:
    dirs = config.get("library.dirs", []) or []
    if not dirs:
        return CheckResult(
            "本地素材库", WARN, "未配置目录（「翻库优先」关闭，不影响流程）",
            hint="在 config.toml 的 [library] 段填 dirs，例如 dirs = ['D:\\\\素材库']",
        )
    existing = [d for d in dirs if d]
    return CheckResult("本地素材库", PASS, f"已配置 {len(existing)} 个目录：" + "；".join(existing))


def check_paths_lib(config: Config) -> CheckResult:
    """`[paths].lib` —— 多项目化的核心。配了它，定妆库 / 定妆卡 / 项目风格文件
    全部自动派生；不配则退回仓库根 `cast/`（老行为，但"多本书"时定妆库会串）。

    ★ 配了但目录不存在是**最危险的静默失效**：`cast_dir` 会一路派生到
    `<lib>/_cast`，而那个目录不存在时定妆闸门读不到锁、锚定不注入，不报错。
    所以这里把"配了但不存在"降为**警告**，而不是只查"配没配"。
    """
    from lvs import cast as cast_mod
    from lvs.config import lib_dir

    lib = lib_dir(config)
    if not lib:
        return CheckResult(
            "素材库根 [paths].lib", WARN,
            "未配置（定妆库退回仓库根 cast/；多本书时建议配置）",
            hint="在 config.toml 的 [paths] 段填 lib = \"<素材库路径>\"，"
                 "或用 `lvs init \"<素材库路径>\" --name <名>` 一键接入",
        )

    problems = []
    if not Path(lib).is_dir():
        problems.append(f"路径不存在：{lib}")
    else:
        derived = cast_mod.cast_dir(config)
        if not derived.is_dir():
            problems.append(f"定妆库尚未生成：{derived}（跑 `lvs cast` 会建）")
        card = cast_mod.find_card(config)
        if card is None:
            problems.append("`00-设定/` 下没找到定妆卡（人物锚定的唯一来源）")

    if problems:
        return CheckResult(
            "素材库根 [paths].lib", WARN, "；".join(problems),
            hint="确认 lib 路径正确；定妆卡与锚定是人物一致性的来源，缺了会变脸",
        )
    return CheckResult("素材库根 [paths].lib", PASS, f"已配置：{lib}")


def _probe_http(url: str, timeout: float = 1.5) -> bool:
    """探测一个本地 HTTP 服务是否在线；任何异常都视为不在线。

    ★ 走 `net.urlopen`：**必须绕开系统代理**，否则开了 VPN 时本地服务会被误判为不在线。
    """
    try:
        with net.urlopen(url, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 500
    except Exception:
        return False


# ---- 主流程 ----------------------------------------------------------------


def all_checks(config: Config) -> list[CheckResult]:
    """体检项清单 —— 只在这里定义一次（CLI 与 GUI 共用，免得两处漂移）。"""
    return [
        check_python(),
        check_ffmpeg(),
        check_ffprobe(),
        check_gpu(),
        check_llm(config),
        check_pexels(config),
        check_tts(config),
        check_comfyui(config),
        check_library(config),
        check_paths_lib(config),
        check_source_mode(config),
        check_image_granularity(config),
        check_cast_approval(config),
        check_face_model(config),
    ]


def check_source_mode(config: Config) -> CheckResult:
    """★ **配置自相矛盾检查**：素材来源策略与「有没有对应的钥匙」是否匹配。

    真踩过：`[shots].source_mode = "auto"` + `[pexels].api_key` 为空 ——
    `auto` 会把一部分镜判给 Pexels，而那些镜必然取不到素材。
    实测 13 镜里 8 镜本该出图，因为 5 镜要 Pexels 而**整批受影响**、
    用户翻遍日志也不知道问题出在哪（他以为自己在用本地生图）。

    这条检查的价值是**在花钱之前**就把它指出来 —— 而不是等跑到 assets 才发现。
    """
    mode = str(config.get("shots.source_mode", "auto") or "auto").strip().lower()
    key = str(config.get("pexels.api_key", "") or "").strip()
    dirs = [d for d in (config.get("library.dirs", []) or []) if d]

    if mode == "auto" and not key:
        hints = ['在 config.toml 的 [pexels] 段填 api_key（https://www.pexels.com/api/ 免费）']
        if dirs:
            hints.append('或把 [shots].source_mode 设为 "library"（用你的本地素材库）')
        hints.append('或把 [shots].source_mode 设为 "local"（一律本地生图，需 ComfyUI）')
        return CheckResult(
            "素材来源策略", WARN,
            "source_mode = auto，但 [pexels].api_key 为空 —— "
            "被判给 Pexels 的分镜会取不到素材（其余分镜不受影响）",
            hint="；".join(hints),
        )
    if mode == "pexels" and not key:
        return CheckResult(
            "素材来源策略", MISSING,
            "source_mode = pexels，但 [pexels].api_key 为空 —— 所有实拍镜都会失败",
            hint="填 [pexels].api_key，或把 source_mode 改成 local / library。",
        )
    if mode == "library" and not dirs:
        return CheckResult(
            "素材来源策略", MISSING,
            "source_mode = library，但 [library].dirs 是空的 —— 没有可翻的库，会回退去本地生图",
            hint="填 [library].dirs，或把 source_mode 改成 local。",
        )
    if mode == "local":
        base = config.get("comfyui.base_url", "http://127.0.0.1:8188")
        return CheckResult(
            "素材来源策略", PASS,
            f"source_mode = local（一律本地生图；需要 ComfyUI 在 {base} 运行）",
        )
    return CheckResult("素材来源策略", PASS, f"source_mode = {mode}")


def check_face_model(config: Config) -> CheckResult:
    """人脸检测器能否加载 —— 它决定 `qc` 的 Q006/Q007 跑不跑。

    ★ 实测踩到：`[qc].face_model` 指着一个**结构是 YuNet、但权重退化**的 onnx
    （`obj` 输出恒为 0，`cls` 对纯黑图与人像给出几乎相同的值）。
    后果是「空镜长人」—— 项目注释里写着这是"Z-Image 最顽固的失败模式" ——
    **一次都没真正跑过**，而巡检报告看起来一切正常。

    判据与 `qc` **共用** `qc.face_model_status`，免得"体检说没问题、巡检却跳过了"。
    """
    if not bool(config.get("qc.enable_faces", True)):
        return CheckResult(
            "人脸检测", PASS, "已关闭（`[qc].enable_faces = false`）—— Q006/Q007 不跑"
        )
    from lvs import qc as qc_mod

    path = config.get("qc.face_model")
    ok, why = qc_mod.face_model_status(Path(str(path)).expanduser() if path else None)
    if not ok:
        return CheckResult(
            "人脸检测", WARN,
            f"**不可用**：{why}",
            hint=(
                "影响：`lvs qc` 的 Q006（空镜里长人）/ Q007（人物镜没脸）不会运行 —— "
                "这两条是「空镜长人」唯一的自动判据。"
                "想修：放一个可用的 YuNet onnx 到 `models/yunet.onnx`（或改 `[qc].face_model`）；"
                "确不需要就设 `[qc].enable_faces = false`。"
            ),
        )
    return CheckResult("人脸检测", PASS, f"可用（{why}）")


def check_cast_approval(config: Config) -> CheckResult:
    """定妆库：**有候选图、却一个都没批准** → 明确提醒。

    ★ 实测踩到：5 个角色的候选图与审阅表都出好了（`_审阅表.png` 也在），
    但 `lock.json` 里全是 `state=IMG` → `bindable=False`。后果：
    含 `{NAME}` 的稿子会被 G2 拦下，而用户**完全不知道只差"看一眼审阅表再批"这一步**。
    """
    try:
        from lvs import cast as cast_mod

        root = cast_mod.cast_dir(config)
        if not root.is_dir():
            return CheckResult("定妆批准", PASS, "未接入定妆库（本集不需要人物一致性）")
        lock = cast_mod.load_lock(root)
    except Exception as exc:  # noqa: BLE001 - 定妆库坏掉不该让体检整个失败
        return CheckResult("定妆批准", WARN, f"定妆库读不了：{exc}")

    chars = getattr(lock, "characters", {}) or {}
    if not chars:
        return CheckResult("定妆批准", PASS, "定妆库为空（本集不需要人物一致性）")

    approved = [n for n, e in chars.items() if getattr(e, "bindable", False)]
    pending: list[tuple[str, int, str]] = []
    for name in chars:
        d = root / name
        if not d.is_dir():
            continue
        cands = sorted(d.glob("v*/cand-*.png"))
        if cands:
            pending.append((name, len(cands), sorted({p.parent.name for p in cands})[-1]))

    if pending and not approved:
        detail = "、".join(f"{n}（{c} 张，{v}）" for n, c, v in pending)
        sheet = root / "_审阅表.png"
        steps = [
            f"先看审阅表：{sheet}" if sheet.is_file() else "先出审阅表：`lvs cast --sheet`",
            "然后 `lvs cast --approve <名字>`（会把该轮候选**全部**冻结为参考图；"
            "只想留一张就加 `--from-file <图片路径>`）",
        ]
        return CheckResult(
            "定妆批准", WARN,
            f"{len(pending)} 个角色**有候选图但一个都没批准** —— 人物一致性不会生效。{detail}",
            hint="；".join(steps),
        )
    if pending:
        return CheckResult(
            "定妆批准", PASS,
            f"{len(approved)} 个已批准；另有 {len(pending)} 个有候选未批准"
            f"（{'、'.join(n for n, _, _ in pending)}）",
        )
    if approved:
        return CheckResult("定妆批准", PASS, f"{len(approved)} 个角色已批准并冻结参考图")
    return CheckResult(
        "定妆批准", PASS,
        f"{len(chars)} 个角色已登记但还没有候选图（先 `lvs cast --render ALL`）",
    )


def check_image_granularity(config: Config) -> CheckResult:
    """`[shots].image_granularity` 定了没有 —— 它**没有默认值**。

    ★ 为什么值得体检：这个键决定"同一画面位的多个镜是各出一张图还是共用一张"，
    代价差 3 倍算力、观感也不同，所以**故意不给默认值**，
    由 `lvs assets` / `lvs run` 在真有分歧时停下问人（退出码 2）。

    但"停下问"发生在**跑到那一步**的时候 —— 如果前面还有几百秒的拆镜，
    那就白等了。doctor 提前报出来，可以在开跑前就填好。
    """
    from lvs import assets as assets_mod

    value = assets_mod.image_granularity(config)
    if value is None:
        return CheckResult(
            "图粒度", WARN,
            "`[shots].image_granularity` 没填（**没有默认值**，故意如此）",
            hint=(
                "它决定「同一画面位的多个镜，是各出一张图还是共用一张」，"
                "代价差约 3 倍算力、观感也不同。填一个即可："
                '`image_granularity = "shot"`（每镜一张，保留景别推进）／'
                '`image_granularity = "beat"`（同画面位共用一张，省算力）。'
                "不填也行 —— 但 `lvs assets`/`lvs run` 遇到真有分歧时会停下问你（退出码 2）。"
            ),
        )
    if value not in assets_mod.GRANULARITIES:
        return CheckResult(
            "图粒度", WARN,
            f"`{value}` 不认识（只接受 {' / '.join(assets_mod.GRANULARITIES)}）",
        )
    return CheckResult("图粒度", PASS, f"{value}（{'每镜一张' if value == 'shot' else '同一画面位共用一张'}）")


def run(config: Config, as_json: bool = False) -> int:
    """跑全部体检，打印报告。返回 0 = 无"缺失"；1 = 有必填项缺失。"""
    checks = all_checks(config)

    if as_json:
        print(json.dumps([asdict(c) for c in checks], ensure_ascii=False, indent=2))
    else:
        _print_report(config, checks)

    return 1 if any(c.status == MISSING for c in checks) else 0


def _print_report(config: Config, checks: list[CheckResult]) -> None:
    print(f"LongVideoStudio 环境体检  (lvs {__version__})")
    if config.path:
        print(f"配置文件：{config.path}")
    else:
        print("配置文件：未找到（仅影响需要密钥的阶段；复制 config.example.toml 为 config.toml 即可）")
    print("-" * 60)
    for c in checks:
        print(f"[{c.status}] {c.name}：{c.detail}")
        if c.hint:
            for line in c.hint.splitlines():
                print(f"        ↳ {line}")
    print("-" * 60)

    missing = [c.name for c in checks if c.status == MISSING]
    warned = [c.name for c in checks if c.status == WARN]
    if missing:
        print(f"有必填项缺失：{'、'.join(missing)} —— 请按上面的提示处理后重跑 `lvs doctor`。")
    elif warned:
        print(f"环境可用；以下为可选/待装项：{'、'.join(warned)}。")
    else:
        print("环境就绪 ✓")
