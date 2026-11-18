"""检索层：BM25（纯 Python，零依赖）+ 可选向量库后端。

选 BM25 而不是上 ChromaDB 的理由要能对面试官讲清楚：
1. 需求文档是"关键词密集、术语精确"的语料（限额、幂等、脚注编号），BM25 在这个场景的召回
   并不输给小模型 embedding，而且结果可解释——每条召回都能指回原文块；
2. 零依赖 = 在受限环境也能跑，评测可复现（embedding 模型换版本会漂移，BM25 不会）；
3. 预留 VectorStore 协议，后续接 ChromaDB 只需实现 retrieve()，评测脚本不用改。

注意：这里做的是"证据检索"，不是"答案生成"。检索质量直接决定引用完整性校验能不能过。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

from ..parsers.prd import Chunk
from ..util import tokens

# 中文查询词里高频但无区分度的词，检索时降权
_QUERY_STOP = {"的", "了", "和", "与", "或", "在", "对", "为", "是", "一个", "进行", "以及", "这个"}


@dataclass
class Hit:
    chunk: Chunk
    score: float

    def as_dict(self) -> dict:
        return {"chunk_id": self.chunk.chunk_id, "heading": self.chunk.heading, "score": round(self.score, 4)}


class Retriever(Protocol):
    def retrieve(self, query: str, top_k: int = 6) -> list[Hit]: ...


class BM25Index:
    """BM25 Okapi，参数按中文短文本调过（k1=1.2, b=0.6）。"""

    def __init__(self, chunks: list[Chunk], k1: float = 1.2, b: float = 0.6):
        self.chunks = chunks
        self.k1, self.b = k1, b
        self.docs = [self._terms(f"{c.heading} {c.text}") for c in chunks]
        self.doc_len = [len(d) for d in self.docs]
        self.avg_len = (sum(self.doc_len) / len(self.doc_len)) if self.doc_len else 0.0
        self.df: Counter[str] = Counter()
        for doc in self.docs:
            self.df.update(set(doc))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - df + 0.5) / (df + 0.5)) for t, df in self.df.items()}

    @staticmethod
    def _terms(text: str) -> list[str]:
        return [t for t in tokens(text) if t not in _QUERY_STOP]

    def score(self, query_terms: list[str], i: int) -> float:
        tf = Counter(self.docs[i])
        total = 0.0
        for term in query_terms:
            if term not in tf:
                continue
            f = tf[term]
            denom = f + self.k1 * (1 - self.b + self.b * self.doc_len[i] / (self.avg_len or 1))
            total += self.idf.get(term, 0.0) * (f * (self.k1 + 1)) / (denom or 1)
        return total

    def search(self, query: str, top_k: int = 6, min_score: float = 0.0) -> list[Hit]:
        q = self._terms(query)
        if not q:
            return []
        scored = [(i, self.score(q, i)) for i in range(len(self.docs))]
        scored = [x for x in scored if x[1] > min_score]
        scored.sort(key=lambda x: (-x[1], self.chunks[x[0]].order))
        return [Hit(self.chunks[i], s) for i, s in scored[:top_k]]


class HybridRetriever:
    """BM25 + 编号/脚注符号的精确命中加权。

    隐性规则常挂在「表2 脚注①」「4.2」「v1.4」这类符号上，纯分词会把这些信息打散，
    所以对查询里出现的编号/脚注符号做一次子串命中加分。
    """

    _symbol = re.compile(r"[①-⑩]|\b(?:v\d+(?:\.\d+)*|\d+\.\d+(?:\.\d+)*)\b|表\s*\d+|脚注")

    def __init__(self, chunks: list[Chunk], top_k: int = 6):
        self.index = BM25Index(chunks)
        self.top_k = top_k
        self.chunks = chunks

    def retrieve(self, query: str, top_k: int | None = None) -> list[Hit]:
        top_k = top_k or self.top_k
        hits = self.index.search(query, top_k=max(top_k * 3, 12))
        symbols = set(self._symbol.findall(query))
        boosted: list[Hit] = []
        for hit in hits:
            bonus = 0.0
            if symbols:
                text = f"{hit.chunk.heading} {hit.chunk.text}"
                bonus = 0.35 * sum(1 for s in symbols if s in text)
            boosted.append(Hit(hit.chunk, hit.score + bonus))
        boosted.sort(key=lambda h: (-h.score, h.chunk.order))
        return boosted[:top_k]

    def retrieve_for(self, queries: list[str], top_k: int | None = None) -> list[Hit]:
        """多查询融合（RRF），用于按功能模块召回相关段落。"""
        top_k = top_k or self.top_k
        agg: dict[str, float] = {}
        by_id: dict[str, Hit] = {}
        for q in queries:
            for rank, hit in enumerate(self.retrieve(q, top_k=top_k), start=1):
                cid = hit.chunk.chunk_id
                agg[cid] = agg.get(cid, 0.0) + 1.0 / (60 + rank)
                by_id[cid] = hit
        ordered = sorted(agg.items(), key=lambda kv: -kv[1])[:top_k]
        return [Hit(by_id[cid].chunk, round(score * 100, 4)) for cid, score in ordered]


def build_context(hits: list[Hit], max_chars: int = 6000) -> str:
    """把召回块拼成提示词里的证据区，带 chunk_id 以便模型引用。"""
    parts, used = [], 0
    for hit in hits:
        block = f"[{hit.chunk.chunk_id}] 〈{hit.chunk.heading}〉\n{hit.chunk.text.strip()}"
        if used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


def allowed_ids(hits: list[Hit]) -> set[str]:
    return {h.chunk.chunk_id for h in hits}
