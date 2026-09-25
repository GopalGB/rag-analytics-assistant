"""In-memory vector index. A dense matrix + cosine search via a single matmul.

Exact search: at small-business scale (tens of thousands of chunks) a matmul over a float32 matrix
takes milliseconds. For much larger corpora swap this class for an ANN index (DuckDB VSS, FAISS,
sqlite-vec) behind the same API.
"""

from __future__ import annotations

import numpy as np


class VectorIndex:
    def __init__(self) -> None:
        self._matrix: np.ndarray | None = None

    @property
    def matrix(self) -> np.ndarray | None:
        return self._matrix

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
