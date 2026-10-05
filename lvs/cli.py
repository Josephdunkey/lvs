"""`lvs` 命令行入口。

子命令与流水线阶段一一对应（spec §7）：

    doctor   环境体检（已实现）
    status   续跑摘要：一行一任务（门禁 / 产物 / 下一步）—— **续跑第一步跑这个**
    map      行号锚定读取：show / grep / ls（代替 numbered 转储）
    parse    解析拍摄稿 → parse.json
    shots    LLM 拆镜 + 提示词 → shots.json
    assets   素材获取（本地素材库 → Pexels / 本地生图）
    voice    逐镜配音 + 字幕
    build    ffmpeg 合成 → final.mp4
    run      一键串起全部阶段
    library  本地素材库索引
    image    手动生图（单张/多张），落 .work/_imagegen/
    board    看板：把任务进度渲染成看板（可选 HTML）
    studio   手动向导：素材获取带检查点（与 `lvs run` 全自动并存）
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

from lvs import __version__, errors, sources
from lvs.config import PROJECT_ROOT, Config, ConfigError, find_config
from lvs.doctor import run as run_doctor
from lvs.workspace import Workspace, slugify

# 素材来源策略（票 41）：四个入口共用同一份说明，免得措辞各写各的
SOURCE_HELP = (
    "实拍镜的素材来源策略 —— auto（默认，逐镜判定）/ pexels（下载）/ "
    "local（本地生图）/ library（本地素材库，未命中自动回退本地生图）。"
    "图文/图表 beat 与手工钉死的镜不受影响"
)

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
    "board": "16",
    "studio": "30",
    "gui": "36",
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
        help="任务名；本次运行的产物落在 .work/<NAME>/（默认 default，或取拍摄稿文件名）",
    )
    parser.add_argument(
        "--config", metavar="PATH",
        help="配置文件路径（默认按顺序找：--config → $LVS_CONFIG → ./config.toml → 仓库根 config.toml）",
    )
    # P1 逃生阀：本次不读不写 LLM 缓存（环境变量 LVS_LLM_CACHE=0 同效）。
    parser.add_argument(
        "--no-cache", action="store_true",
        help="本次不读不写 LLM 缓存（默认缓存；同 LVS_LLM_CACHE=0）",
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    style_help = (
        "画面风格名（默认取 config [shots].style）。"
        "`lvs styles` 列出全部预设与说明；项目自定义风格写 "
        "<素材库>/00-设定/风格预设.toml"
    )
    common = _common_options()
    parser = argparse.ArgumentParser(
        prog="lvs",
        description="LongVideoStudio —— 把长视频拍摄稿逐分镜变成成片",
    )
    parser.add_argument("--version", action="version", version=f"lvs {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    p = sub.add_parser(
        "doctor", parents=[common],
        help="环境体检：ffmpeg / Python / GPU / LLM key / 可选组件",
    )
    # ★ `doctor.run(as_json=…)` 早就实现了，但 CLI **从来没传过**、连开关都没定义 ——
    # 典型的"定义了却从不接线"。agent 在跑长任务前要机器可读地确认环境，
    # 没有这个开关就只能去解析 `[警告] xxx` 这种中文散文。
    p.add_argument("--json", action="store_true", help="机器可读输出（体检项数组，给 Agent 用）")
    p.add_argument("--agent", action="store_true",
                   help="先打 ≤20 行人话结论（只列没通过的），再接 JSON —— 排障用")

    p = sub.add_parser("parse", parents=[common], help="解析拍摄稿 → parse.json")
    p.add_argument("manuscript", metavar="拍摄稿.md", nargs="?", help="拍摄稿路径")
    p.add_argument("--json", action="store_true", help="机器可读输出（统一结果信封，给 Agent 用）")

    p = sub.add_parser("shots", parents=[common], help="LLM 拆镜 + 生成提示词 → shots.json")
    p.add_argument("--force", action="store_true", help="忽略已有产物与人工编辑，强制重做")
    p.add_argument("--no-llm", action="store_true", help="不调用 LLM，用启发式拆镜（无 key 时可跑通）")
    p.add_argument(
        "--visual", choices=["graphic", "photo"], metavar="MODE",
        help="画面模式：graphic（默认，图文图表感：图表 beat 交图表渲染器）/ "
             "photo（实拍剧照感：所有 beat 都当场景翻译，一律走生图）",
    )
    p.add_argument("--source", choices=list(sources.MODES), metavar="MODE", help=SOURCE_HELP)
    p.add_argument("--style", metavar="NAME", help=style_help)
    p.add_argument("--style-file", metavar="PATH", help="指定风格预设文件（TOML），临时试风格用")
    p.add_argument("--no-negation-repair", action="store_true",
                   help="不把含否定式的提示词交回 LLM 改写"
                        "（cfg=1.0 的蒸馏模型没有负向引导，否定词里的名词会被按字面画出来）")
    p.add_argument("--json", action="store_true", help="机器可读输出（统一结果信封，给 Agent 用）")
    # 只读读口（S2）：不跑拆镜、不建任务目录，只把 shots.json 摘成几行。
    p.add_argument("--peek", type=int, metavar="ID",
                   help="只读：打印这一镜的摘要（约 300 token）")
    p.add_argument("--index", action="store_true",
                   help="只读：一镜一行全表（516 KB → 约 30 KB）")
    p.add_argument("--index-width", type=int, default=40, metavar="N",
                   help="--index 场景列的截断宽度（默认 40 字）")
    p.add_argument("--index-limit", type=int, metavar="N", help="--index 只看前 N 镜")
    p.add_argument("--full", action="store_true",
                   help="--peek 打印提示词全文（默认截到 140 字）")

    p = sub.add_parser("assets", parents=[common], help="素材获取（本地素材库 → Pexels / 本地生图）")
    p.add_argument("--no-library", action="store_true", help="本次不翻找本地素材库")
    p.add_argument("--only", metavar="LIST",
                   help="只取这些来源，逗号分隔：pexels,local,graphic,library（其余分镜本次不动）")
    p.add_argument("--shots", metavar="LIST",
                   help="只做这些镜号，逗号分隔、支持区间（如 `1-10,25,40-42`）；"
                        "试水与补镜用，其余镜本次不动")
    p.add_argument("--source", choices=list(sources.MODES), metavar="MODE", help=SOURCE_HELP)
    p.add_argument("--no-cast-gate", action="store_true",
                   help="跳过定妆闸门（人物未冻结也照跑；仅用于纯空镜集，会牺牲人物一致性）")
    p.add_argument("--force", action="store_true", help="忽略已有产物，强制重做")
    p.add_argument("--json", action="store_true", help="机器可读输出（统一结果信封，给 Agent 用）")

    p = sub.add_parser("voice", parents=[common], help="逐镜配音 + 字幕 → audio/ + subtitle.srt")
    p.add_argument("--force", action="store_true", help="忽略已有产物，强制重做")
    p.add_argument("--json", action="store_true", help="机器可读输出（统一结果信封，给 Agent 用）")

    p = sub.add_parser("build", parents=[common], help="ffmpeg 合成 → final.mp4")
    p.add_argument("--force", action="store_true", help="忽略已有产物，强制重做")
    p.add_argument("--json", action="store_true", help="机器可读输出（统一结果信封，给 Agent 用）")

    p = sub.add_parser(
        "publish", parents=[common],
        help="投稿物料：B站/抖音 封面（**已叠字**）+ 标题候选 + 简介 + 标签",
    )
    p.add_argument("--out", metavar="DIR", help="同时把物料拷到这个目录（通常是素材库的 08-投稿/）")
    p.add_argument("--base", metavar="PNG", help="封面底图（默认自动挑任务里最后一张画面）")
    p.add_argument("--lines", metavar="TEXT", help="封面三行字，用 | 分隔（默认取拍摄稿【封面文案】）")
    p.add_argument("--llm", action="store_true", help="用 LLM 润色标题/简介（需配置 key；失败自动退回）")
    p.add_argument("--json", action="store_true", help="机器可读输出（统一结果信封，给 Agent 用）")

    p = sub.add_parser("run", parents=[common], help="一键：parse → shots → assets → voice → build")
    p.add_argument("manuscript", metavar="拍摄稿.md", nargs="?", help="拍摄稿路径（--demo 时可省略）")
    p.add_argument("--force", action="store_true", help="忽略已有产物，全量重跑")
    p.add_argument("--no-library", action="store_true", help="本次不翻找本地素材库")
    p.add_argument("--no-cast-gate", action="store_true", help="跳过定妆闸门（同 `lvs assets --no-cast-gate`）")
    p.add_argument("--skip-gates", action="store_true",
                   help="本次不检查流水线门禁（人审闸门）。默认检查 —— 关掉要显式说")
    p.add_argument("--no-llm", action="store_true", help="拆镜不调用 LLM，用启发式")
    p.add_argument(
        "--visual", choices=["graphic", "photo"], metavar="MODE",
        help="画面模式（同 `lvs shots --visual`）",
    )
    p.add_argument("--source", choices=list(sources.MODES), metavar="MODE", help=SOURCE_HELP)
    p.add_argument("--style", metavar="NAME", help=style_help)
    p.add_argument("--style-file", metavar="PATH", help="指定风格预设文件（TOML），临时试风格用")
    p.add_argument("--keep-going", action="store_true",
                   help="**硬失败**（前置条件不对，退出码 2）也不中断，继续跑后面的阶段。"
                        "逐镜的**部分失败**（退出码 1）本来就会继续，不受此开关影响；"
                        "门禁拦住（退出码 3）始终会停。")
    p.add_argument("--demo", action="store_true", help="走路骨架：3 个硬编码分镜跑通全链路（无需 key/网络素材）")
    p.add_argument("--json", action="store_true", help="机器可读输出（统一结果信封，给 Agent 用）")

    p = sub.add_parser("library", parents=[common], help="本地素材库：扫描并建立索引")
    p.add_argument("action", nargs="?", choices=["index", "scan"], default="index",
                   help="index / scan：扫描素材库目录并生成索引（默认 index）")
    p.add_argument("--reindex", action="store_true", help="忽略缓存，强制重建索引")

    p = sub.add_parser("board", parents=[common], help="看板：把任务进度渲染成看板（可选 HTML）")
    p.add_argument("--html", action="store_true", help="同时输出 .work/<task>/board.html")
    p.add_argument("--json", action="store_true", help="机器可读输出（进度与下一步，给 Agent 用）")

    p = sub.add_parser(
        "gui", parents=[common],
        help="本地图形界面（只监听 127.0.0.1）：建任务 / 跑阶段 / 镜级审片 / 看成片",
    )
    p.add_argument("--port", type=int, default=8730, metavar="N",
                   help="监听端口（默认 8730，被占用自动往后找）")
    p.add_argument("--no-open", action="store_true", help="不自动打开浏览器")

    p = sub.add_parser(
        "studio", parents=[common],
        help="手动向导：素材获取带检查点（与 `lvs run` 全自动并存）",
    )
    p.add_argument("manuscript", metavar="拍摄稿.md", nargs="?", help="拍摄稿路径（也可沿用任务里已解析的那份）")
    p.add_argument("--action", metavar="LIST",
                   help="本次取哪些素材，逗号分隔：pexels,local,graphic,library（或 all）；默认交互询问")
    p.add_argument("--source", choices=list(sources.MODES), metavar="MODE", help=SOURCE_HELP)
    p.add_argument("--redo", metavar="IDS", help="只重做这些镜（如 1-10,15），配合 --prompt/--seed")
    p.add_argument("--prompt", metavar="TEXT", help="改图时替换该镜的提示词")
    p.add_argument("--seed", type=int, help="改图时指定 seed（默认在上次基础上 +1）")
    p.add_argument("--visual", choices=["graphic", "photo"], metavar="MODE", help="画面模式（传给 shots）")
    p.add_argument("--redo-shots", action="store_true", help="强制重做拆镜（例如换了 --visual）")
    p.add_argument("--yes", action="store_true", help="所有提问取默认值（非交互；默认继续下一步）")
    p.add_argument("--stop", action="store_true", help="素材完成后就停，不继续 voice/build")

    p = sub.add_parser("image", parents=[common], help="手动生图（单张/多张）→ .work/_imagegen/")
    p.add_argument("--prompt", metavar="TEXT", help="生图提示词（不填则用 --from-shot 从 shots.json 取）")
    p.add_argument("--from-shot", type=int, metavar="N", help="取 .work/<task>/shots.json 第 N 镜的提示词")
    p.add_argument("--out", metavar="PATH", help="输出文件路径（默认 .work/_imagegen/<name>.png）")
    p.add_argument("--seed", type=int, help="随机种子（默认取 config [comfyui].seed）")
    p.add_argument("--count", type=int, default=1, metavar="N", help="连出 N 张（seed 递增，默认 1）")
    p.add_argument("--width", type=int, help="覆盖宽度（默认取 config 或模板默认）")
    p.add_argument("--height", type=int, help="覆盖高度")
    p.add_argument("--workflow", metavar="FILE", help="覆盖 [comfyui].workflow，如 sdxl.json")
    p.add_argument("--model", metavar="NAME", help="覆盖 [comfyui].model")

    # ---- 出图闸门：格式校验 / 定妆 / 巡检（v2）----

    p = sub.add_parser(
        "check", parents=[common],
        help="拍摄稿格式校验：出稿即验，不等到 parse（出图链路的 G0 闸门）",
    )
    p.add_argument("manuscript", metavar="拍摄稿.md", nargs="?", help="拍摄稿路径")

    p = sub.add_parser(
        "migrate", parents=[common],
        help="拍摄稿 v2 迁移：补 [场景]/[图表] 标注与 {NAME} 人物槽位，并列出待人工判断项",
    )
    p.add_argument("manuscript", metavar="拍摄稿.md", nargs="?", help="原拍摄稿路径")
    p.add_argument("--out", metavar="DIR", help="输出目录（默认原目录下 _v2/；绝不覆盖原稿）")
    p.add_argument("--cast-dir", metavar="PATH", help="定妆库目录（默认取 config [cast].dir）")
    p.add_argument("--pronoun", metavar="P=ID", action="append",
                   help="补充/覆盖代词映射，可重复，如 `--pronoun 他=渡边彻`。"
                        "默认从定妆卡的「代词：」行推断（唯一才换）")

    p = sub.add_parser(
        "cast", parents=[common],
        help="定妆：提取人物 → 出候选定妆照 → 审阅批准 → 冻结参考（出图链路的 G2 闸门）",
    )
    p.add_argument("--dir", metavar="PATH", help="定妆库目录（默认取 config [cast].dir 或仓库根 cast/）")
    p.add_argument("--extract", action="store_true",
                   help="从本任务的拍摄稿与定妆卡提取人物，写入 cast.json")
    p.add_argument("--render", metavar="IDS",
                   help="出候选定妆照：人物 ID 逗号分隔，或用 ALL 出全部有锚定描述的")
    p.add_argument("--count", type=int, default=4, metavar="N", help="每人候选张数（默认 4）")
    p.add_argument("--seed", type=int, help="候选起始 seed（默认取 config [cast].seed）")
    p.add_argument("--variant", type=int, default=1, metavar="N",
                   help="用第 N 段锚定描述（定妆卡里一人可能有两段，如渡边彻的青年/中年）")
    p.add_argument("--approve", metavar="IDS",
                   help="批准并冻结：ID 列表，可写 NAME=vN 指定版本（默认取最新一轮）")
    p.add_argument("--reject", metavar="IDS",
                   help="打回某轮候选：ID 列表，可写 NAME=vN（移入 _rejected/，不删）")
    p.add_argument("--from-file", metavar="PATH", help="批准时改用这张现成的图作为参考")
    p.add_argument("--note", metavar="TEXT", help="打回原因（写进 lock.json，供下次调参）")
    p.add_argument("--sheet", nargs="?", const="", metavar="PATH",
                   help="只重出审阅表（把各人候选拼成一张图），可指定输出路径")
    p.add_argument("--lint", nargs="?", const="", metavar="IDS",
                   help="体检锚定描述（不可画的写法：度量/动作/比喻/否定/属性过多/特征没前置）")

    p = sub.add_parser(
        "qc", parents=[common],
        help="生图巡检：完整性 / 彩度 / 人数 / 提示词版本（出图链路的 G3 闸门）",
    )
    p.add_argument("--block", type=int, metavar="N", help="每块多少张（默认取 config [qc].block=20）")
    p.add_argument("--only", metavar="IDS", help="只查这些镜，如 1-20,35")
    p.add_argument("--fix", action="store_true", help="彩度超标就地转灰度（原图移入 _fixed/）")
    p.add_argument("--sheet", action="store_true",
                   help="同时生成带镜号的缩略图拼版（contact-sheet-NN.png，给多模态模型一眼看一批）")
    # ★ `--final`：**同一个命令的两副面孔**。默认还是生图巡检（G3 门禁的一环）；
    # 加了 `--final` 才是**成片自检**（ffprobe + 抽帧 + 音量 + 字幕）。
    # 为什么不单开一个命令：它是"同一件事的另一半"（查产物），且**只报警不改判** ——
    # 与生图巡检共用 `qc` 这个名字，人少记一个词（详见 lvs/qc_final.py 顶部）。
    p.add_argument("--final", action="store_true",
                   help="→成片自检：ffprobe（时长/流/分辨率）+ 4 帧抽检 + 音频静音/削波 + 字幕在场")
    p.add_argument("--video", metavar="PATH",
                   help="成片路径（默认 .work/<task>/final.mp4）")
    p.add_argument("--frames", type=int, metavar="N", help="抽检帧数（默认 4）")
    p.add_argument("--subtitle", metavar="PATH",
                   help="字幕路径（默认找成片同目录的 subtitle.srt；给 none = 字幕已烧进画面，不查）")
    p.add_argument("--out", metavar="PATH",
                   help="自检报告写到这个路径（默认 .work/<task>/qc/final-report.json）")
    p.add_argument("--json", action="store_true", help="机器可读输出（统一结果信封，给 Agent 用）")

    p = sub.add_parser(
        "styles", parents=[common],
        help="画面风格预设：列出全部可选项（按题材分组、标注是否单色）",
    )
    p.add_argument("tag", nargs="?", metavar="分类", help="只列某一类，如 `黑白版画` / `摄影电影`")
    p.add_argument("--style-file", metavar="PATH", help="指定风格预设文件（TOML）")
    p.add_argument("--current", metavar="NAME", help="标出「当前在用」是哪个（默认取 config）")
    p.add_argument("--json", action="store_true", help="机器可读输出（给 Agent 选风格用）")

    p = sub.add_parser(
        "init",
        help="接入一个新项目（书/题材）：建素材库骨架 + 写项目风格与配置 + 体检待办",
    )
    p.add_argument("lib", metavar="素材库路径", help="该项目的素材库根目录")
    p.add_argument("--name", metavar="NAME", help="项目名（默认取素材库的上级目录名）")
    p.add_argument("--style", metavar="NAME", help="画面风格名（默认取内置默认；`lvs styles` 看全部）")
    p.add_argument("--config-out", metavar="PATH", help="项目配置文件输出路径（默认仓库根 config.<名>.toml）")
    p.add_argument("--seed", type=int, default=7777, help="定妆候选起始 seed（默认 7777）")

    p = sub.add_parser(
        "clean",
        help="清理 .work/ 下的任务目录（默认**只报告**，加 --yes 才真删）",
    )
    # 刻意**不**继承 common：clean 是纯本地操作，不读配置，也不建任务目录。
    # 自带 `--task` 才能把措辞写准（common 那份说的是"本次运行的产物落在…"）。
    p.add_argument("--task", dest="task_filter", metavar="NAME",
                   help="只清理这一个任务目录")
    p.add_argument("--keep", type=int, metavar="N", help="保留最近 N 个任务，其余清掉")
    p.add_argument("--all", action="store_true", help="清掉全部任务目录")
    p.add_argument("--yes", action="store_true", help="真的删除（不加则只报告）")

    p = sub.add_parser(
        "gate", parents=[common],
        help="流水线门禁账本：看哪道门没放行 / 批准 / 打回 / 跳过（每个环节的人审闸门）",
    )
    p.add_argument("--approve", metavar="GATE", help="批准某道门（G0..G5 或 key，如 script/cast）")
    p.add_argument("--reject", metavar="GATE", help="打回某道门（必须配 --note）")
    p.add_argument("--skip", metavar="GATE", help="跳过某道门（必须配 --note，会记进账本）")
    p.add_argument("--reset", nargs="?", const="", metavar="GATE",
                   help="把门禁打回未批准（不给值=清空全部）")
    p.add_argument("--note", metavar="TEXT", help="批准/打回/跳过的原因（打回与跳过必填）")
    p.add_argument("--by", metavar="WHO", help="谁批的（默认 user；Agent 代批时写 agent）")
    p.add_argument("--next", action="store_true", help="只打印「下一道该过的门」与要敲的命令")
    p.add_argument("--json", action="store_true", help="机器可读输出（给 Agent 用）")

    p = sub.add_parser(
        "status", parents=[common],
        help="续跑摘要：一行一任务（门禁 / 产物 / 下一步）—— 续跑第一步跑这个，别读 shots.json",
    )
    p.add_argument("--brief", action="store_true", default=True,
                   help="只打表格（**默认行为**；≤8 行。写上是为了脚本自解释）")
    p.add_argument("--legend", action="store_true", help="表格后追加列说明（给人看，不省 token）")
    p.add_argument("--all", dest="show_all", action="store_true",
                   help="列出全部任务（默认只看 UGE 前缀的雨月物语）")
    p.add_argument("--prefix", default="UGE", help="只看某前缀的任务（默认 UGE）")
    p.add_argument("--json", action="store_true", help="机器可读输出（给 Agent 用）")
    p.add_argument("--fresh", action="store_true",
                   help="忽略 5 分钟结果缓存，重扫门禁（默认命中缓存直接返回）")

    # 续跑一条命令（P1 / T2）：把"我在哪 + 下一步敲什么 + 最近失败了什么"合成一条。
    p = sub.add_parser(
        "resume", parents=[common],
        help="续跑：门禁向量 / 下一道门 / 下一条命令 / 最近失败（--json ≤40 行）",
    )
    p.add_argument("--all", dest="show_all", action="store_true",
                   help="列出全部任务（默认只看 UGE 前缀）")
    p.add_argument("--json", action="store_true",
                   help="机器可读输出（≤40 行，含 next_gate / next_command）")

    # 成本（P1 / T5）：读 runlog 的 llm_cost 事件，按阶段拆分。
    p = sub.add_parser(
        "cost", parents=[common],
        help="LLM 成本：token 与估算费用（按阶段拆分）；预算见 [budget]",
    )
    p.add_argument("--all", dest="show_all", action="store_true", help="列出全部任务")
    p.add_argument("--json", action="store_true", help="机器可读输出")

    # 配置体检（P2 / B2）：**必填在不在 / 类型对不对 / 键名写没写错 / 各段认不认得出**。
    # ★ 刻意**不**继承 common：common 也带 `--config`，继承会让 `lvs config check --config X`
    #   变成"同一个开关出现两次"的歧义。它自己声明一份，措辞也更准（它就是来校验配置的）。
    # ★ 也刻意**不读任务目录**：它跟 clean 一样是仓库级工具（配置是全项目一份）。
    p = sub.add_parser(
        "config",
        help="配置体检：config.toml 的必填/类型/未知键/段识别（写错了不会报错的那些坑）",
    )
    csub = p.add_subparsers(dest="config_command", metavar="<动作>")
    c = csub.add_parser("check", help="校验配置文件（默认按 --config → $LVS_CONFIG → ./config.toml 找）")
    c.add_argument("--config", metavar="PATH", help="要校验的配置文件（默认自动定位）")
    c.add_argument("--json", action="store_true", help="机器可读输出（findings 数组 + 段识别）")

    # 行号锚定读取（S4）：取代“转储成 *_numbered.txt 再分页读”。
    # 不接 parents=[common]：它不读配置、不碰任务目录，只看磁盘上的文件。
    p = sub.add_parser(
        "map", help="行号锚定读取：按区间看源码/文档（别再造 *_numbered.txt）",
    )
    msub = p.add_subparsers(dest="map_command", metavar="<动作>")
    s = msub.add_parser("show", help="打印某文件的第 start..end 行（默认带行号）")
    s.add_argument("file", metavar="文件")
    s.add_argument("start", type=int, metavar="起")
    s.add_argument("end", type=int, nargs="?", default=None, metavar="止",
                   help="给定则打到这一行；不给 = 起 + 59")
    s.add_argument("--raw", action="store_true", help="不打行号（方便直接复制）")
    g = msub.add_parser("grep", help="只打命中行（默认 50 处封顶）")
    g.add_argument("pattern", metavar="正则")
    g.add_argument("paths", nargs="*", metavar="路径", help="文件或目录；缺省 = 仓库根")
    g.add_argument("--context", type=int, default=0, metavar="N", help="每处多打上下各 N 行")
    g.add_argument("--limit", type=int, default=50, metavar="N", help="最多打多少处（默认 50）")
    g.add_argument("-i", "--ignore-case", action="store_true", help="忽略大小写")
    g.add_argument("--width", type=int, default=160, metavar="N", help="命中行截断宽度（默认 160）")
    msub.add_parser("ls", help="列出遗留的 *_numbered.txt（S4 要消灭的中间产物）")
    # BGM（独立功能 P5）：本地生成**可商用**配乐（自适应文案风格）+ 压低/闪避混音。
    # 为什么单开一个命令而不是塞进 build：生成要加载几 GB 的模型、跑几分钟，
    # 与"合成"是两种节奏；而 mix 又要能对**已出的成片**反复试参数（原片不动）。
    # 推理走 CPU（GPU 被 ComfyUI / Qwen3-TTS 串行占用），权重走 hf-mirror。
    p = sub.add_parser(
        "bgm", parents=[common],
        help="BGM：本地生成可商用配乐（随文案风格）+ 压低闪避混音（CPU）",
        description=(
            "背景音乐（BGM）：按讲稿风格本地生成可商用配乐，再压低 + 闪避混进成片。\n"
            "动作：generate（默认）/ download / prompt / mix\n"
            "模型：stabilityai/stable-audio-3-small-music"
            "（Stability AI Community License，年收入 <100 万美元可商用，输出归用户）"
        ),
    )
    p.add_argument(
        "action", nargs="?", default=None, metavar="<动作>",
        help="generate（默认，生成）/ download（只下权重）/ "
             "prompt（只打印提示词与风格，秒出）/ mix（混进成片）",
    )
    p.add_argument("--duration", type=float, metavar="SEC",
                   help="生成时长秒数（默认 60，夹在 15–180；混音时循环铺满成片）")
    p.add_argument("--seed", type=int, help="随机种子（默认 20261005，同种子可复现）")
    p.add_argument("--steps", type=int, metavar="N",
                   help="采样步数（默认 8 —— 模型卡给 SA3 的值；CPU 上步数≈耗时）")
    p.add_argument("--cfg-scale", type=float, metavar="F",
                   help="提示词贴合度（默认 1.0 —— 模型卡给 SA3 的值）")
    p.add_argument("--prompt", metavar="TEXT", help="手动指定提示词（跳过风格推断）")
    p.add_argument("--no-llm", action="store_true",
                   help="不用 LLM，只用内置关键词规则推风格（没 key 时的默认行为）")
    p.add_argument("--force", action="store_true", help="已有产物也重做 / 重新下载权重")
    p.add_argument("--video", metavar="PATH",
                   help="mix：要混的成片（默认 .work/<task>/final.mp4）")
    p.add_argument("--out", metavar="PATH", help="mix：输出路径（默认 bgm_<原片名>）")
    p.add_argument("--volume-db", type=float, metavar="DB",
                   help="mix：BGM 基础音量 dB（默认 -16，负值=压低）")
    p.add_argument("--duck-threshold", type=float, metavar="F",
                   help="mix：闪避触发阈值（线性幅度，默认 0.03 ≈ -30 dBFS）")
    p.add_argument("--duck-ratio", type=float, metavar="F", help="mix：闪避压缩比（默认 8）")
    p.add_argument("--no-duck", action="store_true", help="mix：不开闪避（只压低）")
    p.add_argument("--json", action="store_true", help="机器可读输出（生成元信息）")

    return parser


def _resolve_task(args: argparse.Namespace) -> str:
    """任务名：显式 `--task` 优先，否则取拍摄稿文件名，再否则 default。"""
    if getattr(args, "task", None):
        return slugify(args.task)
    manuscript = getattr(args, "manuscript", None)
    if manuscript:
        return slugify(Path(manuscript).stem)
    return DEFAULT_TASK


def _dispatch(args: argparse.Namespace, config: Config, ws: Workspace) -> int:
    """把命令路由到对应实现。"""
    if args.command == "parse":
        from lvs import parse as parse_mod

        return parse_mod.run_command(config, ws, args)

    if args.command == "shots":
        from lvs import shots as shots_mod

        return shots_mod.run_command(config, ws, args)

    if args.command == "assets":
        from lvs import assets as assets_mod

        return assets_mod.run_command(config, ws, args)

    if args.command == "voice":
        from lvs import tts as tts_mod

        return tts_mod.run_command(config, ws, args)

    if args.command == "build":
        from lvs import build as build_mod

        return build_mod.run_command(config, ws, args)

    if args.command == "run":
        from lvs import run as run_mod

        return run_mod.run_command(config, ws, args)

    if args.command == "publish":
        from lvs import publish as publish_mod

        return publish_mod.run_command(config, ws, args)

    if args.command == "library":
        from lvs import library as library_mod

        return library_mod.run_command(config, ws, args)

    if args.command == "board":
        from lvs import board as board_mod

        return board_mod.run_command(config, ws, args)

    if args.command == "studio":
        from lvs import studio as studio_mod

        return studio_mod.run_command(config, ws, args)

    if args.command == "cast":
        from lvs import cast as cast_mod

        return cast_mod.run_command(config, ws, args)

    if args.command == "migrate":
        from lvs import migrate as migrate_mod

        return migrate_mod.run_command(config, ws, args)

    if args.command == "qc":
        from lvs import qc as qc_mod

        return qc_mod.run_command(config, ws, args)

    if args.command == "gate":
        from lvs import pipeline as pipeline_mod

        return pipeline_mod.run_command(config, ws, args)

    if args.command == "styles":
        from lvs import styles as styles_mod

        return styles_mod.run_command(config, args)

    if args.command == "bgm":
        from lvs import bgm as bgm_mod

        return bgm_mod.run_command(config, ws, args)
    issues = _STAGE_ISSUES.get(args.command, "?")
    print(f"未知命令 `{args.command}`（对应票据 {issues}）。")
    return 3


#: 会产出**统一结果信封**的命令（工作命令）。`gate` / `styles` 有自己的
#: `--json`（形状不同、用途不同），刻意不在这个集合里 —— 免得两套 JSON 打架。
_ENVELOPE_COMMANDS: frozenset[str] = frozenset(
    {"parse", "shots", "assets", "voice", "build", "run", "publish", "qc"}
)


def _run_and_report(args, config, ws) -> int:  # noqa: ANN001
    """跑一条命令，需要时把**统一结果信封**打到 stdout。

    `--json` 时信封是**最后一段**输出（以 `{` 开头），人类可读的散文照旧在前面 ——
    所以"给人看"与"给机器读"两件事都不牺牲，也不必给每段散文加开关。
    agent 只取最后那段 JSON 即可。

    信封构造失败**绝不**让命令失败（它是增强，不是主流程）—— 出错时退化成
    一个带 `envelope_error` 的最小信封，退出码仍是真实的。
    """
    _t0 = time.monotonic()
    code = _dispatch(args, config, ws)

    # 轨迹 + 耗时：**直接敲单条命令**也要记（agent 正是逐条敲的）。
    # `run` 走 `stage.run_stage`，那条路径已经在里面记了 —— 与这里共用
    # `stage.note_stage_end`，保证两条路的轨迹一致。
    if args.command in _ENVELOPE_COMMANDS and args.command != "run":
        try:
            from lvs import stage as stage_mod

            stage_mod.note_stage_end(
                ws, config, args.command, int(code),
                seconds=time.monotonic() - _t0,
            )
        except Exception:  # noqa: BLE001 - 观测不该变成新的故障点
            pass

    if getattr(args, "json", False) and args.command in _ENVELOPE_COMMANDS:
        from lvs import result as result_mod

        try:
            env = result_mod.build(ws, args.command, code).envelope()
        except Exception as exc:  # noqa: BLE001 - 信封是增强，不该让命令失败
            env = {
                "task": getattr(ws, "task", ""),
                "stage": args.command,
                "ok": code == 0,
                "status": result_mod.status_of(code),
                "exit_code": int(code),
                "counts": {"total": 0, "done": 0, "skipped": 0, "failed": 0},
                "next_hint": "",
                "envelope_error": f"{type(exc).__name__}: {exc}",
            }
        result_mod.emit(env)
    return code


def main(argv: list[str] | None = None) -> int:
    """`lvs` 的**唯一入口**。把未捕获异常统一映射成退出码。

    ## 为什么必须有一层兜底

    改造前这里没有任何 `try/except`：任何阶段抛出的 `LvsError`
    （`AssetError` / `TTSError` / `ComfyError` / `FFmpegError` / `CastError` …）
    会**冒泡出 `main()`**，用户看到的是一段 Python traceback，而退出码变成
    Python 默认的 `1` —— **丢掉了错误自带的语义**。

    后果有两层：

    1. **提示不像人话**：本该打印"定妆库损坏，无法解析：<路径>"，实际打的是堆栈。
    2. **脚本分不清该等还是该报警**：`StyleError`（=2，输入问题，改了再来）与
       `BlockedError`（=3，等人审批）都变成 `1`，CI / 自动化无法据此决策。

    这正是本项目反复吃亏的"**定义了却从不接线**"（上一轮修的是
    `EXIT_FAILED` 从不被 `assets`/`tts` 返回）。`errors.exit_code_for()` 与
    `errors.describe()` 就是为了这一层写的 —— 以前只被测试调用，现在是真在用。
    """
    _configure_stdio()
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print("\n已中断（Ctrl-C）。已完成的阶段下次会跳过。", file=sys.stderr)
        return errors.EXIT_FAILED
    except errors.LvsError as exc:
        # 领域错误：打**人话**（不带堆栈），退出码用错误自带的语义。
        print(str(exc), file=sys.stderr)
        code = errors.exit_code_for(exc)
        if code != errors.EXIT_FAILED:
            # 只在"语义特殊"时补一句含义，免得把"失败"也解释一遍
            print(f"（{errors.describe(code)}）", file=sys.stderr)
        if _debug_enabled():
            traceback.print_exc()
        return code
    except Exception as exc:  # noqa: BLE001 - 最外层兜底，必须宽
        # 未预期的内部错误：这一定是 bug，把类型与消息打出来方便反馈。
        print(f"内部错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("  这是未预期的错误。请把上面信息连同你敲的命令一起反馈。", file=sys.stderr)
        if _debug_enabled():
            traceback.print_exc()
        else:
            print("  （要看完整堆栈：设环境变量 LVS_DEBUG=1 再跑一次）", file=sys.stderr)
        return errors.EXIT_FAILED


def _debug_enabled() -> bool:
    import os

    return str(os.environ.get("LVS_DEBUG", "")).strip() not in ("", "0", "false", "False")


def _shots_readonly(args: argparse.Namespace) -> bool:
    """`shots --peek` / `shots --index` 是**只读读口**（S2）—— 不跑拆镜、不建任务目录、不记轨迹。

    为什么单独抽成一个函数（2026-10-05 实测踩过）：`shots` 在
    `_ENVELOPE_COMMANDS` 里，而那条路径会调 `stage.note_stage_end`。
    于是“看一眼”就会：① 往 `logs/run-<date>.jsonl` 落一条 stage_end；
    ② 把 `manifest.json` 的 mtime 顶到当前时间（假的“最近活动”）。
    抽出来也好让测试直接钉住这条路由。
    """
    if getattr(args, "command", None) != "shots":
        return False
    return getattr(args, "peek", None) is not None or bool(getattr(args, "index", False))


def _main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # P1 逃生阀：`--no-cache` 立刻关掉缓存（模块级上下文，早于任何 LLM 调用）。
    if getattr(args, "no_cache", False):
        from lvs import llm_cache

        llm_cache.disable()

    if not args.command:
        parser.print_help()
        return 0

    # clean：纯本地清理，**不需要**配置文件，也不建任务目录
    if args.command == "clean":
        from lvs import cleanup as cleanup_mod

        return cleanup_mod.run_command(None, None, args)

    # init：**不需要**配置文件 —— 它就是来为这个项目生成配置的
    if args.command == "init":
        from lvs import project as project_mod

        return project_mod.run_init(args)

    config_path = find_config(getattr(args, "config", None))

    # doctor：允许没有配置文件（它就是来告诉你缺什么的）
    if args.command == "doctor":
        try:
            config = Config.load(config_path) if config_path else Config.empty()
        except ConfigError as exc:
            print(f"配置错误：{exc}", file=sys.stderr)
            config = Config.empty()
        return run_doctor(config, as_json=bool(getattr(args, "json", False)),
                          as_agent=bool(getattr(args, "agent", False)))

    # status：续跑摘要（一行一任务）。**不需要配置文件** ——
    # 它按任务前缀自己找 config，找不到也照样列（config 列打「≈」，提示 G2 指纹不可信）。
    if args.command == "status":
        from lvs import summary as summary_mod

        return summary_mod.run_command(config_path, args)

    # resume / cost（P1）：都**不需要配置文件** ——
    # resume 按任务前缀自己找 config（找不到就按"G2 指纹不可信"处理）；
    # cost 没有 [budget] 段就按默认 observe 展示。
    if args.command == "resume":
        from lvs import resume as resume_mod

        return resume_mod.run_command(config_path, args)

    if args.command == "cost":
        from lvs import llm_cost

        return llm_cost.run_command(config_path, args)

    # map：行号锚定读取（S4）。同样**不需要配置文件** ——
    # 它只是把磁盘上的某几行打出来，不碰任务目录、不写任何东西。
    if args.command == "map":
        from lvs import maptool

        return maptool.run_command(args)

    # config check（P2）：配置体检。★ 必须排在 `Config.load` **之前** ——
    # "配置本身坏了"正是它要报的错；先 load 的话会先在 cli 这层炸成一句
    # `配置错误：…` 并退出 2，报告里就少了"哪一行、哪个键、怎么改"。
    # 它自己调 `Config.load` 并把异常转成 finding（见 lvs/configcheck.check）。
    if args.command == "config":
        if getattr(args, "config_command", None) != "check":
            print("用法：lvs config check [--config 配置文件] [--json]")
            return 2
        from lvs import configcheck

        return configcheck.run_command(config_path, args)

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

    # image：一次性手动出图，不建任务目录、不写流水线状态
    if args.command == "image":
        from lvs import imagegen as imagegen_mod

        return imagegen_mod.run_command(config, args)

    # check / migrate：纯读写拍摄稿本身，不建任务目录
    if args.command == "check":
        from lvs import check as check_mod

        return check_mod.run_command(config, None, args)

    if args.command == "migrate":
        from lvs import migrate as migrate_mod

        return migrate_mod.run_command(config, None, args)

    if _shots_readonly(args):
        from lvs import shots as shots_mod

        return shots_mod.run_command(config, Workspace.read(_resolve_task(args)), args)

    # `qc --final`（P2）：成片自检 —— **只读**。它不写门禁账本、不记阶段轨迹、
    # 不建任务目录（所以走 `Workspace.read`，与 board/gate 同一条路）。
    # ★ 这也是验收项④的落地方式：**qc 的结果只打印/落盘，不进 G5 判定** ——
    #   它在 `_ENVELOPE_COMMANDS` 之外，因此连 `stage.note_stage_end` 都不会碰。
    if args.command == "qc" and getattr(args, "final", False):
        from lvs import qc_final

        return qc_final.run_command(config, Workspace.read(_resolve_task(args)), args)

    # `lvs bgm prompt` 只看会用什么提示词（读 shots.json）：同 `board` 一样**不该替当前目录建
    # 任务目录**（票 23）。其余动作（generate / download / mix）要写产物，仍走下面的 `.ensure()`。
    if args.command == "bgm" and getattr(args, "action", None) == "prompt":
        from lvs import bgm as bgm_mod

        return bgm_mod.run_command(config, Workspace.read(_resolve_task(args)), args)

    # board / cast / gate / styles：纯读取为主，不替当前目录建任务目录（票 23 验收项）
    if args.command in ("board", "cast", "gate", "styles"):
        ws = Workspace.read(_resolve_task(args))
        return _run_and_report(args, config, ws)

    # gui：自己管自己的任务，不替当前目录建任务目录
    if args.command == "gui":
        from lvs.gui import app as gui_app

        return gui_app.serve(
            config_path=config_path,
            port=int(getattr(args, "port", 8730)),
            open_browser=not bool(getattr(args, "no_open", False)),
        )

    ws = Workspace(task=_resolve_task(args)).ensure()
    return _run_and_report(args, config, ws)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
