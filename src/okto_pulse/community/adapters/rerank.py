"""Community CrossEncoder reranker adapter (spec R05-B, Onda A / IMP3).

Implements the ``cross_encoder`` rerank strategy with Community-owned concrete
ML dependencies and registers it with the core rerank factory's registration
hook — so the concrete adapter lives in the Community edition
WITHOUT the core importing community. ``none`` / ``token_overlap`` / ``llm`` and
the token_overlap fallback are unchanged (register-before-remove).

Import-light: ``sentence-transformers`` is imported lazily inside ``__init__``;
when it is absent the constructor raises ``ImportError`` so the core factory
degrades to ``token_overlap`` (R13-C behaviour preserved).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Sequence

# token_overlap is the preserved fallback (NOT an Onda A adapter); its text
# extractor is reused so the prompt/scoring text matches the core exactly.
from okto_pulse.core.kg.rerank.token_overlap import TokenOverlapReranker


class CommunityCrossEncoderReranker:
    """sentence-transformers cross-encoder second stage (Community edition)."""

    name = "cross_encoder"
    default_model = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    def __init__(self, *, model_name: str | None = None) -> None:
        try:
            from sentence_transformers import CrossEncoder  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover — optional dep
            raise ImportError(
                "CommunityCrossEncoderReranker needs `sentence-transformers`. "
                "Install the optional extra: `pip install "
                "okto-pulse-community`."
            ) from exc
        self._model = CrossEncoder(model_name or self.default_model)

    def rerank(
        self,
        query: str,
        candidates: Sequence,
        *,
        top_n: int = 10,
    ) -> list:
        if top_n <= 0 or not candidates:
            return []
        if not query.strip():
            return list(candidates[:top_n])

        pairs = [(query, TokenOverlapReranker._text_of(c)) for c in candidates]
        scores = self._model.predict(pairs).tolist()

        scored = list(zip(scores, range(len(candidates)), candidates))
        scored.sort(key=lambda t: (t[0], -t[1]), reverse=True)

        out: list = []
        for new_score, _idx, cand in scored[:top_n]:
            if hasattr(cand, "score") and hasattr(cand, "__dataclass_fields__"):
                try:
                    out.append(replace(cand, score=float(new_score)))
                    continue
                except (TypeError, ValueError):
                    pass
            out.append(cand)
        return out


def _community_cross_encoder_factory(model_name: str | None):
    """Factory the core rerank registry calls for ``cross_encoder``. Raises
    ImportError (optional dep absent) -> core falls back to token_overlap."""
    return CommunityCrossEncoderReranker(model_name=model_name)


def register_community_reranker() -> None:
    """Register the Community CrossEncoder factory with the core rerank factory.
    Idempotent — safe to call on every boot/composition."""
    from okto_pulse.core.kg.rerank.factory import register_cross_encoder_factory

    register_cross_encoder_factory(_community_cross_encoder_factory)


__all__ = [
    "CommunityCrossEncoderReranker",
    "register_community_reranker",
]
