"""In-memory vector index. A dense matrix + cosine search via a single matmul.

This deliberately replaces the "JSON vectors scanned row-by-row in Python" anti-pattern.
For large corpora, swap this class for a real ANN index (sqlite-vec, DuckDB VSS, FAISS) —
see ADOPTION-GUIDE.md. The public API stays the same.
"""

from __future__ import annotations

import numpy as np


class VectorIndex:
    def __init__(self) -> None:
        self._matrix: np.ndarray | None = None

    def build(self, matrix: np.ndarray) -> VectorIndex:
        self._matrix = matrix if matrix.size else None
        return self

    def search(self, query_vec: np.ndarray, k: int = 5) -> list[tuple[int, float]]:
        if self._matrix is None:
            return []
        if query_vec.shape[0] != self._matrix.shape[1]:
            # Dimension mismatch (e.g. provider changed between build and query) — fail safe.
            return []
        sims = self._matrix @ query_vec  # rows are L2-normalized, so dot == cosine
        if k >= sims.shape[0]:
            order = np.argsort(-sims)
        else:
            top = np.argpartition(-sims, k)[:k]
            order = top[np.argsort(-sims[top])]
        return [(int(i), float(sims[i])) for i in order]
