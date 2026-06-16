"""Embeddings with a zero-dependency local fallback and an optional OpenAI provider.

The local provider is a deterministic hashing embedder: no model download, fully offline,
good enough for demos and tests. Swap in OpenAI (or any model) by setting the env vars.
"""

from __future__ import annotations

import hashlib

import numpy as np

from app.data.textindex import tokenize


class EmbeddingService:
    def __init__(
        self,
        provider: str = "local",
        dim: int = 256,
        openai_api_key: str | None = None,
        openai_base_url: str = "https://api.openai.com/v1",
        openai_model: str = "text-embedding-3-small",
    ):
        self.dim = dim
        self.openai_api_key = openai_api_key
        self.openai_base_url = openai_base_url.rstrip("/")
        self.openai_model = openai_model
        # Resolve "auto": use OpenAI only when a key is present.
        if provider == "auto":
            provider = "openai" if openai_api_key else "local"
        self.provider = provider

    @property
    def name(self) -> str:
        return f"openai:{self.openai_model}" if self.provider == "openai" else f"local:hashing-{self.dim}"

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        if self.provider == "openai":
            try:
                return self._embed_openai(texts)
            except Exception:
                # Fail safe to local embeddings rather than crashing the request path.
                pass
        return self._embed_local(texts)

    def _embed_local(self, texts: list[str]) -> np.ndarray:
        mat = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for tok in tokenize(text):
                h = int(hashlib.blake2b(tok.encode("utf-8"), digest_size=8).hexdigest(), 16)
                mat[i, h % self.dim] += 1.0
        return _l2_normalize(mat)

    def _embed_openai(self, texts: list[str]) -> np.ndarray:
        import requests  # imported lazily; only exercised on the OpenAI embedding path

        resp = requests.post(
            f"{self.openai_base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.openai_api_key}"},
            json={"model": self.openai_model, "input": texts},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        mat = np.array([row["embedding"] for row in data], dtype=np.float32)
        self.dim = mat.shape[1]
        return _l2_normalize(mat)


def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms
