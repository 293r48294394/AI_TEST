"""人工闸门复核助手（只读，不改任何数据）。

用法：
    python scripts/review_testmap.py --run <run_id>

输出三块：
  1. PRD 每一章被引用了多少次 —— 0 次的就是"AI 没溯源到"的章节
  2. 每个功能拆了几条规则 —— 明显偏少的要重点看
  3. 数字型约束体检 —— 规则里有没有出现"字/元/%/天/位"等可测边界
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_DEPS = ROOT / ".deps"
if _DEPS.exists():
    sys.path.insert(0, str(_DEPS))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from atp.config import Settings  # noqa: E402
from atp.store import RunStore  # noqa: E402


def _cid(src) -> str:
    return src.chunk_id if hasattr(src, "chunk_id") else src["chunk_id"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="review_testmap.py")
    ap.add_argument("--run", required=True, help="run_id")
    args = ap.parse_args(argv)

    settings = Settings.from_env()
    store = RunStore(settings)
    state = store.load(args.run)
    tm = state.testmap_ai
    if tm is None:
        print(f"运行 {args.run} 还没有测试地图", file=sys.stderr)
        return 1

    art = store.dir(args.run) / "artifacts"
    chunks = json.loads((art / "00_prd_chunks.json").read_text(encoding="utf-8"))

    cite: dict[str, int] = {}
    for f in tm.features:
        for s in f.sources:
            cite[_cid(s)] = cite.get(_cid(s), 0) + 1

    print(f"运行 {args.run} | 状态 {state.status} | 阶段 {state.stage}")
    print(f"功能 {len(tm.features)} 个 | 规则 {sum(len(f.rules) for f in tm.features)} 条 "
          f"| 覆盖 PRD 章节 {len(cite)}/{len(chunks)}\n")

    print("【1】PRD 章节溯源")
    print(f"{'块号':<8}{'引用':<6}标题")
    missing = []
    for c in chunks:
        n = cite.get(c["chunk_id"], 0)
        mark = "" if n else "   <-- 未被引用"
        if not n:
            missing.append(c["heading"])
        print(f"{c['chunk_id']:<8}{n:<6}{c['heading'][:44]}{mark}")
    if missing:
        print(f"\n  未溯源章节 {len(missing)} 个：{ '、'.join(missing) }")

    print("\n【2】各功能规则数")
    for f in tm.features:
        bar = "#" * len(f.rules)
        print(f"  {f.id}  {len(f.rules):>2} 条 {bar:<10} {f.name}")

    print("\n【3】数字型约束体检（规则里是否含可测边界）")
    pat = re.compile(r"\d")
    unit = re.compile(r"[字元%天分秒位个条次]")
    for f in tm.features:
        with_num = [r for r in f.rules if pat.search(r) and unit.search(r)]
        print(f"  {f.id}  {len(with_num)}/{len(f.rules)} 条含数字边界")

    print("\n提示：数字型约束 = 一行数字很可能是边界值测试的富矿；")
    print("      某功能规则数明显少、或大量章节未被引用，都是打回重做的理由。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
