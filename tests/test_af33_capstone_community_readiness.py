from __future__ import annotations

from pathlib import Path

import pytest

from okto_pulse.community.adapters.core_import_boundary import (
    COMMUNITY_CORE_REACH_IN_LEDGER,
    audit_community_core_import_boundary,
)
from okto_pulse.core.application.boundary.adapter_readiness_inventory import (
    build_adapter_inventory,
)
from okto_pulse.core.application.boundary.capstone_conformance import (
    CAPSTONE_OWNERSHIP_MATRIX,
    REQUIRED_SWAP_TARGETS,
    audit_capstone_ledgers,
    validate_capstone_readme_sync,
)


COMMUNITY_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = COMMUNITY_ROOT.parent / "okto_labs_pulse_core"


def _core_readme() -> str:
    readme = CORE_ROOT / "README.md"
    if not readme.exists():
        pytest.skip("Core sibling repo not available for AF33 README sync")
    return readme.read_text(encoding="utf-8")


def test_af33_community_readme_matches_core_capstone_matrix() -> None:
    community_readme = (COMMUNITY_ROOT / "README.md").read_text(encoding="utf-8")

    report = validate_capstone_readme_sync(
        core_readme=_core_readme(),
        community_readme=community_readme,
    )

    assert report.ok is True, report.violations
    for target in REQUIRED_SWAP_TARGETS:
        assert target in community_readme


def test_af33_community_readme_mismatch_fails_closed() -> None:
    community_readme = (COMMUNITY_ROOT / "README.md").read_text(encoding="utf-8")
    mutated = community_readme.replace(
        "APScheduler local runtime -> runtime scheduler adapter",
        "APScheduler local runtime -> embedded singleton",
    )

    report = validate_capstone_readme_sync(
        core_readme=_core_readme(),
        community_readme=mutated,
    )

    assert report.ok is False
    assert "readme_matrix_mismatch" in {v.code for v in report.violations}


def test_af33_community_reach_ins_are_ledgered_with_withdrawal_criteria() -> None:
    boundary_report = audit_community_core_import_boundary(COMMUNITY_ROOT)
    ledger_report = audit_capstone_ledgers(
        community_reach_in_ledger=COMMUNITY_CORE_REACH_IN_LEDGER,
    )

    assert boundary_report["ok"] is True, boundary_report
    assert boundary_report["violations"] == []
    assert boundary_report["stale_ledger"] == []
    assert boundary_report["incomplete_ledger"] == []
    assert ledger_report.ok is True, ledger_report.violations


def test_af33_new_private_core_bypass_fails_closed(tmp_path: Path) -> None:
    rogue = tmp_path / "src" / "okto_pulse" / "community" / "adapters" / "rogue.py"
    rogue.parent.mkdir(parents=True)
    rogue.write_text(
        "from okto_pulse.core.infra.database import get_engine\n",
        encoding="utf-8",
    )

    report = audit_community_core_import_boundary(tmp_path, ledger=())

    assert report["ok"] is False
    assert report["violations"][0]["module"] == "okto_pulse.core.infra.database"
    assert report["violations"][0]["symbols"] == ("get_engine",)


def test_af33_community_adapters_cover_capstone_swap_surfaces() -> None:
    adapter_files = {
        "src/okto_pulse/community/adapters/sqlalchemy_database.py",
        "src/okto_pulse/community/adapters/sqlalchemy_unit_of_work.py",
        "src/okto_pulse/community/adapters/sqlalchemy_repositories.py",
        "src/okto_pulse/community/adapters/relational_schema_lifecycle.py",
        "src/okto_pulse/community/adapters/kuzu_graph_store.py",
        "src/okto_pulse/community/adapters/global_discovery_runtime.py",
        "src/okto_pulse/community/adapters/rebuild_audit_storage.py",
        "src/okto_pulse/community/adapters/storage.py",
        "src/okto_pulse/community/adapters/telemetry_store.py",
        "src/okto_pulse/community/adapters/telemetry_sender.py",
        "src/okto_pulse/community/adapters/scheduler.py",
        "src/okto_pulse/community/adapters/resources.py",
        "src/okto_pulse/community/adapters/capability_descriptors.py",
    }
    missing_files = [
        rel for rel in adapter_files if not (COMMUNITY_ROOT / rel).exists()
    ]
    community_inventory_paths = {
        "src/" + entry.current_module
        for entry in build_adapter_inventory()
        if entry.current_module.startswith("okto_pulse/community/adapters/")
    }
    matrix_text = "\n".join(
        " ".join(
            (
                row.surface,
                row.core_contract,
                row.community_adapter,
                row.saas_swap_target,
                " ".join(row.gate_refs),
            )
        )
        for row in CAPSTONE_OWNERSHIP_MATRIX
    )

    assert missing_files == []
    assert adapter_files & community_inventory_paths
    for token in (
        "SQLite",
        "LadybugDB/Kuzu",
        "filesystem",
        "telemetry",
        "APScheduler",
        "resource catalog",
    ):
        assert token in matrix_text
