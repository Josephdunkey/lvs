"""`lvs map` \u2014\u2014 \u884c\u53f7\u951a\u5b9a\u8bfb\u53d6\uff08S4\uff1a\u7528\u201c\u6309\u533a\u95f4\u8bfb\u201d\u4ee3\u66ff numbered \u8f6c\u50a8\uff09\u3002

\u75c5\u6839\uff1a\u4e3a\u4e86\u80fd\u201c\u6309\u884c\u53f7\u8d34\u8865\u4e01\u201d\uff0c\u4f1a\u8bdd\u91cc\u628a\u6e90\u7801\u8f6c\u50a8\u6210 `_cli_numbered.txt`
\u7b49 8 \u4efd\u6587\u4ef6\uff08\u5171 481 KB \u2248 **15 \u4e07 token**\uff09\uff0c\u518d\u5206\u6b21\u8bfb\u56de\u6765\u3002\u4e2d\u95f4\u6587\u4ef6\u65e2\u70e7 token\uff0c
\u53c8\u5728 `.work/tmp` \u91cc\u5806\u78c1\u76d8\u5783\u573e\u3002\u73b0\u5728\u76f4\u63a5\u8bfb\u539f\u6587\u4ef6\u3001\u53ea\u8f93\u51fa\u8981\u7684\u90a3\u4e00\u6bb5\uff0c
**\u4e0d\u4ea7\u751f\u4efb\u4f55\u4e2d\u95f4\u6587\u4ef6**\u3002

    lvs map show lvs/cli.py 180 240      # \u53ea\u62ff 61 \u884c\uff08\u7ea6 800 token\uff09
    lvs map show lvs/cli.py 180 --raw    # \u4e0d\u6253\u884c\u53f7\uff08\u76f4\u63a5\u590d\u5236\u7528\uff09
    lvs map grep "add_parser" lvs/cli.py # \u53ea\u62ff\u547d\u4e2d\u884c\uff08\u9ed8\u8ba4 50 \u5904\u5c01\u9876\uff09
    lvs map ls                           # \u770b .work/tmp \u91cc\u9057\u7559\u7684 *_numbered.txt
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from lvs.config import PROJECT_ROOT

#: \u9ed8\u8ba4\u533a\u95f4\u957f\u5ea6\uff08\u4e0d\u7ed9 end \u65f6\uff09\u2014\u2014 \u4e00\u5c4f\u5de6\u53f3\uff0c\u4e0d\u4f1a\u628a\u6574\u4e2a\u6587\u4ef6\u5012\u8fdb\u4e0a\u4e0b\u6587\u3002
DEFAULT_SPAN = 60

#: `grep` \u53ea\u626b\u8fd9\u4e9b\u6269\u5c55\u540d\uff08\u6e90\u7801 / \u914d\u7f6e / \u6587\u6863\uff09\u2014\u2014 \u907f\u5f00\u4e8c\u8fdb\u5236\u4e0e\u5a92\u4f53\u3002
TEXT_EXTS = frozenset(
    ".py .pyi .toml .json .jsonl .md .txt .yml .yaml .cfg .ini .js .css .html .cmd .ps1 .sh .srt".split()
)

#: \u8df3\u8fc7\u7684\u76ee\u5f55\u540d\uff08\u7f13\u5b58 / \u4f9d\u8d56 / \u8fd0\u884c\u4ea7\u7269\uff09\u3002
SKIP_DIRS = frozenset(
    {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
     ".work", ".pytest_cache", ".mypy_cache", ".ruff_cache", "models", "outputs"}
)


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def show(path: Path, start: int, end: int | None = None, *, numbered: bool = True) -> str:
    """\u6253\u5370 `path` \u7684\u7b2c start..end \u884c\uff08\u542b\u4e24\u7aef\uff0c1 \u57fa\uff09\u3002

    \u8d85\u51fa\u6587\u4ef6\u8303\u56f4\u7684\u8bf7\u6c42\u81ea\u52a8\u6536\u7d27\u5230\u771f\u5b9e\u884c\u6570\uff1bstart > end \u4e5f\u4e0d\u62a5\u9519\uff0c
    \u53ea\u8fd4\u56de\u7a7a\u2014\u2014 \u8fd9\u662f\u7ed9 agent \u7528\u7684\u8bfb\u53e3\uff0c\u4e0d\u8be5\u56e0\u4e3a\u7b97\u9519\u4e00\u884c\u5c31\u629b\u5f02\u5e38\u3002
    """
    lines = read_lines(path)
    if not lines:
        return ""
    lo = max(1, int(start))
    hi = len(lines) if end is None else min(len(lines), int(end))
    if hi < lo:
        return ""
    if numbered:
        return "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(lo, hi + 1))
    return "\n".join(lines[lo - 1:hi])


def iter_files(paths: list[str], *, root: Path = PROJECT_ROOT):
    """\u628a\u201c\u6587\u4ef6\u6216\u76ee\u5f55\u201d\u5c55\u5f00\u6210\u8981\u626b\u7684\u6587\u4ef6\uff08\u5e26\u6269\u5c55\u540d\u767d\u540d\u5355\u4e0e\u76ee\u5f55\u8df3\u8fc7\uff09\u3002"""
    for raw in paths or ["."]:
        p = Path(raw)
        if not p.is_absolute():
            p = root / p
        if p.is_file():
            yield p
            continue
        if not p.is_dir():
            continue
        for child in sorted(p.rglob("*")):
            if not child.is_file():
                continue
            if child.suffix.lower() not in TEXT_EXTS:
                continue
            parts = set(child.relative_to(p).parts)
            if parts & SKIP_DIRS:
                continue
            yield child


def grep(
    pattern: str,
    paths: list[str],
    *,
    context: int = 0,
    limit: int = 50,
    ignore_case: bool = False,
    width: int = 160,
    root: Path = PROJECT_ROOT,
) -> str:
    """\u53ea\u6253\u547d\u4e2d\u884c\uff08`\u6587\u4ef6:\u884c\u53f7: \u5185\u5bb9`\uff09\uff0c\u5c01\u9876\u540e\u544a\u8bc9\u4f60\u8fd8\u6709\u591a\u5c11\u5904\u3002"""
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return f"\u6b63\u5219\u5199\u9519\u4e86\uff1a{exc}"
    out: list[str] = []
    hit = 0
    seen = 0
    for path in iter_files(paths, root=root):
        try:
            lines = read_lines(path)
        except OSError:
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.as_posix()
        for idx, line in enumerate(lines, start=1):
            if rx.search(line) is None:
                continue
            seen += 1
            if hit >= limit:
                continue
            hit += 1
            text = line.strip()
            if len(text) > width:
                text = text[:width] + "\u2026"
            if context <= 0:
                out.append(f"{rel}:{idx}: {text}")
                continue
            lo = max(1, idx - context)
            hi = min(len(lines), idx + context)
            for n in range(lo, hi + 1):
                mark = ":" if n == idx else "-"
                out.append(f"{rel}{mark}{n}{mark} {lines[n - 1].rstrip()}")
    if not out:
        return f"\u6ca1\u547d\u4e2d\uff1a{pattern}"
    if seen > hit:
        out.append(f"\u2026 \u8fd8\u6709 {seen - hit} \u5904\u547d\u4e2d\uff08--limit \u8c03\u5927\u770b\u5168\uff09")
    return "\n".join(out)


def dump_dirs(root: Path = PROJECT_ROOT) -> list[Path]:
    """\u5019\u9009\u76ee\u5f55\uff1a\u5386\u53f2\u4e0a\u8f6c\u50a8\u8fc7 numbered \u6587\u4ef6\u7684\u5730\u65b9\u3002"""
    return [root / ".work" / "tmp", root / ".work" / "tools", root]


def list_dumps(root: Path = PROJECT_ROOT) -> str:
    """\u5217\u51fa\u9057\u7559\u7684 `*_numbered.txt`\uff08\u8fd9\u4e9b\u5c31\u662f S4 \u8981\u6d88\u706d\u7684\u4e2d\u95f4\u4ea7\u7269\uff09\u3002"""
    rows: list[str] = []
    total = 0
    for d in dump_dirs(root):
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*numbered*.txt")):
            total += p.stat().st_size
            rows.append(f"{p.stat().st_size:>10,} B  {p.relative_to(root).as_posix()}")
    if not rows:
        return "\u5e72\u51c0\uff1a\u6ca1\u6709\u9057\u7559\u7684 *_numbered.txt\uff08\u7ee7\u7eed\u7528 `lvs map show` \u5c31\u4e0d\u4f1a\u518d\u4ea7\u751f\uff09"
    rows.append(f"\u5408\u8ba1 {total:,} B\uff08\u2248 {total / 3.2 / 1000:.0f}K token\uff09\u2014\u2014 \u786e\u8ba4\u65e0\u7528\u540e\u53ef\u5220")
    return "\n".join(rows)


def run_command(args) -> int:  # noqa: ANN001 - \u7531 cli \u4f20\u5165
    action = getattr(args, "map_command", None)
    if action == "show":
        path = Path(args.file)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.is_file():
            print(f"\u6ca1\u6709\u8fd9\u4e2a\u6587\u4ef6\uff1a{args.file}")
            return 2
        end = args.end if args.end is not None else args.start + DEFAULT_SPAN - 1
        text = show(path, args.start, end, numbered=not args.raw)
        if not text:
            total = len(read_lines(path))
            print(f"\uff08\u7a7a\uff09{args.file} \u53ea\u6709 {total} \u884c")
            return 0
        print(text)
        return 0
    if action == "grep":
        print(grep(args.pattern, list(args.paths or []), context=args.context,
                   limit=args.limit, ignore_case=args.ignore_case, width=args.width))
        return 0
    if action == "ls":
        print(list_dumps())
        return 0
    print("用法：lvs map show <文件> <起> [止] [--raw] | lvs map grep <正则> [路径…] | lvs map ls")
    return 2
