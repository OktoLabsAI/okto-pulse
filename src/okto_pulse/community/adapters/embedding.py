"""Community embedding adapters (spec R05-B, Onda A).

Two providers implementing the core ``EmbeddingProvider`` port:

  * ``CommunityStubEmbeddingProvider`` — deterministic, zero-dep hash vectors.
  * ``CommunitySentenceTransformerProvider`` — lazy ``sentence-transformers``.

Both expose ``embedding_metadata()`` (R13-A capability) so callers describe /
select them by METADATA, never ``isinstance`` against a concrete class. The
sentence-transformers provider also exposes an OPTIONAL ``preload()`` so a host
can warm the model and degrade to the stub on failure (the host emits
``kg.embedding.load_failed`` keeping the same dimension).

Import-light: ``sentence-transformers`` / ``torch`` are imported lazily inside
``_get_model`` / ``preload`` — importing this module pulls no heavy deps.
Extracted to Community so the core concretes can be retired in R05-E
(register-before-remove).
"""

from __future__ import annotations

import hashlib
import logging
import math
import struct
from typing import Sequence

logger = logging.getLogger("okto_pulse.community.embedding")


class CommunityStubEmbeddingProvider:
    """Deterministic hash-based provider (no external deps)."""

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def encode_batch(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.encode(t) for t in texts]

    def encode(self, text: str) -> list[float]:
        seed = hashlib.sha256((text or "").encode("utf-8")).digest()
        vec: list[float] = []
        counter = 0
        while len(vec) < self.dim:
            chunk = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            for i in range(0, 32, 4):
                if len(vec) >= self.dim:
                    break
                u = struct.unpack(">I", chunk[i : i + 4])[0]
                vec.append((u / 0xFFFFFFFF) * 2.0 - 1.0)
            counter += 1
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    def embedding_metadata(self) -> dict:
        return {
            "model_name": None,
            "embedding_dimension": self.dim,
            "is_loaded": True,
            "is_stub": True,
        }


class CommunitySentenceTransformerProvider:
    """Lazy-loaded sentence-transformers provider (Community edition)."""

    def __init__(self, model_name: str, dim: int = 384) -> None:
        self.model_name = model_name
        self.dim = dim
        self._model = None

    def _get_model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "sentence-transformers is not installed — install with "
                    "`pip install okto-pulse-core[kg-embeddings]` or use stub mode"
                ) from exc
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def preload(self) -> None:
        """Optional warm-up — loads the model now (raises on failure). The host
        uses capability metadata to decide whether to call this."""
        self._get_model()

    def encode(self, text: str) -> list[float]:
        model = self._get_model()
        vec = model.encode(text or "", normalize_embeddings=True)
        return vec.tolist() if hasattr(vec, "tolist") else list(vec)

    def encode_batch(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._get_model()
        batch = model.encode(list(texts), normalize_embeddings=True)
        return [row.tolist() if hasattr(row, "tolist") else list(row) for row in batch]

    def embedding_metadata(self) -> dict:
        return {
            "model_name": self.model_name,
            "embedding_dimension": self.dim,
            "is_loaded": self._model is not None,
            "is_stub": False,
        }


def build_community_embedding_provider(
    *,
    mode: str,
    model_name: str,
    dim: int,
):
    """Pick the embedding provider from settings — sentence-transformers when
    requested, else the deterministic stub. No model is loaded here (lazy)."""
    normalized = (mode or "stub").lower()
    if normalized in ("sentence-transformers", "sentence_transformers", "st"):
        return CommunitySentenceTransformerProvider(model_name=model_name, dim=dim)
    return CommunityStubEmbeddingProvider(dim=dim)


__all__ = [
    "CommunityStubEmbeddingProvider",
    "CommunitySentenceTransformerProvider",
    "build_community_embedding_provider",
]
