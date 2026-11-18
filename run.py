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


def cmd_cost(args) -> int:
    from atp.eval.cost import render_markdown, summarize

    settings = _settings()
    store = RunStore(settings)
    if not store.exists(args.run):
        print(f"[error] 运行不存在：{args.run}", file=sys.stderr)
        return 2
    state = store.load(args.run)
    calls = store.llm_calls(args.run)
    if not calls:
        print(f"[warn] {args.run} 没有任何 LLM 调用记录（mock 模式也会记录）", file=sys.stderr)
    price_in = args.price_in if args.price_in is not None else _env_float("ATP_PRICE_IN")
    price_out = args.price_out if args.price_out is not None else _env_float("ATP_PRICE_OUT")
    summary = summarize(calls, price_in=price_in, price_out=price_out)
    cases = len(state.cases.cases) if state.cases else 0
    print(render_markdown([(state.run_id, summary, cases)]))
    if summary.by_task:
        print("\n## 按阶段拆分\n")
        print("| 阶段 | 调用 | 估算输入 token | 估算输出 token |")
        print("|---|---|---|---|")
        for task, row in sorted(summary.by_task.items()):
            print(f"| {task} | {row['calls']} | {row['in_tokens']} | {row['out_tokens']} |")
    if summary.est_cost is None:
        print("\n> 未配置单价：用 --price-in/--price-out（元/百万 token）或环境变量 ATP_PRICE_IN/ATP_PRICE_OUT 传入。")
    return 0


def _env_float(name: str) -> float | None:
    import os

    raw = os.environ.get(name)
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def cmd_batch(args) -> int:
    from atp.pipeline.batch import run_batch

    settings = _settings()
    ids = [p.strip() for p in args.prds.split(",") if p.strip()]
    if not ids:
        print("[error] --prds 为空", file=sys.stderr)
        return 2
    print(f"[batch] {len(ids)} 份 PRD，并发 {args.workers}，模式 {args.mode}")
    result = run_batch(settings, ids, mode=args.mode, workers=args.workers, auto_confirm=True)

    width = max(len(i.prd_id) for i in result.items)
    print(f"\n{'文档'.ljust(width)}  状态      测试点  用例  评分  闸门    耗时")
    for item in result.items:
        print(
            f"{item.prd_id.ljust(width)}  {item.status:<9} {item.points:>5}  {item.cases:>4}  "
            f"{item.score if item.score is not None else '-':>4}  {item.gate or '-':<6} {item.duration_ms:>5}ms"
            + (f"  ! {item.error}" if item.error else "")
        )
    s = result.summary()
    print(
        f"\n[summary] 成功 {s['success']}/{s['documents']}，失败率 {s['failure_rate']:.1%}，"
        f"总耗时 {s['wall_ms'] / 1000:.1f}s，平均 {s['avg_ms']:.0f}ms/份，吞吐 {s['throughput_per_sec']:.2f} 份/秒"
    )
    return 0 if result.failed == 0 else 1


def cmd_review_apply(args) -> int:
    from atp.eval.golden_lint import lint
    from atp.eval.review import apply_verdicts, load_doc, parse_verdicts, render_template, save_doc
    from atp.parsers.prd import parse_file

    settings = _settings()
    golden = Path(args.golden)
    if not golden.is_absolute() and not golden.exists():
        golden = settings.golden_dir / golden.name
    if not golden.exists():
        print(f"[error] 找不到基准集：{golden}", file=sys.stderr)
        return 2
    doc = load_doc(golden)

    if args.template:
        out = Path(args.verdicts)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_template(doc), encoding="utf-8")
        print(f"[template] 复核录入表已生成：{out}")
        print("  用编辑器打开，每行补上 `通过` / `改 字段=值` / `删 理由`，然后重跑本命令（不带 --template）。")
        return 0

    verdicts_path = Path(args.verdicts)
    if not verdicts_path.exists():
        print(f"[error] 找不到复核文件：{verdicts_path}（先加 --template 生成）", file=sys.stderr)
        return 2
    verdicts, parse_errors = parse_verdicts(verdicts_path.read_text(encoding="utf-8"))
    if parse_errors:
        print("[error] 录入格式有问题：", file=sys.stderr)
        for e in parse_errors[:10]:
            print("  x " + e, file=sys.stderr)
        return 2

    annotator = args.annotator.strip() or "未署名"
    rows, report = apply_verdicts(
        doc["test_points"], verdicts, annotator=annotator, version=args.version, allow_partial=args.partial
    )
    print(f"[review] {report.summary()}")
    print(f"  通过: {report.passed[:8]}{'…' if len(report.passed) > 8 else ''}")
    print(f"  修改: {report.edited or '-'}")
    print(f"  删除: {report.deleted or '-'}")
    if report.errors:
        print("[error] 应用失败（未写入任何文件）：", file=sys.stderr)
        for e in report.errors[:10]:
            print("  x " + e, file=sys.stderr)
        return 1

    # 先在内存副本上跑质检，0 error 才允许落盘
    prd_path = Path(args.prd)
    if not prd_path.exists():
        for suffix in (".md", ".docx", ".txt"):
            cand = settings.prd_dir / f"{args.prd}{suffix}"
            if cand.exists():
                prd_path = cand
                break
    if not prd_path.exists():
        print(f"[error] 找不到 PRD：{args.prd}", file=sys.stderr)
        return 2
    prd = parse_file(prd_path, prd_id=prd_path.stem)
    lint_report = lint(prd, rows)
    print(f"[lint] {len(rows)} 条 → error {len(lint_report.errors)} / warn {len(lint_report.warns)}")
    if report.deleted and any(w.code == "schema.id_sequence" for w in lint_report.warns):
        print("  注意：有删除，编号不连续属正常（warn 不阻断评测）。")
    for issue in lint_report.errors[:10]:
        print(f"  x [{issue.code}] {issue.gid}: {issue.message}")
    if not lint_report.ok:
        print("[error] 质检未通过，拒绝写入。请修正上面的 error 后重试。", file=sys.stderr)
        return 1

    if args.dry_run:
        print("[dry-run] 校验通过，但未写入（去掉 --dry-run 才落盘）")
        return 0

    save_doc(golden, doc, rows, annotator=annotator, version=args.version)
    print(f"[done] 已写入 {golden}（annotator={annotator}, version={args.version}）")
    print("  下一步：python run.py eval --prd coupon-center-prd --golden data/golden/coupon-golden.json")
    return 0


def cmd_review_export(args) -> int:
    from atp.eval.review import load_doc
    from atp.eval.review_xlsx import export_review_xlsx
    from atp.parsers.prd import parse_file

    settings = _settings()
    golden = Path(args.golden)
    if not golden.is_absolute() and not golden.exists():
        golden = settings.golden_dir / golden.name
    prd_path = _resolve_prd_path(settings, args.prd)
    if not golden.exists() or prd_path is None:
        print(f"[error] 找不到文件：golden={golden} prd={args.prd}", file=sys.stderr)
        return 2
    prd = parse_file(prd_path, prd_id=prd_path.stem)
    out = Path(args.out)
    export_review_xlsx(prd, load_doc(golden), out, annotator=args.annotator)
    print(f"[export] Excel 复核表已生成：{out}")
    print("  填 G 列「结论」（下拉：通过/改/删），结论=改 时在 H 列写 标题=...; 期望=...")
    print(f"  填完执行：python run.py review-import --golden {golden} --xlsx {out} --annotator \"你的名字\"")
    return 0


def _resolve_prd_path(settings, ref: str) -> Path | None:
    path = Path(ref)
    if path.exists():
        return path
    for suffix in (".md", ".docx", ".txt"):
        cand = settings.prd_dir / f"{ref}{suffix}"
        if cand.exists():
            return cand
    return None


def cmd_review_import(args) -> int:
    from atp.eval.review import apply_with_gate, load_doc, save_doc
    from atp.eval.review_xlsx import read_verdicts_xlsx
    from atp.parsers.prd import parse_file

    settings = _settings()
    golden = Path(args.golden)
    if not golden.is_absolute() and not golden.exists():
        golden = settings.golden_dir / golden.name
    xlsx = Path(args.xlsx)
    prd_path = _resolve_prd_path(settings, args.prd)
    if not golden.exists() or not xlsx.exists() or prd_path is None:
        print(f"[error] 文件缺失：golden={golden} xlsx={xlsx} prd={args.prd}", file=sys.stderr)
        return 2

    verdicts, sheet_annotator, errors = read_verdicts_xlsx(xlsx)
    if errors:
        print("[error] Excel 读取有问题：", file=sys.stderr)
        for e in errors[:10]:
            print("  x " + e, file=sys.stderr)
        return 2
    annotator = (args.annotator or sheet_annotator or "").strip()
    if not annotator:
        print("[error] 缺少复核人：在 Excel『说明』页 B1 填，或用 --annotator", file=sys.stderr)
        return 2

    prd = parse_file(prd_path, prd_id=prd_path.stem)
    doc = load_doc(golden)
    rows, report, lint_report = apply_with_gate(
        prd=prd,
        doc=doc,
        verdicts=verdicts,
        annotator=annotator,
        version=args.version,
        allow_partial=args.partial,
    )
    print(f"[review] {report.summary()}")
    if not report.ok:
        print("[error] 应用失败（未写入）：", file=sys.stderr)
        for e in report.errors[:10]:
            print("  x " + e, file=sys.stderr)
        return 1
    print(f"[lint] {len(rows)} 条 → error {len(lint_report.errors)} / warn {len(lint_report.warns)}")
    if report.deleted and any(w.code == "schema.id_sequence" for w in lint_report.warns):
        print("  注意：有删除，编号不连续属正常（warn 不阻断评测）。")
    for issue in lint_report.errors[:10]:
        print(f"  x [{issue.code}] {issue.gid}: {issue.message}")
    if not lint_report.ok:
        print("[error] 质检未通过，拒绝写入。", file=sys.stderr)
        return 1
    if args.dry_run:
        print("[dry-run] 校验通过，未写入")
        return 0
    save_doc(golden, doc, rows, annotator=annotator, version=args.version)
    print(f"[done] 已写入 {golden}（annotator={annotator}）")
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

    p = sub.add_parser("cost", help="单次运行的 token/成本估算")
    p.add_argument("--run", required=True)
    p.add_argument("--price-in", type=float, default=None, help="输入单价（元/百万 token）")
    p.add_argument("--price-out", type=float, default=None, help="输出单价（元/百万 token）")
    p.set_defaults(func=cmd_cost)

    p = sub.add_parser("batch", help="多份 PRD 并发批跑")
    p.add_argument("--prds", required=True, help="逗号分隔的 prd_id")
    p.add_argument("--mode", default="rag", choices=["bare", "rag", "rag_hitl"])
    p.add_argument("--workers", type=int, default=3)
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("review-apply", help="把人工复核结论应用到基准集（自动质检后才落盘）")
    p.add_argument("--golden", required=True)
    p.add_argument("--verdicts", default="reports/golden-review.txt", help="复核录入文件")
    p.add_argument("--prd", default="coupon-center-prd")
    p.add_argument("--annotator", default="", help="你的名字（会写进 JSON 的 annotator 字段）")
    p.add_argument("--version", default="v1.0")
    p.add_argument("--template", action="store_true", help="生成空白录入表")
    p.add_argument("--partial", action="store_true", help="允许只复核一部分")
    p.add_argument("--dry-run", action="store_true", help="只校验不写入")
    p.set_defaults(func=cmd_review_apply)

    p = sub.add_parser("review-export", help="导出 Excel 复核表（PRD 原文可读、结论列下拉）")
    p.add_argument("--golden", required=True)
    p.add_argument("--prd", default="coupon-center-prd")
    p.add_argument("--out", default="reports/golden-review.xlsx")
    p.add_argument("--annotator", default="")
    p.set_defaults(func=cmd_review_export)

    p = sub.add_parser("review-import", help="读回 Excel 复核表并应用（质检通过才落盘）")
    p.add_argument("--golden", required=True)
    p.add_argument("--xlsx", default="reports/golden-review.xlsx")
    p.add_argument("--prd", default="coupon-center-prd")
    p.add_argument("--annotator", default="")
    p.add_argument("--version", default="v1.0")
    p.add_argument("--partial", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_review_import)

    p = sub.add_parser("export", help="导出 Excel")
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
