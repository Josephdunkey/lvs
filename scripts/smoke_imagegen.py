"""真机出图冒烟测试（票据 19 的「待验证」项）。

用途：模型下完、ComfyUI 起好后，**一条命令**确认「能出图」——不用跑整条流水线。

它做的事：
1. 探活 ComfyUI（`/system_stats`）
2. 核对 config.toml `[comfyui]` 里配置的模型文件是否都在（找不到就明确告诉你缺哪个）
3. 按 `workflows/` 的模板出**一张小图**（默认 768×768、4 步，约几十秒）
4. 落盘到 `.work/_smoke/out.png`，并打印耗时与图片大小

用法：

    .venv\\Scripts\\python.exe scripts\\smoke_imagegen.py
    .venv\\Scripts\\python.exe scripts\\smoke_imagegen.py --prompt "宋代汴京街市，黄昏"
    .venv\\Scripts\\python.exe scripts\\smoke_imagegen.py --size 1024 --steps 8   # 更接近实际
    .venv\\Scripts\\python.exe scripts\\smoke_imagegen.py --workflow sdxl.json   # 测备用模型
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lvs import doctor, imagegen  # noqa: E402
from lvs.config import Config, ConfigError, find_config  # noqa: E402

DEFAULT_PROMPT = (
    "中国古代城郭鸟瞰，夯土城墙与九鼎礼器，黄昏金色余晖，"
    "写实电影感，厚重色调，纪实摄影，高清细节"
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ComfyUI 真机出图冒烟测试")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--size", type=int, default=768, help="正方形边长（默认 768，正式生图建议 1344×768）")
    ap.add_argument("--steps", type=int, default=4, help="步数（默认 4，正式为 8）")
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--workflow", default=None, help="覆盖 config 里的模板名，如 sdxl.json")
    ap.add_argument("--timeout", type=int, default=900, help="单张超时（秒）")
    args = ap.parse_args(argv)

    path = find_config()
    if path is None:
        print("未找到 config.toml，请先 `copy config.example.toml config.toml`。")
        return 2
    try:
        config = Config.load(path)
    except ConfigError as exc:
        print(exc)
        return 2

    base = config.get("comfyui.base_url", "http://127.0.0.1:8188")
    workflow = args.workflow or config.get("comfyui.workflow", "zimage_turbo.json")
    model = config.get("comfyui.model", "z_image_turbo_int8_convrot.safetensors")

    print("=" * 60)
    print("ComfyUI 真机出图冒烟测试")
    print(f"  服务地址：{base}")
    print(f"  工作流  ：{workflow}")
    print(f"  模型    ：{model}")
    print(f"  分辨率  ：{args.size}x{args.size}  步数：{args.steps}")
    print("=" * 60)

    # 1) 探活
    client = imagegen.ComfyClient(base, timeout=args.timeout)
    if not client.health():
        print(f"\n[✗] ComfyUI 不可达：{base}")
        print(r"    请先启动：scripts\start_comfyui.cmd")
        print("    （首次启动要加载节点，约 1-2 分钟）")
        return 1
    print("\n[✓] ComfyUI 在线")

    # 2) 模型文件核对（复用 doctor 的逻辑）
    result = doctor.check_comfyui(config)
    tag = "✓" if result.status == doctor.PASS else "!"
    print(f"[{tag}] {result.detail}")
    if result.status != doctor.PASS and "缺" in result.detail:
        print(f"    提示：{result.hint}")
        return 1

    # 3) 出图
    tpl = imagegen.template_path(workflow)
    if not tpl.is_file():
        print(f"\n[✗] 找不到模板：{tpl}")
        return 1

    out = Path(".work/_smoke/out.png")
    print(f"\n出图中… （模板 {tpl.name}）")
    t0 = time.time()
    try:
        info = imagegen.generate(
            args.prompt,
            out,
            base_url=base,
            workflow=workflow,
            model=model,
            seed=args.seed,
            extra={
                "WIDTH": args.size,
                "HEIGHT": args.size,
                "STEPS": args.steps,
                "CLIP": config.get("comfyui.clip", imagegen.DEFAULT_VALUES["CLIP"]),
                "VAE": config.get("comfyui.vae", imagegen.DEFAULT_VALUES["VAE"]),
            },
            client=client,
        )
    except imagegen.ComfyError as exc:
        print(f"\n[✗] 出图失败：{exc}")
        print("\n常见原因：")
        print("  - 模型文件名与 config.toml 不一致（上面已核对）")
        print("  - 显存不足 → 关掉本地 TTS，或给 ComfyUI 加 --lowvram")
        print("  - 模板与模型不匹配（Z-Image 用 zimage_turbo.json，SDXL 用 sdxl.json）")
        return 1

    dt = time.time() - t0
    size_mb = out.stat().st_size / 1e6 if out.is_file() else 0
    print(f"\n[✓] 出图成功：{out}  （{size_mb:.2f} MB，耗时 {dt:.1f}s）")
    print(f"    prompt_id={info.get('prompt_id')}  seed={info.get('seed')}")
    print(f"    ComfyUI 的 output 目录里也有一份（前缀 {imagegen.DEFAULT_VALUES['PREFIX']}）")
    print("\n接下来就可以跑真实流程了：")
    print("  lvs shots --no-llm      # 或填好 LLM key 后不加 --no-llm")
    print("  lvs assets              # local 分镜会走 ComfyUI 生图")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
