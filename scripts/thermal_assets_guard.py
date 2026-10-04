"""控温守卫：出图跑到 GPU 过热就停，凉下来自动续跑。

背景：8GB 显存笔记本 + Z-Image Turbo 连续生图，GPU 会顶到 87°C+
（用户明确要求"搞一会停一会，注意控温"）。

出图任务（lvs assets）天生可断点续跑——已完成的镜会被 existing_asset
跳过——所以"杀掉重启"是安全的，最多损失当前正在生成的那一张。

行为：
  1. 先检查 nvidia-smi 可用（不可用就直接退出，绝不乱杀任务）
  2. 找到在跑的 lvs assets 进程并接管（杀掉，进入冷却）
  3. 冷却到 <= LO°C 后拉起出图任务
  4. 监控：温度 >= HI°C → 杀掉冷却 → 回到 3
  5. 任务自然退出：rc==0 视为完成（exit 0）；否则原样透传退出码
     （退出码 3 = 人审门禁，交给外层代理处理）

退出码：0 完成｜原样透传任务失败码｜2 环境不可用（无 nvidia-smi 等）
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import psutil

HI = 85           # >= 85°C 停
LO = 75           # 降到 <= 75°C 才续
POLL = 15         # 监控采样间隔（秒）
COOLDOWN_MAX = 30 * 60   # 冷却最多等 30 分钟，超了就续跑（别无限等）

REPO = Path(r"D:\LongVideoStudio")
LOG = REPO / "assets_full.log"
# 用法：python scripts/thermal_assets_guard.py [任务名] [配置文件]
#   缺省 UGE01 config.ugetsu.toml（保持旧行为）
_TASK = sys.argv[1] if len(sys.argv) > 1 else "UGE01"
_CFG = sys.argv[2] if len(sys.argv) > 2 else "config.ugetsu.toml"
CMD = [
    r"D:\anaconda\python.exe", "-m", "lvs", "assets",
    "--task", _TASK, "--source", "local", "--config", _CFG,
]


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [控温] {msg}", flush=True)


def gpu_temp() -> int:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=temperature.gpu",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=20,
    )
    if out.returncode != 0:
        raise RuntimeError(f"nvidia-smi 失败: {out.stderr.strip()}")
    return int(out.stdout.strip().splitlines()[0])


def find_assets_roots() -> list[int]:
    """找出在跑的 lvs assets 进程（只返回根，不返回其子进程）。"""
    hits: list[tuple[int, int]] = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cl = " ".join(p.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "-m lvs assets" in cl and "thermal_assets_guard" not in cl:
            hits.append((p.info["pid"], p.ppid() if psutil.pid_exists(p.ppid()) else 0))
    hit_pids = {h[0] for h in hits}
    return [pid for pid, ppid in hits if ppid not in hit_pids]


def kill_assets() -> None:
    for pid in find_assets_roots():
        try:
            p = psutil.Process(pid)
            tree = p.children(recursive=True) + [p]
            log(f"暂停出图任务 pid={pid}（含 {len(tree) - 1} 个子进程）")
            for x in tree:
                x.terminate()
            _, alive = psutil.wait_procs(tree, timeout=15)
            for x in alive:
                x.kill()
            log("已暂停")
        except psutil.NoSuchProcess:
            pass


def start_assets() -> subprocess.Popen:
    fh = open(LOG, "a", encoding="utf-8", errors="replace")
    log(f"启动出图任务：{' '.join(CMD)}")
    return subprocess.Popen(CMD, cwd=str(REPO), stdout=fh, stderr=subprocess.STDOUT)


def cooldown() -> None:
    """冷却到 <= LO°C（最多等 COOLDOWN_MAX）。"""
    t0 = time.time()
    while True:
        t = gpu_temp()
        waited = time.time() - t0
        if t <= LO:
            log(f"已冷却到 {t}°C（等待 {int(waited)}s），继续")
            return
        if waited >= COOLDOWN_MAX:
            log(f"冷却等待超过 {COOLDOWN_MAX // 60} 分钟仍有 {t}°C，强制续跑")
            return
        log(f"冷却中 {t}°C（阈值 {LO}°C，已等 {int(waited)}s）")
        time.sleep(20)


def main() -> int:
    try:
        gpu_temp()
    except Exception as exc:                      # noqa: BLE001
        log(f"环境不可用，不接管任务：{exc}")
        return 2

    # 接管现有任务（若有）——先停再冷，避免带着 87°C 直接开工
    if find_assets_roots():
        kill_assets()
        time.sleep(2)
    cooldown()

    while True:
        proc = start_assets()
        killed_by_heat = False
        while proc.poll() is None:
            time.sleep(POLL)
            try:
                t = gpu_temp()
            except Exception as exc:              # noqa: BLE001
                log(f"读温度失败（{exc}），保持现状继续等")
                continue
            if t >= HI:
                log(f"GPU {t}°C >= {HI}°C，暂停出图降温")
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                kill_assets()          # 连子进程一起停干净
                killed_by_heat = True
                break
        if not killed_by_heat:
            rc = proc.returncode
            log(f"出图任务自然退出 rc={rc}")
            if rc == 0:
                log("全部完成")
                return 0
            return rc if rc is not None else 1
        cooldown()


if __name__ == "__main__":
    sys.exit(main())
