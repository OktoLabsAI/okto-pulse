"""Removed legacy classification has no REST route or relational storage."""
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base, GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES,
)
from okto_pulse.community.api.code_traceability import router
from okto_pulse.community.api.code_traceability import CodeInvestigationReceiptBody
from pydantic import ValidationError
import pytest
from datetime import datetime, timezone


def test_classification_has_no_route_table_or_revision_input():
    assert all('legacy-classifications' not in route.path for route in router.routes)
    removed = {'code_evidence_classification_events', 'code_evidence_classification_heads'}
    assert not removed.intersection(Base.metadata.tables)
    assert not removed.intersection(GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES)


@pytest.mark.parametrize('version,outcome', [(None, 'accessible'), (1, 'accessible'), (2, 'accessible')])
def test_rest_receipt_body_refuses_pre_context_contract(version, outcome):
    payload = dict(
        outcome=outcome, challenge_token='token', capabilities=[],
        tooling=dict(tool_id='agent', tool_version='1', method_id='check'),
        observed_at=datetime(2026, 10, 1, tzinfo=timezone.utc), idempotency_key='receipt',
    )
    if version is not None:
        payload['contract_version'] = version
    with pytest.raises(ValidationError):
        CodeInvestigationReceiptBody.model_validate(payload)


def test_native_storage_requires_context_without_legacy_defaults():
    evidence = Base.metadata.tables['code_evidence']
    receipt = Base.metadata.tables['code_investigation_receipts']
    assert 'outcome' not in receipt.c
    for name in ('source_role', 'relevance_summary', 'scope_relation', 'source_origin',
                 'baseline_presence', 'baseline_workspace_state_id', 'context_contract_version'):
        assert not evidence.c[name].nullable
        assert evidence.c[name].default is None
        assert evidence.c[name].server_default is None
    for name in ('delivery_context', 'contextual_outcome', 'context_contract_version'):
        assert not receipt.c[name].nullable


def test_postgresql_native_evidence_ddl_has_closed_roles_and_context_fields():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable
    table = Base.metadata.tables['code_evidence']
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert ddl == str(CreateTable(table).compile(dialect=postgresql.dialect()))
    for role in ('current_implementation', 'existing_scaffold',
                 'existing_constraint', 'reference_pattern'):
        assert role in ddl
    assert 'uncategorized_legacy' not in ddl
    for field in ('source_role', 'relevance_summary', 'scope_relation',
                  'source_origin', 'interpretation_limit', 'baseline_presence',
                  'baseline_workspace_state_id', 'baseline_provenance_note',
                  'context_contract_version'):
        assert field in ddl
