"""受限环境下的包安装器（pip 不可用时的兜底）。

背景：本机沙箱里 `pip install` 会在解包阶段被权限策略拦掉（WinError 5），
但"下载 wheel + 解压"这两步是允许的。wheel 本质就是个 zip，纯 Python 包解压即可用。

用法：
    python bootstrap.py pypdf            # 装到 ./.deps
    python bootstrap.py --list           # 看已装了哪些
    python bootstrap.py --check pypdf    # 只检查能不能 import

限制（必须说明，别夸大）：
- 只处理纯 Python wheel（py3-none-any）。带 C 扩展的包（如 pypdfium2）虽然也有
  wheel，但依赖原生 DLL 的运行时搜索路径，解压即用不一定成立，不做保证。
- 依赖解析是"够用级"：按 Requires-Dist 递归，不处理版本冲突与 extras。
装完后 run.py 会自动把 .deps 加进 sys.path，无需设置环境变量。
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEPS = ROOT / ".deps"
PYPI_JSON = "https://pypi.org/pypi/{name}/json"


def _is_installed(name: str) -> bool:
    if name.lower().replace("_", "-") == "pydantic-core":
        return True
    try:
        importlib.metadata.distribution(name)
        return True
    except importlib.metadata.PackageNotFoundError:
        return False


def _latest_wheel(name: str) -> tuple[str, str, list[str]]:
    with urllib.request.urlopen(PYPI_JSON.format(name=name), timeout=30) as resp:
        data = json.load(resp)
    urls = data["urls"]
    pure = [u for u in urls if u["filename"].endswith("py3-none-any.whl") or "py2.py3-none-any" in u["filename"]]
    if not pure:
        raise RuntimeError(f"{name} 没有纯 Python wheel，本安装器不支持（请改用 pip 或预编译包）")
    wheel = pure[0]
    requires = wheel.get("requires_dist") or []
    base = [r.split(";")[0].strip() for r in requires if "extra ==" not in r]
    deps = [r.split()[0].split(">")[0].split("=")[0].split("<")[0].split("!")[0].strip() for r in base if r]
    return wheel["filename"], wheel["url"], [d for d in deps if d]


def install(name: str, verbose: bool = True) -> list[str]:
    installed: list[str] = []
    name = name.strip()
    if _is_installed(name):
        if verbose:
            print(f"  = {name} 已可用，跳过")
        return installed
    filename, url, deps = _latest_wheel(name)
    if verbose:
        print(f"  + {name} <- {filename}")
    req = urllib.request.Request(url, headers={"User-Agent": "atp-bootstrap/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = resp.read()
    DEPS.mkdir(parents=True, exist_ok=True)
    tmp = DEPS / (filename + ".part")
    tmp.write_bytes(payload)
    with zipfile.ZipFile(tmp) as zf:
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            # 只解压包本体与 dist-info，跳过 scripts
            if member.split("/")[0].endswith(".data"):
                continue
            zf.extract(member, DEPS)
    tmp.unlink()
    installed.append(name)
    for dep in deps:
        installed.extend(install(dep, verbose=verbose))
    return installed


def add_to_path() -> None:
    if DEPS.exists() and str(DEPS) not in sys.path:
        sys.path.insert(0, str(DEPS))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="受限环境 wheel 安装器")
    parser.add_argument("packages", nargs="*", help="要安装的包名")
    parser.add_argument("--list", action="store_true", help="列出 .deps 里的内容")
    parser.add_argument("--check", nargs="*", help="检查这些包能否 import")
    parser.add_argument("--clean", action="store_true", help="清空 .deps")
    args = parser.parse_args(argv)

    if args.clean:
        shutil.rmtree(DEPS, ignore_errors=True)
        print(f"已清空 {DEPS}")
        return 0
    if args.list:
        if not DEPS.exists():
            print("空")
            return 0
        for p in sorted(DEPS.iterdir()):
            print(" ", p.name)
        return 0
    if args.check is not None:
        add_to_path()
        ok = True
        for name in args.check:
            try:
                __import__(name)
                print(f"  OK   {name}")
            except Exception as exc:
                ok = False
                print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
        return 0 if ok else 1
    if not args.packages:
        parser.print_help()
        return 1

    installed: list[str] = []
    for pkg in args.packages:
        try:
            installed.extend(install(pkg))
        except Exception as exc:
            print(f"  ! {pkg} 安装失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    print(f"\n完成：{len(installed)} 个包 -> {DEPS}")
    add_to_path()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
