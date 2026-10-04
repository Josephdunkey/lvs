"""按 `models.lock.json` 拉取本地生图模型（票据 19）。

为什么自己写下载器：国内直连 huggingface.co / github.com 不通。实测：

| 源 | 实测速度 |
|---|---|
| **www.modelscope.cn**（首选） | ~5 MB/s |
| hf-mirror.com（备用） | ~0.6 MB/s |

用 `curl -C -` 断点续传；若服务端不支持 Range，则自动清空重下。
`.part.url` 记录当前分片来自哪个 URL，换源时自动重新开始，避免拼出坏文件。

用法：

    python scripts/fetch_models.py                    # 全部（推荐先只拉 sdxl 验证）
    python scripts/fetch_models.py --only sdxl
    python scripts/fetch_models.py --only zimage
    python scripts/fetch_models.py --source hf-mirror  # 换备用源
    python scripts/fetch_models.py --force             # 已存在也重下
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCK_FILE = PROJECT_ROOT / "models.lock.json"

CATEGORY_DIRS = {
    "checkpoints": "checkpoints",
    "diffusion_models": "diffusion_models",
    "text_encoders": "text_encoders",
    "vae": "vae",
    "loras": "loras",
}

GROUP_OF = {
    "sd_xl_base_1.0.safetensors": "sdxl",
    "z_image_turbo_int8_convrot.safetensors": "zimage",
    "qwen_3_4b_fp8_mixed.safetensors": "zimage",
    "ae.safetensors": "zimage",
}


def url_for(item: dict, source: str) -> str:
    if source == "modelscope":
        return (
            "https://www.modelscope.cn/api/v1/models/"
            f"{item['repo']}/repo?Revision=master&FilePath={item['file']}"
        )
    return f"https://hf-mirror.com/{item.get('hf_repo', item['repo'])}/resolve/main/{item['file']}"


def _curl(args: list[str]) -> int:
    return subprocess.call(["curl", "-s", "-L", *args])


def download(url: str, dest: Path, retries: int = 6) -> bool:
    """断点续传下载。返回是否成功。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    marker = dest.with_name(dest.name + ".part.url")

    # 换源/换文件 → 旧分片无效，清掉
    if part.is_file() and (not marker.is_file() or marker.read_text(encoding="utf-8") != url):
        part.unlink()
    marker.write_text(url, encoding="utf-8")

    for attempt in range(1, retries + 1):
        before = part.stat().st_size if part.is_file() else 0
        rc = _curl([
            "-C", "-", "--retry", "3", "--retry-delay", "2",
            "--connect-timeout", "30", "--max-time", "7200",
            "-o", str(part), url,
        ])
        size = part.stat().st_size if part.is_file() else 0
        print(f"    [{attempt}/{retries}] rc={rc} 已下载 {size/1e9:.2f} GB")
        if rc == 0 and size > 0:
            part.replace(dest)
            marker.unlink(missing_ok=True)
            return True
        if rc == 33 and size <= before:
            # 服务端不支持 Range → 清空重来
            print("    （服务端不支持断点续传，改为整文件重下）")
            part.unlink(missing_ok=True)
            part.write_bytes(b"")
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按 models.lock.json 拉取生图模型")
    ap.add_argument("--only", choices=["all", "sdxl", "zimage"], default="all")
    ap.add_argument("--source", choices=["modelscope", "hf-mirror"], default=None)
    ap.add_argument("--force", action="store_true", help="已存在也重新下载")
    args = ap.parse_args(argv)

    if not shutil.which("curl"):
        print("未找到 curl。请安装 curl，或按下面清单手动下载：")
        _print_manual("modelscope")
        return 2

    lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    source = args.source or lock.get("source", "modelscope")
    models_root = Path(lock.get("comfyui_dir", r"D:\ComfyUI")) / "models"

    print(f"模型清单：{LOCK_FILE}")
    print(f"下载源  ：{source}（modelscope 更快，hf-mirror 备用）")
    print(f"目标目录：{models_root}\n")

    total = ok = 0
    for category, items in lock.get("models", {}).items():
        subdir = CATEGORY_DIRS.get(category, category)
        for item in items:
            if args.only != "all" and GROUP_OF.get(item["name"], "all") != args.only:
                continue
            total += 1
            dest = models_root / subdir / item["name"]
            approx = item.get("approx_gb")
            print(f"→ [{category}] {item['name']}" + (f"  （约 {approx} GB）" if approx else ""))
            if item.get("note"):
                print(f"    {item['note']}")
            if dest.is_file() and dest.stat().st_size > 1_000_000 and not args.force:
                print(f"    已存在，跳过：{dest.stat().st_size/1e9:.2f} GB")
                ok += 1
                print()
                continue
            url = url_for(item, source)
            if download(url, dest):
                print(f"    ✓ 完成：{dest.stat().st_size/1e9:.2f} GB → {dest}")
                ok += 1
            else:
                print("    ✗ 失败。可换源重试：--source hf-mirror")
                print(f"      或手动下载：{url}")
            print()

    print(f"完成 {ok}/{total} 个模型。")
    if ok < total:
        print("\n手动下载清单：")
        _print_manual(source)
        return 1
    return 0


def _print_manual(source: str = "modelscope") -> None:
    lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    for category, items in lock.get("models", {}).items():
        subdir = CATEGORY_DIRS.get(category, category)
        for item in items:
            print(f"  {url_for(item, source)}")
            print(f"    → {lock.get('comfyui_dir')}\\models\\{subdir}\\{item['name']}")


if __name__ == "__main__":
    raise SystemExit(main())
