"""一键验证：把"能跑"变成一条命令。

用法： python scripts/verify.py
退出码：0 = 全部通过；1 = 有步骤失败

为什么不写 .ps1：Windows 默认执行策略会拒绝未签名脚本，而 CI 又是 Linux。
用一个 Python 脚本两端都能跑，还顺便强制 ATP_LLM_MODE=mock —— 防止忘了切模式误烧额度。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRD = "data/prd/coupon-center-prd.md"
GOLDEN = "data/golden/coupon-golden.json"


def steps() -> list[tuple[str, list[str], bool]]:
    py = sys.executable
    out: list[tuple[str, list[str], bool]] = [
        ("单元与集成测试", [py, "-m", "pytest", "-q"], True),
    ]
    if shutil.which("node"):
        out.append(("前端模板编译", ["node", "scripts/check_web.js"], True))
    else:
        out.append(("前端模板编译", [], False))
    out += [
        ("文档链接", [py, "scripts/check_docs.py"], True),
        ("基准集质检", [py, "run.py", "lint-golden", "--prd", PRD, "--golden", GOLDEN], True),
        ("端到端（bare 自动确认）", [py, "run.py", "run", "--prd", PRD, "--mode", "bare", "--auto"], True),
        ("三模式评测（含阈值敏感性）", [py, "run.py", "eval", "--prd", "coupon-center-prd", "--golden", GOLDEN, "--sweep"], True),
    ]
    return out


def main() -> int:
    os.environ["ATP_LLM_MODE"] = "mock"
    os.chdir(ROOT)
    results: list[tuple[str, str, float]] = []

    for name, cmd, required in steps():
        if not cmd:
            print(f"\n=== {name} ===\n  skip（未安装 node）")
            results.append((name, "SKIP", 0.0))
            continue
        print(f"\n=== {name} ===")
        print("  $ " + " ".join(cmd))
        started = time.perf_counter()
        # 输出直接透传，便于定位失败步骤；但评测输出很长，这里只要结论
        quiet = name.startswith("三模式评测")
        proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=quiet)
        elapsed = time.perf_counter() - started
        if quiet and proc.stdout:
            tail = [ln for ln in proc.stdout.splitlines() if ln.startswith("[eval]")]
            print("\n".join(tail[-3:]))
        ok = proc.returncode == 0
        results.append((name, "PASS" if ok else "FAIL", elapsed))
        if not ok and required:
            if proc.stderr:
                print(proc.stderr[-1500:])
            break

    print("\n=== 汇总 ===")
    width = max(len(n) for n, _, _ in results)
    for name, status, elapsed in results:
        print(f"  {name.ljust(width)}  {status:<4} {elapsed:6.1f}s")

    failed = [r for r in results if r[1] == "FAIL"]
    if failed:
        print(f"\n{len(failed)} 项失败")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
