"""`lvs` 命令行入口。

子命令与流水线阶段一一对应（spec §7）：

    doctor   环境体检（已实现）
    parse    解析拍摄稿 → parse.json
    shots    LLM 拆镜 + 提示词 → shots.json
    assets   素材获取（本地素材库 → Pexels / 本地生图）
    voice    逐镜配音 + 字幕
    build    ffmpeg 合成 → final.mp4
    run      一键串起全部阶段
    library  本地素材库索引

本条（票据 02）只实现 `doctor` 与骨架；其余子命令先占位。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lvs import __version__
from lvs.config import PROJECT_ROOT, Config, ConfigError, find_config
from lvs.doctor import run as run_doctor
from lvs.workspace import Workspace, slugify

DEFAULT_TASK = "default"

# 各命令背后的票据（占位提示里引用，方便对照 .scratch/.../issues/）
_STAGE_ISSUES = {
    "parse": "04、05",
    "shots": "06、07、08",
    "assets": "09、18、19",
    "voice": "10、11、12",
    "build": "13、14、15",
    "run": "16",
    "library": "18",
}


def _configure_stdio() -> None:
    """让中文在任何 Windows 控制台都能打印；不支持时静默忽略。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _common_options() -> argparse.ArgumentParser:
    """各子命令共享的 `--task` / `--config`（票据 02 要求每个子命令都支持 --task）。"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--task", metavar="NAME",
        help="任务名；本次运行的产物落在 .work/<NAME>/（默认 default，或取脚本文件名）",
    )
    parser.add_argument(
        "--config", metavar="PATH",
        help="配置文件路径（默认按顺序找：--config → $LVS_CONFIG → ./config.toml → 仓库根 config.toml）",
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = argparse.ArgumentParser(
        prog="lvs",
        description="LongVideoStudio —— 把长视频拍摄稿逐分镜变成成片",
    )
    parser.add_argument("--version", action="version", version=f"lvs {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    sub.add_parser(
        "doctor", parents=[common],
        help="环境体检：ffmpeg / Python / GPU / LLM key / 可选组件",
    )

    p = sub.add_parser("parse", parents=[common], help="解析拍摄稿 → parse.json")
    p.add_argument("script", metavar="SCRIPT.md", nargs="?", help="拍摄稿路径")

    sub.add_parser("shots", parents=[common], help="LLM 拆镜 + 生成提示词 → shots.json")

    p = sub.add_parser("assets", parents=[common], help="素材获取（本地素材库 → Pexels / 本地生图）")
    p.add_argument("--no-library", action="store_true", help="本次不翻找本地素材库")
    p.add_argument("--force", action="store_true", help="忽略已有产物，强制重做")

    p = sub.add_parser("voice", parents=[common], help="逐镜配音 + 字幕 → audio/ + subtitle.srt")
    p.add_argument("--force", action="store_true", help="忽略已有产物，强制重做")

    p = sub.add_parser("build", parents=[common], help="ffmpeg 合成 → final.mp4")
    p.add_argument("--force", action="store_true", help="忽略已有产物，强制重做")

    p = sub.add_parser("run", parents=[common], help="一键：parse → shots → assets → voice → build")
    p.add_argument("script", metavar="SCRIPT.md", help="拍摄稿路径")
    p.add_argument("--force", action="store_true", help="忽略已有产物，全量重跑")
    p.add_argument("--no-library", action="store_true", help="本次不翻找本地素材库")

    p = sub.add_parser("library", parents=[common], help="本地素材库：扫描并建立索引")
    p.add_argument("action", nargs="?", choices=["index", "scan"], default="index",
                   help="index / scan：扫描素材库目录并生成索引（默认 index）")
    p.add_argument("--reindex", action="store_true", help="忽略缓存，强制重建索引")

    return parser


def _resolve_task(args: argparse.Namespace) -> str:
    """任务名：显式 `--task` 优先，否则取脚本文件名，再否则 default。"""
    if getattr(args, "task", None):
        return slugify(args.task)
    script = getattr(args, "script", None)
    if script:
        return slugify(Path(script).stem)
    return DEFAULT_TASK


def _dispatch(args: argparse.Namespace, config: Config, ws: Workspace) -> int:
    """把命令路由到对应实现。未实现的命令给出占位提示。"""
    if args.command == "parse":
        from lvs import parse as parse_mod

        return parse_mod.run_command(config, ws, args)

    issues = _STAGE_ISSUES.get(args.command, "?")
    print(f"`lvs {args.command}` 尚未实现（对应票据 {issues}）。")
    print(f"  任务目录已就绪：{ws.dir}")
    return 3


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    config_path = find_config(getattr(args, "config", None))

    # doctor：允许没有配置文件（它就是来告诉你缺什么的）
    if args.command == "doctor":
        try:
            config = Config.load(config_path) if config_path else Config.empty()
        except ConfigError as exc:
            print(f"配置错误：{exc}", file=sys.stderr)
            config = Config.empty()
        return run_doctor(config)

    # 其余命令：必须能定位到配置
    if not config_path:
        print(
            "配置错误：未找到配置文件。\n"
            f"  请复制模板：{PROJECT_ROOT / 'config.example.toml'}"
            f"  →  {PROJECT_ROOT / 'config.toml'}",
            file=sys.stderr,
        )
        return 2

    try:
        config = Config.load(config_path)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    ws = Workspace(task=_resolve_task(args)).ensure()
    return _dispatch(args, config, ws)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
