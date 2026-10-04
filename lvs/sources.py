"""素材来源策略（票 41）。

**两个概念要分清**（这是本模块存在的唯一理由）：

- `source_mode` —— **任务级意向**：这一部片子，实拍镜的素材从哪儿来。
- `source`      —— **逐镜现实**：这一镜最后实际是从哪儿来的（会被 `--only`、
  `branches_for`、`existing_asset` 直接消费，所以它必须诚实）。

策略只在满足三个条件的镜上生效：① 是实拍（scene）镜；② 不是图文/图表 beat
（那类画面必须留在本地排版渲染器上，进扩散模型必出伪汉字 —— D20/D24）；
③ 没有被人工钉死（`source_pinned`，D14）。

本模块只做**纯数据变换**（不碰磁盘、不碰 Config），因为四个入口都要用它：
`lvs shots`（决定来源）、`lvs assets`（执行）、`lvs studio`（手动问）、GUI（选择器）。
让它们各写一份的下场，票 40 已经演示过一次了。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from lvs import prompting
from lvs.errors import UsageError

MODE_AUTO = "auto"
MODE_PEXELS = "pexels"
MODE_LOCAL = "local"
MODE_LIBRARY = "library"

MODES: tuple[str, ...] = (MODE_AUTO, MODE_PEXELS, MODE_LOCAL, MODE_LIBRARY)

LABELS: dict[str, str] = {
    MODE_AUTO: "自动判定（LLM 逐镜决定）",
    MODE_PEXELS: "下载实拍素材（Pexels）",
    MODE_LOCAL: "本地生图（ComfyUI）",
    MODE_LIBRARY: "本地素材库",
}

# 人工钉死这一镜用的标记（GUI 的「自己选一张图」会打上）—— 策略绕开它
PIN_FIELD = "source_pinned"

# 来源一变，「上一次解析的结果」就必须作废 —— 否则界面与下游会读到旧来源的残留
# （典型：切了策略之后，这镜还挂着上个来源的失败原因）。
# 注意 `error` 在里面：漏掉它，换来源后的失败镜会一直显示旧策略的报错。
STALE_RESULT_FIELDS = ("asset_path", "resolved_by", "status", "error")

# 连「钉死 / 库命中」的成果（`library_asset`）一起作废 —— 只有**强制换来源**时才该这么干。
# `retarget` 不用它：那一支是保守翻转，已经有库命中的镜不该被动。
STALE_WITH_LIBRARY = ("library_asset",) + STALE_RESULT_FIELDS


class SourceModeError(UsageError, RuntimeError):
    """来源策略取值非法。"""


def normalize(raw: Any) -> str:
    """任意输入 → 合法模式；空值当 auto，其余非法值直接报错（不猜）。"""
    text = re.sub(r"\s+", "", str(raw or "")).lower()
    if not text:
        return MODE_AUTO
    if text not in MODES:
        raise SourceModeError(f"来源策略只支持 {'/'.join(MODES)}，收到 {raw!r}")
    return text


def mode_of(data: dict[str, Any] | None) -> str:
    """从 `shots.json` 读任务级策略；缺省 auto。"""
    raw = (data or {}).get("source_mode")
    try:
        return normalize(raw)
    except SourceModeError:
        return MODE_AUTO


def resolve(cli: Any = None, recorded: Any = None, default: Any = None) -> str:
    """定这次用哪个策略，**优先级只有这一处实现**。

    层级：命令行 > 任务里记着的（非 auto 才算"记着"，因为 shots 每次都会写这个字段，
    写进去的 auto 只是默认值、不是选择）> 配置默认。

    **显式给的 `auto` 必须生效** —— 它的意思是"别再替我定了"，界面用它切回自动。
    """
    if cli not in (None, ""):
        return normalize(cli)
    if recorded not in (None, ""):
        mode = normalize(recorded)
        if mode != MODE_AUTO:
            return mode
    return normalize(default if default not in (None, "") else MODE_AUTO)


def is_scene(shot: dict[str, Any]) -> bool:
    """实拍镜 —— 图文/图表 beat 不算。"""
    return str(shot.get("source") or "") != prompting.KIND_GRAPHIC


def retargetable(shot: dict[str, Any]) -> bool:
    """策略管得着的镜：实拍且没有人工钉死。

    「人工钉死」只能靠 `PIN_FIELD`（`source_pinned`）认，**不能**见 `library_asset`
    就放过 —— 翻库命中的镜也会被自动写上这个字段，那样每次重跑都会把库命中粘死、
    策略再也换不动。所以打标记的责任在**钉图的那一方**（界面「自己选一张图」）。
    """
    return is_scene(shot) and not shot.get(PIN_FIELD)


def scene_shots(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [s for s in ((data or {}).get("shots") or []) if is_scene(s)]


def apply_mode(data: dict[str, Any], mode: str) -> int:
    """**强制**把实拍镜的来源统一成 `mode`（就地改），返回真正被改动的镜数。

    已经是该来源的镜**一个字都不动** —— 它这会儿可能已经解析出了 `library_asset`
    之类的成果，重跑时必须留着（幂等）。
    """
    mode = normalize(mode)
    data["source_mode"] = mode
    if mode == MODE_AUTO:
        return 0

    changed = 0
    for shot in data.get("shots") or []:
        if not retargetable(shot) or shot.get("source") == mode:
            continue
        shot["source"] = mode
        for field in STALE_WITH_LIBRARY:
            shot.pop(field, None)
        changed += 1
    return changed


def retarget(data: dict[str, Any], source: str, target: str) -> int:
    """把 `source == source` 的镜保守地翻成 `target`（就地改），返回镜数。

    「保守」= 只动正好等于 `source` 的那些，且**已经有素材的镜不动**。
    这是原 `route_pexels_to_local` 的行为 —— 勾了「生图」却没勾「下载素材」时，
    那些 pexels 镜会永远缺素材，所以顺手改掉。与 `apply_mode` 的**全量强制**不同：
    这里 `source` 是用户的逐镜编辑，不强推。

    这里跳过的是「`library_asset` 已落定」的镜，**不是** `retargetable()` 认的那种
    人工钉死（`PIN_FIELD`）。两者不是一回事，别混用：本函数只想知道「这镜是不是已经
    有素材了」（有就不用改），而不是「谁选的」。库命中的镜 `branches_for` 第一行就会
    认领，改它的 `source` 只会白丢一次 `library_asset`（票 41 审查订正）。
    """
    flipped = 0
    for shot in data.get("shots") or []:
        if shot.get("source") == source and not shot.get("library_asset"):
            shot["source"] = target
            for field in STALE_RESULT_FIELDS:
                shot.pop(field, None)
            flipped += 1
    return flipped


def choices() -> Iterable[tuple[str, str]]:
    """给菜单用：(值, 中文标签)。"""
    return ((m, LABELS[m]) for m in MODES)
