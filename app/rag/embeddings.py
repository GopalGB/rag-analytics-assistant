"""Embeddings: semantic vectors for search, with a persistent cache and a safe offline fallback.

Providers
    local   deterministic hashing embedder — no model, fully offline, good for names/terms (default fallback)
    ollama  a local semantic model served by Ollama, e.g. `nomic-embed-text` (recommended on the Mac)
    openai  any OpenAI-compatible /embeddings API (OpenAI, a LAN server, LM Studio, llama.cpp …)

Vectors are cached on disk by hash(model, prefix, text), so reindexing only embeds new or changed
chunks. If the semantic provider fails while building the index, the WHOLE index falls back to the
hashing embedder (vectors from different models must never be mixed) and `degraded` explains why.
"""

from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path
from typing import Any

import numpy as np

from app.data.textindex import content_terms

_BATCH = 64


class EmbeddingService:
    def __init__(
        self,
        provider: str = "local",
        dim: int = 256,
        openai_api_key: str | None = None,
        openai_base_url: str = "https://api.openai.com/v1",
        openai_model: str = "text-embedding-3-small",
        is_local: bool | None = None,
        cache_dir: str | Path | None = None,
        query_prefix: str = "",
        doc_prefix: str = "",
        timeout: int = 60,
        http: Any = None,
    ):
        self.dim = dim
        self.openai_api_key = openai_api_key
        self.openai_base_url = openai_base_url.rstrip("/")
        self.openai_model = openai_model
        if provider == "auto":  # legacy: semantic only when a key is present
            provider = "openai" if openai_api_key else "local"
        self.provider = provider
        self.query_prefix = query_prefix
        self.doc_prefix = doc_prefix
        self.timeout = timeout
        self._http = http
        if is_local is None:
            from app.llm.providers import is_local_url

            is_local = provider == "local" or is_local_url(self.openai_base_url)
        self.is_local = is_local
        self.active = provider  # what the current index was actually built with
        self.degraded: str | None = None
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._cache: dict[str, np.ndarray] = {}
        self._dirty = False
        self._lock = threading.Lock()
        self._load_cache()

    # ---- identity ------------------------------------------------------------
    @property
    def semantic(self) -> bool:
        return self.provider != "local"

    @property
    def name(self) -> str:
        if self.provider == "local" or self.active == "local":
            base = f"local:hashing-{self.dim}"
            return f"{base} (fallback: {self.degraded})" if self.degraded else base
        return f"{self.provider}:{self.openai_model}"

    # ---- cache ---------------------------------------------------------------
    def _cache_file(self) -> Path | None:
        if not self.cache_dir or not self.semantic:
            return None
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{self.provider}-{self.openai_model}")
        return self.cache_dir / f"{slug}.npz"

    def _load_cache(self) -> None:
        f = self._cache_file()
        if f and f.exists():
            try:
                data = np.load(f, allow_pickle=False)
                self._cache = dict(zip(data["keys"].tolist(), data["vectors"], strict=False))
            except Exception:
                self._cache = {}

    def save_cache(self) -> None:
        f = self._cache_file()
        if not f or not self._dirty or not self._cache:
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        keys = np.array(list(self._cache.keys()))
        vectors = np.stack(list(self._cache.values())).astype(np.float32)
        tmp = f.with_name(f.name + ".tmp.npz")
        np.savez(tmp, keys=keys, vectors=vectors)
        tmp.replace(f)
        self._dirty = False

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.openai_model}\x00{text}".encode()).hexdigest()

    # ---- public API ------------------------------------------------------------
    def embed(self, texts: list[str]) -> np.ndarray:
        """Embed document texts (legacy name). Fails safe to hashing."""
        return self.embed_documents(texts)

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        if not self.semantic:
            self.active = "local"
            return self._embed_local(texts)
        try:
            mat = self._embed_remote([self.doc_prefix + t for t in texts])
            self.active, self.degraded = self.provider, None
            self.save_cache()
            return mat
        except Exception as exc:
            self.active = "local"
            self.degraded = f"{self.provider} embeddings unavailable ({type(exc).__name__})"
            return self._embed_local(texts)

    def embed_query(self, text: str) -> np.ndarray | None:
        """Embed a query in the same space as the current index; None if that space is unavailable."""
        if self.active == "local":
            return self._embed_local([text])[0]
        try:
            return self._embed_remote([self.query_prefix + text], cache=False)[0]
        except Exception:
            return None

    # ---- backends ------------------------------------------------------------
    def _embed_local(self, texts: list[str]) -> np.ndarray:
        mat = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for tok in content_terms(text):
                h = int(hashlib.blake2b(tok.encode("utf-8"), digest_size=8).hexdigest(), 16)
                mat[i, h % self.dim] += 1.0
        return _l2_normalize(mat)

    @property
    def http(self):
        if self._http is None:
            import requests

            self._http = requests
        return self._http

    def _embed_remote(self, texts: list[str], cache: bool = True) -> np.ndarray:
        keys = [self._key(t) for t in texts]
        missing = [i for i, k in enumerate(keys) if not cache or k not in self._cache]
        fresh: dict[int, np.ndarray] = {}
        for start in range(0, len(missing), _BATCH):
            idx = missing[start : start + _BATCH]
            headers = {"Content-Type": "application/json"}
            if self.openai_api_key:
                headers["Authorization"] = f"Bearer {self.openai_api_key}"
            resp = self.http.post(
                f"{self.openai_base_url}/embeddings",
                headers=headers,
                json={"model": self.openai_model, "input": [texts[i] for i in idx]},
                timeout=self.timeout,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"embeddings HTTP {resp.status_code}")
            rows = sorted(resp.json()["data"], key=lambda r: r.get("index", 0))
            for i, row in zip(idx, rows, strict=True):
                fresh[i] = np.asarray(row["embedding"], dtype=np.float32)
        with self._lock:
            for i, vec in fresh.items():
                if cache:
                    self._cache[keys[i]] = vec
                    self._dirty = True
        vectors = [fresh[i] if i in fresh else self._cache[keys[i]] for i in range(len(texts))]
        mat = np.stack(vectors)
        self.dim = mat.shape[1]
        return _l2_normalize(mat)


def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def build_embeddings(settings: Any, cache_dir: str | Path | None = None) -> tuple[EmbeddingService, str]:
    """Create the embedding service from settings. Returns (service, note for the status page)."""
    from app.llm.providers import is_local_url
    from app.llm.registry import ollama_reachable

    s = settings
    local = EmbeddingService("local", dim=s.local_embedding_dim)
    provider = (s.embedding_provider or "local").lower()
    if provider == "auto":
        provider = "ollama" if ollama_reachable(s.ollama_base_url) else "local"
        if provider == "local":
            return local, "Offline hashing embeddings (start Ollama with nomic-embed-text for semantic search)."
    if provider == "local":
        return local, "Offline hashing embeddings."
    if provider == "ollama":
        base, model, key = s.embedding_base_url or s.ollama_base_url, s.embedding_model or "nomic-embed-text", None
    elif provider == "openai":
        base = s.embedding_base_url or s.openai_base_url
        model = s.embedding_model or s.openai_embedding_model
        key = s.embedding_api_key or s.openai_api_key
    else:
        return local, f"Unknown EMBEDDING_PROVIDER {provider!r}; using offline hashing."
    is_local = is_local_url(base)
    if not is_local and not (s.allow_cloud_ai and s.allow_cloud_embeddings):
        return local, ("Cloud embeddings would send every document's text off this machine; set ALLOW_CLOUD_AI and "
                       "ALLOW_CLOUD_EMBEDDINGS to approve. Using offline hashing.")
    nomic = "nomic" in model.lower()
    svc = EmbeddingService(
        provider, dim=s.local_embedding_dim, openai_api_key=key, openai_base_url=base, openai_model=model,
        is_local=is_local, cache_dir=cache_dir,
        query_prefix=s.embedding_query_prefix if s.embedding_query_prefix is not None else ("search_query: " if nomic else ""),
        doc_prefix=s.embedding_doc_prefix if s.embedding_doc_prefix is not None else ("search_document: " if nomic else ""),
    )
    return svc, f"Semantic embeddings: {provider}:{model} ({'local' if is_local else 'cloud'})."
