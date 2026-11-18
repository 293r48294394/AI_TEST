"""Skill 3：用例生成（分批）。

分批不是性能优化，是**上下文控制**：一次让模型写 40 条用例必然出现前后矛盾、id 重复、
步骤模板化。按 6~8 条一批，每批独立校验，失败只影响该批，并记录批次级重试次数。

每批的校验器同样强制：test_point_id 必须来自本批测试点、步骤不少于 2 步、本批测试点必须全覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings
from ..llm.base import LLMClient
from ..llm.schema import SchemaRepairFailed, call_json, combine, uniqueness_validator
from ..models import TestCase, TestCaseSet, TestMap, TestPoint
from ..retrieval.bm25 import HybridRetriever, build_context

SYSTEM = """你是一名资深测试工程师，把测试点展开成可直接执行的结构化测试用例。
输出必须严格是一个 JSON 对象：
{"cases": [{
  "id": "TC-001",
  "test_point_id": "TP-001",
  "title": "用例标题（含关键条件与预期，不超过 40 字）",
  "category": "normal|exception|boundary|security|compat|performance",
  "priority": "P0|P1|P2",
  "preconditions": ["前置条件，可执行的具体状态"],
  "steps": [{"action": "具体操作（含入参）", "expected": "该步可观测的结果"}],
  "test_data": {"字段名": "具体值"},
  "tags": ["模块名"]
}]}

硬性要求：
1. 每个 test_point_id 必须生成且只生成 1 条用例，不得遗漏，不得新增。
2. steps 至少 2 步，最多 6 步；每步 action 必须含具体入参或界面路径，expected 必须可观测
   （HTTP 状态码 / 提示文案 / 数据库字段 / 埋点 / 库存数值），禁止「正常显示」「符合预期」这类空话。
3. test_data 必须给出可直接使用的具体值（如 {"amount": "100.00", "coupon_id": "C2024"}），
   边界用例必须是临界值本身及其前后一个最小单位。
4. 不得编造需求里不存在的字段、接口、错误码。若需求未定义错误码，用「业务错误提示」描述。
5. 只输出 JSON。"""


@dataclass
class CasesResult:
    cases: list[TestCase] = field(default_factory=list)
    batches: int = 0
    repair_attempts: int = 0
    first_try_ok_batches: int = 0
    failed_batches: list[str] = field(default_factory=list)


def run(
    client: LLMClient,
    settings: Settings,
    tm: TestMap,
    points: list[TestPoint],
    chunks: list,
    *,
    use_rag: bool = True,
) -> CasesResult:
    result = CasesResult()
    if not points:
        return result

    retriever = HybridRetriever(chunks, top_k=settings.retrieval_top_k) if (use_rag and chunks) else None
    size = max(1, settings.max_cases_per_call)
    batches = [points[i : i + size] for i in range(0, len(points), size)]

    for bi, batch in enumerate(batches, start=1):
        result.batches += 1
        batch_ids = {p.id for p in batch}
        points_block = "\n".join(
            f"- {p.id}｜{_feature_name(tm, p.feature_id)}｜{p.category}｜{p.priority}｜{p.title}"
            f"｜依据：{p.rationale}"
            + (f"｜证据：{', '.join(s.chunk_id for s in p.sources)}" if p.sources else "")
            for p in batch
        )
        evidence = ""
        if retriever:
            queries = [f"{p.title} {p.rationale}" for p in batch]
            hits = retriever.retrieve_for(queries, top_k=8)
            evidence = f"\n\n证据块（写 test_data 时以此为准，不得编造）：\n---\n{build_context(hits, max_chars=4000)}\n---"

        user = f"""本批测试点（必须一一对应生成用例）：
{points_block}{evidence}

请为以上 {len(batch)} 个测试点各生成 1 条用例，只输出 JSON。"""

        try:
            outcome = call_json(
                client,
                task="s3_testcases",
                system=SYSTEM,
                user=user,
                model_cls=TestCaseSet,
                validator=combine(uniqueness_validator(), _batch_validator(batch_ids)),
                max_repair=settings.max_repair,
                extra_payload={
                    "points": [p.model_dump() for p in batch],
                    "features": [f.model_dump() for f in tm.features],
                    "chunks": [h.chunk.as_dict() for h in retriever.retrieve_for([p.title for p in batch], top_k=8)]
                    if retriever
                    else [],
                },
            )
        except SchemaRepairFailed as exc:
            result.failed_batches.append(f"batch-{bi}: {exc}")
            continue

        result.repair_attempts += outcome.attempts - 1
        if outcome.first_try_ok:
            result.first_try_ok_batches += 1
        result.cases.extend(outcome.value.cases)

    # 全局重排 id，保证跨批次唯一
    for idx, case in enumerate(result.cases, start=1):
        case.id = f"TC-{idx:03d}"
    return result


def _batch_validator(batch_ids: set[str]):
    def _validate(obj: TestCaseSet) -> list[str]:
        issues: list[str] = []
        got = {c.test_point_id for c in obj.cases}
        missing = sorted(batch_ids - got)
        extra = sorted(got - batch_ids)
        if missing:
            issues.append(f"以下测试点缺少用例：{missing}")
        if extra:
            issues.append(f"出现了不属于本批的 test_point_id：{extra}")
        dupes = [c.test_point_id for c in obj.cases if [x.test_point_id for x in obj.cases].count(c.test_point_id) > 1]
        if dupes:
            issues.append(f"同一测试点生成了多条用例：{sorted(set(dupes))}")
        thin = [c.id for c in obj.cases if len(c.steps) < 2]
        if thin:
            issues.append(f"步骤少于 2 步：{thin}")
        return issues

    return _validate


def _feature_name(tm: TestMap, feature_id: str) -> str:
    for f in tm.features:
        if f.id == feature_id:
            return f.name
    return feature_id
