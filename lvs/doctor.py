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

from lvs import __version__
from lvs.config import Config

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
    exe = shutil.which("ffmpeg")
    if not exe:
        return CheckResult("ffmpeg", MISSING, "未在 PATH 中找到 ffmpeg", hint=FFMPEG_HINT)
    ok, out = _run(["ffmpeg", "-version"])
    if not ok:
        return CheckResult("ffmpeg", WARN, f"找到 {exe} 但执行失败", hint=FFMPEG_HINT)
    first_line = out.splitlines()[0] if out else "未知版本"
    return CheckResult("ffmpeg", PASS, f"{first_line}\n        路径：{exe}")


def check_ffprobe() -> CheckResult:
    exe = shutil.which("ffprobe")
    if not exe:
        return CheckResult(
            "ffprobe", WARN, "未找到（素材库索引将降级为「仅文件名标签」）"
        )
    ok, out = _run(["ffprobe", "-version"])
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
    return CheckResult("TTS 后端", WARN, f"未知后端 `{backend}`（应为 edge 或 openai_speech）")


def check_comfyui(config: Config) -> CheckResult:
    base = config.get("comfyui.base_url", "http://127.0.0.1:8188")
    alive = _probe_http(f"{base.rstrip('/')}/system_stats")
    if alive:
        return CheckResult("ComfyUI", PASS, f"在线：{base}")
    return CheckResult(
        "ComfyUI", WARN, f"未启动：{base}（二期 local 生图才需要）",
        hint="二期本地生图前，先启动 ComfyUI 并确认监听该端口",
    )


def check_library(config: Config) -> CheckResult:
    dirs = config.get("library.dirs", []) or []
    if not dirs:
        return CheckResult(
            "本地素材库", WARN, "未配置目录（「翻库优先」关闭，不影响流程）",
            hint="在 config.toml 的 [library] 段填 dirs，例如 dirs = ['D:\\\\素材库']",
        )
    existing = [d for d in dirs if d]
    return CheckResult("本地素材库", PASS, f"已配置 {len(existing)} 个目录：" + "；".join(existing))


def _probe_http(url: str, timeout: float = 1.5) -> bool:
    """探测一个本地 HTTP 服务是否在线；任何异常都视为不在线。"""
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 500
    except Exception:
        return False


# ---- 主流程 ----------------------------------------------------------------


def run(config: Config, as_json: bool = False) -> int:
    """跑全部体检，打印报告。返回 0 = 无"缺失"；1 = 有必填项缺失。"""
    checks = [
        check_python(),
        check_ffmpeg(),
        check_ffprobe(),
        check_gpu(),
        check_llm(config),
        check_pexels(config),
        check_tts(config),
        check_comfyui(config),
        check_library(config),
    ]

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
