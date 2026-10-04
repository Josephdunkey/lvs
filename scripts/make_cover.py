"""B站封面出图（本地 ComfyUI，zimage_turbo 工作流）。

用法:
  python scripts/make_cover.py <提示词文件> <输出前缀> [--n 2] [--seed N]

提示词文件：纯文本，第一段 = 正面提示词。
输出: --out 目录下 <输出前缀>-<i>.png（默认 D:/LongVideoStudio/covers/）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lvs import net  # noqa: E402  本机代理直连（urllib 不绕 localhost 代理）

REPO = Path(r"D:\LongVideoStudio")
WF = REPO / "workflows" / "zimage_turbo.json"
COMFY = "http://127.0.0.1:8188"


def _json(url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = net.urlopen(
        __import__("urllib.request", fromlist=["Request"]).Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
    )
    return json.loads(req.read())


# 模板里模型名也是占位符（不替换会 400 value_not_in_list）
_MODELS = {
    "{{CKPT}}": "z_image_turbo_int8_convrot.safetensors",
    "{{CLIP}}": "qwen_3_4b_fp8_mixed.safetensors",
    "{{VAE}}": "ae.safetensors",
}


def submit(prompt: str, seed: int, prefix: str, w: int, h: int) -> str:
    raw = WF.read_text(encoding="utf-8")
    for k, v in _MODELS.items():
        raw = raw.replace(k, v)
    wf = json.loads(raw)
    wf["4"]["inputs"]["text"] = prompt
    wf["7"]["inputs"].update({"width": w, "height": h, "batch_size": 1})
    wf["8"]["inputs"]["seed"] = seed
    wf["10"]["inputs"]["filename_prefix"] = prefix
    r = _json(f"{COMFY}/prompt", {"prompt": wf, "client_id": "make_cover"})
    return r["prompt_id"]


def wait_done(pid: str, timeout: float = 600) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        h = _json(f"{COMFY}/history/{pid}")
        if pid in h:
            entry = h[pid]
            if entry.get("status", {}).get("status_str") == "error":
                raise RuntimeError(f"出图失败: {entry['status']}")
            files = [
                img
                for node_out in entry.get("outputs", {}).values()
                for img in node_out.get("images", [])
            ]
            if files:
                return files
        time.sleep(2)
    raise TimeoutError(f"等待超时: {pid}")


def download(img: dict, dst: Path) -> None:
    q = "&".join(
        f"{k}={img[k]}" for k in ("filename", "subfolder", "type") if img.get(k)
    )
    with net.urlopen(f"{COMFY}/view?{q}") as r:
        dst.write_bytes(r.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt_file")
    ap.add_argument("prefix")
    ap.add_argument("--n", type=int, default=2)
    ap.add_argument("--seed", type=int, default=1024)
    ap.add_argument("--width", type=int, default=1344)
    ap.add_argument("--height", type=int, default=768)
    ap.add_argument("--out", default=str(REPO / "covers"))
    args = ap.parse_args()

    prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip().split("\n\n")[0]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    for i in range(args.n):
        seed = args.seed + i * 7919
        pid = submit(prompt, seed, args.prefix, args.width, args.height)
        print(f"[{i + 1}/{args.n}] prompt_id={pid} seed={seed}", flush=True)
        for img in wait_done(pid):
            dst = out_dir / f"{args.prefix}-{i + 1}.png"
            download(img, dst)
            print(f"  -> {dst} ({dst.stat().st_size} bytes)", flush=True)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
