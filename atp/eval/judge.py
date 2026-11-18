"""Fidelity 的 LLM judge 实现。

为什么需要它：启发式代理只看"关键数字有没有被带入"，判不了语义——
比如基准点是"部分退款按实付比例返还，最低 0.01 元"，
生成的是"部分退款返还优惠券"（数字全对，但漏了"按比例"和"最低 0.01"），
代理会误判为忠实，judge 不会。

judge 自身的可信度必须被验证：抽 10 对做人工复评，算与人一致率，≥80% 才敢在报告里用它。

评分口径（写进提示词，避免模型自由发挥）：
  5 = 覆盖该场景全部关键约束，措辞可不同但语义等价
  4 = 覆盖主要约束，次要细节缺失（如未提埋点字段）
  3 = 方向正确但漏掉至少一个关键约束（阈值/状态/角色/金额规则）
  2 = 仅字面相关，换一个场景也说得通
  1 = 不相关，或引入了需求里不存在的约束（幻觉）
  忠实 = score ≥ 4
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..config import Settings
from ..llm.base import LLMClient
from ..llm.schema import call_json
from ..models import TestPoint
from .metrics import FidelityResult, GoldenPoint, PairScore

SYSTEM = """你是资深测试评审专家。给你若干"人工基准测试点"与其对应的"AI生成测试点"，
逐个判断 AI 是否忠实覆盖了基准点承载的业务约束。

评分标准（只能给整数）：
5 = 覆盖该场景全部关键约束，措辞不同但语义等价
4 = 覆盖主要约束，次要细节缺失（例如没提埋点字段、没写提示文案）
3 = 方向正确但漏掉至少一个关键约束（阈值/状态流转/角色/金额规则/幂等要求）
2 = 仅字面相关，换一个场景也说得通
1 = 不相关，或引入了需求里不存在的约束（幻觉）

只输出 JSON：
{"verdicts": [{"pair_index": 0, "score": 4, "reason": "不超过 30 字"}]}
要求：每个 pair_index 恰好一条，不得遗漏；reason 必须指出具体缺了什么，不许写"基本符合"。"""


class Verdict(BaseModel):
    pair_index: int
    score: int = Field(ge=1, le=5)
    reason: str = ""


class JudgeOutput(BaseModel):
    verdicts: list[Verdict] = Field(default_factory=list)


def judge_fidelity(
    client: LLMClient,
    settings: Settings,
    points: list[TestPoint],
    golden: list[GoldenPoint],
    pairs: list[PairScore],
    *,
    batch_size: int = 12,
    min_pairs: int = 1,
) -> FidelityResult:
    if len(pairs) < min_pairs:
        return FidelityResult(judge="llm", judged=0)

    by_golden = {g.id: g for g in golden}
    by_point = {p.id: p for p in points}
    scored: list[dict] = []

    for start in range(0, len(pairs), batch_size):
        batch = pairs[start : start + batch_size]
        lines = []
        for i, pair in enumerate(batch):
            g = by_golden.get(pair.golden_id)
            p = by_point.get(pair.generated_id)
            if not g or not p:
                continue
            steps = " / ".join(f"{s.action}→{s.expected}" for s in getattr(p, "steps", [])[:3]) or "（无步骤）"
            lines.append(
                f"[{i}] 基准点：{g.title}\n"
                f"    期望：{g.expectation or '（未写）'}　出处：{g.source}\n"
                f"    生成点：{p.title}\n"
                f"    理由：{p.rationale or '（无）'}\n"
                f"    步骤：{steps}"
            )
        user = "待评判的配对：\n\n" + "\n\n".join(lines) + f"\n\n共 {len(batch)} 对，请逐对评分。只输出 JSON。"

        try:
            outcome = call_json(
                client,
                task="s5_fidelity_judge",
                system=SYSTEM,
                user=user,
                model_cls=JudgeOutput,
                validator=_validator(len(batch)),
                max_repair=settings.max_repair,
                # PairScore 是 dataclass 不是 pydantic 模型，手动序列化
                extra_payload={
                    "pairs": [
                        {"golden_id": p.golden_id, "generated_id": p.generated_id, "match_score": p.score}
                        for p in batch
                    ]
                },
            )
        except Exception as exc:  # judge 失败不应让整份评测崩掉
            scored.append({"error": f"{type(exc).__name__}: {exc}", "stage": "judge"})
            continue

        for verdict in outcome.value.verdicts:
            if 0 <= verdict.pair_index < len(batch):
                pair = batch[verdict.pair_index]
                scored.append(
                    {
                        "golden": pair.golden_id,
                        "generated": pair.generated_id,
                        "score": verdict.score,
                        "reason": verdict.reason,
                        "match_score": pair.score,
                    }
                )

    rows = [r for r in scored if "score" in r]
    if not rows:
        return FidelityResult(judge="llm", judged=0)
    scores = [r["score"] for r in rows]
    return FidelityResult(
        judged=len(scores),
        mean_score=round(sum(scores) / len(scores), 2),
        faithful_rate=round(sum(1 for s in scores if s >= 4) / len(scores), 4),
        judge="llm",
        details=rows,
    )


def _validator(expected: int):
    def _validate(obj: JudgeOutput) -> list[str]:
        issues = []
        idx = [v.pair_index for v in obj.verdicts]
        missing = sorted(set(range(expected)) - set(idx))
        extra = sorted(set(idx) - set(range(expected)))
        if missing:
            issues.append(f"缺少 pair_index：{missing}")
        if extra:
            issues.append(f"越界 pair_index：{extra}")
        if len(idx) != len(set(idx)):
            issues.append("pair_index 重复")
        return issues

    return _validate


def agreement_with_human(judge_rows: list[dict], human: dict[str, int], tolerance: int = 1) -> float | None:
    """judge 与人工评分的一致率（|差| ≤ tolerance 视为一致）。没有人工样本就返回 None。"""
    common = [r for r in judge_rows if r.get("golden") in human and "score" in r]
    if not common:
        return None
    ok = sum(1 for r in common if abs(r["score"] - human[r["golden"]]) <= tolerance)
    return round(ok / len(common), 4)
