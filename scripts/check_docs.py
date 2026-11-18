"""文档链接校验：README/docs 里的相对链接必须指向真实文件。

为什么值得单独做：作品集仓库里最容易失分的不是代码，是"链接点进去 404"。
这类问题不会被任何测试发现，直到有人真的点。

用法：python scripts/check_docs.py
退出码：0 = 全部存在；1 = 有断链
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
SKIP_PREFIX = ("http://", "https://", "mailto:", "#", "tel:")


def markdown_files() -> list[Path]:
    files = [ROOT / "README.md"]
    files += sorted((ROOT / "docs").rglob("*.md"))
    return [f for f in files if f.exists()]


def check() -> list[str]:
    problems: list[str] = []
    for md in markdown_files():
        text = md.read_text(encoding="utf-8")
        for raw in LINK.findall(text):
            target = raw.strip().split(" ")[0].split("#")[0]
            if not target or target.startswith(SKIP_PREFIX):
                continue
            # 链接里可能带 URL 编码的空格
            target = target.replace("%20", " ")
            path = (md.parent / target).resolve()
            if not path.exists():
                problems.append(f"{md.relative_to(ROOT)} -> {raw}（不存在）")
    return problems


def main() -> int:
    problems = check()
    files = markdown_files()
    if problems:
        print(f"[check_docs] {len(files)} 个文档，发现 {len(problems)} 条断链：")
        for p in problems:
            print("  x " + p)
        return 1
    print(f"[check_docs] {len(files)} 个文档，链接全部有效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
