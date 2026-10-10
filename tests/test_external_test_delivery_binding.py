"""External execution must cover the exact implementation source and revision."""
import pytest
from sqlalchemy import select, update
from test_delivery_evidence_integration import ledger as delivery_ledger, command, record, BOARD_ID, SPEC_ID
from test_evidence_v2_adapter import SCENARIO, SCENARIO_SHA256, ACTOR_ID
from okto_pulse.community.adapters.sqlalchemy_models import CodeInvestigationReceiptRow, Spec
from okto_pulse.community.adapters.test_evidence import (
    CommunityEvidenceLedger, _persist_inline_manifest, _execute_validated_manifest_and_build_evidence_v2,
    ProductExecutionObservation,
)
from okto_pulse.community.adapters.external_test_runner import SCHEMA

ledger = delivery_ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", [None, "source", "revision", "dirty"])
async def test_exact_external_basis_required_before_delivery_credit(ledger, tmp_path, mismatch):
    session, store, _ = ledger
    impl = await record(store, command())
    receipt = (await session.scalars(select(CodeInvestigationReceiptRow))).one()
    basis = dict(source_ref="other" if mismatch == "source" else receipt.source_ref,
                 revision="b" * 40 if mismatch == "revision" else receipt.declared_revision,
                 runner_ref="registered", test_ids=["test.py::Cases.test_a"])
    if mismatch == "dirty":
        receipt.declared_dirty = True
        await session.flush()
    evidence_ledger = CommunityEvidenceLedger(evidence_root=tmp_path / "evidence")
    scope = dict(board_id=BOARD_ID, spec_id=SPEC_ID, scenario_id=SCENARIO["id"], scenario_sha256=SCENARIO_SHA256)
    manifest = dict(schema_version=SCHEMA, purpose="test_scenario_evidence", **scope, execution_basis=basis)
    raw, ref = _persist_inline_manifest(manifest, ledger=evidence_ledger)
    def observe(*_):
        return ProductExecutionObservation("external-run", "passed", (
            dict(name="assertion", expected=1, observed=1, status="passed"),), "2026-10-10T00:00:00Z")
    evidence = await _execute_validated_manifest_and_build_evidence_v2(manifest=raw, canonical_ref=ref,
        normalized_manifest=manifest, **scope, status="passed", actor_id=ACTOR_ID,
        executor=observe, ledger=evidence_ledger, environment="test", execution_basis=basis)
    await session.execute(update(Spec).values(test_scenarios=[{**SCENARIO, "status": "passed", "evidence": evidence}]))
    if mismatch:
        with pytest.raises(ValueError):
            await record(store, command("test", implementation_ids=[impl["id"]]))
    else:
        await record(store, command("test", implementation_ids=[impl["id"]]))
        assert (await store.projection(BOARD_ID, SPEC_ID))["allowed"]
