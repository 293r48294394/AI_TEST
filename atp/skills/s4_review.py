"""Skill 4：质量评审。

分两层，顺序固定：
1. 规则层（coverage/rules.py）：覆盖、查重、可追溯、空话检测 —— 确定性、可单测；
2. 语义层（LLM）：找规则找不到的"需求盲区"和"看起来对但跑不通"的步骤。

最后把两层问题合并、按严重级别扣分、给出 pass/revise 闸门。revise 时导出接口会拒绝
出正式文件（只给草稿），这样"评审"就不是装饰。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..config import Settings
from ..coverage.rules import SEVERITY_WEIGHT, coverage_by_category, gate_of, review_rules
from ..llm.base import LLMClient
from ..llm.schema import call_json
from ..models import Issue, ReviewReport, TestCase, TestMap, TestPoint

SYSTEM = """你是一名测试经理，负责对 AI 生成的测试用例做语义级评审。
规则引擎已经检查过覆盖度、重复、格式，你只需要做规则做不到的三件事：
1. 找需求盲区：哪些业务规则/异常路径在用例里完全没有体现（尤其藏在脚注、表格注释里的约束）；
2. 找"看起来对但跑不通"的步骤：缺少前置数据、依赖未定义的接口、预期无法观测；
3. 找幻觉：用例里出现了 PRD 中不存在的字段、错误码、状态机。

判幻觉的硬性要求：**先到下面给出的原文里核对**。
原文里出现过的错误码、字段、状态、数字，一律不算幻觉；
没有给出原文（或原文里查不到依据）时，不得判幻觉，最多判 risk 或 vague，
message 里要写清楚"原文未覆盖到该处"。

输出严格 JSON：
{"semantic_issues": [{"type": "risk|hallucination|vague|coverage", "severity": "blocker|major|minor", "message": "...", "ref": "TC-003"}],
 "suggestions": ["可执行的改进建议，每条不超过 40 字"]}

要求：最多 6 条 issue、4 条建议；没有发现问题就返回空数组，不要为凑数而编造。"""


class SemanticIssue(BaseModel):
    type: Literal["risk", "hallucination", "vague", "coverage"] = "risk"
    severity: Literal["blocker", "major", "minor"] = "minor"
    message: str
    ref: str = ""


class SemanticReview(BaseModel):
    semantic_issues: list[SemanticIssue] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


def run(
    client: LLMClient,
    settings: Settings,
    tm: TestMap,
    points: list[TestPoint],
    cases: list[TestCase],
    *,
    use_semantic: bool = True,
    chunks: list | None = None,
) -> ReviewReport:
    issues, counts, score = review_rules(tm, points, cases)

    suggestions: list[str] = []
    if use_semantic and cases:
        features_block = "\n".join(
            f"- {f.id} {f.name}｜规则：{'；'.join(f.rules) or '无'}｜证据：{_srcs(f)}" for f in tm.features
        )
        points_block = "\n".join(f"- {p.id} {p.category} {p.title}" for p in points[:40])
        cases_block = "\n".join(
            f"- {c.id} <- {c.test_point_id}｜{c.title}｜步骤："
            + " / ".join(f"{s.action}→{s.expected}" for s in c.steps[:4])
            for c in cases[:40]
        )
        evidence_block = _evidence(tm, chunks)
        user = f"""PRD 原文（按被引用的块摘录；判"PRD 里有没有"只能依据这里）：
{evidence_block}

需求拆解：
{features_block}

已生成的测试点：
{points_block}

已生成的用例：
{cases_block}

请指出需求盲区、不可执行步骤、幻觉内容。只输出 JSON。"""
        try:
            outcome = call_json(
                client,
                task="s4_llm_review",
                system=SYSTEM,
                user=user,
                model_cls=SemanticReview,
                max_repair=settings.max_repair,
                extra_payload={"cases": [c.model_dump() for c in cases]},
            )
            sem: SemanticReview = outcome.value
            for si in sem.semantic_issues[:6]:
                issues.append(
                    Issue(type=_map_type(si.type), severity=si.severity, message=f"[语义评审] {si.message}", ref=si.ref)
                )
            suggestions = sem.suggestions[:4]
        except Exception as exc:  # 语义评审失败不应让整条流水线失败
            suggestions = [f"语义评审未能完成（{type(exc).__name__}），本次仅有规则层结论"]

    score = max(0, 100 - sum(SEVERITY_WEIGHT.get(i.severity, 0) for i in issues))
    # 用测试点统计覆盖（比用例统计更接近"设计意图"）
    coverage = coverage_by_category(points) or coverage_by_category(cases)
    return ReviewReport(
        score=score,
        gate=gate_of(issues, score),  # type: ignore[arg-type]
        coverage=coverage,
        issues=issues,
        suggestions=suggestions,
    )


def _map_type(t: str) -> str:
    return {"risk": "risk", "hallucination": "hallucination", "vague": "vague", "coverage": "coverage"}.get(t, "risk")


def _srcs(f) -> str:
    """把某个功能引用的原文片段压成一行，供评审核对。"""
    parts = [f"{s.chunk_id}「{s.quote[:60]}」" for s in getattr(f, "sources", [])[:3]]
    return "；".join(parts) if parts else "无引用（该功能无法溯源，优先按 risk 提）"


def _evidence(tm: TestMap, chunks: list | None) -> str:
    """把功能引用到的 PRD 块原文摘出来喂给评审。

    没有这一步，评审只能凭模型自身记忆判"错误码是否存在"，
    于是把 PRD 里明明写了的错误码判成幻觉（实测 6 条问题里 2 条属此类）。
    """
    if not chunks:
        return "（本次未提供原文；据此不得判幻觉，只能判 risk/vague）"
    by_id: dict[str, object] = {}
    for c in chunks:
        cid = getattr(c, "chunk_id", None) or (c.get("chunk_id") if isinstance(c, dict) else None)
        if cid:
            by_id[str(cid)] = c
    wanted: list[str] = []
    for f in tm.features:
        for s in getattr(f, "sources", []):
            if s.chunk_id in by_id and s.chunk_id not in wanted:
                wanted.append(s.chunk_id)
    if not wanted:
        return "（功能均未引用任何块；据此不得判幻觉，只能判 risk/vague）"
    lines = []
    for cid in wanted[:12]:
        c = by_id[cid]
        heading = getattr(c, "heading", "") or (c.get("heading", "") if isinstance(c, dict) else "")
        text = getattr(c, "text", "") or (c.get("text", "") if isinstance(c, dict) else "")
        lines.append(f"[{cid}] {heading}\n{str(text)[:900]}")
    omitted = len(wanted) - len(lines)
    tail = f"\n（另有 {omitted} 个被引用块未展开）" if omitted > 0 else ""
    return "\n\n".join(lines) + tail
