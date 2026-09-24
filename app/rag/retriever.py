"""Hybrid retriever: BM25 (lexical) + vectors (semantic) → weighted fusion → MMR diversity → optional rerank.

1. **Candidates**: BM25 and vector search each return their top 4k chunks.
2. **Fusion**: each list is normalised to [0, 1] (divide by its best score) and combined as a weighted
   sum. Unlike plain rank fusion this keeps the *margin* of a strong exact-term match, which matters for
   business documents full of names and numbers. With the offline hashing embedder lexical evidence is
   weighted higher (0.7); with a real semantic model the weights are closer (0.45 lexical).
3. **Diversity (MMR)**: near-duplicate passages (a resubmitted invoice, the same clause quoted twice)
   are pushed down so the k results cover more distinct evidence.
4. **Rerank (optional)**: a callable can reorder the final candidates (e.g. a local model, type-safe).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from app.data.textindex import BM25
from app.rag.embeddings import EmbeddingService
from app.rag.vectors import VectorIndex


@dataclass
class Chunk:
    file: str
    chunk_id: int
    text: str
    score: float = 0.0
    page: int | None = None  # 1-based page for paged documents (PDF); None otherwise


Reranker = Callable[[str, list[Chunk]], list[int]]


class Retriever:
    """Holds the corpus and both indexes. Rebuilt on ingest/refresh."""

    def __init__(self, embeddings: EmbeddingService, lexical_weight: float | None = None, mmr_lambda: float = 0.8):
        self.embeddings = embeddings
        self._fixed_weight = lexical_weight
        self.mmr_lambda = mmr_lambda
        self.chunks: list[Chunk] = []
        self.bm25 = BM25()
        self.vectors = VectorIndex()
        self.reranker: Reranker | None = None

    @property
    def lexical_weight(self) -> float:
        if self._fixed_weight is not None:
            return self._fixed_weight
        return 0.7 if self.embeddings.active == "local" else 0.45

    def build(self, chunks: list[Chunk]) -> Retriever:
        self.chunks = chunks
        corpus = [c.text for c in chunks]
        self.bm25.fit(corpus)
        self.vectors.build(self.embeddings.embed_documents(corpus))
        return self

    def search(self, query: str, k: int = 5, rerank: bool = True) -> list[Chunk]:
        if not self.chunks:
            return []
        pool = k * 4
        lexical = self.bm25.search(query, k=pool)
        qvec = self.embeddings.embed_query(query)
        semantic = [(i, s) for i, s in self.vectors.search(qvec, k=pool) if s > 0] if qvec is not None else []
        w_lex = self.lexical_weight if semantic else 1.0

        fused: dict[int, float] = {}
        for hits, weight in ((lexical, w_lex), (semantic, 1.0 - w_lex)):
            top = hits[0][1] if hits else 0.0
            for idx, score in hits:
                fused[idx] = fused.get(idx, 0.0) + weight * (score / top if top else 0.0)

        ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)[:pool]
        chosen = self._mmr(ranked, k)
        out = [Chunk(file=self.chunks[i].file, chunk_id=self.chunks[i].chunk_id, text=self.chunks[i].text,
                     score=round(s, 5), page=self.chunks[i].page) for i, s in chosen]
        if rerank and self.reranker and len(out) > 1:
            try:
                order = self.reranker(query, out)
                seen = [i for i in dict.fromkeys(order) if 0 <= i < len(out)]
                out = [out[i] for i in seen] + [c for j, c in enumerate(out) if j not in seen]
            except Exception:
                pass  # reranking is an optimisation; never fail the search because of it
        return out

    def _mmr(self, ranked: list[tuple[int, float]], k: int) -> list[tuple[int, float]]:
        """Maximal marginal relevance: trade relevance against similarity to already-picked chunks."""
        mat = self.vectors.matrix
        if mat is None or len(ranked) <= 1:
            return ranked[:k]
        cand = dict(ranked)
        picked: list[int] = []
        while cand and len(picked) < k:
            if not picked:
                best = max(cand, key=cand.get)
            else:
                ids = list(cand)
                redundancy = (mat[ids] @ mat[picked].T).max(axis=1)
                values = [self.mmr_lambda * cand[i] - (1 - self.mmr_lambda) * float(r) for i, r in zip(ids, redundancy, strict=True)]
                best = ids[int(np.argmax(values))]
            picked.append(best)
            cand.pop(best)
        scores = dict(ranked)
        return [(i, scores[i]) for i in picked]

    def doc_summary(self, max_files: int = 20) -> str:
        files = sorted({c.file for c in self.chunks})
        head = ", ".join(files[:max_files])
        more = "" if len(files) <= max_files else f" (+{len(files) - max_files} more)"
        return f"{len(self.chunks)} chunks across files: {head}{more}" if files else ""
