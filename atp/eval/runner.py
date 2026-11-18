"""三模式对比评测执行器。

三种模式的差别只在两个开关上，其他（PRD、Skill 提示词、校验器、评审规则）完全一致——
这是实验有效性的前提，也是面试时最容易被追问的点：
  模式1 bare     ：无检索、无人工确认
  模式2 rag      ：有检索、无人工确认
  模式3 rag_hitl ：有检索 + 人工确认（人工修改版测试地图从磁盘录制回放，保证可复现）

关于"人工确认"的可复现性：真跑一次人审后再复跑，结果就变了。所以评测里回放
data/golden/testmap_confirmed.json（你审过一次后落盘的那份），并在报告里明确标注
"人工确认来自录制回放，非本次实时人审"。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..models import TestMap
from ..pipeline.orchestrator import Orchestrator
from ..store import RunStore
from ..util import read_json
from . import metrics as M
from .judge import judge_fidelity

MODES = ("bare", "rag", "rag_hitl")


@dataclass
class ModeOutcome:
    mode: str
    run_id: str = ""
    status: str = ""
    ok: bool = False
    error: str = ""
    n_points: int = 0
    n_cases: int = 0
    score: int | None = None
    gate: str = ""
    match: M.MatchResult = field(default_factory=M.MatchResult)
    fidelity: M.FidelityResult = field(default_factory=M.FidelityResult)
    adoption: dict = field(default_factory=dict)
    fmt: dict = field(default_factory=dict)
    llm: dict = field(default_factory=dict)
    duration_ms: int = 0
    hitl: dict = field(default_factory=dict)
    sweep: list[dict] = field(default_factory=list)


@dataclass
class Evaluation:
    prd_id: str
    golden_path: str
    llm_mode: str
    model: str
    started_at: str = ""
    outcomes: list[ModeOutcome] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "prd_id": self.prd_id,
            "golden_path": self.golden_path,
            "llm_mode": self.llm_mode,
            "model": self.model,
            "started_at": self.started_at,
            "outcomes": [
                {
                    "mode": o.mode,
                    "run_id": o.run_id,
                    "status": o.status,
                    "error": o.error,
                    "n_points": o.n_points,
                    "n_cases": o.n_cases,
                    "score": o.score,
                    "gate": o.gate,
                    "recall": o.match.recall,
                    "precision": o.match.precision,
                    "f1": o.match.f1,
                    "implicit_recall": o.match.implicit_recall,
                    "implicit_total": o.match.implicit_total,
                    "implicit_matched": o.match.implicit_matched,
                    "fidelity_mean": o.fidelity.mean_score,
                    "fidelity_rate": o.fidelity.faithful_rate,
                    "fidelity_judge": o.fidelity.judge,
                    "adoption": o.adoption,
                    "format": o.fmt,
                    "llm": o.llm,
                    "duration_ms": o.duration_ms,
                    "hitl": o.hitl,
                    "sweep": o.sweep,
                    "missed": [{"id": g.id, "title": g.title, "source": g.source, "implicit": g.implicit_rule} for g in o.match.missed],
                    "by_category": o.match.by_category,
                }
                for o in self.outcomes
            ],
        }


def run_one(
    orc: Orchestrator,
    settings: Settings,
    prd,
    mode: str,
    golden: list[M.GoldenPoint],
    confirmed_map: TestMap | None = None,
    *,
    judge: str = "heuristic",
    sweep: bool = False,
) -> ModeOutcome:
    out = ModeOutcome(mode=mode)
    started = time.perf_counter()
    try:
        state = orc.start(prd, mode=mode)
        if state.status == "awaiting_human":
            if confirmed_map and mode == "rag_hitl":
                state = orc.resume(state.run_id, confirmed_map)
                out.hitl = {"confirmed_from": "recorded", "features": len(confirmed_map.features)}
            else:
                state = orc.confirm_ai_draft(state.run_id)
                out.hitl = {"confirmed_from": "auto(模拟人工确认，未改动)", "features": len(state.testmap_final.features) if state.testmap_final else 0}
    except Exception as exc:
        out.error = f"{type(exc).__name__}: {exc}"
        out.status = "failed"
        out.duration_ms = int((time.perf_counter() - started) * 1000)
        return out

    out.run_id = state.run_id
    out.status = state.status
    out.duration_ms = int((time.perf_counter() - started) * 1000)
    points = state.testpoints.test_points if state.testpoints else []
    cases = state.cases.cases if state.cases else []
    out.n_points, out.n_cases = len(points), len(cases)
    out.score = state.review.score if state.review else None
    out.gate = state.review.gate if state.review else ""
    out.match = M.match_points(points, golden)

    # Fidelity：真实模型 + judge=llm 时用语义评判；否则用启发式代理（报告里会标明）
    if judge == "llm" and settings.live_llm:
        try:
            client = orc._make_client(state.run_id)  # 复用编排器的客户端（含审计回调）
            out.fidelity = judge_fidelity(client, settings, points, golden, out.match.pairs)
        except Exception as exc:
            out.fidelity = M.fidelity_heuristic(points, golden, out.match.pairs)
            out.fidelity.details.insert(0, {"error": f"judge 失败，回退启发式：{type(exc).__name__}: {exc}"})
    else:
        out.fidelity = M.fidelity_heuristic(points, golden, out.match.pairs)

    if sweep:
        out.sweep = M.sweep_threshold(points, golden)

    out.adoption = M.adoption_stats(state)
    out.fmt = M.format_stats(state)
    # 放在最后统计，把 judge 的调用与成本一并算进去
    calls = orc.store.llm_calls(state.run_id)
    out.llm = {
        "calls": len(calls),
        "failed": sum(1 for c in calls if not c.get("ok")),
        "latency_ms": sum(int(c.get("latency_ms") or 0) for c in calls),
        "prompt_chars": sum(int(c.get("prompt_chars") or 0) for c in calls),
        "response_chars": sum(int(c.get("response_chars") or 0) for c in calls),
    }
    out.ok = state.status == "completed"
    return out


def run_all(
    settings: Settings,
    prd_id: str,
    golden_path: Path,
    modes: tuple[str, ...] = MODES,
    *,
    judge: str = "heuristic",
    sweep: bool = False,
) -> Evaluation:
    from ..util import now_iso

    store = RunStore(settings)
    orc = Orchestrator(settings, store)
    prd = orc.load_prd(prd_id)
    golden = M.load_golden(golden_path)
    confirmed = _load_confirmed(settings, prd_id)

    ev = Evaluation(
        prd_id=prd_id,
        golden_path=str(golden_path),
        llm_mode=settings.llm_mode,
        model=settings.model if settings.live_llm else "heuristic-baseline",
        started_at=now_iso(),
    )
    for mode in modes:
        ev.outcomes.append(run_one(orc, settings, prd, mode, golden, confirmed, judge=judge, sweep=sweep))
    return ev


def _load_confirmed(settings: Settings, prd_id: str) -> TestMap | None:
    path = settings.golden_dir / f"{prd_id}.testmap_confirmed.json"
    if not path.exists():
        return None
    return TestMap.model_validate(read_json(path))


def save_report(ev: Evaluation, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"eval-{ev.prd_id}-{stamp}.md"
    path.write_text(render_markdown(ev), encoding="utf-8")
    json_path = out_dir / f"eval-{ev.prd_id}-{stamp}.json"
    json_path.write_text(json.dumps(ev.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def render_markdown(ev: Evaluation) -> str:
    live = ev.llm_mode == "deepseek" and ev.model != "heuristic-baseline"
    banner = (
        f"> 模型：`{ev.model}`（真实调用）"
        if live
        else "> ⚠️ 本次为**离线启发式基线**（未调用真实模型），数字仅证明链路与指标可用，**不代表模型能力**。跑真实对比请设 `ATP_LLM_MODE=deepseek` 并配置 key。"
    )
    judge_kind = ev.outcomes[0].fidelity.judge if ev.outcomes else "heuristic"
    judge_label = "LLM judge" if judge_kind == "llm" else "启发式代理"
    lines = [
        f"# 评测报告 · {ev.prd_id}",
        "",
        banner,
        "",
        f"- 开始时间：{ev.started_at}",
        f"- 基准集：`{ev.golden_path}`",
        f"- Fidelity 判定方式：**{judge_label}**"
        + ("（1–5 分制，≥4 记为忠实）" if judge_kind == "llm" else "（只看关键数字是否带入，属保守下限）"),
        "",
        "## 三种模式对比",
        "",
        "| 模式 | 测试点 | 用例 | 场景召回 Match Rate | Precision | F1 | **隐性规则召回** | Fidelity(≥4) | 评审分 | 闸门 | 耗时 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for o in ev.outcomes:
        lines.append(
            f"| {o.mode} | {o.n_points} | {o.n_cases} | {o.match.recall:.1%} | {o.match.precision:.1%} | {o.match.f1:.3f} | "
            f"**{o.match.implicit_recall:.1%}** ({o.match.implicit_matched}/{o.match.implicit_total}) | "
            f"{o.fidelity.faithful_rate:.1%} | {o.score if o.score is not None else '-'} | {o.gate or '-'} | {o.duration_ms/1000:.1f}s |"
        )
    lines += ["", "## 格式与成本", "", "| 模式 | 首次通过率 | 修复重试 | LLM 调用 | 失败调用 | 累计延迟 | 提示词字符 | 输出字符 |", "|---|---|---|---|---|---|---|---|"]
    for o in ev.outcomes:
        f = o.fmt
        lines.append(
            f"| {o.mode} | {_pct(f.get('first_try_rate'))} | {f.get('total_repairs', 0)} | {o.llm.get('calls', 0)} | "
            f"{o.llm.get('failed', 0)} | {o.llm.get('latency_ms', 0)/1000:.1f}s | {o.llm.get('prompt_chars', 0)} | {o.llm.get('response_chars', 0)} |"
        )

    adoption_any = any(o.adoption.get("total") for o in ev.outcomes)
    lines += ["", "## 采纳率（人工裁决）", ""]
    if not adoption_any:
        lines.append("**未实测**：需要在界面上逐条裁决「可用/需修改/不可用」，本表才有数字。不要用估算值。")
    else:
        lines += ["| 模式 | 裁决条数 | 可用 | 需修改 | 不可用 | 采纳率 |", "|---|---|---|---|---|---|"]
        for o in ev.outcomes:
            a = o.adoption
            lines.append(f"| {o.mode} | {a.get('total', 0)} | {a.get('usable', 0)} | {a.get('needs_edit', 0)} | {a.get('unusable', 0)} | {_pct(a.get('rate'))} |")

    lines += ["", "## 按类别召回", "", "| 模式 | 正常 | 异常 | 边界 | 安全 | 兼容 | 性能 |", "|---|---|---|---|---|---|---|"]
    cats = ("normal", "exception", "boundary", "security", "compat", "performance")
    for o in ev.outcomes:
        cells = []
        for c in cats:
            row = o.match.by_category.get(c)
            cells.append(_pct(row["recall"]) if row else "-")
        lines.append(f"| {o.mode} | " + " | ".join(cells) + " |")

    if any(o.sweep for o in ev.outcomes):
        lines += [
            "",
            "## 阈值敏感性（Match Rate 对匹配阈值的依赖）",
            "",
            "> 如果曲线在 0.5–0.7 之间陡降，说明结论由阈值决定，不能当方案效果讲。",
            "",
            "| 阈值 | 模式 | 命中数 | Recall | Precision | F1 |",
            "|---|---|---|---|---|---|",
        ]
        for o in ev.outcomes:
            for row in o.sweep:
                lines.append(
                    f"| {row['threshold']:.2f} | {o.mode} | {row['matched']} | {row['recall']:.1%} | "
                    f"{row['precision']:.1%} | {row['f1']:.3f} |"
                )

    lines += ["", "## 失败分析：人工基准里被漏掉的场景", ""]
    for o in ev.outcomes:
        lines.append(f"### {o.mode}（漏 {len(o.match.missed)}/{o.match.total_golden}）")
        if not o.match.missed:
            lines.append("- 无漏测")
        for g in o.match.missed[:15]:
            tag = "隐性" if g.implicit_rule else "显性"
            lines.append(f"- [{tag}] `{g.id}` {g.title}　← {g.source}")
        lines.append("")

    judge_flag = " --judge llm" if live else ""
    lines += [
        "## 复现方式",
        "",
        "```bash",
        f"$env:ATP_LLM_MODE='{'deepseek' if live else 'mock'}'\n"
        f"python run.py eval --prd {ev.prd_id} --golden data/golden/{Path(ev.golden_path).name} --sweep{judge_flag}",
        "```",
        "",
        "> 指标定义：Match Rate = 命中的基准点 / 基准点总数（一对一贪心匹配，默认阈值 0.55，敏感性见上表）；",
        "> Fidelity = "
        + (
            "LLM judge 按 1–5 分打分，≥4 记为忠实（judge 与人一致率需另行抽检）。"
            if judge_kind == "llm"
            else "启发式代理：命中对里关键数字/状态是否被带入，属保守下限，接入 `--judge llm` 后改为 1–5 分制。"
        ),
    ]
    return "\n".join(lines)


def _pct(v) -> str:
    return f"{v:.1%}" if isinstance(v, (int, float)) else "未实测"
