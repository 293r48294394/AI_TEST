"""Skill 2：测试点生成。

输入：人工确认后的测试地图（不是 AI 草稿——这是 HITL 的关键收益）。
输出：六类测试点（正常/异常/边界/安全/兼容/性能），每条挂到某个 feature，并带证据引用。

覆盖度由校验器强制：缺类目 → 把缺口写进反馈 → 模型补齐。这样"覆盖"不是靠提示词祈祷，
而是一个可失败的断言。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings
from ..llm.base import LLMClient
from ..llm.schema import call_json, category_coverage_validator, combine, make_citation_validator, uniqueness_validator
from ..models import CATEGORIES, TestMap, TestPointSet
from ..retrieval.bm25 import Hit, HybridRetriever, allowed_ids, build_context
from .s1_testmap import SkillResult

SYSTEM = """你是一名资深测试工程师，负责把需求拆解结果转成"测试点"（不是用例）。
输出必须严格是一个 JSON 对象：
{"test_points": [{
  "id": "TP-001",
  "feature_id": "F-01",
  "category": "normal|exception|boundary|security|compat|performance",
  "title": "一个可验证的具体场景，含具体条件/数值/状态",
  "rationale": "为什么需要这个测试点（指向哪条业务规则或风险）",
  "priority": "P0|P1|P2",
  "sources": [{"chunk_id": "P-0001", "quote": "原文片段"}]
}]}

硬性要求：
1. category 只能用六个值之一，且六类都必须出现（正常/异常/边界/安全/兼容/性能）。
2. 边界类必须落到具体数值或临界条件（如「恰好满 100 元」「第 4 次领取」「结束时间前 1 秒」），
   不要写「测试边界情况」这种空话。
3. 异常类要区分「参数非法」与「业务状态非法」（如券已过期、订单已关闭）。
4. 安全类覆盖越权、重放/幂等、参数篡改；性能类必须带可观测指标（如 P95 延迟、并发数）。
5. title 中不要出现「测试」「验证」开头以外的废话；一条测试点只测一件事。
6. sources 必须引用给定证据块，chunk_id 不得编造；没有依据时留空数组。
7. 只输出 JSON。"""


def _queries(tm: TestMap) -> list[str]:
    q: list[str] = []
    for f in tm.features:
        q.append(f"{f.name} {' '.join(f.rules[:2])}")
    q.append("边界 上限 超限 临界 时间窗 并发 幂等 越权")
    return q


def run(
    client: LLMClient,
    settings: Settings,
    tm: TestMap,
    chunks: list,
    *,
    use_rag: bool = True,
) -> SkillResult:
    hits: list[Hit] = []
    if use_rag and chunks:
        hits = HybridRetriever(chunks, top_k=settings.retrieval_top_k).retrieve_for(_queries(tm), top_k=12)
        evidence = build_context(hits, max_chars=6500)
        ev_block = f"证据块（只能引用这些编号）：\n---\n{evidence}\n---"
    else:
        ev_block = "（无检索模式：请把 sources 设为空数组 []）"

    features_json = "\n".join(
        f"- {f.id} {f.name}：{f.summary}｜规则：{'；'.join(f.rules) if f.rules else '无'}" for f in tm.features
    )
    user = f"""测试地图（已由人工确认，feature_id 必须取自这里）：
{features_json}

{ev_block}

请为每个功能模块生成 3~6 条测试点，总计不少于 15 条，六类覆盖齐全。只输出 JSON。"""

    validator = combine(uniqueness_validator(), _feature_ref(tm), category_coverage_validator(_REQUIRED))
    if use_rag:
        validator = combine(validator, make_citation_validator(allowed_ids(hits)))

    outcome = call_json(
        client,
        task="s2_testpoints",
        system=SYSTEM,
        user=user,
        model_cls=TestPointSet,
        validator=validator,
        max_repair=settings.max_repair,
        extra_payload={"features": [f.model_dump() for f in tm.features], "chunks": [h.chunk.as_dict() for h in hits]},
    )
    return SkillResult(
        value=outcome.value, attempts=outcome.attempts, first_try_ok=outcome.first_try_ok, hits=hits, problems=outcome.problems
    )


_REQUIRED = {"normal": 3, "exception": 3, "boundary": 3, "security": 1, "compat": 1, "performance": 1}


def _feature_ref(tm: TestMap) -> callable:
    ids = tm.feature_ids()

    def _validate(obj: TestPointSet) -> list[str]:
        bad = sorted({p.feature_id for p in obj.test_points if p.feature_id not in ids})
        return [f"feature_id 不存在于测试地图：{bad}，只能是 {sorted(ids)}"] if bad else []

    return _validate


def coverage_counts(points) -> dict[str, int]:
    counts = {c: 0 for c in CATEGORIES}
    for p in points:
        counts[p.category] = counts.get(p.category, 0) + 1
    return counts
