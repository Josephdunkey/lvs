"""统一错误基类与退出码语义 —— 让"失败了"这件事**可被程序判断**。

## 为什么要有它

改造前：16 个错误类各自继承 `RuntimeError`（`ConfigError` 继承 `Exception`、
`StyleError` 继承 `KeyError`），彼此没有共同祖先；退出码 `0/1/2/3` 在 110 处
直接写数字，语义只存在于注释与人的记忆里。

后果有两层：

1. **上层无法统一处理**：编排器想说"这是输入问题，别重试"，
   只能一个个列 `except ConfigError, AssetError, TTSError, ...` —— 加一个新错误类就漏一个。
2. **退出码靠猜**：写脚本的人不知道 `2` 和 `3` 的区别，只能去读代码。

## 约定

```python
EXIT_OK      = 0   # 成功
EXIT_FAILED  = 1   # 跑完了，但有失败件（逐镜隔离：某几镜没取到素材 / 没合出音）
EXIT_USAGE   = 2   # 输入 / 配置 / 前置条件不对 —— 改一下再来（**别自动重试**）
EXIT_BLOCKED = 3   # 被人审门禁或守卫拦住 —— 需要人做决定（**等，不是错**）
```

`EXIT_BLOCKED` 与 `EXIT_FAILED` 分开是关键：前者是**正常流程的一部分**
（门禁就是设计成要停的），后者是真的出问题了。混在一起，自动化脚本就没法区分
"该等人批准"和"该报警"。

## 迁移方式（渐进，不做大爆炸）

既有错误类改成**多重继承**：`class AssetError(LvsError, RuntimeError)`。
这样 `except AssetError`、`except RuntimeError`、`except LvsError` 都能用 ——
800+ 个测试与既有调用点一个都不用改，而新增的代码可以只 `except LvsError`。

`tests/test_architecture.py` 会守住"所有 `*Error` 都继承 `LvsError`"，
免得下次有人新加一个又漏掉。
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_BLOCKED = 3

EXIT_MEANING: dict[int, str] = {
    EXIT_OK: "成功",
    EXIT_FAILED: "跑完了但有失败件（逐镜隔离）",
    EXIT_USAGE: "输入/配置/前置条件不对（改了再来，别重试）",
    EXIT_BLOCKED: "被人审门禁或守卫拦住（等人决定，不是错误）",
}


class LvsError(Exception):
    """所有 `lvs` 领域错误的基类。

    `exit_code` 是**这个错误应当映射到的退出码**。有了它，上层不必再
    `except A, B, C, …` 地枚举 —— `except LvsError as e: return e.exit_code`。
    """

    exit_code: int = EXIT_FAILED

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = str(message)


class UsageError(LvsError):
    """输入 / 配置 / 前置条件不对 —— 改一下再来，**不要自动重试**。"""

    exit_code = EXIT_USAGE


class BlockedError(LvsError):
    """被人审门禁或守卫拦住 —— **正常流程的一部分**，等人做决定。"""

    exit_code = EXIT_BLOCKED


class FailedError(LvsError):
    """跑完了，但有失败件（逐镜失败隔离的汇总）。"""

    exit_code = EXIT_FAILED


def exit_code_for(exc: BaseException) -> int:
    """把一个异常映射成退出码。

    认识的（`LvsError`）用它自带的；`KeyboardInterrupt` 归为失败；
    其余未知异常按 `EXIT_FAILED`（**不是** usage —— 未知异常不该被当成"用户的错"）。
    """
    if isinstance(exc, LvsError):
        return exc.exit_code
    if isinstance(exc, KeyboardInterrupt):
        return EXIT_FAILED
    return EXIT_FAILED


def describe(code: int) -> str:
    """退出码 → 人话（给报错信息与文档用）。"""
    return EXIT_MEANING.get(code, f"未知退出码 {code}")
