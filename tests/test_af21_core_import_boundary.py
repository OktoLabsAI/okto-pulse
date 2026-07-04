from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

from okto_pulse.community.adapters.core_import_boundary import (
    COMMUNITY_CORE_REACH_IN_LEDGER,
    PRIVATE_CORE_IMPORT_PREFIXES,
    PRIVATE_CORE_REEXPORT_SYMBOL_IMPORTS,
    PRIVATE_CORE_SERVICE_REEXPORT_MODULE,
    PRIVATE_CORE_SYMBOL_IMPORTS,
    audit_community_core_import_boundary,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_ts_33e252d6_current_private_core_reach_ins_are_ledgered() -> None:
    report = audit_community_core_import_boundary(REPO_ROOT)

    assert report["ok"] is True, report
    assert report["violations"] == []
    assert report["stale_ledger"] == []
    assert report["incomplete_ledger"] == []
    assert report["occurrence_count"] == report["ledger_count"]
    assert report["occurrence_count"] == 28


def test_ts_33e252d6_ledger_entries_have_withdrawal_criteria() -> None:
    assert COMMUNITY_CORE_REACH_IN_LEDGER
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
    assert report["stale_ledger"] == []
    assert report["violations"] == [
        {
            "file_path": "src/okto_pulse/community/rogue.py",
            "scope": "<module>",
            "module": "okto_pulse.core.models.db",
            "symbols": ("Card",),
            "line": 1,
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

    assert set(guarded_reexports) == database_reexports
