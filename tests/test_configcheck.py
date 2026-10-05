"""`lvs config check`（P2 / 提案 §3 B2）的守卫测试。

`config.toml` 是本项目**唯一"接错就静默"的入口**，所以这里守住三条语义：

- **必填 / 类型写错 → error**（退出码 1，"改了再来"）；
- **未知键只警告** —— 代码里有动态键（`bgm.<key>` / `comfyui.<key>` / `[styles.<名字>]`），
  一刀切会误报；而**误报会让人不再信任校验器**（本项目反复吃过这个亏）；
- **配置本身坏了也要出报告**（TOML 语法错 / 文件不存在）—— 不能先在 cli 那层炸成
  一句"配置错误"、报告里却什么细节都没有。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lvs import configcheck
from lvs.errors import EXIT_FAILED, EXIT_OK, EXIT_USAGE

ROOT = Path(__file__).resolve().parents[1]

#: 最小但合法的配置（必填只有 `paths.lib` 与 `shots.style|visual_mode|source_mode`）。
GOOD_TOML = """\
[paths]
lib = "D:/素材库"

[shots]
style = "水墨"
visual_mode = "graphic"
source_mode = "local"
"""

REAL_CONFIGS = ("config.toml", "config.example.toml", "config.ugetsu.toml", "config.雨月物语.toml")


def _ns(**kw) -> SimpleNamespace:
    ns = SimpleNamespace(json=False, config_command="check")
    for key, value in kw.items():
        setattr(ns, key, value)
    return ns


def _write(tmp_path: Path, text: str, name: str = "cfg.toml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8", newline="\n")
    return p


def _messages(findings, level: str) -> str:
    """把某级别的结论渲成一行 —— 用 `render()`，因为**键名在 render 里**（不在 message 里）。"""
    return " / ".join(f.render() for f in findings if f.level == level)


# ---- 真实配置：必须干净通过 ---------------------------------------------------


@pytest.mark.parametrize("name", REAL_CONFIGS)
def test_real_configs_have_no_errors(name):
    """★ 拿仓库里**真在用的**配置跑（没有就 skip）—— 校验器自己不许误报。"""
    path = ROOT / name
    if not path.is_file():
        pytest.skip(f"仓库里没有 {name}")
    findings, payload = configcheck.check(path)
    assert payload["counts"]["error"] == 0, f"{name} 报了 error：{_messages(findings, 'error')}"


def test_minimal_good_config_passes(tmp_path, capsys):
    path = _write(tmp_path, GOOD_TOML)
    code = configcheck.run_command(path, _ns())
    assert code == EXIT_OK
    assert "✓ 通过" in capsys.readouterr().out


# ---- 必填 / 类型 / 枚举 → error -----------------------------------------------


def test_enum_and_type_mistakes_are_errors(tmp_path):
    path = _write(tmp_path, GOOD_TOML.replace('source_mode = "local"', 'source_mode = "pexels2"')
                  + '\n[tts]\nrate = 12\n')
    findings, payload = configcheck.check(path)
    assert payload["counts"]["error"] == 2, _messages(findings, "error")
    text = _messages(findings, "error")
    assert "source_mode" in text and "auto" in text          # 枚举：说清合法值
    assert "rate" in text and "string" in text               # 类型：说清该是什么


def test_run_command_exit_code_is_one_when_there_are_errors(tmp_path):
    path = _write(tmp_path, '[paths]\nlib = "x"\n')          # 缺 [shots] 三个必填
    assert configcheck.run_command(path, _ns()) == EXIT_FAILED


# ---- 未知键 → **只警告** ------------------------------------------------------


def test_unknown_key_is_only_a_warning(tmp_path, capsys):
    """★ 这是本命令最要紧的一条语义：写错键名**不拦**，但要**说出来**。"""
    path = _write(tmp_path, GOOD_TOML + '\n[provider]\nmode = "local"\n')
    findings, payload = configcheck.check(path)
    assert payload["counts"]["error"] == 0
    assert payload["counts"]["warn"] == 1
    assert "provider" in _messages(findings, "warn")
    assert configcheck.run_command(path, _ns()) == EXIT_OK      # warn 不改变退出码
    assert "未知键" in capsys.readouterr().out


def test_unknown_key_hint_lists_the_known_ones(tmp_path):
    path = _write(tmp_path, GOOD_TOML + '\n[tts]\nspeed = "-10%"\n')
    findings, _ = configcheck.check(path)
    text = _messages(findings, "warn")
    assert "speed" in text and "已知" in text and "rate" in text


def test_dynamic_tables_are_not_reported_as_unknown(tmp_path):
    """`[styles.<名字>]` / `providers.routes.<接入点>` 是**用户自定键名**，不许误报。"""
    path = _write(tmp_path, GOOD_TOML
                  + '\n[styles."我的风格"]\nprompt = "x"\n'
                  + '\n[providers]\nmode = "auto"\n\n[providers.routes]\n"shots.beats" = "local"\n')
    findings, payload = configcheck.check(path)
    assert payload["counts"]["error"] == 0
    assert payload["counts"]["warn"] == 0, _messages(findings, "warn")


# ---- 配置本身坏了也要出报告 ---------------------------------------------------


def test_toml_syntax_error_is_reported_not_raised(tmp_path):
    path = _write(tmp_path, '[paths\nlib = "x"\n')
    findings, payload = configcheck.check(path)
    assert payload["counts"]["error"] == 1
    assert "TOML 语法有误" in _messages(findings, "error")
    assert configcheck.run_command(path, _ns()) == EXIT_FAILED


def test_missing_file_is_a_usage_error(tmp_path, capsys):
    missing = tmp_path / "nope.toml"
    findings, payload = configcheck.check(missing)
    assert payload["counts"]["error"] == 1
    assert "找不到配置文件" in _messages(findings, "error")
    assert configcheck.run_command(missing, _ns()) == EXIT_USAGE
    assert configcheck.run_command(None, _ns()) == EXIT_USAGE


# ---- --json 的形状必须稳定（早退分支也要有） ---------------------------------


@pytest.mark.parametrize("text", [GOOD_TOML, "", "[paths\n"])
def test_json_payload_shape_is_stable(tmp_path, capsys, text):
    """★ `--json` 的形状**任何分支都一样**：agent 拿它当契约，缺字段就是断链。"""
    path = _write(tmp_path, text)
    code = configcheck.run_command(path, _ns(json=True))
    payload = json.loads(capsys.readouterr().out)            # 必须**只**是 JSON
    for key in ("path", "sections", "findings", "counts", "ok"):
        assert key in payload, f"--json 少了 {key}"
    assert set(payload["counts"]) == {"error", "warn", "info"}
    assert payload["ok"] is (payload["counts"]["error"] == 0)
    assert code == (EXIT_OK if payload["ok"] else EXIT_FAILED)


def test_sections_are_recognised(tmp_path):
    path = _write(tmp_path, GOOD_TOML + '\n[budget]\nmode = "warn"\n\n[bgm]\nvolume_db = -18\n')
    _, payload = configcheck.check(path)
    assert {"paths", "shots", "budget", "bgm"} <= set(payload["sections"]["present"])
    assert "publish" in payload["sections"]["missing"]      # 没配的段要显式列出来


def test_section_notes_cover_the_documented_sections(tmp_path):
    """段识别的名单要认得全（少一个段名 = 它在报告里凭空消失）。"""
    _, payload = configcheck.check(_write(tmp_path, GOOD_TOML))
    assert payload["sections"]["present"] == ["paths", "shots"]
    missing = set(payload["sections"]["missing"])
    for section in ("app", "pexels", "tts", "voice", "build", "bgm", "gpu", "comfyui",
                    "cast", "qc", "pipeline", "budget", "providers", "publish", "styles"):
        assert section in missing, f"{section} 不在段名单里"
