"""`lvs doctor` 的 ComfyUI 检查单元测试（票据 01/19）。

重点：模型文件核对不能误报——**文件小于 1MB 视为没下完**（`.part` 残留、0 字节占位文件
都是真实踩过的坑），且 models 目录定位不到时必须**降级**而不是抛异常。
"""

from __future__ import annotations

import json
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from lvs import doctor
from lvs.config import Config


class FindModelTest(unittest.TestCase):
    def test_finds_across_subdirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            (root / "text_encoders").mkdir(parents=True)
            (root / "text_encoders" / "clip.safetensors").write_bytes(b"x" * 2_000_000)
            hit = doctor._find_model(root, "clip.safetensors")
            self.assertIsNotNone(hit)
            self.assertEqual(hit.name, "clip.safetensors")

    def test_tiny_file_is_not_a_hit(self):
        """占位文件 / .part 残留（<1MB）不算找到 —— 否则会误报"模型已就绪"。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            (root / "vae").mkdir(parents=True)
            (root / "vae" / "whatever.safetensors").write_bytes(b"x" * 100)
            self.assertIsNone(doctor._find_model(root, "whatever.safetensors"))

    def test_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            root.mkdir(parents=True)
            self.assertIsNone(doctor._find_model(root, "nope.safetensors"))


class ModelsDirTest(unittest.TestCase):
    def test_explicit_config_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = Config({"comfyui": {"models_dir": tmp}}, None)
            self.assertEqual(doctor._comfyui_models_dir(c), Path(tmp))

    def test_falls_back_to_lock_file(self):
        """没显式配置时读 models.lock.json 的 comfyui_dir（本仓库里存在）。"""
        c = Config({}, None)
        got = doctor._comfyui_models_dir(c)
        # 仓库里有 models.lock.json，且 comfyui_dir 配置为 D:\ComfyUI
        if got is not None:
            self.assertEqual(got.name, "models")
            self.assertTrue(got.parent.name.lower().startswith("comfyui"))


class CheckComfyUITest(unittest.TestCase):
    def test_not_alive_is_warn_not_crash(self):
        # 指向一个几乎不可能有服务的端口
        c = Config({"comfyui": {"base_url": "http://127.0.0.1:65533"}}, None)
        res = doctor.check_comfyui(c)
        self.assertEqual(res.status, doctor.WARN)
        self.assertIn("未启动", res.detail)

    def test_alive_but_models_missing_reports_names(self):
        """ComfyUI 在线但模型没下完 → 明确报出缺哪些文件（这是最容易踩的坑）。"""
        import lvs.doctor as doc

        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp)
            for sub in ("diffusion_models", "text_encoders", "vae", "checkpoints"):
                (models / sub).mkdir(parents=True)
            # 只放一个够大的 clip，其余缺失
            (models / "text_encoders" / "present.safetensors").write_bytes(b"x" * 2_000_000)

            c = Config(
                {
                    "comfyui": {
                        "models_dir": tmp,
                        "model": "absent_unet.safetensors",
                        "clip": "present.safetensors",
                        "vae": "absent_vae.safetensors",
                    }
                },
                None,
            )
            orig = doc._probe_http
            doc._probe_http = lambda *a, **k: True  # 假装在线
            try:
                res = doctor.check_comfyui(c)
            finally:
                doc._probe_http = orig

            self.assertEqual(res.status, doctor.WARN)
            self.assertIn("absent_unet.safetensors", res.detail)
            self.assertIn("absent_vae.safetensors", res.detail)
            self.assertNotIn("present.safetensors", res.detail)


class ComfyExtraTest(unittest.TestCase):
    """assets._comfy_extra：只把**用户显式填了**的旋钮翻成模板占位符覆盖值。"""

    def test_empty_config_yields_empty(self):
        from lvs.assets import _comfy_extra

        self.assertEqual(_comfy_extra(Config({}, None)), {})

    def test_maps_keys_to_tokens(self):
        from lvs.assets import _comfy_extra

        c = Config(
            {"comfyui": {"clip": "c.safetensors", "vae": "v.safetensors", "width": 512, "batch": 2}},
            None,
        )
        self.assertEqual(
            _comfy_extra(c),
            {"CLIP": "c.safetensors", "VAE": "v.safetensors", "WIDTH": 512, "BATCH": 2},
        )

    def test_model_specific_keys_are_not_collected(self):
        """采样器/步数/cfg/shift 属模型专用，固定在模板里，不该由 config 注入（D11）。"""
        from lvs.assets import _comfy_extra

        c = Config(
            {"comfyui": {"steps": 4, "cfg": 2.0, "shift": 5.0, "sampler": "euler", "scheduler": "karras"}},
            None,
        )
        self.assertEqual(_comfy_extra(c), {})

    def test_values_feed_template_rendering(self):
        """端到端：config → extra → 模板渲染，配置真的落到了图里。"""
        from lvs.assets import _comfy_extra
        from lvs.imagegen import DEFAULT_VALUES, _render

        c = Config({"comfyui": {"width": 896, "height": 512}}, None)
        raw = (Path(__file__).resolve().parent.parent / "workflows" / "zimage_turbo.json").read_text(
            encoding="utf-8"
        )
        graph = json.loads(
            _render(
                raw,
                {**DEFAULT_VALUES, **_comfy_extra(c), "PROMPT": "x", "SEED": 1, "CKPT": "m.safetensors"},
            )
        )
        lat = [n for n in graph.values() if n["class_type"] == "EmptySD3LatentImage"][0]
        self.assertEqual((lat["inputs"]["width"], lat["inputs"]["height"]), (896, 512))


class TtsBackendCheckTest(unittest.TestCase):
    """★ `doctor` 必须认识**每一个**真实存在的 TTS 后端。

    这里真踩过：加了 `silent` 后端（离线端到端用）却忘了更新 `doctor`，
    于是体检对着一个**完全正常**的配置报"未知后端" —— 误导用户去查一个不存在的问题。
    → 这就是"接线漏一处"，所以判据要写成**从后端清单来**，而不是手写几个字符串。
    """

    def test_every_supported_backend_is_known(self):
        """凡 `make_backend` 认的后端，`doctor` 都必须认识（不报"未知"）。"""
        from lvs.tts import SUPPORTED_BACKENDS, make_backend

        self.assertTrue(SUPPORTED_BACKENDS, "后端清单不能为空")
        for name in sorted(SUPPORTED_BACKENDS):
            cfg = Config({"tts": {"backend": name}}, None)
            # 吃显存的服务型后端要先能连上，没连上时跳过（不是本测试要管的）
            try:
                make_backend(cfg)
            except Exception:  # noqa: BLE001 - 连不上服务就跳过
                continue
            r = doctor.check_tts(cfg)
            self.assertNotIn("未知", r.detail, f"doctor 不认识后端 `{name}`：{r.detail}")

    def test_silent_is_reported_as_silent(self):
        """`silent` 不报错，但**必须**说清它是静音的 —— 免得以为成片有声。"""
        r = doctor.check_tts(Config({"tts": {"backend": "silent"}}, None))
        self.assertIn("静音", r.detail)

    def test_unknown_backend_still_warns(self):
        r = doctor.check_tts(Config({"tts": {"backend": "乱写的"}}, None))
        self.assertIn("未知", r.detail)
        # 提示里要列全可选值（用户才知道该填什么）
        for name in ("edge", "openai_speech", "silent"):
            self.assertIn(name, r.detail)


class WorkSizeCheckTest(unittest.TestCase):
    """§2-16：`.work/` 也要进体检 —— 磁盘不能单向膨胀，而且要告诉人**能腾出多少**。

    实测 2026-10-05 = 9.4 GB / 18 个任务（一期 1.4–2.1 GB）：在这条检查之前，
    这个数字**任何命令都不会告诉你**。
    """

    def _root(self, tmp: str, tasks: dict[str, int]) -> Path:
        root = Path(tmp)
        for name, size in tasks.items():
            d = root / ".work" / name
            d.mkdir(parents=True)
            (d / "blob.bin").write_bytes(b"x" * size)
        return root

    def test_missing_work_dir_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = doctor.check_work_size(Path(tmp))
            self.assertEqual(r.status, doctor.PASS)
            self.assertIn("还没有任务目录", r.detail)

    def test_small_work_dir_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = doctor.check_work_size(self._root(tmp, {"UGE01": 1024}))
            self.assertEqual(r.status, doctor.PASS)
            self.assertIn("1 个任务", r.detail)

    def test_big_work_dir_warns_and_says_how_much_is_reclaimable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, {"UGE01": 4096, "UGE02": 1024})
            before, doctor.WORK_WARN_BYTES = doctor.WORK_WARN_BYTES, 1
            try:
                r = doctor.check_work_size(root)
            finally:
                doctor.WORK_WARN_BYTES = before
            self.assertEqual(r.status, doctor.WARN)
            self.assertIn("2 个任务", r.detail)
            self.assertIn("lvs clean", r.hint)          # 要给出**能照抄的命令**
            self.assertIn("--keep 1", r.hint)
            self.assertIn("不受影响", r.hint)            # 必须说清"不碰素材库"


class OptionalDepsCheckTest(unittest.TestCase):
    """§2-21：GUI / 投稿物料 / 生图质检三套可选依赖 —— 缺哪条说哪条，并给装法。"""

    def test_reports_pass_or_names_the_missing_group(self):
        r = doctor.check_optional_deps()
        self.assertIn(r.status, (doctor.PASS, doctor.WARN))
        if r.status == doctor.PASS:
            for label in ("GUI", "投稿物料/拼版", "生图质检"):
                self.assertIn(label, r.detail)
        else:
            self.assertIn("缺", r.detail)
            self.assertIn("pip install", r.detail)

    def test_missing_module_is_named_with_its_install_command(self):
        fake = (("假功能", ("lvs_no_such_module_for_test",), "pip install -e .[gui]"),)
        before, doctor._OPTIONAL_GROUPS = doctor._OPTIONAL_GROUPS, fake
        try:
            r = doctor.check_optional_deps()
        finally:
            doctor._OPTIONAL_GROUPS = before
        self.assertEqual(r.status, doctor.WARN)
        self.assertIn("lvs_no_such_module_for_test", r.detail)
        self.assertIn("pip install -e .[gui]", r.detail)


class AgentReportTest(unittest.TestCase):
    """§2-21：`--agent` = 人话在前、JSON 在后，**同一份数据**（all_checks 是真源）。"""

    def test_both_new_checks_are_registered(self):
        names = [c.name for c in doctor.all_checks(Config({}, None))]
        self.assertIn("磁盘 .work", names, names)
        self.assertIn("可选依赖", names, names)

    def test_agent_report_lists_only_problems_then_prints_json(self):
        checks = [
            doctor.CheckResult("甲", doctor.PASS, "没问题"),
            doctor.CheckResult("乙", doctor.WARN, "缺个东西", hint="第一句\n第二句"),
            doctor.CheckResult("丙", doctor.MISSING, "必须有"),
        ]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor._print_agent_report(Config({}, None), checks)
        out = buf.getvalue()

        human = out[:out.index("{")]                    # JSON 段之前的才是"人话段"
        self.assertIn("[警告] 乙：缺个东西", human)
        self.assertIn("↳ 第一句", human)
        self.assertNotIn("第二句", human)               # hint 只带第一句，别把提示全铺开
        self.assertIn("缺失 1 / 警告 1", human)
        self.assertNotIn("甲：没问题", human)           # 通过项不进人话段

        payload = json.loads(out[out.index("{"):])      # JSON 段必须可解析（全文都在）
        self.assertEqual(payload["missing"], ["丙"])
        self.assertEqual(payload["warned"], ["乙"])
        self.assertEqual(len(payload["checks"]), 3)


class AgentReportExitCodeTest(unittest.TestCase):
    def test_run_with_agent_returns_1_only_when_something_is_missing(self):
        cfg = Config({}, None)
        before = doctor.all_checks
        doctor.all_checks = lambda config: [doctor.CheckResult("甲", doctor.WARN, "小问题")]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = doctor.run(cfg, as_agent=True)
        finally:
            doctor.all_checks = before
        self.assertEqual(code, 0)                       # 只有警告不算失败（退出码只看"缺失"）
        self.assertIn("体检 1 项", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
