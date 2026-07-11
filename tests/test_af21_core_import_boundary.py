from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

from okto_pulse.community.adapters.core_import_boundary import (
    AF42_PRIVATE_REACH_IN_BASELINE,
    COMMUNITY_CORE_REACH_IN_LEDGER,
    CORE_IMPORT_GOVERNED_REACH_IN,
    CORE_IMPORT_PUBLIC_CONTRACT,
    CORE_IMPORT_UNGOVERNED_REACH_IN,
    PRIVATE_CORE_DDL_SYMBOL_IMPORTS,
    PRIVATE_CORE_IMPORT_PREFIXES,
    PRIVATE_CORE_REEXPORT_SYMBOL_IMPORTS,
    PRIVATE_CORE_SERVICE_REEXPORT_MODULE,
    PRIVATE_CORE_SYMBOL_IMPORTS,
    PUBLIC_CORE_IMPORT_ALLOWLIST,
    CoreReachInLedgerEntry,
    audit_community_core_import_boundary,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
AF42_EXPECTED_CURRENT_PRIVATE_REACH_INS = 0


def _ledger_entry(
    file_path: str,
    *,
    module: str = "okto_pulse.core.models.db",
    symbols: tuple[str, ...] = ("Card",),
    scope: str = "<module>",
    category: str = "private_namespace_import",
    owner: str = "test-owner",
    reason: str = "test reason",
    target_public_surface: str = "test target surface",
    removal_path: str = "test removal path",
    withdrawal_criterion: str = "test withdrawal criterion",
) -> CoreReachInLedgerEntry:
    return CoreReachInLedgerEntry(
        file_path=file_path,
        scope=scope,
        module=module,
        symbols=tuple(sorted(symbols)),
        category=category,
        owner=owner,
        reason=reason,
        target_public_surface=target_public_surface,
        removal_path=removal_path,
        withdrawal_criterion=withdrawal_criterion,
    )


def test_ts_33e252d6_current_private_core_reach_ins_are_ledgered() -> None:
    report = audit_community_core_import_boundary(REPO_ROOT)

    assert report["ok"] is True, report
    assert report["violations"] == []
    assert report["stale_ledger"] == []
    assert report["incomplete_ledger"] == []
    assert report["baseline_violations"] == ()
    assert report["private_reach_in_baseline"] == AF42_PRIVATE_REACH_IN_BASELINE
    assert report["occurrence_count"] == report["ledger_count"]
    assert report["occurrence_count"] == AF42_EXPECTED_CURRENT_PRIVATE_REACH_INS
    assert report["occurrence_count"] == AF42_PRIVATE_REACH_IN_BASELINE
    assert report["inventory_count"] >= report["occurrence_count"]
    assert report["inventory_by_classification"].get(
        CORE_IMPORT_GOVERNED_REACH_IN, 0
    ) == (
        report["occurrence_count"]
    )
    assert all(entry["cluster"] for entry in report["ledgered"])
    assert {
        entry["classification"] for entry in report["full_inventory"]
    } <= {
        CORE_IMPORT_PUBLIC_CONTRACT,
        CORE_IMPORT_GOVERNED_REACH_IN,
        CORE_IMPORT_UNGOVERNED_REACH_IN,
    }


def test_af42_full_inventory_classifies_every_core_import(tmp_path: Path) -> None:
    adapter = tmp_path / "src" / "okto_pulse" / "community" / "adapter.py"
    adapter.parent.mkdir(parents=True)
    adapter.write_text(
        "from okto_pulse.core.ports.runtime_workers import RuntimeWorkerRegistry\n"
        "from okto_pulse.core.infra.config import CoreSettings\n"
        "from okto_pulse.core.models.db import Card\n",
        encoding="utf-8",
    )

    report = audit_community_core_import_boundary(
        tmp_path,
        ledger=(
            _ledger_entry("src/okto_pulse/community/adapter.py", symbols=("Card",)),
        ),
        private_reach_in_baseline=1,
    )

    by_module = {
        (entry["module"], entry["symbols"]): entry["classification"]
        for entry in report["full_inventory"]
    }

    assert report["ok"] is True, report
    assert report["inventory_count"] == 3
    assert report["inventory_by_classification"] == {
        CORE_IMPORT_GOVERNED_REACH_IN: 1,
        CORE_IMPORT_PUBLIC_CONTRACT: 2,
    }
    assert by_module[
        ("okto_pulse.core.ports.runtime_workers", ("RuntimeWorkerRegistry",))
    ] == CORE_IMPORT_PUBLIC_CONTRACT
    assert by_module[
        ("okto_pulse.core.infra.config", ("CoreSettings",))
    ] == CORE_IMPORT_PUBLIC_CONTRACT
    assert by_module[
        ("okto_pulse.core.models.db", ("Card",))
    ] == CORE_IMPORT_GOVERNED_REACH_IN


def test_af42_governed_baseline_growth_fails_closed(tmp_path: Path) -> None:
    adapter = tmp_path / "src" / "okto_pulse" / "community" / "adapter.py"
    adapter.parent.mkdir(parents=True)
    adapter.write_text(
        "from okto_pulse.core.models.db import Card\n",
        encoding="utf-8",
    )

    report = audit_community_core_import_boundary(
        tmp_path,
        ledger=(
            _ledger_entry("src/okto_pulse/community/adapter.py", symbols=("Card",)),
        ),
        private_reach_in_baseline=0,
    )

    assert report["ok"] is False
    assert report["violations"] == []
    assert report["stale_ledger"] == []
    assert report["incomplete_ledger"] == []
    assert report["baseline_violations"] == (
        {
            "category": "private_reach_in_baseline_growth",
            "reason": "private_reach_in_count_exceeds_af42_baseline",
            "baseline": 0,
            "occurrence_count": 1,
            "ledger_count": 1,
            "remediation_hint": (
                "Burn the reach-in down, or record an explicit decision with "
                "owner, target_public_surface, removal_path and withdrawal_criterion "
                "before raising the governed baseline."
            ),
        },
    )


def test_af42_stale_or_incomplete_ledger_blocks_validation(tmp_path: Path) -> None:
    stale_report = audit_community_core_import_boundary(
        tmp_path / "stale",
        ledger=(
            _ledger_entry("src/okto_pulse/community/missing.py", symbols=("Card",)),
        ),
        private_reach_in_baseline=1,
    )

    incomplete_root = tmp_path / "incomplete"
    adapter = incomplete_root / "src" / "okto_pulse" / "community" / "adapter.py"
    adapter.parent.mkdir(parents=True)
    adapter.write_text(
        "from okto_pulse.core.models.db import Card\n",
        encoding="utf-8",
    )
    incomplete_report = audit_community_core_import_boundary(
        incomplete_root,
        ledger=(
            _ledger_entry(
                "src/okto_pulse/community/adapter.py",
                symbols=("Card",),
                withdrawal_criterion="",
            ),
        ),
        private_reach_in_baseline=1,
    )

    assert stale_report["ok"] is False
    assert len(stale_report["stale_ledger"]) == 1
    assert stale_report["incomplete_ledger"] == []
    assert incomplete_report["ok"] is False
    assert incomplete_report["stale_ledger"] == []
    assert len(incomplete_report["incomplete_ledger"]) == 1


def test_ts_33e252d6_ledger_entries_have_withdrawal_criteria() -> None:
    assert COMMUNITY_CORE_REACH_IN_LEDGER == ()
    for entry in COMMUNITY_CORE_REACH_IN_LEDGER:
        assert entry.file_path.startswith("src/okto_pulse/community/")
        assert entry.scope
        assert entry.module.startswith("okto_pulse.core.")
        assert entry.symbols
        assert entry.owner and entry.owner != "unknown"
        assert entry.reason
        assert entry.target_public_surface
        assert entry.removal_path
        assert entry.withdrawal_criterion


def test_ts_7cc90963_new_private_core_import_fails_closed(tmp_path: Path) -> None:
    rogue = tmp_path / "src" / "okto_pulse" / "community" / "rogue.py"
    rogue.parent.mkdir(parents=True)
    rogue.write_text(
        "from okto_pulse.core.models.db import Card\n",
        encoding="utf-8",
    )

    report = audit_community_core_import_boundary(tmp_path, ledger=())

    assert report["ok"] is False
    assert report["full_inventory"][0]["classification"] == (
        CORE_IMPORT_UNGOVERNED_REACH_IN
    )
    assert report["stale_ledger"] == []
    assert report["violations"] == [
        {
            "file_path": "src/okto_pulse/community/rogue.py",
            "scope": "<module>",
            "module": "okto_pulse.core.models.db",
            "symbols": ("Card",),
            "line": 1,
            "category": "private_namespace_import",
            "reason": "missing_community_core_reach_in_ledger",
            "remediation_hint": (
                "Route through a public core facade/port, or add a ledger entry "
                "with owner, reason, target_public_surface, removal_path and "
                "withdrawal_criterion."
            ),
        }
    ]


def test_ts_7cc90963_public_core_ports_are_not_private_reach_ins(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "src" / "okto_pulse" / "community" / "adapter.py"
    allowed.parent.mkdir(parents=True)
    allowed.write_text(
        "from okto_pulse.core.ports.runtime_workers import RuntimeWorkerRegistry\n"
        "from okto_pulse.core.services import application_kg\n",
        encoding="utf-8",
    )

    report = audit_community_core_import_boundary(tmp_path, ledger=())

    assert report["ok"] is True
    assert report["violations"] == []
    assert report["private_prefixes"] == PRIVATE_CORE_IMPORT_PREFIXES
    assert report["public_core_import_allowlist"] == PUBLIC_CORE_IMPORT_ALLOWLIST
    assert report["private_ddl_symbol_imports"] == PRIVATE_CORE_DDL_SYMBOL_IMPORTS
    assert (
        report["private_reexport_symbol_imports"]
        == PRIVATE_CORE_REEXPORT_SYMBOL_IMPORTS
    )
    assert report["private_symbol_imports"] == PRIVATE_CORE_SYMBOL_IMPORTS
    assert report["private_service_reexport_module"] == PRIVATE_CORE_SERVICE_REEXPORT_MODULE


def test_ts_7cc90963_get_kg_registry_package_reexport_fails_closed(
    tmp_path: Path,
) -> None:
    rogue = tmp_path / "src" / "okto_pulse" / "community" / "kg_adapter.py"
    rogue.parent.mkdir(parents=True)
    rogue.write_text(
        "from okto_pulse.core.kg.interfaces import get_kg_registry\n",
        encoding="utf-8",
    )

    report = audit_community_core_import_boundary(tmp_path, ledger=())

    assert report["ok"] is False
    assert report["violations"][0]["module"] == "okto_pulse.core.kg.interfaces"
    assert report["violations"][0]["symbols"] == ("get_kg_registry",)


def test_ts_7cc90963_core_services_reexported_service_fails_closed(
    tmp_path: Path,
) -> None:
    rogue = tmp_path / "src" / "okto_pulse" / "community" / "seed.py"
    rogue.parent.mkdir(parents=True)
    rogue.write_text(
        "from okto_pulse.core.services import AgentService\n",
        encoding="utf-8",
    )

    report = audit_community_core_import_boundary(tmp_path, ledger=())

    assert report["ok"] is False
    assert report["violations"][0]["module"] == "okto_pulse.core.services"
    assert report["violations"][0]["symbols"] == ("AgentService",)


def test_ts_7cc90963_private_submodule_via_parent_package_fails_closed(
    tmp_path: Path,
) -> None:
    rogue = tmp_path / "src" / "okto_pulse" / "community" / "migrator.py"
    rogue.parent.mkdir(parents=True)
    rogue.write_text(
        "from okto_pulse.core.infra import database as _database\n"
        "from okto_pulse.core.kg.workers import consolidation\n"
        "from okto_pulse.core.mcp import server\n",
        encoding="utf-8",
    )

    report = audit_community_core_import_boundary(tmp_path, ledger=())

    assert report["ok"] is False
    assert [
        (violation["module"], violation["symbols"])
        for violation in report["violations"]
    ] == [
        ("okto_pulse.core.infra", ("database",)),
        ("okto_pulse.core.kg.workers", ("consolidation",)),
        ("okto_pulse.core.mcp", ("server",)),
    ]


def test_ts_7cc90963_core_infra_database_reexports_fail_closed(
    tmp_path: Path,
) -> None:
    rogue = tmp_path / "src" / "okto_pulse" / "community" / "startup.py"
    rogue.parent.mkdir(parents=True)
    rogue.write_text(
        "from okto_pulse.core.infra import get_engine, init_db\n",
        encoding="utf-8",
    )

    report = audit_community_core_import_boundary(tmp_path, ledger=())

    assert report["ok"] is False
    assert report["violations"][0]["module"] == "okto_pulse.core.infra"
    assert report["violations"][0]["symbols"] == ("get_engine", "init_db")


def test_af28_private_core_symbol_import_fails_closed(tmp_path: Path) -> None:
    rogue = tmp_path / "src" / "okto_pulse" / "community" / "kg_adapter.py"
    rogue.parent.mkdir(parents=True)
    rogue.write_text(
        "from okto_pulse.core.kg.tier_power import _normalize_unicode\n",
        encoding="utf-8",
    )

    report = audit_community_core_import_boundary(tmp_path, ledger=())

    assert report["ok"] is False
    assert report["violations"][0]["module"] == "okto_pulse.core.kg.tier_power"
    assert report["violations"][0]["symbols"] == ("_normalize_unicode",)


def test_af28_core_global_discovery_ddl_import_fails_closed(tmp_path: Path) -> None:
    rogue = tmp_path / "src" / "okto_pulse" / "community" / "global_adapter.py"
    rogue.parent.mkdir(parents=True)
    rogue.write_text(
        "from okto_pulse.core.kg.global_discovery.schema import NODE_DDL, VECTOR_INDEXES\n",
        encoding="utf-8",
    )

    report = audit_community_core_import_boundary(tmp_path, ledger=())

    assert report["ok"] is False
    assert report["violations"][0]["module"] == (
        "okto_pulse.core.kg.global_discovery.schema"
    )
    assert report["violations"][0]["symbols"] == ("NODE_DDL", "VECTOR_INDEXES")


def test_af42_private_mcp_server_and_kg_worker_imports_fail_closed(
    tmp_path: Path,
) -> None:
    rogue = tmp_path / "src" / "okto_pulse" / "community" / "worker.py"
    rogue.parent.mkdir(parents=True)
    rogue.write_text(
        "from okto_pulse.core.mcp.server import run_mcp_server\n"
        "from okto_pulse.core.kg.workers.deterministic_worker import run_worker\n",
        encoding="utf-8",
    )

    report = audit_community_core_import_boundary(tmp_path, ledger=())

    assert report["ok"] is False
    assert [
        (violation["module"], violation["symbols"])
        for violation in report["violations"]
    ] == [
        ("okto_pulse.core.mcp.server", ("run_mcp_server",)),
        ("okto_pulse.core.kg.workers.deterministic_worker", ("run_worker",)),
    ]
    assert {
        entry["classification"] for entry in report["full_inventory"]
    } == {CORE_IMPORT_UNGOVERNED_REACH_IN}


def test_af28_dynamic_private_getattr_fails_closed(tmp_path: Path) -> None:
    rogue = tmp_path / "src" / "okto_pulse" / "community" / "dynamic_adapter.py"
    rogue.parent.mkdir(parents=True)
    rogue.write_text(
        "from okto_pulse.core.kg import tier_power as tp\n"
        "fn = getattr(tp, '_normalize_unicode')\n",
        encoding="utf-8",
    )

    report = audit_community_core_import_boundary(tmp_path, ledger=())

    assert report["ok"] is False
    assert report["violations"][0]["module"] == "okto_pulse.core.kg.tier_power"
    assert report["violations"][0]["symbols"] == ("_normalize_unicode",)


def test_af28_dynamic_private_import_module_fails_closed(tmp_path: Path) -> None:
    rogue = tmp_path / "src" / "okto_pulse" / "community" / "dynamic_import.py"
    rogue.parent.mkdir(parents=True)
    rogue.write_text(
        "import importlib\n"
        "mod = importlib.import_module('okto_pulse.core.kg._private_runtime')\n",
        encoding="utf-8",
    )

    report = audit_community_core_import_boundary(tmp_path, ledger=())

    assert report["ok"] is False
    assert report["violations"][0]["module"] == (
        "okto_pulse.core.kg._private_runtime"
    )


def test_af28_s2_dynamic_internal_access_reports_structured_violations(
    tmp_path: Path,
) -> None:
    rogue = tmp_path / "src" / "okto_pulse" / "community" / "dynamic_access.py"
    rogue.parent.mkdir(parents=True)
    rogue.write_text(
        "import importlib\n"
        "from okto_pulse.core.infra import database as _database\n"
        "step_id = '_migrate_legacy'\n"
        "mod = importlib.import_module('okto_pulse.core.infra.database')\n"
        "fn = getattr(_database, step_id)\n",
        encoding="utf-8",
    )

    report = audit_community_core_import_boundary(tmp_path, ledger=())

    assert report["ok"] is False
    assert [
        (violation["module"], violation["symbols"], violation["category"])
        for violation in report["violations"]
    ] == [
        (
            "okto_pulse.core.infra",
            ("database",),
            "private_namespace_import",
        ),
        (
            "okto_pulse.core.infra.database",
            ("*",),
            "dynamic_import_module",
        ),
        (
            "okto_pulse.core.infra.database",
            ("<dynamic:step_id>",),
            "dynamic_getattr",
        ),
    ]
    assert all("remediation_hint" in violation for violation in report["violations"])


def test_af28_s2_tracked_af30_getattrs_and_broad_allowlist_fail_closed() -> None:
    report = audit_community_core_import_boundary(REPO_ROOT)

    tracked = [
        entry
        for entry in report["ledgered"]
        if entry["category"] == "tracked_dynamic_getattr"
    ]

    assert tracked == []

    allowlist_report = audit_community_core_import_boundary(
        REPO_ROOT,
        public_allowlist=("okto_pulse.core.kg",),
    )

    assert allowlist_report["ok"] is False
    assert allowlist_report["invalid_allowlist_entries"] == (
        {
            "module": "okto_pulse.core.kg",
            "category": "invalid_public_core_allowlist",
            "reason": "broad_or_private_public_allowlist",
            "remediation_hint": (
                "Replace broad/private allowlist entries with explicit public "
                "facades or ledgered temporary exceptions carrying owner and "
                "withdrawal criteria."
            ),
        },
    )


def test_ts_7cc90963_core_infra_database_reexport_guard_matches_core() -> None:
    spec = importlib.util.find_spec("okto_pulse.core.infra")
    assert spec is not None
    assert spec.origin is not None

    tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
    database_reexports: set[str] = set()
    for node in tree.body:
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "okto_pulse.core.infra.database"
        ):
            database_reexports.update(alias.name for alias in node.names)

    guarded_reexports = dict(PRIVATE_CORE_REEXPORT_SYMBOL_IMPORTS)[
        "okto_pulse.core.infra"
    ]

    assert database_reexports == set()
    assert set(guarded_reexports).isdisjoint(database_reexports)
