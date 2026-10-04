"""工作流模板单元测试（票据 19）。

ComfyUI 的 API 图是一坨 JSON，最容易出的错是：
1. 模板里写了 `{{TOKEN}}` 但 `DEFAULT_VALUES` 没有 → 渲染后占位符原样留在 JSON 里，
   ComfyUI 会报「节点参数非法」，且报错信息完全指不到模板。
2. 节点引用（`["2", 0]`）指向不存在的节点 id → 提交后被拒。
3. 提示词里的引号/换行把 JSON 撑坏。

这些测试不联网、不起 ComfyUI，纯本地校验。
"""

from __future__ import annotations

import json
import re
import unittest

from lvs import imagegen
from lvs.config import PROJECT_ROOT

TEMPLATES = sorted((PROJECT_ROOT / "workflows").glob("*.json"))

# 运行时才填（由 imagegen.generate 从 shot / config 注入），可以不在 DEFAULT_VALUES 里
RUNTIME_TOKENS = {"PROMPT", "SEED", "CKPT", "MODEL"}

TOKEN_RE = re.compile(r"\{\{([A-Z_]+)\}\}")


class TemplatesExistTest(unittest.TestCase):
    def test_at_least_zimage(self):
        names = {p.name for p in TEMPLATES}
        self.assertIn("zimage_turbo.json", names, "主力模板不见了")


class TemplateRenderTest(unittest.TestCase):
    """对所有模板统一做的体检。"""

    def _values(self) -> dict:
        v = dict(imagegen.DEFAULT_VALUES)
        v.update(
            {
                "PROMPT": '中文提示词，「引号」、逗号, 换行\n第二行 \\ 反斜杠',
                "SEED": 123456,
                "CKPT": "z_image_turbo_int8_convrot.safetensors",
            }
        )
        return v

    def test_all_templates_are_json(self):
        for p in TEMPLATES:
            with self.subTest(template=p.name):
                json.loads(p.read_text(encoding="utf-8"))

    def test_no_placeholder_without_default(self):
        """每个占位符要么有默认值，要么由运行时注入 —— 不能裸奔。"""
        for p in TEMPLATES:
            with self.subTest(template=p.name):
                raw = p.read_text(encoding="utf-8")
                tokens = set(TOKEN_RE.findall(raw))
                orphan = tokens - set(imagegen.DEFAULT_VALUES) - RUNTIME_TOKENS
                self.assertEqual(orphan, set(), f"{p.name} 里这些占位符没有默认值：{orphan}")

    def test_render_leaves_no_token(self):
        for p in TEMPLATES:
            with self.subTest(template=p.name):
                raw = p.read_text(encoding="utf-8")
                out = imagegen._render(raw, self._values())
                self.assertNotIn("{{", out, f"{p.name} 渲染后仍有未替换的占位符")
                json.loads(out)  # 必须是合法 JSON

    def test_node_refs_point_to_existing_nodes(self):
        """`["2", 0]` 形式的引用必须指向图里真实存在的节点 id。"""
        for p in TEMPLATES:
            with self.subTest(template=p.name):
                graph = json.loads(imagegen._render(p.read_text(encoding="utf-8"), self._values()))
                for nid, node in graph.items():
                    for key, val in node["inputs"].items():
                        if isinstance(val, list) and val and isinstance(val[0], str):
                            self.assertIn(
                                val[0], graph, f"{p.name} 节点 {nid}.{key} 引用了不存在的节点 {val[0]}"
                            )

    def test_prompt_survives_escaping(self):
        """提示词里的引号/换行/反斜杠必须还原成原文，不能把 JSON 撑坏。"""
        tricky = '他说：「天下大势」。\n第二行\t制表 \\ 反斜杠'
        for p in TEMPLATES:
            with self.subTest(template=p.name):
                graph = json.loads(
                    imagegen._render(
                        p.read_text(encoding="utf-8"), {**self._values(), "PROMPT": tricky}
                    )
                )
                texts = [
                    n["inputs"]["text"]
                    for n in graph.values()
                    if n["class_type"] == "CLIPTextEncode" and n["inputs"].get("text") == tricky
                ]
                self.assertTrue(texts, f"{p.name} 没有把提示词原样放进 CLIPTextEncode")


class ZImageTemplateTest(unittest.TestCase):
    """针对主力模板的结构断言（对着官方模板核对过）。"""

    @classmethod
    def setUpClass(cls):
        raw = (PROJECT_ROOT / "workflows" / "zimage_turbo.json").read_text(encoding="utf-8")
        cls.raw = raw
        cls.graph = json.loads(imagegen._render(raw, {
            **imagegen.DEFAULT_VALUES, "PROMPT": "x", "SEED": 1,
            "CKPT": "z_image_turbo_int8_convrot.safetensors",
        }))

    def _node_of(self, class_type: str) -> dict:
        hits = [n for n in self.graph.values() if n["class_type"] == class_type]
        self.assertEqual(len(hits), 1, f"期望恰好一个 {class_type}")
        return hits[0]

    def test_expected_nodes(self):
        classes = {n["class_type"] for n in self.graph.values()}
        for expected in (
            "UNETLoader",
            "CLIPLoader",
            "VAELoader",
            "CLIPTextEncode",
            "ConditioningZeroOut",
            "ModelSamplingAuraFlow",
            "EmptySD3LatentImage",
            "KSampler",
            "VAEDecode",
            "SaveImage",
        ):
            self.assertIn(expected, classes)

    def test_clip_loader_uses_lumina2(self):
        """Z-Image 的 Qwen3-4B 编码器走 CLIPLoader(type=lumina2)（ZImage 继承 Lumina2）。"""
        self.assertEqual(self._node_of("CLIPLoader")["inputs"]["type"], "lumina2")

    def test_latent_is_flux_sized(self):
        """Z-Image 用 Flux 潜空间：EmptySD3LatentImage（16 通道，/8）。"""
        lat = self._node_of("EmptySD3LatentImage")
        self.assertEqual({"width", "height", "batch_size"}, set(lat["inputs"]))

    def test_sampler_defaults_are_zimage_official(self):
        ks = self._node_of("KSampler")["inputs"]
        self.assertEqual(ks["sampler_name"], "res_multistep")
        self.assertEqual(ks["scheduler"], "simple")
        self.assertEqual(ks["steps"], 8)
        self.assertEqual(ks["cfg"], 1.0)

    def test_shift_is_three(self):
        self.assertEqual(self._node_of("ModelSamplingAuraFlow")["inputs"]["shift"], 3.0)

    def test_seed_override(self):
        graph = json.loads(imagegen._render(self.raw, {
            **imagegen.DEFAULT_VALUES, "PROMPT": "x", "SEED": 987654,
            "CKPT": "m.safetensors",
        }))
        ks = [n for n in graph.values() if n["class_type"] == "KSampler"][0]
        self.assertEqual(ks["inputs"]["seed"], 987654)
        self.assertIsInstance(ks["inputs"]["seed"], int)

    def test_config_overrides_flow_through(self):
        """通用旋钮（尺寸/文件名）由 assets._comfy_extra 覆盖；模型专用参数不受影响（D11）。"""
        graph = json.loads(imagegen._render(self.raw, {
            **imagegen.DEFAULT_VALUES, "PROMPT": "x", "SEED": 1, "CKPT": "m.safetensors",
            "WIDTH": 512, "HEIGHT": 512, "CLIP": "c.safetensors", "VAE": "v.safetensors",
        }))
        nodes = {n["class_type"]: n for n in graph.values()}
        self.assertEqual(nodes["EmptySD3LatentImage"]["inputs"]["width"], 512)
        self.assertEqual(nodes["CLIPLoader"]["inputs"]["clip_name"], "c.safetensors")
        self.assertEqual(nodes["VAELoader"]["inputs"]["vae_name"], "v.safetensors")
        # 采样器/步数等写死在模板里，本模块不再持有它们（D11）
        self.assertEqual(nodes["KSampler"]["inputs"]["sampler_name"], "res_multistep")
        self.assertEqual(nodes["KSampler"]["inputs"]["steps"], 8)


if __name__ == "__main__":
    unittest.main()
