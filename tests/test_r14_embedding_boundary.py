"""R14 - Community embedding provider registration and core-stub boundary."""

from __future__ import annotations

import ast
from pathlib import Path

import okto_pulse.community as community_pkg
from okto_pulse.community.adapters.composition import (
    build_community_base_registry,
    build_community_embedding,
)


class _StubSettings:
    kg_embedding_mode = "stub"
    kg_embedding_model = "ignored"
    kg_embedding_dim = 384
    kg_session_ttl_seconds = 3600


class _SentenceTransformerSettings(_StubSettings):
    kg_embedding_mode = "sentence-transformers"
    kg_embedding_model = "sentence-transformers/all-MiniLM-L6-v2"


def test_community_stub_mode_registers_explicit_deterministic_provider() -> None:
    provider = build_community_embedding(settings=_StubSettings())
    assert type(provider).__name__ == "CommunityStubEmbeddingProvider"
    assert provider.embedding_metadata() == {
        "model_name": None,
        "embedding_dimension": 384,
        "is_loaded": True,
        "is_stub": True,
    }
    assert provider.encode("same text") == provider.encode("same text")
    assert len(provider.encode("same text")) == 384

    registry = build_community_base_registry(settings=_StubSettings())
    assert type(registry.embedding_provider).__name__ == "CommunityStubEmbeddingProvider"


def test_community_sentence_transformers_provider_stays_lazy() -> None:
    provider = build_community_embedding(settings=_SentenceTransformerSettings())
    assert type(provider).__name__ == "CommunitySentenceTransformerProvider"
    assert provider.embedding_metadata() == {
        "model_name": "sentence-transformers/all-MiniLM-L6-v2",
        "embedding_dimension": 384,
        "is_loaded": False,
        "is_stub": False,
    }
    assert provider._model is None


def test_community_production_code_does_not_import_core_stub_provider() -> None:
    community_root = Path(community_pkg.__file__).resolve().parent
    offenders: list[str] = []
    for path in community_root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "okto_pulse.core.kg.embedding":
                continue
            imported = {alias.name for alias in node.names}
            if "StubEmbeddingProvider" in imported:
                offenders.append(path.relative_to(community_root).as_posix())

    assert offenders == []
