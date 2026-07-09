from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_release_docs_match_current_dockerfile_claims() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    assert "FROM python:3.12-slim AS base" in dockerfile
    assert "python:3.12-slim" in claude
    assert "python:3.14-slim" not in claude
    assert "HF_MODEL_SHA256" not in claude


def test_readme_documents_implemented_terms_and_metric_guarantees() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Terms acceptance drift is gated" in readme
    assert "Operational metric samples are bounded" in readme
    assert "backend acceptance constants" in readme


def test_af42_boundary_oracle_matches_executable_report() -> None:
    from okto_pulse.community.adapters.core_import_boundary import (
        AF42_PRIVATE_REACH_IN_BASELINE,
        CORE_IMPORT_COMMUNITY_IMPLEMENTATION,
        CORE_IMPORT_GOVERNED_REACH_IN,
        CORE_IMPORT_PUBLIC_CONTRACT,
        audit_community_core_import_boundary,
    )

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    report = audit_community_core_import_boundary(ROOT)
    classes = report["inventory_by_classification"]

    assert "<!-- AF42-BOUNDARY-ORACLE:BEGIN -->" in readme
    assert "<!-- AF42-BOUNDARY-ORACLE:END -->" in readme
    assert f"| Historical private reach-in baseline | `{AF42_PRIVATE_REACH_IN_BASELINE}` |" in readme
    assert f"| Current governed private reach-ins | `{report['occurrence_count']}` |" in readme
    assert f"| Current full Community->Core import inventory | `{report['inventory_count']}` |" in readme
    assert (
        "| Inventory classification | "
        f"`public_contract={classes[CORE_IMPORT_PUBLIC_CONTRACT]}`, "
        f"`community_owned_implementation={classes[CORE_IMPORT_COMMUNITY_IMPLEMENTATION]}`, "
        f"`governed_temporary_reach_in={classes[CORE_IMPORT_GOVERNED_REACH_IN]}` |"
    ) in readme
    assert (
        "| Boundary violations | `0` violations, `0` stale ledger entries, "
        "`0` incomplete ledger entries, `0` baseline-growth violations |"
    ) in readme

    for token in (
        "core.ports.*",
        "core.services.application_*",
        "core.models.db",
        "core.infra.database",
        "core.services.main",
        "core.mcp.server",
        "core.kg.workers.*",
    ):
        assert token in readme

    for entry in report["ledgered"]:
        documented_path = entry["file_path"].replace("src/okto_pulse/", "")
        assert documented_path in readme

    assert "105 passed" in readme
    assert "67 passed" in readme
    assert "62 passed, 5 failed" not in readme
