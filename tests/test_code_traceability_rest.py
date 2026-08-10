"""REST parity and architectural boundary tests for Code Traceability."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import SecretStr
import pytest

from okto_pulse.community.api import code_traceability as api
from okto_pulse.core.domain.code_traceability import (
    CodeInvestigationSubmissionLimitExceeded,
    CodeInvestigationUnavailable,
    CodeTraceabilityPageCursor,
    CodeTraceabilityRemediation,
)
from okto_pulse.core.ports.code_traceability import (
    CodeTraceabilityRevisionConflict,
)


def test_transport_bodies_exclude_every_path_owned_identifier() -> None:
    expectations = {
        api.StartCodeInvestigationBody: {"board_id"},
        api.CodeInvestigationReceiptBody: {"board_id", "request_id"},
        api.CodeEvidenceBody: {"board_id"},
        api.CodeEvidenceSupersessionBody: {
            "board_id",
            "supersedes_evidence_id",
        },
        api.CodeEvidenceRevokeBody: {"board_id", "evidence_id"},
        api.CodeEvidenceSpecLinkBody: {"board_id", "spec_id"},
        api.CodeEvidenceDispositionBody: {
            "board_id",
            "spec_id",
            "evidence_id",
        },
        api.SpecCodeEvidenceRebasePreviewBody: {"board_id", "spec_id"},
        api.SpecCodeEvidenceRebaseApplyBody: {"board_id", "spec_id"},
        api.ImplementationTargetCreateBody: {"board_id", "card_id"},
        api.ImplementationTargetUpdateBody: {
            "board_id",
            "card_id",
            "target_id",
        },
        api.ImplementationTargetResolutionBody: {
            "board_id",
            "card_id",
            "target_id",
        },
        api.ImplementationTargetExecutionBody: {
            "board_id",
            "card_id",
            "target_id",
        },
        api.TargetOverlapAcknowledgementBody: {"board_id", "card_id"},
        api.CodeTraceabilityWaiverBody: {"board_id"},
    }
    for body, forbidden in expectations.items():
        assert forbidden.isdisjoint(body.model_fields)
        assert body.model_config["extra"] == "forbid"


def test_rest_error_envelope_matches_typed_core_contract() -> None:
    error = CodeInvestigationUnavailable(
        details={"reason": "agent_reported_unavailable"},
        remediation=(
            CodeTraceabilityRemediation(
                "rerun_external_agent_check",
                "okto_pulse_start_code_investigation",
            ),
        ),
    )
    projected = api._http_error(error)
    assert projected.status_code == 409
    assert projected.detail == error.to_error_dict()

    persistence_error = CodeTraceabilityRevisionConflict(
        details={"target_id": "target-1"}
    )
    projected_persistence = api._http_error(persistence_error)
    assert projected_persistence.status_code == 409
    assert projected_persistence.detail == persistence_error.to_error_dict()

    quota_error = CodeInvestigationSubmissionLimitExceeded(
        details={"reason": "open_request_limit"}
    )
    projected_quota = api._http_error(quota_error)
    assert projected_quota.status_code == 409
    assert projected_quota.detail == quota_error.to_error_dict()


def test_cursor_is_signed_and_bound_to_board_and_filters(monkeypatch) -> None:
    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: SimpleNamespace(
            guideline_policy_cursor_signing_key=SecretStr("k" * 64)
        ),
    )
    cursor = CodeTraceabilityPageCursor(
        created_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
        item_id="evidence-1",
    )
    binding = api._cursor_binding(
        "code_evidence",
        "board-1",
        parent_type="spec",
    )
    encoded = api._encode_cursor(cursor, binding=binding)
    assert encoded is not None
    assert api._decode_cursor(encoded, binding=binding) == cursor

    replacement = "A" if encoded[-1] != "A" else "B"
    with pytest.raises(HTTPException) as tampered:
        api._decode_cursor(encoded[:-1] + replacement, binding=binding)
    assert tampered.value.status_code == 400

    with pytest.raises(HTTPException) as cross_scope:
        api._decode_cursor(
            encoded,
            binding=api._cursor_binding(
                "code_evidence",
                "board-2",
                parent_type="spec",
            ),
        )
    assert cross_scope.value.status_code == 400


def test_routes_are_board_scoped_and_overlap_ack_is_card_scoped() -> None:
    paths = {route.path for route in api.router.routes}
    assert paths
    assert all(path.startswith("/boards/{board_id}/") for path in paths)
    assert (
        "/boards/{board_id}/cards/{card_id}/implementation-overlaps/acknowledgements"
        in paths
    )
    assert "/boards/{board_id}/code-evidence/{evidence_id}/revoke" in paths
    assert (
        "/boards/{board_id}/specs/{spec_id}/code-evidence/rebase/preview" in paths
    )
    assert "/boards/{board_id}/specs/{spec_id}/code-evidence/rebase" in paths
    assert "/boards/{board_id}/implementation-overlap-acknowledgements" not in paths


def test_code_traceability_community_modules_have_no_source_acquisition() -> None:
    root = Path(__file__).resolve().parents[1]
    relative_paths = (
        "src/okto_pulse/community/adapters/sqlalchemy_code_traceability.py",
        "src/okto_pulse/community/adapters/sqlalchemy_code_traceability_event_effects.py",
        "src/okto_pulse/community/adapters/relational_application.py",
        "src/okto_pulse/community/api/code_traceability.py",
        "src/okto_pulse/community/commands/code_traceability_diagnostics.py",
    )
    forbidden_import_roots = {
        "dulwich",
        "git",
        "gitpython",
        "httpx",
        "pathlib",
        "pygit2",
        "subprocess",
    }
    forbidden_calls = {
        "clone",
        "glob",
        "open",
        "popen",
        "read_text",
        "read_bytes",
        "rglob",
        "system",
        "walk",
    }
    forbidden_identifiers = {
        "ContentIngestionResolver",
        "GitRepository",
        "RepositoryReader",
        "SourceProvider",
        "WorkspaceReader",
    }

    for relative_path in relative_paths:
        source = (root / relative_path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(
                    alias.name.split(".", 1)[0].lower()
                    not in forbidden_import_roots
                    for alias in node.names
                ), relative_path
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert (
                    node.module.split(".", 1)[0].lower()
                    not in forbidden_import_roots
                ), relative_path
            elif isinstance(node, ast.Call):
                called = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else ""
                )
                assert called.lower() not in forbidden_calls, (
                    relative_path,
                    called,
                )
            elif isinstance(node, ast.Name):
                assert node.id not in forbidden_identifiers, (
                    relative_path,
                    node.id,
                )

    cli_path = root / "src/okto_pulse/community/cli.py"
    cli_tree = ast.parse(cli_path.read_text(encoding="utf-8"), filename=str(cli_path))
    traceability_functions = [
        node
        for node in cli_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {
            "_emit_code_traceability_diagnostics",
            "cmd_code_traceability",
        }
    ]
    assert {node.name for node in traceability_functions} == {
        "_emit_code_traceability_diagnostics",
        "cmd_code_traceability",
    }
    for function in traceability_functions:
        for node in ast.walk(function):
            if isinstance(node, ast.Import):
                assert all(
                    alias.name.split(".", 1)[0].lower()
                    not in forbidden_import_roots
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert (
                    node.module.split(".", 1)[0].lower()
                    not in forbidden_import_roots
                )
            elif isinstance(node, ast.Name):
                assert node.id not in forbidden_identifiers
