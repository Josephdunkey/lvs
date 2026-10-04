"""本地生图 —— ComfyUI HTTP 客户端（票据 19）。

ComfyUI 启动后监听 `127.0.0.1:8188`，本模块：

1. `ComfyClient.health()` —— `/system_stats` 探活
2. `ComfyClient.generate()` —— 提交工作流 → 轮询 `/history` → 下载图片

工作流以**带占位符的 JSON 模板**存放在 `workflows/`，占位符形如 `{{PROMPT}}`：
模板决定用哪些节点（Z-Image 与 SDXL 的节点图不同），本模块只负责替换与调用，
因此**换模型 = 换模板**，不用改代码。

生成结果可复现：`seed` 写入 `manifest.json`（票据 19）。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from lvs.config import PROJECT_ROOT
from lvs import net
from lvs.errors import LvsError, EXIT_FAILED


class ComfyError(LvsError, RuntimeError):
    """ComfyUI 调用失败。消息面向用户。"""

    exit_code = EXIT_FAILED


def _http_json(url: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    # ★ 走 `net.urlopen`：ComfyUI 是**本机**服务，必须绕开系统代理，
    # 否则用户开了 VPN/代理时，这里会被代理拦下、误报"ComfyUI 不可达"。
    with net.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _http_bytes(url: str, timeout: int = 120) -> bytes:
    with net.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def _render(template: str, values: dict[str, Any]) -> str:
    """把 `{{KEY}}` 替换成字符串值；数值原样（不加引号）由模板负责。"""
    out = template
    for key, val in values.items():
        token = "{{" + key + "}}"
        if token not in out:
            continue
        if isinstance(val, (int, float)):
            out = out.replace(f'"{token}"', str(val))  # 模板里写成 "{{SEED}}" 的数值位
            out = out.replace(token, str(val))
        else:
            escaped = json.dumps(str(val), ensure_ascii=False)[1:-1]
            out = out.replace(token, escaped)
    return out


class ComfyClient:
    def __init__(self, base_url: str, timeout: int = 600) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.client_id = uuid.uuid4().hex

    # ---- 探活 -------------------------------------------------------------

    def health(self) -> bool:
        try:
            _http_json(f"{self.base_url}/system_stats", timeout=3)
            return True
        except Exception:
            return False

    def object_info(self) -> dict[str, Any]:
        return _http_json(f"{self.base_url}/object_info", timeout=30)

    def ensure_alive(self) -> None:
        if not self.health():
            raise ComfyError(
                f"ComfyUI 未启动或不可达：{self.base_url}\n"
                "  请先启动 ComfyUI（见 README「本地生图」），确认它是监听该地址；"
                "然后 `lvs doctor` 应显示 ComfyUI 通过。"
            )

    # ---- 生图 -------------------------------------------------------------

    def submit(self, graph: dict[str, Any]) -> str:
        resp = _http_json(
            f"{self.base_url}/prompt",
            {"prompt": graph, "client_id": self.client_id},
            timeout=60,
        )
        if "prompt_id" not in resp:
            raise ComfyError(f"ComfyUI 拒绝工作流：{json.dumps(resp, ensure_ascii=False)[:400]}")
        return resp["prompt_id"]

    def wait(self, prompt_id: str, poll: float = 1.0) -> dict[str, Any]:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            history = _http_json(f"{self.base_url}/history/{prompt_id}", timeout=30)
            entry = history.get(prompt_id)
            if entry:
                status = entry.get("status", {})
                if status.get("status_str") == "error" or not status.get("completed", True):
                    if status.get("status_str") == "error":
                        raise ComfyError(
                            "ComfyUI 执行出错："
                            + json.dumps(status.get("messages", []), ensure_ascii=False)[:400]
                        )
                if entry.get("outputs"):
                    return entry
            time.sleep(poll)
        raise ComfyError(f"ComfyUI 生成超时（>{self.timeout}s）")

    def images_of(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        images: list[dict[str, Any]] = []
        for node_out in (entry.get("outputs") or {}).values():
            for img in node_out.get("images", []) or []:
                images.append(img)
        return images

    def download(self, image: dict[str, Any]) -> bytes:
        query = urllib.parse.urlencode(
            {
                "filename": image.get("filename", ""),
                "subfolder": image.get("subfolder", ""),
                "type": image.get("type", "output"),
            }
        )
        return _http_bytes(f"{self.base_url}/view?{query}")

    # ---- 上传（参考图用） --------------------------------------------------

    def upload_image(self, path: Path, *, subfolder: str = "") -> str:
        """把一张本机图片送进 ComfyUI 的 `input/`，返回 `LoadImage` 能用的文件名。

        ★ 为什么必须**上传**，不能直接给绝对路径：
        `LoadImage` 只在 ComfyUI **自己的** `input/` 目录里找文件。而定妆照住在
        `<素材库>/_cast/<角色>/v1/`，ComfyUI 根本看不见它 —— 所以参考图必须先经
        `POST /upload/image` 送过去。

        这正是 L2（参考图一致性）原先断掉的第二环：`_workflow_for` 会"选模板"，
        但**没有任何代码把图送进去**（模板里也没有 `{{REF_IMAGE}}` 占位符）。

        返回 `"subfolder/name.png"` 形式（`LoadImage` 认这个写法）。
        """
        src = Path(path).expanduser()
        if not src.is_file():
            raise ComfyError(f"参考图不存在：{src}")

        blob = src.read_bytes()
        boundary = "----lvsform" + uuid.uuid4().hex
        ctype = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }.get(src.suffix.lower(), "application/octet-stream")

        # 手写 multipart：**不引入 requests 依赖**（本项目把 requests 放在可选
        # 的 [http] extra 里，参考图这条链不该因此变成硬依赖）。
        body = bytearray()

        def _part(headers: str, payload: bytes) -> None:
            body.extend(f"--{boundary}\r\n{headers}\r\n\r\n".encode("utf-8"))
            body.extend(payload)
            body.extend(b"\r\n")

        _part(
            f'Content-Disposition: form-data; name="image"; filename="{src.name}"\r\n'
            f"Content-Type: {ctype}",
            blob,
        )
        _part('Content-Disposition: form-data; name="overwrite"', b"true")
        if subfolder:
            _part(
                'Content-Disposition: form-data; name="subfolder"',
                subfolder.encode("utf-8"),
            )
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))

        req = urllib.request.Request(
            f"{self.base_url}/upload/image",
            data=bytes(body),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
            method="POST",
        )
        try:
            with net.urlopen(req, timeout=120) as resp:
                info = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise ComfyError(f"上传参考图失败（HTTP {exc.code}）：{detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ComfyError(f"上传参考图失败：{exc}") from exc

        name = str(info.get("name") or "")
        if not name:
            raise ComfyError(
                "ComfyUI 上传接口返回异常（没有 name 字段）："
                + json.dumps(info, ensure_ascii=False)[:300]
            )
        sub = str(info.get("subfolder") or "")
        return f"{sub}/{name}" if sub else name


# ---- 工作流模板 ------------------------------------------------------------


def template_path(name: str) -> Path:
    return PROJECT_ROOT / "workflows" / name


# 通用旋钮 + 部署回退值。**模板里出现的每个 `{{KEY}}` 都必须在这里有默认值，
# 或写死在模板内**，否则会原样留在 JSON 里、导致 ComfyUI 报「节点参数非法」。
# 模型专用的采样参数（采样器 / 步数 / cfg / shift / denoise / weight_dtype）不住在这里，
# 写在 `workflows/*.json` 的字面量里 —— 这样「换模型 = 换模板」，本模块不含模型专用知识（D11）。
DEFAULT_VALUES = {
    "WIDTH": 1344,
    "HEIGHT": 768,
    "BATCH": 1,
    "NEGATIVE": "text, watermark, signature, blurry, low quality, extra fingers, deformed",
    "PREFIX": "lvs",  # ComfyUI output 目录里的文件名前缀
    # 分体模型的文件回退值（config.toml [comfyui] 的 clip / vae 可覆盖）
    "CLIP": "qwen_3_4b_fp8_mixed.safetensors",
    "VAE": "ae.safetensors",
    # 参考图（img2img）用：由 `generate(ref_image=…)` 在上传后填真实文件名。
    # 留空串在这里是为了"模板里出现的每个占位符都有默认值"这条规则；
    # 真要用时**必须**被填上 —— `generate()` 会拦下"模板要参考图但没给"的情况。
    "REF_IMAGE": "",
    # img2img 去噪强度。0.45–0.60 是 v2 方案给的起点（太高换脸、太低换不出场景）。
    # 逐镜可用 config `[cast].ref_denoise` 覆盖（经 `extra` 传进来）。
    "DENOISE": 0.55,
}


def generate(
    prompt: str,
    out_path: Path,
    *,
    base_url: str,
    workflow: str,
    model: str,
    seed: int,
    extra: dict[str, Any] | None = None,
    client: ComfyClient | None = None,
    ref_image: Path | str | None = None,
) -> dict[str, Any]:
    """生成一张图并写到 `out_path`，返回本次参数（含 seed，供 manifest 记录）。

    `ref_image` 给了就走参考图（img2img）：先上传到 ComfyUI 的 `input/`，
    再把返回的文件名填进模板的 `REF_IMAGE` 占位符。

    ★ 两条**互斥**的一致性检查（都是"宁可大声失败"）：
      · 模板要参考图（含 `REF_IMAGE`）却没给 → 报错。少了它 `LoadImage` 会拿到
        空文件名，ComfyUI 回一句"节点参数非法"，那种错极难归因。
      · 给了参考图但模板用不上 → 也报错。这说明**选模板与传参考图两处不一致**，
        属于接线 bug，静默忽略会让"以为开了 L2、其实还是纯文生图"。
    """
    tpl_file = template_path(workflow)
    if not tpl_file.is_file():
        raise ComfyError(
            f"未找到工作流模板：{tpl_file}\n"
            "  请检查 config.toml 的 [comfyui].workflow 是否指向 workflows/ 下的文件名。"
        )
    template = tpl_file.read_text(encoding="utf-8")
    values = {**DEFAULT_VALUES, **(extra or {})}
    values.update({"PROMPT": prompt, "SEED": int(seed), "CKPT": model, "MODEL": model})

    wants_ref = "{{REF_IMAGE}}" in template
    if wants_ref and not ref_image:
        raise ComfyError(
            f"工作流模板 `{workflow}` 需要参考图（含 REF_IMAGE 占位符），但没有传 ref_image。\n"
            "  这通常意味着「选模板」与「传参考图」两处不一致 —— "
            "见 `assets._workflow_for` / `assets._gen_local`。"
        )
    if ref_image and not wants_ref:
        raise ComfyError(
            f"传了参考图，但工作流模板 `{workflow}` 用不上它（没有 REF_IMAGE 占位符）。\n"
            "  这会让「以为在用参考图、实际还是纯文生图」—— 请检查选模板的判据。"
        )

    cli = client or ComfyClient(base_url)
    cli.ensure_alive()

    uploaded = ""
    if ref_image:
        uploaded = cli.upload_image(Path(ref_image))
        values["REF_IMAGE"] = uploaded

    graph = json.loads(_render(template, values))
    prompt_id = cli.submit(graph)
    entry = cli.wait(prompt_id)
    images = cli.images_of(entry)
    if not images:
        raise ComfyError("ComfyUI 完成但没有产出图片（检查工作流是否包含 SaveImage）。")
    blob = cli.download(images[0])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(blob)
    return {
        "workflow": workflow,
        "model": model,
        "seed": int(seed),
        "prompt_id": prompt_id,
        "width": values["WIDTH"],
        "height": values["HEIGHT"],
        "bytes": len(blob),
        # 供 manifest 追溯"这一镜到底用没用参考图"（L1 文字锚定 / L2 参考图）
        "ref_image": uploaded,
        "ref_source": str(ref_image) if ref_image else "",
    }


# ---- CLI：`lvs image`（手动生图） -------------------------------------------

IMAGE_DIR = "_imagegen"  # 手动出图落在 .work/_imagegen/


def _prompt_from_shot(ws, shot_id: int) -> tuple[str, str] | None:
    """从 `.work/<task>/shots.json` 取某镜的 (prompt, 说明)。找不到返回 None。"""
    shots_file = ws.path("shots.json")
    if not shots_file.is_file():
        return None
    shots = json.loads(shots_file.read_text(encoding="utf-8"))
    for shot in shots:
        if int(shot.get("id", -1)) == shot_id:
            text = shot.get("prompt") or shot.get("visual") or shot.get("narration") or ""
            return text, f"shot-{shot_id:03d}"
    return None


def run_command(config, args) -> int:  # noqa: ANN001 - Config，避免循环导入
    """`lvs image` —— 手动出图（单张/多张），不写 shots.json、不碰流水线状态。

    用途：先把图生好放素材库，或单独调提示词。产物默认落 `.work/_imagegen/`。
    """
    from lvs import guard  # 局部导入：guard 不依赖 imagegen，避免顶层耦合
    from lvs.workspace import Workspace, slugify

    base = config.get("comfyui.base_url", "http://127.0.0.1:8188")
    workflow = getattr(args, "workflow", None) or config.get("comfyui.workflow", "zimage_turbo.json")
    model = getattr(args, "model", None) or config.get(
        "comfyui.model", "z_image_turbo_int8_convrot.safetensors"
    )
    seed = getattr(args, "seed", None)
    seed = int(config.get("comfyui.seed", 42)) if seed is None else int(seed)

    # 提示词来源：--prompt 优先，其次从 shots.json 按镜号取
    prompt = getattr(args, "prompt", None)
    default_name = f"seed-{seed}"
    ws = None
    if not prompt and getattr(args, "from_shot", None) is not None:
        task = slugify(getattr(args, "task", None) or "default")
        ws = Workspace(task=task, root=PROJECT_ROOT)  # 只读 shots.json，不 ensure()
        got = _prompt_from_shot(ws, int(args.from_shot))
        if got is None:
            print(
                f"在 {ws.path('shots.json')} 里找不到第 {args.from_shot} 镜。\n"
                "  先跑 `lvs shots`，或用 `--prompt` 直接给提示词。"
            )
            return 2
        prompt, default_name = got
    if not prompt:
        print("需要提示词：`lvs image --prompt \"...\"`，或 `lvs image --task <任务名> --from-shot <镜号>`")
        return 2

    cli = ComfyClient(base)
    if not cli.health():
        print(
            f"ComfyUI 不可达：{base}\n"
            r"  启动：scripts\start_comfyui.cmd"
        )
        return 1

    # 显存守卫（与 assets 的 local 分支同一套规则；只拦"本地 TTS 在跑"这种真冲突）
    try:
        note = guard.check("imagegen", config)
        if note:
            print(f"[GPU 守卫] {note}")
    except guard.GPUConflict as exc:
        print(f"[GPU 守卫] {exc}")
        return 1

    extra = {k: v for k, v in {
        "WIDTH": getattr(args, "width", None),
        "HEIGHT": getattr(args, "height", None),
        "CLIP": config.get("comfyui.clip"),
        "VAE": config.get("comfyui.vae"),
    }.items() if v is not None}

    out_arg = getattr(args, "out", None)
    count = max(1, int(getattr(args, "count", 1) or 1))
    root = (ws.root if ws else PROJECT_ROOT)
    out_dir = root / ".work" / IMAGE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    ok = 0
    for i in range(count):
        this_seed = seed + i
        if out_arg:
            out = Path(out_arg)
            if count > 1:
                out = out.with_name(f"{out.stem}-{i + 1}{out.suffix}")
        else:
            suffix = "" if count == 1 else f"-{i + 1}"
            out = out_dir / f"{default_name}{suffix}.png"
        print(f"出图中 → {out}  (seed={this_seed}, {workflow})")
        try:
            info = generate(
                prompt, out,
                base_url=base, workflow=workflow, model=model,
                seed=this_seed, extra=extra or None, client=cli,
            )
        except ComfyError as exc:
            print(f"  [失败] {exc}")
            continue
        print(f"  [完成] {info['bytes'] / 1e6:.2f} MB  prompt_id={info['prompt_id']}")
        ok += 1

    if ok == 0:
        return 1
    if count > 1:
        print(f"\n共生成 {ok}/{count} 张 → {out.parent}")
    return 0
