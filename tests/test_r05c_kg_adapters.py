"""R05-C (COMMUNITY target) — KG graph adapters behind the #06 ports.

Scenario mapping (1:1):

  ts_e49513c1 — import audit (negative): core/src imports NO okto_pulse.community
                AND a synthetic core-importing-community is flagged.
  ts_f7b7374d — the Community base registry supplies all six #06 graph slots
                (Community Kùzu adapters, isinstance of the ports); with
                include_graph=False the core still fills them.
  ts_7413e7b2 — REST/MCP rebuild lifecycle preserves structured errors: the
                Community GraphLifecycle returns RebuildReport/PurgeReport DTOs
                (status/reason), never a raw exception.
  ts_fde3a548 — KG replay (e2e): bootstrap + schema version + cypher/vector
                query + lifecycle close through the Community adapters.
  ts_9ee86cb6 — CLI init/seed consume the composition (graph adapters wired).
  ts_6145a84f — dependency audit separates Ladybug/Kùzu as Community-local; a
                non-ledgered core Ladybug import is an offender (negative).
  ts_a6c30200 — schema/layer/safety invariants are unchanged through the
                Community graph store.
"""

from __future__ import annotations

import ast
import asyncio
import os
import tempfile
from pathlib import Path

import pytest

# Importing community.main wires the registry with the Community adapters
# (incl. the six graph adapters) at import time — the e2e smoke surface.
import okto_pulse.community.cli as _cli_mod
import okto_pulse.community.main as _main_mod
import okto_pulse.community.seed as _seed_mod
import okto_pulse.core
from okto_pulse.community.adapters.kg_dependency_audit import (
    LADYBUG_LEDGERED_CORE_MODULES,
    audit_ladybug_ownership,
)

CORE_PKG = Path(okto_pulse.core.__file__).parent


def _module_imports(tree) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


@pytest.fixture
def _isolated_kg():
    """Configure the Community KG registry against an ISOLATED temp KG dir so
    bootstrap/query/rebuild touch a throwaway graph; restore settings+registry."""
    from okto_pulse.core.infra import config as _config
    from okto_pulse.core.infra.config import CoreSettings
    from okto_pulse.core.kg.interfaces import registry as _reg

    saved_settings = _config._settings_instance
    saved_engine = (_reg._registry, _reg._configured)
    saved_data = os.environ.get("DATA_DIR")
    saved_kg = os.environ.get("KG_BASE_DIR")
    tmp = tempfile.mkdtemp()
    try:
        os.environ["DATA_DIR"] = tmp
        os.environ["KG_BASE_DIR"] = str(Path(tmp) / "boards")
        _config.configure_settings(CoreSettings())
        _reg.reset_registry_for_tests()
        from okto_pulse.community.adapters.composition import (
            configure_community_kg_registry,
        )

        # session_factory=None -> no audit/event_bus auto-wire (pure graph e2e).
        configure_community_kg_registry(None)
        yield _reg.get_kg_registry()
    finally:
        try:
            from okto_pulse.core.kg.interfaces.registry import get_kg_registry

            lifecycle = get_kg_registry().graph_lifecycle
            asyncio.run(asyncio.to_thread(lambda: asyncio.run(lifecycle.close(None))))
        except Exception:
            pass
        _config._settings_instance = saved_settings
        _reg._registry, _reg._configured = saved_engine
        if saved_data is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = saved_data
        if saved_kg is None:
            os.environ.pop("KG_BASE_DIR", None)
        else:
            os.environ["KG_BASE_DIR"] = saved_kg


# ===========================================================================
# ts_e49513c1 — import audit (negative).
# ===========================================================================
def test_ts_e49513c1_core_does_not_import_community():
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


def test_ts_e49513c1_synthetic_core_importing_community_is_detectable(tmp_path):
    rogue = tmp_path / "rogue.py"
    rogue.write_text(
        "from okto_pulse.community.adapters.kg import CommunityKuzuGraphStore\n",
        encoding="utf-8",
    )
    mods = _module_imports(ast.parse(rogue.read_text(encoding="utf-8")))
    assert any(m.startswith("okto_pulse.community") for m in mods)


# ===========================================================================
# ts_f7b7374d — Community registry graph providers.
# ===========================================================================
def test_ts_f7b7374d_base_registry_supplies_community_graph_slots():
    from okto_pulse.community.adapters.composition import build_community_base_registry
    from okto_pulse.community.adapters.kg import build_community_graph_providers
    from okto_pulse.core.kg.interfaces.graph_lifecycle import GraphLifecycle
    from okto_pulse.core.kg.interfaces.graph_path_resolver import GraphPathResolver
    from okto_pulse.core.kg.interfaces.graph_schema_manager import GraphSchemaManager
    from okto_pulse.core.kg.interfaces.graph_store import SemanticGraphStore
    from okto_pulse.core.kg.interfaces.graph_transaction import GraphTransaction

    providers = build_community_graph_providers()
    assert set(providers) == {
        "graph_store", "cypher_executor", "graph_transaction",
        "graph_schema_manager", "graph_lifecycle", "graph_path_resolver",
    }
    # Each satisfies its #06 port (subclass IS-A the embedded which IS-A port).
    assert isinstance(providers["graph_store"], SemanticGraphStore)
    assert isinstance(providers["graph_schema_manager"], GraphSchemaManager)
    assert isinstance(providers["graph_lifecycle"], GraphLifecycle)
    assert isinstance(providers["graph_path_resolver"], GraphPathResolver)
    assert isinstance(providers["graph_transaction"], GraphTransaction)
    # They are the Community classes (registered behind the ports).
    assert type(providers["graph_store"]).__name__ == "CommunityKuzuGraphStore"

    # include_graph defaults to wiring the graph slots into the base registry.
    base = build_community_base_registry()
    from okto_pulse.community.adapters.composition import _apply_graph_providers

    _apply_graph_providers(base)
    assert type(base.graph_store).__name__ == "CommunityKuzuGraphStore"
    assert type(base.cypher_executor).__name__ == "CommunityKuzuCypherExecutor"


# ===========================================================================
# ts_7413e7b2 — rebuild lifecycle preserves structured errors.
# ===========================================================================
def test_ts_7413e7b2_lifecycle_returns_structured_reports(_isolated_kg):
    from okto_pulse.core.kg.interfaces.graph_lifecycle import (
        PurgeReport,
        RebuildReport,
    )

    lifecycle = _isolated_kg.graph_lifecycle
    board_id = "r05c-rebuild-board"

    async def drive():
        rebuild = await lifecycle.rebuild(board_id)
        purge = await lifecycle.purge(board_id, reason="r05c_test")
        return rebuild, purge

    rebuild, purge = asyncio.run(drive())
    # Structured DTOs (status/reason/steps) — never a raw exception.
    assert isinstance(rebuild, RebuildReport)
    assert rebuild.status in ("rebuilt", "skipped", "failed")
    assert rebuild.board_id == board_id
    assert isinstance(purge, PurgeReport)
    assert purge.status in ("purged", "noop")
    assert purge.reason == "r05c_test"


# ===========================================================================
# ts_fde3a548 — KG replay (e2e) through the Community adapters.
# ===========================================================================
def test_ts_fde3a548_kg_replay_through_community_adapters(_isolated_kg):
    from okto_pulse.core.kg.schema_contract import SCHEMA_VERSION

    reg = _isolated_kg
    board_id = "r05c-replay-board"

    async def drive():
        # bootstrap (GraphSchemaManager port, off-loop-safe sync-under-async).
        await reg.graph_schema_manager.ensure_bootstrapped(board_id)
        version = await reg.graph_schema_manager.current_version(board_id)
        validation = await reg.graph_schema_manager.validate(board_id)
        return version, validation

    version, validation = asyncio.run(drive())
    assert version == SCHEMA_VERSION  # schema invariant preserved
    assert validation.expected_version == SCHEMA_VERSION

    # query surface (no error on an empty board) through the Community store.
    hits = reg.graph_store.vector_search(board_id, "Decision", [0.0] * 384, 5, 0.3)
    assert hits == []
    persisted = reg.graph_store.get_schema_version(board_id)
    assert persisted == SCHEMA_VERSION


# ===========================================================================
# ts_9ee86cb6 — CLI init/seed consume the composition.
# ===========================================================================
def test_ts_9ee86cb6_call_sites_consume_composition_with_graph():
    for mod in (_main_mod, _cli_mod, _seed_mod):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "configure_community_kg_registry" in src, mod.__name__
    # the composition wires the six graph adapters (include_graph default True).
    import inspect

    from okto_pulse.community.adapters import composition as comp

    sig = inspect.signature(comp.configure_community_kg_registry)
    assert "include_graph" in sig.parameters
    assert sig.parameters["include_graph"].default is True


def test_ts_9ee86cb6_cli_seed_bootstrap_migrated_to_port():
    """R05-C (B PARTIAL): the CLI init + demo-seed bootstrap surfaces consume the
    #06 GraphSchemaManager port (ensure_bootstrapped) — NOT the direct
    kg.schema.bootstrap_board_graph symbol (which is gone from these files)."""
    for mod in (_cli_mod, _seed_mod):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        # consumes the port...
        assert "graph_schema_manager.ensure_bootstrapped" in src, mod.__name__
        # ...and no longer imports the direct bootstrap symbol from kg.schema.
        tree = ast.parse(src)
        imported = {
            a.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and (node.module or "") == "okto_pulse.core.kg.schema"
            for a in node.names
        }
        assert "bootstrap_board_graph" not in imported, mod.__name__


# ===========================================================================
# ts_6145a84f — Ladybug/Kùzu dependency audit (negative).
# ===========================================================================
def test_ts_6145a84f_dependency_audit_real_core_is_ledgered():
    report = audit_ladybug_ownership(CORE_PKG)
    assert report["ownership"] == "community-local"
    assert report["ok"] is True, f"non-ledgered Ladybug import: {report['offenders']}"
    assert report["offenders"] == []
    # the core must no longer expose Ladybug at all; the Community runtime owns it.
    assert report["core_ladybug_files"] == []
    assert LADYBUG_LEDGERED_CORE_MODULES == frozenset()


def test_ts_6145a84f_dependency_audit_flags_new_ladybug_import(tmp_path):
    pkg = tmp_path
    (pkg / "kg").mkdir(parents=True, exist_ok=True)
    (pkg / "kg" / "schema.py").write_text("import ladybug as kuzu\n", encoding="utf-8")
    (pkg / "services").mkdir(parents=True, exist_ok=True)
    (pkg / "services" / "rogue.py").write_text(
        "import ladybug  # NEW unledgered dependency leak\n", encoding="utf-8"
    )
    report = audit_ladybug_ownership(pkg)
    assert report["ok"] is False
    assert report["offenders"] == ["kg/schema.py", "services/rogue.py"]


# ===========================================================================
# ts_a6c30200 — schema/layer/safety invariants through the Community store.
# ===========================================================================
def test_ts_a6c30200_schema_layer_safety_invariants():
    from okto_pulse.community.adapters.kg import CommunityKuzuGraphStore
    from okto_pulse.core.kg.schema_contract import (
        EDGE_LAYERS,
        NODE_TYPES,
        SCHEMA_VERSION,
        VECTOR_INDEX_TYPES,
        vector_index_name,
    )

    store = CommunityKuzuGraphStore()
    # get_schema_info is pure metadata (no graph connection) — invariants only.
    info = store.get_schema_info("any-board", include_internal=True)
    assert info["schema_version"] == SCHEMA_VERSION  # schema version invariant
    names = {n["name"] for n in info["stable_node_types"]}
    assert names == set(NODE_TYPES)  # NODE_TYPES invariant
    # vector index names invariant.
    idx_by_type = {v["node_type"]: v["index_name"] for v in info["vector_indexes"]}
    for nt in VECTOR_INDEX_TYPES:
        assert idx_by_type[nt] == vector_index_name(nt)
        assert idx_by_type[nt].endswith("_embedding_idx") or "embedding" in idx_by_type[nt]
    # edge provenance layers are unchanged (layer-isolation safety invariant).
    assert "deterministic" in EDGE_LAYERS and "cognitive" in EDGE_LAYERS
