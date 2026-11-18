"""Skill 1：需求拆解（prd-to-test-map）。

产出：功能清单 + 业务规则 + 风险预判，每条都带证据块引用。
这个阶段的**唯一目标**不是"写得全"，而是"不漏隐性规则"——所以提示词里显式点名
脚注、表格注释、修订记录、待定问题四处，并在校验器里强制至少 2 个功能、每功能至少 1 条引用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings
from ..llm.base import LLMClient
from ..llm.schema import call_json, combine, make_citation_validator, uniqueness_validator
from ..models import TestMap
from ..retrieval.bm25 import Hit, allowed_ids, build_context

SYSTEM = """你是一名有 8 年经验的资深测试分析师，擅长从 PRD 中拆解可测试的功能模块。
你的输出必须严格是一个 JSON 对象，字段如下：
{
  "prd_id": string,
  "features": [
    {
      "id": "F-01",
      "name": "功能模块名（不超过 20 字）",
      "summary": "一句话说明该模块做什么",
      "rules": ["可验证的业务规则，一条一句话，尽量带上数值/状态/条件"],
      "risks": ["该模块最容易出问题的点，一句话"],
      "sources": [{"chunk_id": "P-0001", "quote": "支撑该结论的原文片段（≤60字）"}]
    }
  ]
}

硬性要求：
1. 每个 feature 必须至少引用 1 个 sources，chunk_id 必须来自用户提供的证据块编号，严禁编造。
2. 你必须主动寻找并抽取「隐性规则」：它们通常藏在表格脚注（①②③）、括号补充、修订记录、
   埋点说明、待定问题的答复里，而不是正文主句。隐性规则要在 rules 里原样保留其约束条件。
3. rules 要写成可判定的断言（含数值、状态、时限、角色），不要写"要保证正确"这类空话。
4. 功能拆分粒度：按用户可独立操作的功能划分，6~8 个为宜，**最多 10 个**；
   超过 10 个必须合并同类项（例如把"创建/编辑/下架"合并为"券模板管理"），
   因为输出过长会导致 JSON 被截断。
5. 只输出 JSON，不要任何解释文字或 Markdown 代码围栏。"""


@dataclass
class SkillResult:
    value: object
    attempts: int = 1
    first_try_ok: bool = True
    hits: list[Hit] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def retrieve(prd, top_k: int = 14) -> list[Hit]:
    """广撒网召回：用章节标题 + 关键词组成多查询，确保脚注/规则段不被漏掉。"""
    from ..retrieval.bm25 import HybridRetriever

    headings = [s.heading for s in prd.sections][:20]
    queries = [" ".join(headings)] + [
        "业务规则 约束 上限 限制 不得 不可",
        "脚注 表 注释 ①②③",
        "修订记录 变更 版本",
        "待定问题 确认 结论",
        "异常 边界 并发 幂等",
    ]
    return HybridRetriever(prd.chunks, top_k=top_k).retrieve_for(queries, top_k=top_k)


def build_user_prompt(prd, hits: list[Hit], use_rag: bool, prd_id: str) -> str:
    if use_rag:
        context = build_context(hits, max_chars=7000)
        evidence = f"""以下是需求文档中检索到的证据块（格式 [块编号] 〈章节〉 正文）：
--- 证据开始 ---
{context}
--- 证据结束 ---

sources[].chunk_id 只能取上面出现过的块编号。"""
    else:
        head = "\n".join(f"{s.heading}" for s in prd.sections[:25])
        body = prd.text[:5000]
        evidence = f"""以下是需求文档全文（无检索，直接截断）：
--- 文档开始 ---
{body}
--- 文档结束 ---
注意：本模式没有证据块编号，请把 sources 设为空数组 []。"""

    return f"""需求文档：{prd_id}
章节结构：{[s.heading for s in prd.sections][:25]}

{evidence}

请输出测试地图（JSON）。重点关注：这段需求里有哪些约束是「没写在正文里、但一旦漏测就会线上出事」的。"""


def run(client: LLMClient, settings: Settings, prd, *, use_rag: bool = True, prd_id: str | None = None) -> SkillResult:
    prd_id = prd_id or prd.prd_id
    # 检索面过宽会让模型拆出过多功能、输出被截断；10 块足够覆盖规则/脚注/待定问题
    top_k = min(10, settings.retrieval_top_k * 2)
    hits = retrieve(prd, top_k=top_k) if use_rag else []
    user = build_user_prompt(prd, hits, use_rag, prd_id)

    # 无检索模式下，上下文就是全文（截断），把它作为 payload 传给离线基线，
    # 保证 mock 与真实模型看到的信息量一致，否则 bare 模式在 mock 下会退化成空输出。
    payload_chunks = [h.chunk.as_dict() for h in hits] if use_rag else [c.as_dict() for c in prd.chunks[:24]]

    validator = uniqueness_validator()
    if use_rag:
        validator = combine(validator, make_citation_validator(allowed_ids(hits)), _min_features)

    outcome = call_json(
        client,
        task="s1_testmap",
        system=SYSTEM,
        user=user,
        model_cls=TestMap,
        validator=validator,
        max_repair=settings.max_repair,
        extra_payload={"prd_id": prd_id, "chunks": payload_chunks},
    )
    return SkillResult(
        value=outcome.value, attempts=outcome.attempts, first_try_ok=outcome.first_try_ok, hits=hits, problems=outcome.problems
    )


def _min_features(tm: TestMap) -> list[str]:
    issues = []
    if len(tm.features) < 2:
        issues.append(f"功能模块至少需要 2 个，当前 {len(tm.features)} 个")
    if len(tm.features) > 12:
        issues.append(f"功能模块过多（{len(tm.features)} 个），请合并到 12 个以内，避免输出被截断")
    empty = [f.id for f in tm.features if not f.rules]
    if empty and len(tm.features) <= 3:
        issues.append(f"以下功能没有任何 rules，请从原文补充业务规则：{empty}")
    bloated = [f.id for f in tm.features if len(f.rules) > 8]
    if bloated:
        issues.append(f"以下功能的 rules 超过 8 条，请合并同类项：{bloated}")
    return issues
