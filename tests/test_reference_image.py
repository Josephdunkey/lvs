"""参考图（L2 · img2img）管道 —— 上传、占位符、互斥判据。

## 这一组守的是什么

出图流程的"人物一致性"分三档（`docs/出图流程-v2-编排方案.md` §四）：

| 档 | 机制 | 状态 |
|---|---|---|
| L1 文字锚定 | `{NAME}` → `lock.json.anchor` 注入提示词 | 早已实现 |
| **L2 参考图 img2img** | 冻结定妆照当参考图，低去噪重绘 | **这组测试守的就是它** |
| L3 LoRA | 云训角色 LoRA | 未做 |

L2 曾长期处于"**半接线**"状态 —— 这个项目最大的坑型：

- `assets._workflow_for` 有分支会读 `[cast].ref_workflow`（选模板）✅
- 但模板文件不存在、`generate()` 没有参考图参数、模板里没有 `{{REF_IMAGE}}` ❌

结果是"**看起来开了 L2，实际还是纯文生图**"。补齐需要三件事同时到位，
所以测试也要三面都钉住：**能上传**、**占位符有值**、**调用方给得出图**。
"""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
import time
from pathlib import Path

import pytest

from lvs import doctor, qc
from lvs.config import Config
from lvs.imagegen import ComfyClient, ComfyError, DEFAULT_VALUES, generate, template_path

ROOT = Path(__file__).resolve().parent.parent


# ---- ① 模板占位符守则（防"模板里加了占位符、代码没给值"） --------------------


#: 由 `generate()` 在运行时注入，不必出现在 `DEFAULT_VALUES` 里。
_RUNTIME_KEYS = {"PROMPT", "SEED", "CKPT", "MODEL"}


def test_every_placeholder_has_a_value():
    """★ 所有工作流模板里的 `{{KEY}}` 都必须有值可填。

    这是 `lvs/imagegen.py` 自己声明的规则：

    > 模板里出现的每个 `{{KEY}}` 都必须在这里有默认值，或写死在模板内，
    > 否则会原样留在 JSON 里、导致 ComfyUI 报「节点参数非法」。

    这条测试把"规则"变成"判据" —— 否则下一个人（包括我自己）加了占位符
    却忘了给默认值，会在**真出图时**才炸，而那时报错是 ComfyUI 说的
    "节点参数非法"，离根因很远。
    """
    missing: list[str] = []
    for tpl in sorted((ROOT / "workflows").glob("*.json")):
        text = tpl.read_text(encoding="utf-8")
        for token in {t.split("}}")[0] for t in text.split("{{")[1:]}:
            if token not in DEFAULT_VALUES and token not in _RUNTIME_KEYS:
                missing.append(f"{tpl.name}: {token}")
    assert missing == [], f"这些占位符没有值可填，会让 ComfyUI 报「节点参数非法」：{missing}"


def test_reference_template_declares_the_placeholder_and_denoise():
    """参考图模板必须同时含 `REF_IMAGE` 与 `DENOISE` —— 少了任一个 L2 就不成立。"""
    text = template_path("zimage_scene_ref.json").read_text(encoding="utf-8")
    assert "{{REF_IMAGE}}" in text, "没有参考图占位符 = 参考图喂不进去"
    assert "{{DENOISE}}" in text, "没有 denoise 占位符 = 无法调那个唯一的松紧旋钮"
    # img2img 的判据：latent 来自 VAEEncode（参考图），而不是空 latent
    graph = json.loads(text.replace('"{{WIDTH}}"', "1").replace('"{{HEIGHT}}"', "1"))
    ks = [n for n in graph.values() if n.get("class_type") == "KSampler"][0]
    src = ks["inputs"]["latent_image"][0]
    assert graph[src]["class_type"] == "VAEEncode", "latent 不是从参考图编码来的，这不是 img2img"


# ---- ② `generate()` 的两条互斥判据（都在联网之前就该炸） ---------------------


def _ref_args(**kw):  # noqa: ANN202
    base = {
        "base_url": "http://127.0.0.1:1",   # 不会被访问：判据在 ensure_alive 之前
        "model": "m.safetensors",
        "seed": 1,
    }
    base.update(kw)
    return base


def test_template_wants_reference_but_none_given(tmp_path: Path):
    """模板要参考图却没给 → **报错**，而不是让 ComfyUI 说"节点参数非法"。"""
    with pytest.raises(ComfyError) as ctx:
        generate("p", tmp_path / "o.png", workflow="zimage_scene_ref.json",
                 ref_image=None, **_ref_args())
    msg = str(ctx.value)
    assert "参考图" in msg
    assert "_workflow_for" in msg, "报错要指向该检查的接线处"


def test_reference_given_but_template_cannot_use_it(tmp_path: Path):
    """★ 反向也要拦：给了参考图、模板却用不上 —— 那正是"以为开了 L2"的来源。"""
    with pytest.raises(ComfyError) as ctx:
        generate("p", tmp_path / "o.png", workflow="zimage_turbo.json",
                 ref_image=tmp_path / "any.png", **_ref_args())
    msg = str(ctx.value)
    assert "用不上" in msg
    assert "纯文生图" in msg


def test_missing_template_names_the_right_config_key(tmp_path: Path):
    with pytest.raises(ComfyError) as ctx:
        generate("p", tmp_path / "o.png", workflow="没有这个.json", **_ref_args())
    assert "[comfyui].workflow" in str(ctx.value)


# ---- ③ 上传：真的把图送进 ComfyUI 的 input/ --------------------------------


class _UploadStub(http.server.BaseHTTPRequestHandler):
    """最小 `/upload/image` 服务：记下收到的 multipart，回一个 ComfyUI 形状的 JSON。"""

    received: list[bytes] = []
    ctype: str = ""

    def do_POST(self):  # noqa: N802 - 标准库约定
        n = int(self.headers.get("Content-Length") or 0)
        type(self).received.append(self.rfile.read(n))
        type(self).ctype = self.headers.get("Content-Type") or ""
        body = json.dumps({"name": "ref.png", "subfolder": "", "type": "input"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # noqa: ANN002 - 静音
        pass


@pytest.fixture
def upload_server():
    _UploadStub.received = []
    _UploadStub.ctype = ""
    srv = socketserver.TCPServer(("127.0.0.1", 0), _UploadStub)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()
    srv.server_close()


def test_upload_sends_multipart_and_returns_the_name(upload_server: str, tmp_path: Path):
    """上传要带上文件名与内容，并返回 `LoadImage` 能用的名字。"""
    img = tmp_path / "cand-01.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)

    name = ComfyClient(upload_server).upload_image(img)

    assert name == "ref.png"
    assert _UploadStub.received, "服务端没收到任何内容"
    body = _UploadStub.received[0]
    assert b'name="image"; filename="cand-01.png"' in body
    assert b'name="overwrite"' in body
    assert b"\x89PNG" in body, "图的内容没被送出去"
    assert _UploadStub.ctype.startswith("multipart/form-data; boundary=")


def test_upload_rejects_a_missing_file(tmp_path: Path):
    with pytest.raises(ComfyError) as ctx:
        ComfyClient("http://127.0.0.1:1").upload_image(tmp_path / "没有这个.png")
    assert "不存在" in str(ctx.value)


# ---- ④ 人脸检测：静默降级必须变成明说 --------------------------------------


def test_face_model_status_reports_a_missing_file_hash():
    ok, why = qc.face_model_status(Path("D:/绝对不存在的目录/yunet.onnx"))
    assert ok is False
    assert why, "失败必须给出原因（原先只返回 None，静默跳过）"


def test_face_model_status_rejects_a_bogus_file(tmp_path: Path):
    """★ 文件在、但不是一个能被 cv2 读的 onnx —— 必须**报出来**。

    实测踩到的正是这种：文件大小对得上、onnxruntime 也能加载，
    但 cv2 读不了，于是 Q006/Q007 静默跳过，而报告看起来一切正常。
    """
    bogus = tmp_path / "yunet.onnx"
    bogus.write_bytes(b"not an onnx at all")
    ok, why = qc.face_model_status(bogus)
    assert ok is False
    assert why


def test_doctor_face_check_passes_when_explicitly_disabled():
    """明确关掉就不再啰嗦（否则用户关不掉这个提示，只能忍受噪音）。"""
    cfg = Config({"qc": {"enable_faces": False}}, None)
    assert doctor.check_face_model(cfg).status == "通过"


def test_doctor_face_check_warns_when_unavailable():
    cfg = Config({"qc": {"enable_faces": True, "face_model": "D:/不存在/yunet.onnx"}}, None)
    r = doctor.check_face_model(cfg)
    assert r.status == "警告"
    assert "Q006" in r.hint and "Q007" in r.hint, "要说清代价：哪两条检查没跑"


# ---- ⑤ 中文路径：cv2 读不了非 ASCII 路径（2026-10-03 实锤的根因）-----------


def test_ascii_cache_copy_passes_ascii_paths_through_untouched(tmp_path: Path):
    p = tmp_path / "yunet.onnx"
    p.write_bytes(b"x")
    assert qc._ascii_cache_copy(p) == p


def test_ascii_cache_copy_moves_chinese_path_to_ascii_cache(tmp_path: Path):
    """★ 权重放在中文目录（用户的常态）→ 必须复制到 ASCII 缓存再加载。

    实锤过的坑：同一份字节，`models/yunet.onnx` 能被 cv2 加载，
    `村上春树/…/yunet.onnx` 报 `Can't read ONNX file` —— 之前被误诊成
    "权重退化"查了很久。缓存按内容哈希命名，换权重自动换缓存。
    """
    中文目录 = tmp_path / "村上春树" / "10-语料"
    中文目录.mkdir(parents=True)
    p = 中文目录 / "yunet.onnx"
    p.write_bytes(b"same-bytes")
    cached = qc._ascii_cache_copy(p)
    assert cached != p
    str(cached).encode("ascii")  # 不抛
    assert cached.read_bytes() == b"same-bytes"
    # 幂等：再调一次命中同一缓存，不重复复制
    assert qc._ascii_cache_copy(p) == cached


def test_face_model_status_loads_a_chinese_path_with_real_weights(tmp_path: Path):
    """★ 端到端：真权重 + 中文路径 → 可用（这正是用户配置的形态）。"""
    from lvs.config import PROJECT_ROOT

    真权重 = PROJECT_ROOT / "models" / "yunet.onnx"
    if not 真权重.is_file():
        真权重 = PROJECT_ROOT / "models" / "yunet_new.onnx"
    if not 真权重.is_file():
        pytest.skip("仓库里没有 YuNet 权重（models/yunet*.onnx），无法端到端验证")
    中文目录 = tmp_path / "挪威的森林" / "定妆"
    中文目录.mkdir(parents=True)
    p = 中文目录 / "yunet.onnx"
    p.write_bytes(真权重.read_bytes())
    ok, why = qc.face_model_status(p)
    assert ok is True, f"中文路径下的真权重必须可用，实际：{why}"


# ---- ⑤ 定妆批准：有候选却没人批 → 提醒 --------------------------------------


def _cast_dir(tmp_path: Path, *, candidates: int, state: str) -> Path:
    root = tmp_path / "_cast"
    (root / "NAOKO" / "v1").mkdir(parents=True)
    for i in range(candidates):
        (root / "NAOKO" / "v1" / f"cand-{i + 1:02d}.png").write_bytes(b"x")
    lock = {
        "version": 2,
        "style": "",
        "characters": {
            "NAOKO": {
                "display": "直子",
                "anchor": "a young woman",
                "state": state,
                "refs": ([str(root / "NAOKO" / "v1" / "cand-01.png")] if state == "REF" else []),
            }
        },
        "locations": {},
        "props": {},
    }
    (root / "lock.json").write_text(json.dumps(lock, ensure_ascii=False), encoding="utf-8")
    return root


def test_pending_candidates_are_reported(tmp_path: Path):
    """★ 核心：候选图出好了、审阅表也在，但一个都没批准 —— 必须主动说。

    实测踩过：5 个角色全是 `state=IMG`，导致含 `{NAME}` 的稿被 G2 拦下，
    而用户完全不知道只差"看一眼审阅表再批"这一步。
    """
    root = _cast_dir(tmp_path, candidates=4, state="IMG")
    (root / "_审阅表.png").write_bytes(b"x")

    r = doctor.check_cast_approval(Config({"cast": {"dir": str(root)}}, None))

    assert r.status == "警告"
    assert "没批准" in r.detail or "没有批准" in r.detail
    assert "NAOKO" in r.detail
    assert "_审阅表.png" in r.hint, "要直接把审阅表的位置给出来"
    assert "approve" in r.hint


def test_approved_character_is_not_reported_as_pending(tmp_path: Path):
    root = _cast_dir(tmp_path, candidates=4, state="REF")
    r = doctor.check_cast_approval(Config({"cast": {"dir": str(root)}}, None))
    assert r.status == "通过"


def test_no_cast_dir_is_not_an_error(tmp_path: Path):
    """没接定妆库 = 本集不需要人物一致性 —— 不该报警（否则人人都在看噪音）。"""
    r = doctor.check_cast_approval(Config({"cast": {"dir": str(tmp_path / "空")}}, None))
    assert r.status == "通过"


def test_both_new_checks_are_registered():
    """★ 新检查必须真的接进体检清单 —— 防"定义了却从不接线"。"""
    names = [c.name for c in doctor.all_checks(Config({}, None))]
    assert "定妆批准" in names, names
    assert "人脸检测" in names, names


# ---- ⑥ 开跑前的汇总：不许让"没生效"看起来像"生效了" --------------------------


class _FakeEntry:
    def __init__(self, state: str, refs: list[str]):
        self.state = state
        self.refs = refs

    @property
    def bindable(self) -> bool:
        return self.state == "REF" and bool(self.refs)

    @property
    def primary(self) -> str:
        return self.refs[0] if self.refs else ""


class _FakeLock:
    def __init__(self, **entries):  # noqa: ANN003
        self.characters = entries


def test_report_plan_says_it_will_not_be_used(capsys):
    """★ 一镜都用不上时必须**说明原因**。

    这条是防"以为开了 L2"的核心 —— 参考图档有三处会悄悄失效，
    不说出来，用户就会把"没生效"当成"生效了"。
    """
    from lvs import assets as assets_mod

    cfg = Config({"cast": {"ref_workflow": "zimage_scene_ref.json"}}, None)
    # 有定妆库、但这一镜没有人物槽位 → 原因该是"无人物槽位"
    assets_mod._report_reference_plan(
        [{"id": 1, "visual": "一片安静的草地"}], cfg, _FakeLock()
    )
    out = capsys.readouterr().out
    assert "都不会" in out
    assert "回退纯文生图" in out
    assert "无人物槽位" in out


def test_report_plan_explains_a_missing_lock(capsys):
    """没有定妆库时也要说清 —— 原因不同，用户要做的事也不同。"""
    from lvs import assets as assets_mod

    cfg = Config({"cast": {"ref_workflow": "zimage_scene_ref.json"}}, None)
    assets_mod._report_reference_plan([{"id": 1, "visual": "{NAOKO} 走过"}], cfg, None)
    out = capsys.readouterr().out
    assert "定妆库" in out


def test_report_plan_counts_what_will_use_it(capsys):
    from lvs import assets as assets_mod

    cfg = Config({"cast": {"ref_workflow": "zimage_scene_ref.json"}}, None)
    lock = _FakeLock(NAOKO=_FakeEntry("REF", ["D:/x/ref.png"]))
    shots = [
        {"id": 1, "visual": "{NAOKO} 走过草地"},
        {"id": 2, "visual": "一片安静的草地"},
    ]
    assets_mod._report_reference_plan(shots, cfg, lock)
    out = capsys.readouterr().out
    assert "1/2 镜" in out
    assert "1 镜回退纯文生图" in out


def test_report_plan_raises_early_on_a_bad_template():
    """模板缺失要在**开跑前**就抛，而不是等第 300 镜。"""
    from lvs import assets as assets_mod

    cfg = Config({"cast": {"ref_workflow": "根本没有.json"}}, None)
    with pytest.raises(assets_mod.AssetError):
        assets_mod._report_reference_plan([{"id": 1, "visual": "{NAOKO} x"}], cfg, None)


# ---- ⑦ 逐镜记下"用没用参考图"（翻图时最想知道这个） -------------------------


def test_per_shot_record_of_reference_usage(tmp_path: Path, monkeypatch):
    """★ 把"这一镜的图是怎么来的"写进 `shots.json`。

    为什么值得：翻 300 张图时，"这一镜为什么脸不一样"的答案只有两种 ——
    **它没用参考图**（空镜 / 角色未批准），或者**用了但 denoise 没调好**。
    不记下来就只能靠猜。
    """
    import types

    from lvs import assets as assets_mod
    from lvs.workspace import Workspace

    ws = Workspace(task="t", root=tmp_path).ensure()
    ws.write_shots({
        "source_mode": "local",
        "shots": [{"id": 1, "source": "local", "narration": "a", "visual": "{NAOKO} 走过"}],
    })
    # ⚠ 刻意**不创建**产物文件：产物已存在时流水线会判定「已完成」直接跳过，
    #   那样 resolve_shot 根本不会被调用，测试就成了假的。
    made = ws.path("assets", "local", "shot-001.png")

    def fake_resolve(shot, **kw):  # noqa: ANN001, ANN003
        return assets_mod.Resolution(
            True, "local", made,
            info={
                "workflow": "zimage_scene_ref.json",
                "ref_source": "D:/cast/NAOKO/v1/cand-01.png",
            },
        )

    monkeypatch.setattr(assets_mod, "resolve_shot", fake_resolve)
    args = types.SimpleNamespace(
        force=False, only=None, no_library=True, source=None,
        no_cast_gate=True, task="t",
    )
    cfg = Config({"comfyui": {"backend": "placeholder"}, "library": {"dirs": []}}, None)

    assets_mod.run_command(cfg, ws, args)

    shot = ws.load_shots()["shots"][0]
    assert shot["reference_image"] == "D:/cast/NAOKO/v1/cand-01.png"
    assert shot["gen_workflow"] == "zimage_scene_ref.json"


def test_per_shot_record_is_absent_when_no_reference_was_used(tmp_path: Path, monkeypatch):
    """没用参考图时**不写**这两个字段 —— 免得每一镜都挂一堆空值。"""
    import types

    from lvs import assets as assets_mod
    from lvs.workspace import Workspace

    ws = Workspace(task="t", root=tmp_path).ensure()
    ws.write_shots({
        "source_mode": "local",
        "shots": [{"id": 1, "source": "local", "narration": "a", "visual": "一片草地"}],
    })
    # ⚠ 刻意**不创建**产物文件：产物已存在时流水线会判定「已完成」直接跳过，
    #   那样 resolve_shot 根本不会被调用，测试就成了假的。
    made = ws.path("assets", "local", "shot-001.png")

    monkeypatch.setattr(
        assets_mod, "resolve_shot",
        lambda shot, **kw: assets_mod.Resolution(
            True, "local", made, info={"workflow": "zimage_turbo.json", "ref_source": ""}
        ),
    )
    args = types.SimpleNamespace(
        force=False, only=None, no_library=True, source=None,
        no_cast_gate=True, task="t",
    )
    cfg = Config({"comfyui": {"backend": "placeholder"}, "library": {"dirs": []}}, None)

    assets_mod.run_command(cfg, ws, args)

    shot = ws.load_shots()["shots"][0]
    assert "reference_image" not in shot


# ---- ⑧ 「图」的粒度：beat 复用（★ 省算力的那个旋钮） -----------------------


class _Bench:
    """最小 bench 替身：只需要 `rendered` 那个 `dict[beat→Path]`。"""

    def __init__(self):
        self.rendered: dict[str, Path] = {}


def _cfg(gran: str) -> Config:
    return Config({"comfyui": {"backend": "placeholder"},
                   "library": {"dirs": []},
                   "shots": {"image_granularity": gran}}, None)


def _shot(i: int, visual: str, prompt: str) -> dict:
    return {"id": i, "source": "local", "kind": "scene",
            "visual": visual, "narration": str(i), "prompt": prompt}


def test_beat_reuse_skips_generation_for_the_same_beat(tmp_path, monkeypatch):
    """★★ 核心：同一条 beat 的 N 个镜，只应**生成一次**。

    为什么重要：分镜数是**旁白的时间粒度**（一句一镜），图是**画面粒度**。
    实测《雨月物语》：435 镜 vs 稿子里声明的 46 个画面位 ——
    每镜一张 = 390 张（3.9 小时），按 beat 复用 ≈ 112 张（1.1 小时）。

    ⚠️ 判据必须是**生成次数**，不是文件数 ——
    复用是"复制已有的图"，每镜照样有自己的文件。第一次我用文件数当判据，
    结果 shot/beat 两种都报 5，看不出差别（假绿）。
    """
    import types

    from lvs import assets as assets_mod
    from lvs.workspace import Workspace

    calls = {"n": 0}
    orig = assets_mod._placeholder_png

    def counting(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(assets_mod, "_placeholder_png", counting)

    ws = Workspace(task="t", root=tmp_path).ensure()
    ws.write_shots({"source_mode": "local", "shots": [
        _shot(1, "土墩", "wide"), _shot(2, "土墩", "close-up"), _shot(3, "土墩", "extreme"),
        _shot(4, "松林", "wide"), _shot(5, "松林", "mid"),
    ]})
    args = types.SimpleNamespace(force=False, only=None, no_library=True,
                                 source=None, no_cast_gate=True, task="t")

    assets_mod.run_command(_cfg("beat"), ws, args)

    assert calls["n"] == 2, f"5 镜 / 2 条 beat 应当只生成 2 次，实际 {calls['n']}"
    # 每镜仍各有自己的产物文件（下游按镜号取图，不能少）
    files = sorted(p.name for p in ws.path("assets", "local").glob("*.png"))
    assert len(files) == 5, files


def test_granularity_shot_is_the_default_and_changes_nothing(tmp_path, monkeypatch):
    """★ 默认必须是 `shot`（每镜一张）—— 不显式开就不改变既有行为。

    这条是"没说要改，却改了"的守门人：默认值一旦漂，所有人都跟着变。
    """
    import types

    from lvs import assets as assets_mod
    from lvs.workspace import Workspace

    calls = {"n": 0}
    orig = assets_mod._placeholder_png
    monkeypatch.setattr(
        assets_mod, "_placeholder_png",
        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), orig(*a, **k))[1],
    )

    ws = Workspace(task="t", root=tmp_path).ensure()
    ws.write_shots({"source_mode": "local", "shots": [
        _shot(1, "土墩", "wide"), _shot(2, "土墩", "close-up"),
    ]})
    args = types.SimpleNamespace(force=False, only=None, no_library=True,
                                 source=None, no_cast_gate=True, task="t")

    assets_mod.run_command(_cfg("shot"), ws, args)

    assert calls["n"] == 2, f"默认应当每镜一张（2 次），实际 {calls['n']}"


def test_beat_reuse_requires_a_bench(tmp_path):
    """没有 bench 时**不复用**（而不是崩）—— 显式降级，不静默改变行为。"""
    from lvs import assets as assets_mod

    dst = tmp_path / "o.png"
    shot = _shot(1, "土墩", "wide")
    assert assets_mod._reuse_beat_image(shot, dst, config=_cfg("beat"), bench=None) is False


def test_beat_reuse_ignores_shots_without_a_visual(tmp_path):
    """没有 beat 可归的镜（`visual` 为空）不去重 —— 否则会把无关的镜并成一张图。"""
    from lvs import assets as assets_mod

    dst = tmp_path / "o.png"
    shot = _shot(1, "", "wide")
    assert assets_mod._reuse_beat_image(shot, dst, config=_cfg("beat"), bench=_Bench()) is False


def test_beat_reuse_copies_the_cached_file(tmp_path):
    """命中缓存时要**真的复制文件**（下游按文件路径取图，不能只给个引用）。"""
    from lvs import assets as assets_mod

    src = tmp_path / "cached.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\nCACHED")
    dst = tmp_path / "out.png"
    bench = _Bench()
    bench.rendered["土墩"] = src

    assert assets_mod._reuse_beat_image(_shot(1, "土墩", "wide"),
                                        dst, config=_cfg("beat"), bench=bench) is True
    assert dst.read_bytes() == src.read_bytes()


def test_bench_exists_even_without_graphic_shots(tmp_path):
    """★★ 纯 local 项目也必须建出 bench。

    踩过的坑：`bench` 原先只在 `need_graphic` 时建 → 纯 local 的项目（雨月物语）
    拿不到 bench → **开关打开但复用静默失效**（数字一点没变）。
    又一次"定义了却从不接线"。所以这里按**生成次数**验，不看开关。
    """
    import types

    from lvs import assets as assets_mod
    from lvs.workspace import Workspace

    calls = {"n": 0}
    orig = assets_mod._placeholder_png
    # 不用 pytest fixture：本用例自己换回来（try/finally 保证还原）
    assets_mod._placeholder_png = lambda *a, **k: (
        calls.__setitem__("n", calls["n"] + 1), orig(*a, **k))[1]
    try:
        ws = Workspace(task="t", root=tmp_path).ensure()
        ws.write_shots({"source_mode": "local", "shots": [
            _shot(1, "土墩", "wide"), _shot(2, "土墩", "close-up"),
        ]})   # 没有任何 graphic 镜
        args = types.SimpleNamespace(force=False, only=None, no_library=True,
                                     source=None, no_cast_gate=True, task="t")
        assets_mod.run_command(_cfg("beat"), ws, args)
    finally:
        assets_mod._placeholder_png = orig

    assert calls["n"] == 1, f"纯 local 项目也该复用（1 次），实际 {calls['n']}"


def test_granularity_report_says_how_many_will_be_generated(capsys):
    """★ 开跑前必须报出"本次要生成多少张"。

    真踩过（2026-10-03）：`[shots].image_granularity` **只在进程启动时读一次**。
    用户改了配置却没重启那个正在跑的 assets 进程 → 静默按老档跑、一镜一张，
    而输出里**一个字都没提粒度**，跑了近一小时才发现没生效。

    报出"复用 N 镜 / 生成 M 张"之后，数字对不上就能立刻发现。
    """
    from lvs import assets as assets_mod

    shots = [
        _shot(1, "土墩", "wide"), _shot(2, "土墩", "close-up"), _shot(3, "土墩", "extreme"),
        _shot(4, "松林", "wide"), _shot(5, "松林", "mid"), _shot(6, "松林", "far"),
    ]
    assets_mod._report_granularity_plan(shots)
    out = capsys.readouterr().out
    assert "生成 2 张" in out, out
    assert "复用 4 镜" in out, out
    assert "2 个画面位" in out, out


def test_granularity_report_stays_quiet_without_local_shots(capsys):
    """没有生图镜时不啰嗦（纯 graphic 的一集没必要看这行）。"""
    from lvs import assets as assets_mod

    assets_mod._report_granularity_plan([
        {"id": 1, "source": "graphic", "kind": "graphic", "visual": "标题", "narration": "x"},
    ])
    assert capsys.readouterr().out == ""
