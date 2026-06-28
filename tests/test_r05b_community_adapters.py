"""R05-B (COMMUNITY target) — Onda A adapters, helper wiring, audits.

Scenario mapping (1:1):

  ts_6aef1dc4 — import audit: core/src imports NO okto_pulse.community AND
                community production code imports NO Onda A core concrete
                (only /adapters/ + tests may).
  ts_6a49bf90 — CommunityFileSystemStorage save/load/delete roundtrip (unit).
  ts_87cf9551 — embedding capability/metadata + preload + degrade-to-stub
                keeping kg.embedding.load_failed + dimension, NO isinstance.
  ts_34ab1390 — Community CrossEncoder registrable; token_overlap fallback when
                the optional dep is absent; none/token_overlap preserved.
  ts_4727a157 — Onda A readiness is ``ready`` only with complete evidence
                (register-before-remove evidence stays blocked) (unit).
  ts_6201bdf5 — init/seed/app consume the composition helper (e2e smoke).
  ts_2b099962 — the audit blocks a synthetic contamination + nothing
                out-of-scope was physically moved (negative).
"""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path

# Importing community.main builds the app + wires the registry with the
# Community adapters at import time (the e2e smoke surface).
import okto_pulse.community.cli as _cli_mod
import okto_pulse.community.main as _main_mod
import okto_pulse.community.seed as _seed_mod
import okto_pulse.core
from okto_pulse.community.adapters.embedding import (
    CommunitySentenceTransformerProvider,
    CommunityStubEmbeddingProvider,
)
from okto_pulse.community.adapters.readiness_evidence import (
    ONDA_A_ADAPTER_KEYS,
    community_onda_a_evidence,
)
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage

CORE_PKG = Path(okto_pulse.core.__file__).parent  # .../src/okto_pulse/core
COMMUNITY_PKG = Path(_main_mod.__file__).parent  # .../src/okto_pulse/community

_ONDA_A_CONCRETES = (
    "FileSystemStorageProvider",
    "InMemoryCacheBackend",
    "InMemoryTokenBucket",
    "InMemorySessionStore",
    "SentenceTransformerProvider",
    "StubEmbeddingProvider",
    "CrossEncoderReranker",
)


def _imported_names(tree) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    return names


def audit_onda_a_concrete_imports(
    root: Path, *, allow_subdirs=("adapters",)
) -> list[str]:
    """Production-code violations: any non-adapter, non-test community module
    importing an Onda A core concrete. Returns ``file::symbol`` keys."""
    violations: list[str] = []
    for py in root.rglob("*.py"):
        rel = py.relative_to(root).as_posix()
        if any(part in allow_subdirs for part in rel.split("/")):
            continue
        if "test" in rel or "__pycache__" in rel:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        names = _imported_names(tree)
        for concrete in _ONDA_A_CONCRETES:
            if concrete in names:
                violations.append(f"{rel}::{concrete}")
    return violations


# ===========================================================================
# ts_6aef1dc4 — import audit.
# ===========================================================================
def test_ts_6aef1dc4_core_does_not_import_community():
    offenders: list[str] = []
    for py in CORE_PKG.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        try:
            for mod in _module_imports(ast.parse(py.read_text(encoding="utf-8"))):
                if mod.startswith("okto_pulse.community"):
                    offenders.append(f"{py.name}: {mod}")
        except SyntaxError:
            continue
    assert offenders == [], f"core imports community: {offenders}"


def test_ts_6aef1dc4_community_prod_imports_no_onda_a_concrete():
    violations = audit_onda_a_concrete_imports(COMMUNITY_PKG)
    assert violations == [], f"community prod imports Onda A concretes: {violations}"


def _module_imports(tree) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


# ===========================================================================
# ts_6a49bf90 — storage roundtrip.
# ===========================================================================
def test_ts_6a49bf90_storage_roundtrip(tmp_path):
    store = CommunityFileSystemStorage(str(tmp_path))

    async def drive():
        path = await store.save("board-1", "doc.txt", b"hello world")
        loaded = await store.load(path)
        first_delete = await store.delete(path)
        second_delete = await store.delete(path)
        return path, loaded, first_delete, second_delete

    path, loaded, first_delete, second_delete = asyncio.run(drive())
    assert loaded == b"hello world"
    assert Path(path).name.endswith("_doc.txt")  # safe-name + token prefix
    assert first_delete is True
    assert second_delete is False  # already gone


# ===========================================================================
# ts_87cf9551 — embedding capability/preload + fallback (NO isinstance).
# ===========================================================================
def test_ts_87cf9551_embedding_capability_metadata():
    stub = CommunityStubEmbeddingProvider(dim=8)
    assert stub.embedding_metadata() == {
        "model_name": None, "embedding_dimension": 8, "is_loaded": True, "is_stub": True
    }
    assert len(stub.encode("x")) == 8

    st = CommunitySentenceTransformerProvider(model_name="m", dim=384)
    # metadata describes WITHOUT loading the model.
    assert st.embedding_metadata() == {
        "model_name": "m", "embedding_dimension": 384, "is_loaded": False, "is_stub": False
    }
    assert st._model is None


def test_ts_87cf9551_preload_degrades_to_stub_keeping_event_and_dim(caplog):
    from okto_pulse.community.main import _preload_embedding_model
    from okto_pulse.core.kg.interfaces.registry import (
        get_kg_registry,
        reset_registry_for_tests,
    )

    class _FailingDuckProvider:
        """Duck-typed (NOT a core SentenceTransformerProvider) — proves no
        isinstance gating."""

        def embedding_metadata(self):
            return {"model_name": "broken/model", "embedding_dimension": 11,
                    "is_loaded": False, "is_stub": False}

        def preload(self):
            raise OSError("model file unreadable")  # non-transient -> fail fast

    class _Settings:
        kg_embedding_dim = 384

    reset_registry_for_tests()
    try:
        from okto_pulse.core.kg.interfaces.registry import (
            _build_defaults,
            configure_kg_registry,
        )
        from okto_pulse.core.kg.providers.testing.memory_audit_repo import (
            InMemoryAuditRepository,
        )
        from okto_pulse.core.kg.providers.testing.memory_event_bus import (
            InMemoryEventBus,
        )

        configure_kg_registry(
            defaults_factory=_build_defaults,
            event_bus=InMemoryEventBus(),
            audit_repo=InMemoryAuditRepository(),
        )
        reg = get_kg_registry()
        reg.embedding_provider = _FailingDuckProvider()
        with caplog.at_level(logging.WARNING, logger="okto_pulse.community.embedding"):
            asyncio.run(_preload_embedding_model(_Settings()))
        swapped = get_kg_registry().embedding_provider
        assert type(swapped).__name__ == "CommunityStubEmbeddingProvider"
        assert swapped.dim == 11  # dimension preserved from the provider metadata
        failed = [r for r in caplog.records if getattr(r, "event", None) == "kg.embedding.load_failed"]
        assert failed, "kg.embedding.load_failed not emitted"
        assert failed[0].fallback == "CommunityStubEmbeddingProvider"
    finally:
        reset_registry_for_tests()


def test_ts_87cf9551_preload_noop_for_stub_provider():
    from okto_pulse.community.main import _preload_embedding_model
    from okto_pulse.core.kg.interfaces.registry import (
        get_kg_registry,
        reset_registry_for_tests,
    )

    class _Settings:
        kg_embedding_dim = 384

    reset_registry_for_tests()
    try:
        from okto_pulse.core.kg.interfaces.registry import (
            _build_defaults,
            configure_kg_registry,
        )
        from okto_pulse.core.kg.providers.testing.memory_audit_repo import (
            InMemoryAuditRepository,
        )
        from okto_pulse.core.kg.providers.testing.memory_event_bus import (
            InMemoryEventBus,
        )

        configure_kg_registry(
            defaults_factory=_build_defaults,
            event_bus=InMemoryEventBus(),
            audit_repo=InMemoryAuditRepository(),
        )
        reg = get_kg_registry()
        original = CommunityStubEmbeddingProvider(dim=5)
        reg.embedding_provider = original
        asyncio.run(_preload_embedding_model(_Settings()))
        # stub provider -> nothing to preload -> unchanged.
        assert get_kg_registry().embedding_provider is original
    finally:
        reset_registry_for_tests()


# ===========================================================================
# ts_34ab1390 — Community CrossEncoder registrable + token_overlap fallback.
# ===========================================================================
def test_ts_34ab1390_cross_encoder_registrable_and_fallback(monkeypatch):
    import sys

    from okto_pulse.community.adapters.rerank import register_community_reranker
    from okto_pulse.core.kg.rerank.factory import (
        get_reranker,
        reset_cross_encoder_factory,
        reset_reranker_cache,
    )

    # Make sentence-transformers unavailable so the community factory raises
    # ImportError -> the core degrades to token_overlap (R13-C preserved).
    for mod in list(sys.modules):
        if mod.startswith("sentence_transformers"):
            monkeypatch.delitem(sys.modules, mod, raising=False)

    class _Blocker:
        def __getattr__(self, name):
            raise ImportError("sentence_transformers blocked by fixture")

    monkeypatch.setitem(sys.modules, "sentence_transformers", _Blocker())

    reset_reranker_cache()
    reset_cross_encoder_factory()
    try:
        register_community_reranker()  # registers the Community factory in core
        rr = get_reranker("cross_encoder", cross_encoder_model="blocked-model")
        assert rr.name == "token_overlap"  # ST absent -> fallback

        # none / token_overlap unaffected.
        reset_reranker_cache()
        assert get_reranker("none").name == "noop"
        assert get_reranker("token_overlap").name == "token_overlap"
    finally:
        reset_cross_encoder_factory()
        reset_reranker_cache()


# ===========================================================================
# ts_4727a157 — Onda A readiness (register-before-remove stays blocked).
# ===========================================================================
def test_ts_4727a157_onda_a_ready_only_with_complete_evidence():
    from okto_pulse.core.application.boundary.adapter_readiness_inventory import (
        REQUIRED_EVIDENCE,
        AdapterEvidence,
        build_adapter_inventory,
        evaluate_removal,
    )

    inv = {e.adapter_key: e for e in build_adapter_inventory()}
    evidence = community_onda_a_evidence()
    assert set(evidence) == set(ONDA_A_ADAPTER_KEYS)

    # register-before-remove: dependency_audit_passed=False -> NOT ready.
    for key in ONDA_A_ADAPTER_KEYS:
        verdict = evaluate_removal(inv[key], evidence[key])
        assert verdict.is_ready is False
        assert verdict.status in ("blocked", "deferred")
        assert "dependency_audit_passed" in verdict.failed_evidence

    # complete evidence -> ready (proves ready REQUIRES full evidence).
    full = AdapterEvidence(**{name: True for name in REQUIRED_EVIDENCE})
    ready = evaluate_removal(inv["filesystem_storage_provider"], full)
    assert ready.is_ready and ready.status == "ready"


# ===========================================================================
# ts_6201bdf5 — init/seed/app consume the composition helper (e2e smoke).
# ===========================================================================
def test_ts_6201bdf5_call_sites_consume_the_helper():
    # AST/text: main / cli / seed reference the helper, not the concretes.
    for mod in (_main_mod, _cli_mod, _seed_mod):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "configure_community_kg_registry" in src, (
            f"{mod.__name__} must consume configure_community_kg_registry"
        )
    main_src = Path(_main_mod.__file__).read_text(encoding="utf-8")
    cli_src = Path(_cli_mod.__file__).read_text(encoding="utf-8")
    assert "community_storage_provider" in main_src
    assert "community_storage_provider" in cli_src

    # e2e: configuring via the helper wires the Community Onda A adapters while
    # the core mounts graph/audit/event_bus.
    from okto_pulse.community.adapters.composition import (
        configure_community_kg_registry,
    )
    from okto_pulse.core.kg.interfaces.registry import (
        get_kg_registry,
        reset_registry_for_tests,
    )

    reset_registry_for_tests()
    try:
        configure_community_kg_registry(object())
        reg = get_kg_registry()
        assert type(reg.cache_backend).__name__ == "CommunityInMemoryCache"
        assert type(reg.rate_limiter).__name__ == "CommunityInMemoryRateLimiter"
        assert type(reg.session_store).__name__ == "CommunityInMemorySessionStore"
        assert reg.graph_store is not None  # core-mounted
        assert reg.audit_repo is not None and reg.event_bus is not None
    finally:
        reset_registry_for_tests()


# ===========================================================================
# ts_2b099962 — audit blocks contamination + no out-of-scope move (negative).
# ===========================================================================
def test_ts_2b099962_audit_flags_synthetic_contamination(tmp_path):
    rogue = tmp_path / "okto_pulse" / "community" / "services" / "rogue.py"
    rogue.parent.mkdir(parents=True, exist_ok=True)
    rogue.write_text(
        "from okto_pulse.core.infra.storage import FileSystemStorageProvider\n"
        "def wire(): return FileSystemStorageProvider('/tmp')\n",
        encoding="utf-8",
    )
    violations = audit_onda_a_concrete_imports(tmp_path / "okto_pulse" / "community")
    assert violations == ["services/rogue.py::FileSystemStorageProvider"]

    # ...but a file UNDER adapters/ is allowed (register-before-remove home).
    ok_adapter = tmp_path / "okto_pulse" / "community" / "adapters" / "legit.py"
    ok_adapter.parent.mkdir(parents=True, exist_ok=True)
    ok_adapter.write_text(
        "from okto_pulse.core.infra.storage import StorageProvider  # port, fine\n",
        encoding="utf-8",
    )
    assert audit_onda_a_concrete_imports(tmp_path / "okto_pulse" / "community") == [
        "services/rogue.py::FileSystemStorageProvider"
    ]


def test_ts_2b099962_deferred_adapters_not_physically_moved():
    # R05-B moved nothing physically; later refactor specs moved concrete
    # relational, Kuzu/Ladybug and ML provider adapters to Community.
    # Remaining deferred/out-of-scope core helpers still exist at their core paths.
    for rel in (
        "kg/providers/embedded/mcp_auth_context.py",
        "telemetry/store.py",
        # Core-owned helpers/ports still stay; concrete ML providers moved out.
        "infra/storage.py",
        "kg/embedding.py",
        "kg/providers/embedded/memory_cache.py",
    ):
        assert (CORE_PKG / rel).exists(), f"core adapter unexpectedly moved/removed: {rel}"

    for rel in (
        "kg/providers/embedded/sqlite_outbox_event_bus.py",
        "kg/providers/embedded/sqlalchemy_audit_repo.py",
        "kg/providers/embedded/kuzu_graph_store.py",
        "kg/providers/embedded/kuzu_cypher_executor.py",
        "kg/providers/embedded/kuzu_graph_transaction.py",
        "kg/providers/embedded/kuzu_graph_schema_manager.py",
        "kg/providers/embedded/kuzu_graph_lifecycle.py",
        "kg/providers/embedded/kuzu_graph_path_resolver.py",
        "kg/rerank/cross_encoder.py",
    ):
        assert not (CORE_PKG / rel).exists(), f"moved adapter still in core: {rel}"

    community_pkg = Path(__file__).resolve().parents[1] / "src" / "okto_pulse" / "community"
    for rel in (
        "adapters/sqlite_outbox_event_bus.py",
        "adapters/sqlalchemy_audit_repo.py",
        "adapters/kuzu_graph_store.py",
        "adapters/kuzu_cypher_executor.py",
        "adapters/kuzu_graph_transaction.py",
        "adapters/kuzu_graph_schema_manager.py",
        "adapters/kuzu_graph_lifecycle.py",
        "adapters/kuzu_graph_path_resolver.py",
        "adapters/embedding.py",
        "adapters/rerank.py",
    ):
        assert (community_pkg / rel).exists(), f"community adapter missing: {rel}"
