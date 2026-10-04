"""`cli.main` 的**统一异常兜底**与 `stage.run_stage` 的异常映射。

## 为什么单独一个文件

改造前 `cli.main()` 里没有任何 `try/except`：任何 `LvsError`
（`AssetError` / `TTSError` / `ComfyError` / `CastError` …）会**冒泡出 main()** ——
用户看到一段 Python traceback，退出码变成 Python 默认的 1，**丢掉错误自带的语义**。

`errors.exit_code_for()` 与 `errors.describe()` 就是为这一层写的，
但**以前只被测试调用**，生产代码从不接线 —— 本项目最典型的坑型
（"定义了却从不返回"，上一轮修的是 `EXIT_FAILED`）。

这里钉死三件事：
1. `LvsError` → 打**人话**（不带堆栈），退出码用错误自带语义
2. 未知异常 → 退出码 1，且提示可开 `LVS_DEBUG` 看堆栈
3. `KeyboardInterrupt` → 退出码 1，不当成崩溃
"""

from __future__ import annotations

import pytest

from lvs import cli, errors


def _raiser(exc: BaseException):
    def _fn(argv=None):  # noqa: ANN001, ANN202
        raise exc

    return _fn


def test_lvs_error_maps_to_its_own_exit_code(monkeypatch, capsys):
    class _MyUsage(errors.UsageError):
        pass

    monkeypatch.setattr(cli, "_main", _raiser(_MyUsage("定妆库损坏，无法解析")))
    rc = cli.main([])
    assert rc == errors.EXIT_USAGE == 2

    err = capsys.readouterr().err
    assert "定妆库损坏" in err, "该打错误消息"
    assert "Traceback" not in err, "领域错误不该打堆栈（那是给用户看的）"


def test_blocked_error_maps_to_exit_3(monkeypatch, capsys):
    """★ 2 与 3 必须分开：3 是"等人决定"，不是错误。"""
    monkeypatch.setattr(cli, "_main", _raiser(errors.BlockedError("等你看一眼")))
    assert cli.main([]) == errors.EXIT_BLOCKED == 3


def test_unknown_exception_maps_to_failed(monkeypatch, capsys):
    """未知异常不该被当成"用户的错"（那会是 2），而是失败（1）。"""
    monkeypatch.setattr(cli, "_main", _raiser(ValueError("内部炸了")))
    rc = cli.main([])
    assert rc == errors.EXIT_FAILED == 1

    err = capsys.readouterr().err
    assert "ValueError" in err and "内部炸了" in err
    assert "LVS_DEBUG" in err, "该告诉用户怎么拿到堆栈"


def test_debug_flag_prints_traceback(monkeypatch, capsys):
    monkeypatch.setenv("LVS_DEBUG", "1")
    monkeypatch.setattr(cli, "_main", _raiser(ValueError("boom")))
    cli.main([])
    assert "Traceback" in capsys.readouterr().err


def test_keyboard_interrupt_is_not_a_crash(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_main", _raiser(KeyboardInterrupt()))
    rc = cli.main([])
    assert rc == errors.EXIT_FAILED
    assert "中断" in capsys.readouterr().err


def test_normal_path_returns_untouched(monkeypatch):
    monkeypatch.setattr(cli, "_main", lambda argv=None: 0)
    assert cli.main([]) == 0


# ---- `stage.run_stage`：阶段异常转成返回码 -----------------------------------
#
# 为什么必须在**这一层**转：`run.py` 的 `--keep-going` 判的是"返回码"，
# 异常冒泡的话它根本看不到 —— 于是"某阶段抛异常"就等于"整条崩"，
# `--keep-going` 的语义（失败也往下走）就成了一句空话。


class _Ws:
    task = "t1"

    def path(self, *a):  # noqa: ANN002, ANN201
        from pathlib import Path

        return Path("/tmp") / "x"

    def is_stage_done(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        return False


class _Cfg:
    def get(self, key, default=None):
        return default


def _stage_with(monkeypatch, exc: BaseException | None, code: int = 0):
    from lvs import stage as stage_mod

    def fake_resolve(impl: str):
        def _impl(config, ws, args):  # noqa: ANN001, ANN202
            if exc is not None:
                raise exc
            return code

        return _impl

    monkeypatch.setattr(stage_mod, "resolve_impl", fake_resolve)
    st = stage_mod.Stage("probe", "探针", impl="lvs.shots:run_command")
    ctx = stage_mod.StageContext(config=_Cfg(), ws=_Ws())
    return stage_mod, st, ctx


def test_run_stage_turns_lvs_error_into_code(monkeypatch):
    stage_mod, st, ctx = _stage_with(monkeypatch, errors.UsageError("前置不对"))
    res = stage_mod.run_stage(st, ctx, log=lambda *a: None)
    assert res.code == errors.EXIT_USAGE
    assert not res.ok


def test_run_stage_turns_unknown_error_into_failed(monkeypatch):
    stage_mod, st, ctx = _stage_with(monkeypatch, RuntimeError("炸了"))
    res = stage_mod.run_stage(st, ctx, log=lambda *a: None)
    assert res.code == errors.EXIT_FAILED


def test_run_stage_does_not_swallow_keyboard_interrupt(monkeypatch):
    """★ Ctrl-C 必须能中断 —— 捕获它会让"卡住"看起来像"某阶段失败"。"""
    stage_mod, st, ctx = _stage_with(monkeypatch, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        stage_mod.run_stage(st, ctx, log=lambda *a: None)


def test_run_stage_passes_through_normal_code(monkeypatch):
    stage_mod, st, ctx = _stage_with(monkeypatch, None, code=0)
    res = stage_mod.run_stage(st, ctx, log=lambda *a: None)
    assert res.code == 0 and res.ok
