"""GPU 串行调度守卫（票据 17）。

依据 spec §11：本机 RTX 4060 Laptop 只有 **8 GB 显存**，而
- 本地生图（Z-Image / SDXL）约需 6–8 GB
- 本地 TTS（Qwen3-TTS 1.7B）单模型占 4.45 GB

两者**不能同时驻留**。冲突时**显式报错并说明停哪个服务**，绝不自动抢占、绝不静默降级。

## 为什么用"探测对端服务"而不是"看空闲显存"

最初用「空闲显存 ≥ 6000 MB」当判据，**实测踩坑**：ComfyUI 一旦加载过模型，就会常驻
6 GB 左右；此时空闲只剩 ~2.6 GB —— 但**这恰恰是正常且可用的状态**（ComfyUI 自己管显存，
768×768 出图实测正常）。结果第 2 个分镜起就被守卫拦下，270 个分镜全废。

所以判据改为**探测对端服务是否在跑**（精确、廉价、不误报）：

| 阶段 | 真正的风险 | 判据 |
|---|---|---|
| `imagegen` | 本地 TTS 服务占着显存 | 配了 `tts.backend=openai_speech` 且其 `/health` 可达 → 冲突 |
| `tts`（仅 `openai_speech`） | ComfyUI 占着显存 | `comfyui.base_url` 的 `/system_stats` 可达 → 冲突 |

`tts.backend=edge` 是联网合成、**不吃本地显存**，因此 `lvs voice` 不做任何拦截。

显存（`nvidia-smi`）只用于**报告**与低余量**告警**，不再作为硬闸门。

可配置：
- `[gpu].skip_check = true`     换大显存卡 / 服务器环境时跳过
- `[gpu].need_imagegen_mb = …`  生图阶段**告警**阈值（不阻断）
- `[gpu].need_tts_mb = …`       本地 TTS 阶段**硬性**最低空闲显存
"""

from __future__ import annotations
from lvs.errors import UsageError

import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass

from lvs import net


class GPUConflict(UsageError, RuntimeError):
    """显存冲突。消息面向用户，必须给出"停哪个服务、怎么停"。

    归类为 `UsageError`（退出码 2）而不是 `BlockedError`（3）：
    它的解药是"**关掉另一个服务再来**"（前置条件不满足），
    不是"等人审批准"。人审门禁（`GateBlocked`）才是后者 ——
    把这两件事混成一个码，自动化脚本就没法区分"该等人"和"该修环境"。
    """


@dataclass
class Vram:
    name: str
    total_mb: int
    used_mb: int
    free_mb: int


def _run(cmd: list[str], timeout: int = 8) -> str:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout or ""


def vram() -> Vram | None:
    """读当前显存；无 nvidia-smi 或解析失败返回 None（静默降级）。"""
    if not shutil.which("nvidia-smi"):
        return None
    out = _run([
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.used,memory.free",
        "--format=csv,noheader,nounits",
    ])
    line = (out.strip().splitlines() or [""])[0]
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 4:
        return None
    try:
        return Vram(parts[0], int(parts[1]), int(parts[2]), int(parts[3]))
    except ValueError:
        return None


def processes() -> list[str]:
    """列出占显存的进程（"pid | 名称 | 显存"），供报错时让用户知道停谁。"""
    out = _run([
        "nvidia-smi",
        "--query-compute-apps=pid,process_name,used_memory",
        "--format=csv,noheader",
    ])
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _alive(url: str, timeout: float = 1.5) -> bool:
    """探测一个本地 HTTP 服务是否在监听；任何异常都视为不在(静默降级)。

    ★ 走 `net.urlopen`：**必须绕开系统代理** —— 否则用户开着 VPN/代理时，
    探测会被代理拦下，于是"服务明明在跑"却被判成"不可达"（实测踩过）。
    """
    try:
        with net.urlopen(url, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _peer_conflict(stage: str, config) -> tuple[str, str] | None:  # noqa: ANN001 - Config
    """返回 (对方服务名, 停它的提示)；无冲突返回 None。"""
    if stage == "imagegen":
        # 只有"指向本地 Qwen3-TTS 的 openai_speech 后端"才会本地驻留显存
        if config.get("tts.backend") != "openai_speech":
            return None
        base = config.get("tts.base_url")
        if base and _alive(str(base).rstrip("/") + "/health"):
            return (
                "本地 TTS 服务（Qwen3-TTS，约 4.45 GB）正在运行",
                "  请**停掉本地 TTS 服务**（关闭它的终端 / Ctrl-C）后重跑 `lvs assets`。",
            )
        return None

    # stage == "tts"
    if config.get("tts.backend") == "edge":
        return None  # edge 联网合成，不吃本地显存
    base = config.get("comfyui.base_url")
    if base and _alive(str(base).rstrip("/") + "/system_stats"):
        return (
            "ComfyUI（本地生图，模型常驻约 6 GB）正在运行",
            "  请**关掉 ComfyUI**（关闭它的窗口）后重跑 `lvs voice`。",
        )
    return None


def check(stage: str, config) -> str | None:  # noqa: ANN001 - Config
    """阶段入口检查。返回人类可读的说明；冲突时抛 `GPUConflict`。"""
    if bool(config.get("gpu.skip_check", False)):
        return "已配置跳过 GPU 检查（gpu.skip_check=true）"

    # ---- 1) 服务级互斥（精确判据，见模块 docstring）----
    peer = _peer_conflict(stage, config)
    if peer is not None:
        name, hint = peer
        procs = processes()
        detail = "\n".join(f"    {p}" for p in procs) or "    （未读到进程明细）"
        raise GPUConflict(
            f"GPU 串行冲突：{name}。\n"
            f"  8 GB 显存下生图与本地 TTS **不能同时驻留**（spec §11）。\n"
            f"  当前占用显存的进程：\n{detail}\n" + hint
        )

    # ---- 2) 显存报告 / 阈值 ----
    info = vram()

    if stage == "imagegen":
        # 生图阶段绝不用显存硬卡：ComfyUI 常驻后会自然压低"空闲"值，
        # 那不代表冲突（ComfyUI 自己按需装载/卸载）。只在余量很低时提醒。
        if info is None:
            return "未检测到 nvidia-smi，跳过显存检查（静默降级）"
        soft = int(config.get("gpu.need_imagegen_mb", 6000))
        note = f"显存：空闲 {info.free_mb} / {info.total_mb} MB（仅提示，不阻断）"
        if info.free_mb < soft:
            note += (
                f"；低于提示阈值 {soft} MB —— 若 ComfyUI 是自己常驻的则属正常，"
                "若出图报 OOM，请关掉其它占用显存的程序，或给 ComfyUI 加 --lowvram"
            )
        return note

    # ---- stage == "tts" ----
    if config.get("tts.backend") == "edge":
        # edge-tts 是云端合成，本地不驻留模型（词边界也由 edge 给，不需要 whisper）
        return "TTS 后端为 edge（云端合成，不占本地显存），跳过显存检查"

    # 本地 TTS 服务**已经就绪**时，模型显存已由该服务持有 ——
    # 此时"空闲显存低"正是服务在工作的表现，不是冲突。
    # （实测踩坑：服务已加载 1.7B 占 5.8 GB，空闲仅 2.3 GB，
    #   守卫按 need_tts_mb=4600 硬拦，把正常状态判成冲突。）
    if _tts_ready(config):
        return (
            "本地 TTS 服务已就绪（模型已驻留显存），跳过空闲显存检查。\n"
            "  服务负责自己的显存管理；`lvs voice` 只是通过 HTTP 调用它。"
        )

    if info is None:
        return "未检测到 nvidia-smi，跳过显存检查（静默降级）"

    need = int(config.get("gpu.need_tts_mb", 4600))
    if info.free_mb < need:
        procs = processes()
        detail = "\n".join(f"    {p}" for p in procs) or "    （未读到进程明细）"
        raise GPUConflict(
            f"显存不足：当前空闲 {info.free_mb} MB，需要约 {need} MB（{info.name}）。\n"
            f"  当前占用显存的进程：\n{detail}\n"
            "  请先释放显存（关掉 ComfyUI / 其它占显存的程序）后重跑 `lvs voice`。\n"
            "  若本地 TTS 服务**已在运行**却仍报此错，说明判据失灵 —— "
            "请检查 [tts].base_url 是否指向该服务。"
        )
    return f"显存检查通过：空闲 {info.free_mb} / {info.total_mb} MB（需要 {need} MB）"


def _tts_ready(config) -> bool:  # noqa: ANN001 - Config
    """本地 TTS 服务是否已加载模型就绪（探测 /health 的 model_loaded）。"""
    base = config.get("tts.base_url")
    if not base:
        return False
    try:
        import json as _json
        with net.urlopen(
            str(base).rstrip("/") + "/health", timeout=1.5
        ) as resp:
            if not (200 <= getattr(resp, "status", 200) < 500):
                return False
            body = _json.loads(resp.read().decode("utf-8", "replace"))
            return bool(body.get("model_loaded"))
    except Exception:
        return False
