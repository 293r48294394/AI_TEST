"""评测指标：把"AI 生成的测试点"和"人工标注基准集"做场景级比对。

三个指标刻意分开定义，因为它们回答三个不同的问题：
- Match Rate（召回视角）：人工认为必须测的场景，AI 覆盖了多少？→ 漏测风险
- Fidelity Rate（准确视角）：命中的场景里，语义上真的忠实于需求的比例 → 幻觉风险
- 采纳率（人类视角）：逐条裁决后可直接用的比例 → 落地成本

另外单列"隐性规则召回率"：只统计藏在脚注/表格/修订记录里的那批基准点。
这是整套评测里最能说明问题的数字 —— 它直接指向 PRD 测试的失败模式。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..models import TestPoint
from ..util import read_json, similarity


@dataclass
class GoldenPoint:
    id: str
    feature: str
    category: str
    title: str
    expectation: str
    priority: str
    source: str
    implicit_rule: bool


@dataclass
class PairScore:
    golden_id: str
    generated_id: str
    score: float
    title: str


@dataclass
class MatchResult:
    total_golden: int = 0
    matched_golden: int = 0
    total_generated: int = 0
    matched_generated: int = 0
    recall: float = 0.0
    precision: float = 0.0
    f1: float = 0.0
    implicit_total: int = 0
    implicit_matched: int = 0
    implicit_recall: float = 0.0
    by_category: dict[str, dict[str, float]] = field(default_factory=dict)
    pairs: list[PairScore] = field(default_factory=list)
    missed: list[GoldenPoint] = field(default_factory=list)
    unmatched_generated: list[str] = field(default_factory=list)


def load_golden(path: Path) -> list[GoldenPoint]:
    data = read_json(path)
    rows = data["test_points"] if isinstance(data, dict) else data
    return [
        GoldenPoint(
            id=r["id"],
            feature=r.get("feature", ""),
            category=r.get("category", "normal"),
            title=r["title"],
            expectation=r.get("expectation", ""),
            priority=r.get("priority", "P1"),
            source=r.get("source", ""),
            implicit_rule=bool(r.get("implicit_rule", False)),
        )
        for r in rows
    ]


def _score(pair_golden: GoldenPoint, point: TestPoint) -> float:
    """场景相似度：标题为主，期望文本与类别作修正项。

    不用 embedding 是刻意的：基准集只有几十条，人工可核对；而且指标必须可复现——
    换一个 embedding 模型就换一套结论，不叫评测，叫抽签。
    """
    base = similarity(pair_golden.title, point.title)
    base = max(base, similarity(f"{pair_golden.title} {pair_golden.expectation}", f"{point.title} {point.rationale}") * 0.95)
    if pair_golden.category == point.category:
        base += 0.08
    elif {pair_golden.category, point.category} == {"boundary", "exception"}:
        base += 0.02
    return round(min(base, 1.0), 4)


def match_points(points: list[TestPoint], golden: list[GoldenPoint], threshold: float = 0.55) -> MatchResult:
    """一对一贪心匹配：每个基准点最多被一条生成点命中（避免一个宽泛标题命中多条）。"""
    res = MatchResult(total_golden=len(golden), total_generated=len(points))
    if not golden or not points:
        res.missed = list(golden)
        res.unmatched_generated = [p.id for p in points]
        return res

    scored: list[tuple[float, int, int]] = []
    for gi, g in enumerate(golden):
        for pi, p in enumerate(points):
            s = _score(g, p)
            if s >= threshold:
                scored.append((s, gi, pi))
    scored.sort(reverse=True)

    used_g: set[int] = set()
    used_p: set[int] = set()
    for s, gi, pi in scored:
        if gi in used_g or pi in used_p:
            continue
        used_g.add(gi)
        used_p.add(pi)
        g = golden[gi]
        res.pairs.append(PairScore(g.id, points[pi].id, s, g.title))

    res.matched_golden = len(used_g)
    res.matched_generated = len(used_p)
    res.recall = round(res.matched_golden / len(golden), 4)
    res.precision = round(res.matched_generated / len(points), 4)
    res.f1 = round(2 * res.recall * res.precision / (res.recall + res.precision), 4) if (res.recall + res.precision) else 0.0
    res.missed = [g for gi, g in enumerate(golden) if gi not in used_g]
    res.unmatched_generated = [p.id for pi, p in enumerate(points) if pi not in used_p]

    implicit = [g for g in golden if g.implicit_rule]
    res.implicit_total = len(implicit)
    res.implicit_matched = sum(1 for p in res.pairs if any(g.id == p.golden_id for g in implicit))
    res.implicit_recall = round(res.implicit_matched / len(implicit), 4) if implicit else 0.0

    for cat in {g.category for g in golden}:
        cat_golden = [g for g in golden if g.category == cat]
        hit = sum(1 for p in res.pairs if any(g.id == p.golden_id and g.category == cat for g in cat_golden))
        res.by_category[cat] = {"total": len(cat_golden), "matched": hit, "recall": round(hit / len(cat_golden), 4)}
    return res


# ------------------------------------------------------------------ fidelity


@dataclass
class FidelityResult:
    judged: int = 0
    mean_score: float = 0.0
    faithful_rate: float = 0.0  # 评分 >= 4 的比例
    judge: str = "heuristic"
    details: list[dict] = field(default_factory=list)


def fidelity_heuristic(points: list[TestPoint], golden: list[GoldenPoint], pairs: list[PairScore]) -> FidelityResult:
    """无 LLM 时的保守代理指标：允许"语义忠实"的下限判断。

    规则：命中对的标题里若出现基准点的关键约束（数字/状态词），记 4 分；仅词面相似记 3 分。
    它低估真实质量，但方向不会错——这个下限数字会把"看起来匹配但没测到约束"的用例压下去。
    """
    by_id = {g.id: g for g in golden}
    gen_by_id = {p.id: p for p in points}
    details = []
    scores = []
    for pair in pairs:
        g = by_id.get(pair.golden_id)
        p = gen_by_id.get(pair.generated_id)
        if not g or not p:
            continue
        text = f"{p.title} {p.rationale}"
        numbers = {tok for tok in _numbers(g.title) | _numbers(g.expectation)}
        hit = {n for n in numbers if n in text}
        score = 5 if numbers and len(hit) == len(numbers) else 4 if hit else 3
        scores.append(score)
        details.append({"golden": g.id, "generated": p.id, "score": score, "numbers": sorted(numbers), "hit": sorted(hit)})
    if not scores:
        return FidelityResult(judge="heuristic")
    return FidelityResult(
        judged=len(scores),
        mean_score=round(sum(scores) / len(scores), 2),
        faithful_rate=round(sum(1 for s in scores if s >= 4) / len(scores), 4),
        judge="heuristic",
        details=details,
    )


def _numbers(text: str) -> set[str]:
    import re

    return set(re.findall(r"\d+(?:\.\d+)?", text or ""))


# ----------------------------------------------------------------- adoption


def adoption_stats(state) -> dict:
    total = len(state.adoption)
    if not total:
        return {"total": 0, "rate": None, "note": "未实测：需要人工逐条裁决后才有此指标"}
    usable = sum(1 for a in state.adoption if a.verdict == "usable")
    needs = sum(1 for a in state.adoption if a.verdict == "needs_edit")
    unusable = sum(1 for a in state.adoption if a.verdict == "unusable")
    return {
        "total": total,
        "usable": usable,
        "needs_edit": needs,
        "unusable": unusable,
        "rate": round(usable / total, 4),
    }


# ------------------------------------------------------- format compliance


def format_stats(state) -> dict:
    """格式合规率 = 一次通过契约校验的调用单元数 / 总调用单元数。

    单元定义要跟"一次 LLM 输出"对齐：s1/s2 各算 1 个单元，s3 每一批算 1 个单元。
    数据全部来自阶段指标（attempts / first_try_ok / batches / repair_attempts），不额外埋点。
    """
    units = 0
    first = 0
    repairs = 0
    for stage in state.stages:
        m = stage.metrics or {}
        if "attempts" in m:
            units += 1
            if m.get("first_try_ok", 0) == 1.0:
                first += 1
            repairs += max(0, int(m.get("attempts", 1)) - 1)
        if "batches" in m:
            units += int(m["batches"])
            first += int(m.get("first_try_ok_batches", 0))
            repairs += int(m.get("repair_attempts", 0))
    if not units:
        return {"units": 0, "first_try": 0, "first_try_rate": None, "total_repairs": 0}
    return {
        "units": units,
        "first_try": first,
        "first_try_rate": round(first / units, 4),
        "total_repairs": repairs,
    }
