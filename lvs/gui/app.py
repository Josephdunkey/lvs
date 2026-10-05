"""本地 GUI 的 HTTP 层（票据 36）。

规矩（都是 grill 时定下来的）：
- **只监听 127.0.0.1**，不做鉴权 —— 它就是个本机工具，不暴露到局域网。
- **只在 `.work/<task>/` 内写**：跑阶段（子进程）、改 shots.json、存上传的拍摄稿。
  `config.toml` 一律只读展示 + 校验，素材库只读。
- **服务文件必须防越界**：任务名和文件相对路径都要检查是否真的落在 `.work` 里。
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import (
    Flask, Response, abort, current_app, jsonify, render_template, request, send_file,
)

from lvs.config import PROJECT_ROOT, Config, ConfigError, find_config
from lvs import media
from lvs import sources
from lvs.errors import EXIT_USAGE, LvsError
from lvs.gui import configio, jobs, store
from lvs.workspace import Workspace, write_shots_json

THUMB_WIDTH = 480
THUMB_DIRNAME = ".thumbs"
MANUSCRIPT_NAME = "manuscript.md"

# 用户自己挑的图（票 40）
PICK_DIRNAME = "picked"
# 允许上传的图片格式 —— 真源 `lvs.media`（缺陷 B02/Q02），不再自己抄一份字面量。
# ★ **刻意跟着 media 走**：比原先多出 .gif/.tif/.tiff，是有意为之 ——
#   `assets` 阶段本来就会把素材库里的 tif 拷进来，而 `build` 现在也认它是图片
#   （收窄在这里只会拦下本来能用的图）。真正的把关是下面 `Image.verify()`。
PICK_SUFFIXES = media.IMAGE_EXTS
MAX_PICK_BYTES = 24 * 1024 * 1024

#: 认作"本机"的主机名。判跨站写请求用（见 `_block_cross_site_writes`）。
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class GuiError(LvsError, RuntimeError):
    """界面层的输入不合法（用法问题，改了再来）。"""

    exit_code = EXIT_USAGE


def thumb_is_fresh(cache: Path, src: Path) -> bool:
    """缩略图必须比原图**严格更新**才算新鲜（票 38 / S1）。

    同一秒内重渲会让两者 mtime 相等；用 `>=` 就会把旧缩略图端出去。
    宁可多重生成一次，也不要显示过期的图。
    """
    try:
        return cache.is_file() and cache.stat().st_mtime > src.stat().st_mtime
    except OSError:
        return False


# ---- 路径安全 --------------------------------------------------------------


def _root() -> Path:
    """本次 app 的根目录（`create_app(root)` 传进来的那个）。

    **必须走它，不能吃 `store` 的默认值** —— 那几个默认值是 `PROJECT_ROOT`，
    于是 `/media/` `/thumb/` 这类"取产物"的路由会绕过 app 自己配的 root，
    去读仓库根的 `.work/`。后果有两层：
      - 测试里 `create_app(tmp)` 造的任务取不到（404）；
       - 真机上用 `--root` 指向别处时，产物服务会指向**另一个目录树**。
    其余路由一直是对的（都显式传了 `app.config["LVS_ROOT"]`），
    只有下面这四个辅助函数漏了 —— 它们曾经"碰巧正确"，因为测试用的是仓库根。
    """
    return Path(current_app.config["LVS_ROOT"])


def _task_dir_or_404(name: str) -> Path:
    d = store.task_dir(name, _root())
    if d is None:
        abort(404, description=f"任务 {name!r} 不存在")
    return d


def safe_member(task_dir: Path, rel: str) -> Path | None:
    """把 `rel` 解析成任务目录内的真实文件；越界或不存在都返回 None。"""
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return None
    base = task_dir.resolve()
    candidate = (base / rel).resolve()
    if candidate == base or base not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def _shot_source(task: str, sid: int) -> str:
    data = store.read_shots(task, _root())
    for shot in data.get("shots") or []:
        if int(shot.get("id", -1)) == sid:
            return str(shot.get("source") or "")
    return ""


def _shot_asset(task: str, sid: int) -> Path | None:
    """从 shots.json 里取该镜的素材路径，并确认它确实在本任务目录内。"""
    data = store.read_shots(task, _root())
    for shot in data.get("shots") or []:
        if int(shot.get("id", -1)) != sid:
            continue
        raw = shot.get("asset_path")
        if not raw:
            return None
        d = store.task_dir(task, _root())
        if d is None:
            return None
        path = Path(str(raw))
        try:
            resolved = path.resolve()
        except OSError:
            return None
        base = d.resolve()
        if base not in resolved.parents or not resolved.is_file():
            return None
        return resolved
    return None


# ---- 应用 ------------------------------------------------------------------


def create_app(root: Path = PROJECT_ROOT, config_path: Path | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["LVS_ROOT"] = Path(root)
    app.config["LVS_CONFIG_PATH"] = Path(config_path) if config_path else find_config(None)
    registry = jobs.JobRegistry(root=Path(root), config_path=config_path)
    app.extensions["lvs_registry"] = registry

    # ---- 跨站写防护（2026-10-05 审查 S03）---------------------------------
    #
    # ★ 为什么需要：`serve()` 的取舍是"只绑 127.0.0.1 + 不做鉴权"，但这个取舍
    #   **不成立** —— 浏览器的跨站规则里有一类"简单请求"是不发预检的：
    #   GUI 开着时切到任意标签页，那个页面里一个自动提交的表单就能建任务、
    #   覆盖已有拍摄稿（毁掉几小时拆镜成果）、把来源策略重置成 auto、
    #   停掉正在跑的生图 job。实测三个写端点跨站 POST 当**全都返 200**。
    #
    # ★ 判据用 `Origin`（没有则退到 `Referer`）：浏览器**一定会**给跨站写请求
    #   带上它们；而 curl / 脚本 / 单测两个都不发 —— 那种情况放行。
    #   这是"防浏览器跨站写"，**不是**"防本机恶意进程"，别把它说成后者。
    @app.before_request
    def _block_cross_site_writes():
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        source = request.headers.get("Origin") or request.headers.get("Referer") or ""
        if not source:
            return None                      # 非浏览器客户端（curl / 单测 / 脚本）
        host = (urlsplit(source).hostname or "").lower()
        self_host = (request.host or "").split(":")[0].lower()
        # `Origin: null`（沙箱 iframe / file://）解析出的 host 是空串 → 一并拒掉
        if host and (host in _LOCAL_HOSTS or host == self_host):
            return None
        return jsonify({
            "error": "拒绝跨站写请求：本机 GUI 只服务本机页面，不对外提供接口。",
            "origin": source,
        }), 403

    @app.template_filter("ts")
    def _ts(value: Any) -> str:
        """mtime 浮点 → 本地时间字符串。"""
        import datetime as _dt

        try:
            return _dt.datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, OSError):
            return ""

    def current_config() -> tuple[Config | None, str | None]:
        """配置只读：能读到就展示，读不到就把原因给界面（不写、不修）。"""
        path = Path(config_path) if config_path else find_config(None)
        if not path or not Path(path).is_file():
            return None, "未找到 config.toml（复制 config.example.toml 即可）"
        try:
            return Config.load(path), None
        except ConfigError as exc:
            return None, str(exc)

    # ---- 页面 -------------------------------------------------------------

    @app.get("/")
    def page_index():
        tasks = store.list_tasks(app.config["LVS_ROOT"])
        return render_template(
            "index.html", tasks=tasks, running=registry.current(),
            stages=store.STAGES, stage_labels=store.STAGE_LABELS,
        )

    @app.get("/task/<name>")
    def page_task(name: str):
        task = store.read_task(name, app.config["LVS_ROOT"])
        if task is None:
            abort(404)
        return render_template(
            "task.html", task=task, running=registry.current(),
            stages=store.STAGES, stage_labels=store.STAGE_LABELS,
            stage_spec=jobs.STAGE_SPEC,
            editable=store.EDITABLE_SHOT_FIELDS,
        )

    @app.get("/library")
    def page_library():
        config, config_error = current_config()
        dirs = []
        if config is not None:
            dirs = [str(d) for d in (config.get("library.dirs", []) or []) if d]
        return render_template(
            "library.html",
            dirs=[{"path": d, "exists": Path(d).expanduser().is_dir()} for d in dirs],
            config_error=config_error,
        )

    @app.get("/settings")
    def page_settings():
        config, config_error = current_config()
        rows: list[tuple[str, str]] = []
        if config is not None:
            rows = _config_rows(config)
        return render_template(
            "settings.html", rows=rows, config_error=config_error,
            config_path=str(config_path) if config_path else str(find_config(None) or ""),
        )

    # ---- API：配置（可编辑，票据 37） --------------------------------------

    @app.get("/api/config")
    def api_config_get():
        cfg = app.config.get("LVS_CONFIG_PATH")
        if not cfg or not Path(cfg).is_file():
            return jsonify({"error": "未找到配置文件", "path": str(cfg or ""), "fields": {}})
        try:
            return jsonify({"path": str(cfg), "fields": configio.read_editable(cfg)})
        except configio.ConfigIOError as exc:
            return jsonify({"error": str(exc), "path": str(cfg), "fields": {}})

    @app.post("/api/config")
    def api_config_set():
        cfg = app.config.get("LVS_CONFIG_PATH")
        if not cfg or not Path(cfg).is_file():
            return jsonify({"error": "未找到配置文件"}), 400
        body = request.get_json(silent=True) or {}
        try:
            fields = configio.apply(cfg, body)
        except configio.ConfigIOError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "path": str(cfg), "fields": fields})

    def _demote_pexels_if_no_key(task: str, task_path: Path, options: dict[str, Any]) -> str:
        """一键到底之前：没配 Pexels key 就把 pexels 镜改成本地生图（票 38 / S3）。

        `lvs run` 碰到 pexels 镜会在 assets 停下（没 key 拿不到素材），
        不改这一下，"一键到底"就是个走不通的按钮。改的是 shots.json（D14 的真相源）。

        **只在自动策略下做**：用户要是明确选了 Pexels 或本地素材库，这一改就是
        在悄悄推翻他刚做的选择（票 41）。
        """
        config, _err = current_config()
        has_key = bool(config is not None and str(config.get("pexels.api_key") or "").strip())

        data = store.read_shots(task, app.config["LVS_ROOT"])
        if _effective_source_mode(data, options) != sources.MODE_AUTO:
            return ""
        count = demote_pexels(data, has_key)
        if not count:
            return ""
        try:
            write_shots_file(task_path, data)
        except OSError as exc:
            return f"（想把 pexels 镜改成本地生图，但写 shots.json 失败：{exc}）"
        return f"未配置 Pexels key：已把这 {count} 个 pexels 镜改成本地生图，否则一键会在素材阶段停下"

    # ---- API：任务 ---------------------------------------------------------

    @app.get("/api/tasks")
    def api_tasks():
        return jsonify([_task_json(t) for t in store.list_tasks(app.config["LVS_ROOT"])])

    @app.get("/api/task/<name>")
    def api_task(name: str):
        task = store.read_task(name, app.config["LVS_ROOT"])
        if task is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(_task_json(task))

    @app.post("/api/task/<name>/source-mode")
    def api_source_mode(name: str):
        """记下这个任务的实拍镜来源策略（票 41）。

        只写 `shots.json` 里的**意向**字段（`source_mode`），不碰逐镜 `source`。
        把意向落成逐镜来源是 `lvs shots` 与 `lvs assets` 的事 —— 两边都调
        `sources.apply_mode`（实现只有那一份，本端点不碰）。
        还没拆镜时无处置记，如实返回 `persisted: false`，由界面把它带在首次运行的参数里。
        """
        d = store.task_dir(name, app.config["LVS_ROOT"])
        if d is None:
            return jsonify({"error": "not found"}), 404
        body = request.get_json(silent=True) or {}
        # ★ 空 body **不是**"回到 auto"，是"你没说要用哪个"（2026-10-05 审查 S03：
        #   实测跨站表单 POST 空 body → `normalize(None)` 给出 "auto"，
        #   于是"什么都没传"成了一个**有效的写操作**，能把用户的 local 重置掉）。
        #   区分"显式给了 auto"和"根本没给"，正是 sources.resolve 的语义。
        raw_mode = body.get("mode", request.form.get("mode"))
        if raw_mode is None:
            return jsonify({"error": "缺少 mode（想显式回自动就传 auto）"}), 400
        try:
            mode = sources.normalize(raw_mode)
        except sources.SourceModeError as exc:
            return jsonify({"error": str(exc)}), 400

        data = store.read_shots(name, app.config["LVS_ROOT"])
        if not (data.get("shots") or []):
            return jsonify({"ok": True, "mode": mode, "persisted": False})
        data["source_mode"] = mode
        try:
            write_shots_file(d, data)
        except OSError as exc:
            return jsonify({"error": f"写 shots.json 失败：{exc}"}), 500
        return jsonify({"ok": True, "mode": mode, "persisted": True})

    @app.get("/api/task/<name>/shots")
    def api_shots(name: str):
        if store.task_dir(name, app.config["LVS_ROOT"]) is None:
            return jsonify({"error": "not found"}), 404
        data = store.read_shots(name, app.config["LVS_ROOT"])
        shots = []
        for shot in data.get("shots") or []:
            sid = int(shot.get("id", 0))
            shots.append({
                "id": sid,
                "kind": shot.get("kind") or "",
                "source": shot.get("source") or "",
                "resolved_by": shot.get("resolved_by") or "",
                "status": shot.get("status") or "",
                "start": shot.get("start"),
                "end": shot.get("end"),
                "narration": shot.get("narration") or "",
                "visual": shot.get("visual") or "",
                "prompt": shot.get("prompt") or "",
                "seed": shot.get("seed"),
                "error": shot.get("error") or "",
                "pinned": shot.get("library_asset") or "",
                "thumb": f"/thumb/{name}/{sid}",
                "has_asset": bool(shot.get("asset_path")),
            })
        return jsonify({"task": name, "count": len(shots), "shots": shots})

    @app.post("/api/task/<name>/shots/<int:sid>")
    def api_edit_shot(name: str, sid: int):
        """改一个分镜（写回 shots.json —— 它是人工可编辑的真相源，D14）。"""
        d = store.task_dir(name, app.config["LVS_ROOT"])
        if d is None:
            return jsonify({"error": "not found"}), 404
        patch = request.get_json(silent=True) or {}
        bad = sorted(set(patch) - set(store.EDITABLE_SHOT_FIELDS))
        if bad:
            return jsonify({"error": f"这些字段不允许改：{bad}"}), 400

        path = d / "shots.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return jsonify({"error": f"读不到 shots.json：{exc}"}), 400

        target = None
        for shot in data.get("shots") or []:
            if int(shot.get("id", -1)) == sid:
                target = shot
                break
        if target is None:
            return jsonify({"error": f"没有这个镜号：{sid}"}), 404

        for key, value in patch.items():
            if key == "seed":
                target[key] = int(value) if str(value).strip() else None
            else:
                target[key] = str(value)
        try:
            write_shots_file(d, data)
        except OSError as exc:
            return jsonify({"error": f"写 shots.json 失败：{exc}"}), 500
        return jsonify({"ok": True, "id": sid, "patch": patch})

    @app.post("/api/task")
    def api_create_task():
        """新建任务：拍摄稿来自上传的文件或粘贴的文本，一律落到 `.work/<task>/manuscript.md`。

        两条保护（票 38 / S2）：**先校验再建目录**（否则一次无效提交会留下空任务目录），
        以及**不静默覆盖**已有拍摄稿 —— 要先拿到 `overwrite` 才算数。
        """
        from lvs.workspace import slugify

        name = (request.form.get("task") or "").strip()
        if not name:
            return jsonify({"error": "请给任务起个名字"}), 400

        upload = request.files.get("manuscript")
        if upload is not None and upload.filename:
            body = upload.read().decode("utf-8", errors="replace")
        else:
            body = request.form.get("text") or ""
        if not body.strip():
            return jsonify({"error": "拍摄稿是空的（上传文件或粘贴文本二选一）"}), 400

        task = slugify(name)
        d = store.work_root(app.config["LVS_ROOT"]) / task
        target = d / MANUSCRIPT_NAME

        running = registry.current()
        if running is not None and running.task == task:
            return jsonify({"error": f"任务 {task} 正在跑阶段，先停掉再换拍摄稿"}), 409

        overwrite = (request.form.get("overwrite") or "").strip().lower() in ("1", "true", "yes", "on")
        if target.is_file() and not overwrite:
            return jsonify({
                "task": task, "exists": True,
                "error": f"任务 {task} 已有拍摄稿。确认要覆盖吗？（会替换掉 manuscript.md）",
            }), 409

        d.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return jsonify({"ok": True, "task": task, "path": str(target), "overwrote": overwrite})

    @app.get("/api/task/<name>/manuscript")
    def api_manuscript(name: str):
        d = store.task_dir(name, app.config["LVS_ROOT"])
        if d is None:
            return jsonify({"error": "not found"}), 404
        path = d / MANUSCRIPT_NAME
        if not path.is_file():
            return jsonify({"exists": False, "text": ""})
        return jsonify({"exists": True, "text": path.read_text(encoding="utf-8", errors="replace")})

    # ---- API：运行 ---------------------------------------------------------

    @app.post("/api/task/<name>/shot/<int:sid>/redo")
    def api_redo_shot(name: str, sid: int):
        """按镜重做一张图 —— 走 `lvs studio --redo`（D22 手动流程），不另造轮子。

        `--action` 按该镜自身的来源收窄，这样既快、又不会误伤别的镜（票 30）。
        """
        d = store.task_dir(name, app.config["LVS_ROOT"])
        if d is None:
            return jsonify({"error": "not found"}), 404

        body = request.get_json(silent=True) or {}
        options: dict[str, Any] = {"redo": str(sid), "yes": True}

        source = _shot_source(name, sid)
        if source in store.ASSET_BRANCHES:
            options["action"] = source
        if body.get("prompt"):
            options["prompt"] = str(body["prompt"])
        if body.get("seed") not in (None, ""):
            options["seed"] = str(int(body["seed"]))

        try:
            job = registry.start(name, "studio", options)
        except jobs.JobBusy as exc:
            return jsonify({"error": str(exc), "busy": True}), 409
        except (jobs.JobError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "job": job.as_dict()})

    @app.post("/api/task/<name>/shot/<int:sid>/pick")
    def api_pick_image(name: str, sid: int):
        """用**用户自己挑的图**替换这一镜的画面（票 40）。

        走的是项目本来就有的机制（D14「我用哪张我说了算」）：
        `library_asset` 钉死 → `source=library` → `lvs studio --redo` 重出这一镜。
        不新造一套"钉图"逻辑，也不自己删片段/清缓存（那是 studio 的活，D2）。
        """
        d = store.task_dir(name, app.config["LVS_ROOT"])
        if d is None:
            return jsonify({"error": "not found"}), 404

        # 先看有没有阶段在跑：别在忙的时候改了 shots.json 却起不了 job，留下半套状态
        running = registry.current()
        if running is not None:
            return jsonify({"error": f"正在跑「{running.stage}」，先等它结束再换图", "busy": True}), 409

        upload = request.files.get("image")
        if upload is None or not upload.filename:
            return jsonify({"error": "没有收到图片（表单字段名要用 image）"}), 400

        data = store.read_shots(name, app.config["LVS_ROOT"])
        target = next((s for s in (data.get("shots") or []) if int(s.get("id", -1)) == sid), None)
        if target is None:
            return jsonify({"error": f"没有这个镜号：{sid}"}), 404

        try:
            picked = save_picked_image(d, sid, upload.filename, upload.read())
        except GuiError as exc:
            return jsonify({"error": str(exc)}), 400
        except OSError as exc:
            return jsonify({"error": f"存图失败：{exc}"}), 500

        # 清掉这一镜的旧产物：不清的话 `existing_asset` 会把旧图当"已有"，
        # 新钉的图根本不会被材化；片段也必须删（片段缓存只看时长，票 30）。
        cleared = Workspace(task=name, root=app.config["LVS_ROOT"]).clear_shot_artifacts(sid)

        target["library_asset"] = str(picked)      # 钉死：§8.1 ① 会直接用它
        target["source"] = "library"
        # **必须打上这个标记**：`sources.apply_mode` 靠它认出"这是人挑的，策略不许动"。
        # 不打的后果是，用户换成「本地生图」重跑时，这镜被当成可重定向的镜，
        # `library_asset` 连同他挑的图一起被清掉（sources.STALE_WITH_LIBRARY）。
        target[sources.PIN_FIELD] = True
        for stale in sources.STALE_RESULT_FIELDS:
            target.pop(stale, None)
        target["status"] = "pending"
        try:
            write_shots_file(d, data)
        except OSError as exc:
            return jsonify({"error": f"写 shots.json 失败：{exc}"}), 500

        # 注意：**不能**走 `studio --redo` —— 它的 clear_shot_assets 会 pop 掉
        # `library_asset`（那是"重做要的是新一张"的设计），正好把刚钉上的清掉。
        # 这里只要 assets 把钉死的文件材化进 assets/library/ 就够了。
        try:
            job = registry.start(name, "assets", {"only": "library", "no_library": True})
        except (jobs.JobBusy, jobs.JobError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"ok": True, "picked": str(picked), "cleared": cleared, "job": job.as_dict()})

    @app.get("/api/stage-spec")
    def api_stage_spec():
        return jsonify({
            stage: [
                {"key": o.key, "flag": o.flag, "label": o.label, "kind": o.kind,
                 "choices": list(o.choices), "default": o.default, "hint": o.hint}
                for o in spec
            ]
            for stage, spec in jobs.STAGE_SPEC.items()
        })

    @app.get("/api/job/current")
    def api_job_current():
        job = registry.current()
        return jsonify({"job": job.as_dict() if job else None})

    @app.post("/api/task/<name>/run")
    def api_run(name: str):
        d = store.task_dir(name, app.config["LVS_ROOT"])
        if d is None:
            return jsonify({"error": "not found"}), 404
        body = request.get_json(silent=True) or {}
        stage = str(body.get("stage") or "")
        options = body.get("options") or {}
        if not isinstance(options, dict):
            return jsonify({"error": "options 必须是对象"}), 400

        positional = None
        if stage in jobs.STAGE_POSITIONAL:
            manuscript = d / MANUSCRIPT_NAME
            if not manuscript.is_file():
                return jsonify({"error": "这个任务还没有拍摄稿（先在任务页上传或粘贴）"}), 400
            positional = str(manuscript)

        note = _demote_pexels_if_no_key(name, d, options) if stage == "run" else ""

        try:
            job = registry.start(name, stage, options, positional=positional)
        except jobs.JobBusy as exc:
            return jsonify({"error": str(exc), "busy": True}), 409
        except jobs.JobError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "job": job.as_dict(), "note": note})

    @app.post("/api/job/stop")
    def api_stop():
        stopped = registry.stop()
        return jsonify({"ok": stopped})

    @app.get("/api/job/<job_id>/stream")
    def api_job_stream(job_id: str):
        if registry.get(job_id) is None:
            return jsonify({"error": "not found"}), 404

        def gen():
            for line in registry.stream(job_id):
                yield f"data: {json.dumps(line, ensure_ascii=False)}\n\n"
            yield "event: end\ndata: {}\n\n"

        return Response(gen(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        })

    # ---- API：只读杂项 -----------------------------------------------------

    @app.get("/api/library")
    def api_library():
        """素材库只读（D13：上游资产一律不改）。"""
        config, config_error = current_config()
        if config is None:
            return jsonify({"error": config_error or "没有配置", "dirs": []})
        out = []
        for raw in (config.get("library.dirs", []) or []):
            if not raw:
                continue
            p = Path(str(raw)).expanduser()
            entry = {"path": str(p), "exists": p.is_dir(), "count": 0,
                     "by_suffix": {}, "samples": []}
            if entry["exists"]:
                stems: dict[str, int] = {}
                samples: list[str] = []
                for f in sorted(p.rglob("*")):
                    if not f.is_file():
                        continue
                    suffix = f.suffix.lower()
                    if suffix not in store.ASSET_SUFFIXES:
                        continue
                    stems[suffix] = stems.get(suffix, 0) + 1
                    if len(samples) < 12:
                        samples.append(str(f.relative_to(p)))
                entry["by_suffix"] = stems
                entry["count"] = sum(stems.values())
                entry["samples"] = samples
            out.append(entry)
        return jsonify({"dirs": out, "min_score": config.get("library.min_score", 1)})

    @app.get("/api/doctor")
    def api_doctor():
        """环境体检：复用 `lvs doctor` 的检查函数，只读。"""
        config, config_error = current_config()
        try:
            from lvs import doctor
        except ImportError:      # pragma: no cover
            return jsonify({"error": "doctor 模块不可用"})
        try:
            checks = doctor.all_checks(config if config is not None else Config.empty())
        except Exception as exc:                        # noqa: BLE001 - 体检不应把界面打挂
            return jsonify({"error": str(exc)})
        return jsonify({
            "config_error": config_error,
            "checks": [_check_json(c) for c in checks],
        })

    # ---- 产物 -----------------------------------------------------------------

    @app.get("/thumb/<name>/<int:sid>")
    def media_thumb(name: str, sid: int):
        d = _task_dir_or_404(name)
        src = _shot_asset(name, sid)
        if src is None:
            abort(404)

        cache_dir = d / THUMB_DIRNAME
        cache = cache_dir / f"shot-{sid:03d}.jpg"
        if not thumb_is_fresh(cache, src):
            try:
                from PIL import Image

                cache_dir.mkdir(parents=True, exist_ok=True)
                with Image.open(src) as im:
                    im = im.convert("RGB")
                    ratio = THUMB_WIDTH / max(1, im.width)
                    im = im.resize((THUMB_WIDTH, max(1, int(im.height * ratio))), Image.LANCZOS)
                    im.save(cache, "JPEG", quality=82)
            except Exception:                            # noqa: BLE001 - 缩略图失败就退回原图
                return send_file(src, conditional=True)
        if cache.is_file():
            return send_file(cache, mimetype="image/jpeg", conditional=True)
        return send_file(src, conditional=True)

    @app.get("/media/<name>/<path:rel>")
    def media_file(name: str, rel: str):
        d = _task_dir_or_404(name)
        path = safe_member(d, rel)
        if path is None:
            abort(404)
        guessed = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return send_file(path, mimetype=guessed, conditional=True)

    return app


def _pick_port(host: str, want: int, attempts: int = 20) -> int:
    """从 `want` 起找一个能bind的端口（被占就 +1），省得为了端口去改配置。"""
    import socket

    for offset in range(attempts):
        port = want + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
        return port
    raise GuiError(f"{want}~{want + attempts - 1} 都被占用了，换个端口吧")


def serve(
    root: Path = PROJECT_ROOT,
    config_path: Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8730,
    open_browser: bool = True,
) -> int:
    """起本地服务。**只绑 127.0.0.1** —— 它是本机工具，不给局域网开门，也不做鉴权。"""
    import threading
    import webbrowser

    app = create_app(root, config_path)
    port = _pick_port(host, port)
    url = f"http://{host}:{port}/"

    print(f"LongVideoStudio GUI  →  {url}")
    print("  只监听本机；Ctrl+C 停止服务。")
    print("  关掉浏览器或这个终端都不影响已启动的阶段 —— 它跑在独立子进程里，")
    print("  下次打开界面会从 .work/ 的产物与日志把现场恢复出来。")

    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()

    try:
        app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)
    except KeyboardInterrupt:      # pragma: no cover - 手动 Ctrl+C
        print("\n已停止服务。")
    return 0


# ---- 小工具 ----------------------------------------------------------------


def _task_json(task: store.Task) -> dict[str, Any]:
    return {
        "name": task.name,
        "created_at": task.created_at,
        "shots_total": task.shots_total,
        "has_final": task.has_final,
        "final_duration": task.final_duration,
        "source_mode": task.source_mode,
        "updated": task.updated,
        "done_stages": task.done_stages,
        "stages": [
            {"name": s.name, "label": s.label, "status": s.status,
             "done": s.done, "total": s.total, "at": s.at, "note": s.note}
            for s in task.stages
        ],
    }


def _check_json(check: Any) -> dict[str, Any]:
    """`doctor` 的检查项 -> 界面能渲染的字典（字段名兼容几种形态）。"""
    if isinstance(check, dict):
        return check
    out: dict[str, Any] = {}
    for key in ("name", "label", "title", "status", "ok", "level", "detail", "hint", "message"):
        if hasattr(check, key):
            out[key] = getattr(check, key)
    if not out:
        out = {"name": getattr(check, "__class__", type(check)).__name__, "detail": str(check)}
    return out


def save_picked_image(task_dir: Path, sid: int, filename: str, data: bytes) -> Path:
    """把用户选的图存成任务自己的副本，返回落盘路径（票 40）。

    存到 `picked/shot-NNN.<ext>`（**原图**），再由 `lvs studio --redo` 把它按
    §8.1 ①「`library_asset` 钉死」材化进 `assets/library/`。

    为什么两份分开：`_materialize(src, dst)` 是先 `dst.unlink()` 再 `os.link(src, dst)`，
    若 `library_asset` 正好指向它要材化的那个路径（src == dst），就会**先删掉再链接**，
    把用户选的图弄丢。分开命名就绕开了这个自杀式路径。
    """
    suffix = Path(filename or "").suffix.lower()
    if suffix not in PICK_SUFFIXES:
        raise GuiError(f"只收这些格式的图：{'/'.join(sorted(PICK_SUFFIXES))}（收到 {suffix or '没有后缀'}）")
    if not data:
        raise GuiError("这个文件是空的")
    if len(data) > MAX_PICK_BYTES:
        raise GuiError(f"图太大了（{len(data) / 1048576:.1f} MB，上限 {MAX_PICK_BYTES // 1048576} MB）")

    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(data)) as im:
            im.verify()                       # 后缀不算数，能解码才算数
    except Exception as exc:                  # noqa: BLE001 - 任何解码失败都是"这图不认"
        raise GuiError(f"这张图读不出来（可能不是图片或已损坏）：{exc}") from exc

    directory = task_dir / PICK_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    out = directory / f"shot-{sid:03d}{suffix}"
    out.write_bytes(data)
    return out


def _effective_source_mode(shots_data: dict[str, Any], options: dict[str, Any] | None) -> str:
    """这次运行实际会用哪个来源策略：请求里带的就是它，否则看任务里记着的。

    界面必须和 CLI 算出同一个答案，否则会做出相反的事
    （典型：用户在界面上选了「Pexels 下载」，后台却因为没 key 把镜偷偷改成本地生图）。
    """
    try:
        return sources.resolve((options or {}).get("source"), sources.mode_of(shots_data))
    except sources.SourceModeError:
        return sources.mode_of(shots_data)   # 非法值留给 build_argv 报错


def demote_pexels(shots_data: dict[str, Any], has_pexels_key: bool) -> int:
    """没配 Pexels key 时，把 `source=pexels` 的镜改成本地生图（就地改），返回改了几个。

    委托给 `sources.retarget`（唯一 owner）—— 它同时会清掉这些镜属于旧来源的
    `asset_path`/`status`，正是一支让位给另一支时该做的事。
    理由见票 38 / S3：`lvs run` 碰到 pexels 镜会在 assets 停下，
    不改这一下，界面上的「一键到底」就是个走不通的按钮。
    """
    if has_pexels_key:
        return 0
    return sources.retarget(shots_data, sources.MODE_PEXELS, sources.MODE_LOCAL)


def write_shots_file(task_dir: Path, data: dict[str, Any]) -> None:
    """把 shots.json 写回任务目录。

    这里只决定"写哪个文件"，**格式归 `workspace.write_shots_json`**（唯一 owner）——
    界面手上是任务目录而不是 `Workspace`，所以调那个模块级函数（票 42）。
    """
    write_shots_json(task_dir / "shots.json", data)


def _config_rows(config: Config) -> list[tuple[str, str]]:
    """把配置摊平成「键 → 值」供只读展示。

    遮蔽判定与打码都交给 `configio`（唯一owner）—— 原先这里另写了一套后缀表，
    两家已经开始漂（那边有 `api_key`、这边靠 `key` 子串兜）。
    """
    flat: list[tuple[str, str]] = []
    for section, values in (config.as_dict() or {}).items():
        if not isinstance(values, dict):
            flat.append((str(section), str(values)))
            continue
        for key, value in values.items():
            dotted = f"{section}.{key}"
            flat.append((dotted, configio.mask(value) if configio.is_secret_key(dotted) else str(value)))
    return flat
