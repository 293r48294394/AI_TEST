"""命令行入口。

  python run.py serve  --port 8000          启动 Web 控制台 + API
  python run.py run    --prd <id|path>      跑一次流水线（默认停在人工确认闸门）
  python run.py run    --prd ... --auto     无人值守跑完（自动确认 AI 草稿）
  python run.py eval   --prd ... --golden ... 三模式对比评测
  python run.py list                        列出已有 PRD 与运行
  python run.py export --run <run_id>       导出 Excel
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
# 受限环境里用 bootstrap.py 把依赖解压到 .deps，这里自动挂上
_DEPS = ROOT / ".deps"
if _DEPS.exists():
    sys.path.insert(0, str(_DEPS))

from atp.config import Settings  # noqa: E402
from atp.pipeline.orchestrator import Orchestrator  # noqa: E402
from atp.store import RunStore  # noqa: E402


def _settings() -> Settings:
    return Settings.from_env()


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run("app.api:app", host=args.host, port=args.port, reload=False, log_level="info")
    return 0


def _resolve_prd(orc: Orchestrator, settings: Settings, ref: str):
    path = Path(ref)
    if path.exists():
        from atp.parsers.prd import parse_file

        return parse_file(path, prd_id=path.stem)
    return orc.load_prd(ref)


def cmd_run(args) -> int:
    settings = _settings()
    orc = Orchestrator(settings, RunStore(settings))
    prd = _resolve_prd(orc, settings, args.prd)
    state = orc.start(prd, mode=args.mode)
    print(f"[run] {state.run_id} status={state.status} stage={state.stage} features={len(state.testmap_ai.features)}")
    for i, f in enumerate(state.testmap_ai.features, start=1):
        print(f"  {f.id} {f.name}  rules={len(f.rules)}  refs={[s.chunk_id for s in f.sources]}")
    if state.status == "awaiting_human":
        print("\n[gate] 停在人工确认闸门。下一步：")
        print(f"  python run.py confirm --run {state.run_id}   # 接受 AI 草稿继续")
        print(f"  或打开控制台 http://127.0.0.1:{args.port} 修改测试地图后提交")
        if args.auto:
            state = orc.confirm_ai_draft(state.run_id)
    if state.status == "completed":
        _print_summary(state)
    return 0


def cmd_confirm(args) -> int:
    settings = _settings()
    orc = Orchestrator(settings, RunStore(settings))
    state = orc.confirm_ai_draft(args.run)
    _print_summary(state)
    return 0


def _print_summary(state) -> None:
    print(f"[done] {state.run_id}")
    print(f"  testpoints={len(state.testpoints.test_points) if state.testpoints else 0}  "
          f"cases={len(state.cases.cases) if state.cases else 0}")
    if state.review:
        print(f"  review score={state.review.score} gate={state.review.gate} issues={len(state.review.issues)}")
        for issue in state.review.issues[:6]:
            print(f"    - [{issue.severity}/{issue.type}] {issue.message[:90]}")
    for stage in state.stages:
        print(f"  stage {stage.stage:<14} {stage.duration_ms:>5}ms  {stage.metrics}")


def cmd_eval(args) -> int:
    from atp.eval.runner import run_all, save_report

    settings = _settings()
    golden = Path(args.golden)
    if not golden.is_absolute():
        golden = settings.golden_dir / golden.name if (settings.golden_dir / golden.name).exists() else golden
    if not golden.exists():
        print(f"[error] 找不到基准集：{golden}", file=sys.stderr)
        return 2
    print(f"[eval] llm_mode={settings.llm_mode} golden={golden} judge={args.judge} sweep={args.sweep}")
    ev = run_all(
        settings,
        args.prd,
        golden,
        modes=tuple(args.modes.split(",")),
        judge=args.judge,
        sweep=args.sweep,
    )
    report = save_report(ev, ROOT / "reports")
    print(f"[eval] 报告已生成：{report}\n")
    print(report.read_text(encoding="utf-8"))
    return 0


def cmd_lint(args) -> int:
    from atp.eval.golden_lint import lint_files, render_markdown, render_review_sheet

    settings = _settings()
    prd_path = Path(args.prd)
    if not prd_path.exists():
        for suffix in (".md", ".docx", ".txt"):
            cand = settings.prd_dir / f"{args.prd}{suffix}"
            if cand.exists():
                prd_path = cand
                break
    golden = Path(args.golden)
    if not golden.is_absolute() and not golden.exists():
        golden = settings.golden_dir / golden.name
    if not prd_path.exists() or not golden.exists():
        print(f"[error] 文件不存在：prd={prd_path} golden={golden}", file=sys.stderr)
        return 2
    rep = lint_files(prd_path, golden)
    print(render_markdown(rep))

    if args.sheet:
        from atp.eval.golden_lint import load_rows
        from atp.parsers.prd import parse_file

        sheet = Path(args.sheet)
        prd = parse_file(prd_path, prd_id=prd_path.stem)
        sheet.write_text(render_review_sheet(prd, load_rows(golden)), encoding="utf-8")
        print(f"\n[sheet] 人工复核表单已生成：{sheet}")
    return 0 if rep.ok else 1


def cmd_doctor(args) -> int:
    """环境自检：换机器/换虚拟机时先跑这个。"""
    import importlib
    import shutil as _shutil

    settings = _settings()
    print(f"Python      : {sys.version.split()[0]} ({sys.executable})")
    print(f"工作目录    : {ROOT}")
    for mod in ("fastapi", "uvicorn", "pydantic", "openpyxl", "pytest"):
        try:
            importlib.import_module(mod)
            print(f"  OK   {mod}")
        except Exception as exc:
            print(f"  FAIL {mod}: {type(exc).__name__}")
    print(f"Node        : {_shutil.which('node') or '未安装（前端校验会跳过）'}")
    print(f"LLM 模式    : {settings.llm_mode}（live={settings.live_llm}）")
    print(f"API Key     : {'已配置' if settings.api_key else '未配置'}")
    print(f"PRD 目录    : {settings.prd_dir}（{len(list(settings.prd_dir.glob('*')))} 个文件）")
    print(f"运行目录    : {settings.runs_dir}（{len(RunStore(settings).list_runs())} 次运行）")
    if settings.llm_mode == "deepseek" and not settings.api_key:
        print("  ! ATP_LLM_MODE=deepseek 但缺 LLM_API_KEY，会直接报错")
    if settings.llm_mode == "mock":
        print("  ! 当前为离线基线模式，评测数字不代表模型能力")
    return 0


def cmd_list(args) -> int:
    settings = _settings()
    orc = Orchestrator(settings, RunStore(settings))
    print("PRD:")
    for p in orc.list_prds():
        print(f"  - {p['prd_id']}  ({p['file']}, {p['size']} bytes)")
    print("\nRUNS:")
    for r in RunStore(settings).list_runs():
        print(f"  - {r['run_id']}  {r['mode']:<8} {r['status']:<14} pts={r['n_testpoints']:<3} cases={r['n_cases']:<3} score={r['review_score']}")
    return 0


def cmd_export(args) -> int:
    from atp.export.xlsx import ExportBlocked, export_xlsx

    settings = _settings()
    store = RunStore(settings)
    state = store.load(args.run)
    try:
        path = export_xlsx(state, store.dir(args.run) / "artifacts")
    except ExportBlocked as exc:
        print(f"[blocked] {exc}", file=sys.stderr)
        return 3
    print(f"[export] {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run.py", description="AI 测试用例生成平台")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="启动 Web 控制台")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("run", help="跑一次流水线")
    p.add_argument("--prd", required=True, help="prd_id 或文件路径")
    p.add_argument("--mode", default="rag_hitl", choices=["bare", "rag", "rag_hitl"])
    p.add_argument("--auto", action="store_true", help="自动确认 AI 草稿并跑完")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("confirm", help="接受 AI 草稿并继续下游")
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_confirm)

    p = sub.add_parser("eval", help="三模式对比评测")
    p.add_argument("--prd", required=True)
    p.add_argument("--golden", required=True)
    p.add_argument("--modes", default="bare,rag,rag_hitl")
    p.add_argument("--judge", default="heuristic", choices=["heuristic", "llm"],
                   help="llm 需要真实模型（ATP_LLM_MODE=deepseek）")
    p.add_argument("--sweep", action="store_true", help="附加匹配阈值敏感性分析")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("list", help="列出 PRD 与运行")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("lint-golden", help="基准集质检（来源可定位/标题可判定/无重复）")
    p.add_argument("--prd", required=True, help="prd_id 或文件路径")
    p.add_argument("--golden", required=True)
    p.add_argument("--sheet", default="", help="同时生成人工复核表单（Markdown 路径）")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("doctor", help="环境自检")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("export", help="导出 Excel")
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
