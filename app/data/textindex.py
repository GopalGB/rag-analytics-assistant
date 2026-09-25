"""Minimal, dependency-free BM25 ranker over a list of text chunks."""

from __future__ import annotations

import math
import re
from collections import Counter

_TOKEN = re.compile(r"[a-z0-9]+")

STOPWORDS = frozenset(
    """a about after all also am an and any are as at be been before being between both but by can could
    did do does doing for from had has have having he her here hers him his how i if in into is it its
    me more most my no nor not of off on once only or other our ours out over own same she should so
    some such than that the their them then there these they this those through to too under until up
    very was we were what when where which while who whom why will with would you your yours
    much many need needed please tell give show find list know get""".split()
)


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def content_terms(text: str) -> list[str]:
    """Tokens minus stopwords (falls back to all tokens if the text is only stopwords)."""
    toks = tokenize(text)
    kept = [t for t in toks if t not in STOPWORDS]
    return kept or toks


class BM25:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs: list[list[str]] = []
        self.df: Counter[str] = Counter()
        self.idf: dict[str, float] = {}
        self.avgdl: float = 0.0

    def fit(self, corpus: list[str]) -> BM25:
        self.docs = [tokenize(d) for d in corpus]
        n = len(self.docs) or 1
        self.df = Counter()
        for doc in self.docs:
            for term in set(doc):
                self.df[term] += 1
        # BM25+ idf (always positive) to avoid negative scores on very common terms.
        self.idf = {
            t: math.log(1 + (n - df + 0.5) / (df + 0.5)) for t, df in self.df.items()
        }
        self.avgdl = sum(len(d) for d in self.docs) / n
        return self

    def search(self, query: str, k: int = 5) -> list[tuple[int, float]]:
        q = content_terms(query)
        scores: list[tuple[int, float]] = []
        for idx, doc in enumerate(self.docs):
            if not doc:
                continue
            freqs = Counter(doc)
            dl = len(doc)
            score = 0.0
            for term in q:
                if term not in freqs:
                    continue
                idf = self.idf.get(term, 0.0)
                tf = freqs[term]
                denom = tf + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                score += idf * (tf * (self.k1 + 1)) / denom
            if score > 0:
                scores.append((idx, score))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:k]
