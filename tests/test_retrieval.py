"""检索层：BM25 排序、符号加权、多查询融合、上下文拼装。"""

from __future__ import annotations

from atp.retrieval.bm25 import BM25Index, HybridRetriever, allowed_ids, build_context


def test_bm25_ranks_footnote_chunk_first(parsed):
    idx = BM25Index(parsed.chunks)
    hits = idx.search("脚注 ① 限领 跨活动累计", top_k=3)
    assert hits, "应当召回至少一个块"
    assert "限领" in hits[0].chunk.text or "①" in hits[0].chunk.text


def test_hybrid_boosts_symbol_queries(parsed):
    retriever = HybridRetriever(parsed.chunks, top_k=3)
    hits = retriever.retrieve("表 3 业务规则 ②")
    assert any("②" in h.chunk.text or "业务规则" in h.chunk.heading for h in hits)


def test_retrieve_for_fuses_queries(parsed):
    retriever = HybridRetriever(parsed.chunks, top_k=4)
    hits = retriever.retrieve_for(["退款返还", "过期 恢复"], top_k=4)
    assert 1 <= len(hits) <= 4
    assert len({h.chunk.chunk_id for h in hits}) == len(hits)


def test_empty_query_returns_nothing(parsed):
    idx = BM25Index(parsed.chunks)
    assert idx.search("", top_k=3) == []


def test_build_context_has_chunk_markers(parsed):
    retriever = HybridRetriever(parsed.chunks, top_k=3)
    hits = retriever.retrieve("库存 原子 超卖")
    ctx = build_context(hits)
    for hit in hits:
        assert f"[{hit.chunk.chunk_id}]" in ctx
    assert len(allowed_ids(hits)) == len(hits)


def test_context_respects_char_budget(parsed):
    retriever = HybridRetriever(parsed.chunks, top_k=10)
    hits = retriever.retrieve("优惠券")
    ctx = build_context(hits, max_chars=120)
    assert len(ctx) <= 400  # 至少包含一个块，且不超预算太多
