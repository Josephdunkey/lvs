"""项目接入（`lvs init` / `lvs/project.py`）的测试。

这一组守的是**"接第二本书不能比第一本更麻烦"**：

- 目录骨架幂等（重复跑不出乱）
- 项目 config **不覆盖**已存在的（里面可能有手工调过的音色与密钥）
- 生成出来的 config 真的能被 `Config.load` 读，且路径派生到该项目自己的素材库
- 体检要能把"还缺什么"说清楚（否则脚手架只是换了个地方让人迷路）
"""

from __future__ import annotations

from pathlib import Path

from lvs import cast as cast_mod
from lvs import project as proj_mod
from lvs import styles as styles_mod
from lvs.config import Config, lib_dir


class _Args:
    def __init__(self, **kw):
        self.lib = None
        self.name = None
        self.style = None
        self.config_out = None
        self.seed = 7777
        self.__dict__.update(kw)


# ---- 骨架 ------------------------------------------------------------------


def test_scaffold_creates_the_canonical_dirs(tmp_path: Path):
    lib = tmp_path / "书" / "10-语料" / "知识视频素材库"
    created, existing = proj_mod.scaffold_lib(lib)
    assert existing == []
    assert {p.name for p in created} == {name for name, _ in proj_mod.LIB_DIRS}
    for name, _why in proj_mod.LIB_DIRS:
        assert (lib / name).is_dir()


def test_scaffold_is_idempotent(tmp_path: Path):
    lib = tmp_path / "lib"
    proj_mod.scaffold_lib(lib)
    (lib / "05-拍摄稿" / "001-实测.md").write_text("x", encoding="utf-8")
    created, existing = proj_mod.scaffold_lib(lib)
    assert created == []
    assert len(existing) == len(proj_mod.LIB_DIRS)
    assert (lib / "05-拍摄稿" / "001-实测.md").is_file(), "已存在的目录内容不许被碰"


def test_scaffold_uses_underscore_cast_dir(tmp_path: Path):
    """★ 必须与 `cast.cast_dir` 的派生值一致 —— 否则 init 建一个、cast 读另一个，
    凭空多出一个空目录而且不报错。"""
    lib = tmp_path / "lib"
    proj_mod.scaffold_lib(lib)
    cfg = _Cfg({"paths.lib": str(lib)})
    assert cast_mod.cast_dir(cfg) == lib / "_cast"
    assert cast_mod.cast_dir(cfg).is_dir()


# ---- 项目 config -----------------------------------------------------------


class _Cfg:
    def __init__(self, data=None):
        self._d = data or {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def has(self, key):
        return key in self._d and self._d[key] not in ("", [], {})


def test_project_config_roundtrips_through_config_loader(tmp_path: Path):
    lib = tmp_path / "书" / "10-语料" / "知识视频素材库"
    proj_mod.scaffold_lib(lib)
    out = tmp_path / "config.mine.toml"
    proj_mod.write_project_config(lib, name="mine", style="chinese-ink-wash", out=out)

    cfg = Config.load(out)
    assert lib_dir(cfg) == lib
    assert cfg.get("shots.style") == "chinese-ink-wash"
    # 派生：定妆库落在**这个项目**的素材库里
    assert cast_mod.cast_dir(cfg) == lib / "_cast"


def test_project_config_is_not_overwritten(tmp_path: Path):
    lib = tmp_path / "lib"
    proj_mod.scaffold_lib(lib)
    out = tmp_path / "config.mine.toml"
    proj_mod.write_project_config(lib, name="mine", style="historical-documentary", out=out)
    out.write_text(out.read_text(encoding="utf-8") + "\n# 手改过\n", encoding="utf-8")
    proj_mod.write_project_config(lib, name="mine", style="cinematic-film-noir", out=out)
    body = out.read_text(encoding="utf-8")
    assert "# 手改过" in body
    assert "cinematic-film-noir" not in body, "已存在的项目 config 不许被覆盖"


# ---- 体检 ------------------------------------------------------------------


def test_audit_lists_missing_creative_artifacts(tmp_path: Path):
    lib = tmp_path / "lib"
    proj_mod.scaffold_lib(lib)
    todos = "\n".join(proj_mod.audit_lib(lib))
    assert "定妆卡" in todos
    assert "拍摄稿" in todos


def test_audit_silent_when_everything_is_in_place(tmp_path: Path):
    lib = tmp_path / "lib"
    proj_mod.scaffold_lib(lib)
    (lib / "00-设定" / "00-风格与人物定妆卡.md").write_text("### 甲\n", encoding="utf-8")
    styles_mod.scaffold_style_file(lib)
    sf = styles_mod.project_style_file(lib)
    sf.write_text('default = "x"\n\n[styles.x]\nsuffix = "y"\n', encoding="utf-8")
    (lib / "05-拍摄稿" / "001-测试-拍摄稿.md").write_text("# t\n", encoding="utf-8")
    todos = proj_mod.audit_lib(lib)
    assert todos == [], f"应无待办，实得：{todos}"


def test_audit_flags_multiple_cards(tmp_path: Path):
    """多份像定妆卡的文件时**不猜** —— 静默挑错一份会污染整个定妆库。"""
    lib = tmp_path / "lib"
    proj_mod.scaffold_lib(lib)
    (lib / "00-设定" / "00-风格与人物定妆卡.md").write_text("a", encoding="utf-8")
    (lib / "00-设定" / "01-配角定妆卡.md").write_text("b", encoding="utf-8")
    todos = "\n".join(proj_mod.audit_lib(lib))
    assert "[cast].card" in todos


def test_audit_ignores_migration_reports_as_manuscripts(tmp_path: Path):
    lib = tmp_path / "lib"
    proj_mod.scaffold_lib(lib)
    d = lib / "05-拍摄稿"
    (d / "001-x-迁移报告.md").write_text("x", encoding="utf-8")
    (d / "_v2" / "001-x.md").parent.mkdir(parents=True, exist_ok=True)
    (d / "_v2" / "001-x.md").write_text("x", encoding="utf-8")
    todos = "\n".join(proj_mod.audit_lib(lib))
    assert "还没有拍摄稿" in todos, "迁移报告与 _v2 不算拍摄稿 —— 否则会谎报'已有稿子'"


# ---- 端到端 ----------------------------------------------------------------


def test_run_init_end_to_end(tmp_path: Path, capsys):
    lib = tmp_path / "书" / "10-语料" / "知识视频素材库"
    out = tmp_path / "config.book.toml"
    code = proj_mod.run_init(_Args(lib=str(lib), name="book", style="chinese-ink-wash", config_out=str(out)))
    printed = capsys.readouterr().out
    assert code == 0
    assert out.is_file()
    assert styles_mod.project_style_file(lib).is_file()
    assert "还差" in printed and "定妆卡" in printed
    assert "lvs styles --config" in printed, "要给出下一步的确切命令"


def test_run_init_rejects_unknown_style(tmp_path: Path, capsys):
    code = proj_mod.run_init(_Args(lib=str(tmp_path / "lib"), name="x", style="no-such-style"))
    assert code == 2
    assert "未知画面风格" in capsys.readouterr().out


def test_run_init_without_lib_prints_usage(capsys):
    assert proj_mod.run_init(_Args()) == 2
    assert "用法" in capsys.readouterr().out


def test_run_init_derives_name_from_path(tmp_path: Path):
    lib = tmp_path / "挪威的森林" / "10-语料" / "知识视频素材库"
    out = tmp_path / "cfg.toml"
    proj_mod.run_init(_Args(lib=str(lib), config_out=str(out)))
    assert "挪威的森林" in out.read_text(encoding="utf-8")


# ---- 项目 config 的 [base] 继承 -------------------------------------------------
#
# 为什么要有这组测试：`lvs init` 生成的项目 config 只写「跟这本书绑定」的项，
# 生成物里明写着「机器相关的项沿用主 config.toml」—— 但 `--config` 是**整体替换**，
# 那句话曾经从来没兑现过：跑 `lvs --config config.某书.toml shots` 会报
# 「未配置 app.openai_api_key」并静默退回启发式拆镜（拆出 445 镜而不是 60 余镜）。
# 修的方案是显式继承（不复制密钥 —— 项目 config 不在 .gitignore 里，复制等于提交密钥）。


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_base_inherits_machine_keys(tmp_path: Path):
    base = _write(tmp_path / "config.toml", """
[app]
openai_api_key = "sk-test"
openai_model_name = "deepseek-chat"

[comfyui]
base_url = "http://127.0.0.1:8188"
""")
    proj = _write(tmp_path / "config.book.toml", """
[base]
config = "config.toml"

[paths]
lib = "D:/libs/book"

[shots]
style = "ukiyo-e-woodblock"
""")
    cfg = Config.load(proj)
    assert cfg.get("app.openai_api_key") == "sk-test"
    assert cfg.get("comfyui.base_url") == "http://127.0.0.1:8188"
    # 项目自己的项照旧
    assert cfg.get("shots.style") == "ukiyo-e-woodblock"
    assert lib_dir(cfg) == Path("D:/libs/book")
    # `[base]` 是加载指令，不是配置项 —— 不许残留在 data 里
    assert "base" not in cfg.as_dict()
    del base


def test_project_overrides_base_on_same_key(tmp_path: Path):
    _write(tmp_path / "config.toml", """
[app]
openai_api_key = "sk-from-main"

[cast]
style_suffix = "main manga style"
width = 1024
""")
    proj = _write(tmp_path / "config.book.toml", """
[base]
config = "config.toml"

[cast]
style_suffix = "book woodblock style"
""")
    cfg = Config.load(proj)
    assert cfg.get("cast.style_suffix") == "book woodblock style", "项目必须赢"
    assert cfg.get("cast.width") == 1024, "同表内没被覆盖的键要留着"


def test_no_base_key_means_no_inheritance(tmp_path: Path):
    """不写 `[base]` 时行为与从前完全一致 —— 临时 config 与测试夹具不受影响。"""
    _write(tmp_path / "config.toml", '[app]\nopenai_api_key = "sk-from-main"\n')
    proj = _write(tmp_path / "config.mine.toml", '[paths]\nlib = "D:/libs/x"\n')
    cfg = Config.load(proj)
    assert not cfg.has("app.openai_api_key")


def test_base_missing_file_is_not_fatal(tmp_path: Path):
    """底配置找不到时只当没继承 —— 不让一句路径写错把整条流水线拦死。"""
    proj = _write(tmp_path / "config.mine.toml", """
[base]
config = "nope-missing.toml"

[paths]
lib = "D:/libs/x"
""")
    cfg = Config.load(proj)
    assert lib_dir(cfg) == Path("D:/libs/x")
    assert "base" not in cfg.as_dict()


def test_base_cycle_does_not_hang(tmp_path: Path):
    """写成环的继承链必须能停 —— 否则加载配置会挂死，且没有任何输出。"""
    a = _write(tmp_path / "a.toml", '[base]\nconfig = "b.toml"\n[paths]\nlib = "D:/a"\n')
    _write(tmp_path / "b.toml", '[base]\nconfig = "a.toml"\n[shots]\nstyle = "x"\n')
    cfg = Config.load(a)
    assert lib_dir(cfg) == Path("D:/a")
    assert "base" not in cfg.as_dict()


def test_init_template_carries_base_key(tmp_path: Path):
    """生成物必须带 `[base]` —— 否则新项目又会踩「读不到 key」那个坑。"""
    lib = tmp_path / "lib"
    proj_mod.scaffold_lib(lib)
    out = tmp_path / "config.new.toml"
    proj_mod.write_project_config(lib, name="new", style="chinese-ink-wash", out=out)
    body = out.read_text(encoding="utf-8")
    assert "[base]" in body and 'config = "config.toml"' in body


# ---- ★ 项目风格文件的 default 必须被 init 尊重（2026-10-03 实测踩到） -------


def _lib_with_style(tmp_path, *, default: str, extra: str = "") -> object:
    lib = tmp_path / "lib"
    (lib / "00-设定").mkdir(parents=True)
    (lib / "00-设定" / "风格预设.toml").write_text(
        f'default = "{default}"\n\n[styles.{default}]\nsuffix = "x"\n{extra}',
        encoding="utf-8",
    )
    return lib


def test_init_follows_the_projects_declared_default_style(tmp_path, monkeypatch, capsys):
    """★★ `lvs init` 必须**跟随**项目风格文件声明的 default，而不是覆盖它。

    真踩到的：雨月物语的项目风格文件写着 `default = "ukiyo-e-woodblock"`
    （浮世绘·怪谈夜色，还为本作专门收紧了后缀），
    而 `lvs init` 往 config 里写的是**内置默认** `historical-documentary`。

    为什么一撞就出问题：`styles.py` 的默认风格优先级是
    **`config [shots].style` > 项目文件的 default > 内置默认** ——
    所以 init 写进 config 的那一个会**盖掉**项目声明，
    一跑 shots 整个画面风格跑偏（浮世绘 → 历史纪录片）。

    修法：让 init 与出图**走同一个真源** —— 给 registry 一个能推出 lib 的 config，
    它自己就会读项目风格文件。
    """
    lib = _lib_with_style(tmp_path, default="ukiyo-e-woodblock")
    out = tmp_path / "config.x.toml"

    args = type("A", (), {"lib": str(lib), "name": "x", "style": None,
                          "config_out": str(out), "seed": None})()
    rc = proj_mod.run_init(args)
    assert rc == 0, capsys.readouterr().out

    body = out.read_text(encoding="utf-8")
    assert 'style = "ukiyo-e-woodblock"' in body, body
    assert "historical-documentary" not in body, "内置默认盖掉了项目声明"


def test_init_explicit_style_still_wins(tmp_path, capsys):
    """显式 `--style` 仍然最高优先 —— 那是人主动要覆盖。"""
    lib = _lib_with_style(tmp_path, default="ukiyo-e-woodblock")
    out = tmp_path / "config.y.toml"

    args = type("A", (), {"lib": str(lib), "name": "y", "style": "jp-youth-manga-bw",
                          "config_out": str(out), "seed": None})()
    assert proj_mod.run_init(args) == 0
    assert 'style = "jp-youth-manga-bw"' in out.read_text(encoding="utf-8")


def test_init_without_project_style_file_falls_back_to_builtin(tmp_path):
    """没有项目风格文件（全新项目）→ 用内置默认（照旧行为）。"""
    lib = tmp_path / "fresh"
    lib.mkdir()
    out = tmp_path / "config.z.toml"
    args = type("A", (), {"lib": str(lib), "name": "z", "style": None,
                          "config_out": str(out), "seed": None})()
    assert proj_mod.run_init(args) == 0
    assert "style = " in out.read_text(encoding="utf-8")


def test_init_accepts_a_custom_style_defined_only_in_the_project_file(tmp_path, capsys):
    """★ 项目里**自定义**的风格名（不在内置里）也要能被 init 认可。

    原先 `build_registry(None)` 推不出 lib → 只读内置 →
    项目自定义的风格会被误判成"不存在"，init 直接报错退出 2。
    """
    lib = tmp_path / "lib2"
    (lib / "00-设定").mkdir(parents=True)
    (lib / "00-设定" / "风格预设.toml").write_text(
        'default = "my-custom-ink"\n\n[styles.my-custom-ink]\nsuffix = "x"\n',
        encoding="utf-8",
    )
    out = tmp_path / "config.c.toml"
    args = type("A", (), {"lib": str(lib), "name": "c", "style": None,
                          "config_out": str(out), "seed": None})()
    rc = proj_mod.run_init(args)
    assert rc == 0, f"自定义风格被误判为不存在：{capsys.readouterr().out}"
    assert 'style = "my-custom-ink"' in out.read_text(encoding="utf-8")
