"""Native immutable proof history with scoped Target/source currentness."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import subprocess

import pytest
from sqlalchemy import select

import test_delivery_evidence_integration as delivery
from test_code_traceability_persistence import _attestation_bundle
from okto_pulse.community.adapters.sqlalchemy_code_traceability import (
    CommunitySqlAlchemyCodeInvestigationStore, CommunitySqlAlchemyCodeTraceabilityStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    CardDeliveryEvidenceRecordRow as Record,
    ImplementationTargetRow as Target,
    ImplementationTargetExecutionRecordRow as Execution,
)
from okto_pulse.core.domain.code_traceability import code_investigation_observation_sha256_v2
from okto_pulse.core.domain.delivery_evidence import CardDeliveryScope, implementation_binding_proof_issue

ledger = delivery.ledger


async def advance_source(session, *, independent_target=False, disposition="touched", revision="b" * 40):
    """Accepted origin facts are fixture input; persist using native append/CAS.

    This isolates the consumer's currentness predicate, not agent attestation
    authentication. Observation digest and immutable head lineage are coherent.
    """
    now = datetime.now(timezone.utc)
    opened, consumed, receipt, head, workspace = _attestation_bundle(now, subject_id="task")
    request_changes = dict(id="request-2", board_id=delivery.BOARD_ID,
        expected_head_generation=1, expected_predecessor_receipt_id="receipt-1",
        challenge_token_hash="e" * 64, idempotency_key="request-2")
    opened = replace(opened, **request_changes)
    consumed = replace(consumed, **request_changes)
    workspace = replace(workspace, declared_revision=revision, workspace_state_id="workspace-2")
    digest = code_investigation_observation_sha256_v2(source_ref=receipt.source_ref,
        selector_scope_digest=receipt.selector_scope_digest, delivery_context=receipt.delivery_context,
        outcome=receipt.contextual_outcome, capabilities=receipt.capabilities,
        source_identity_digest=receipt.source_identity_digest, declared_revision=workspace.declared_revision,
        workspace_state=workspace, omission_manifest=())
    receipt = replace(receipt, id="receipt-2", request_id=opened.id, board_id=delivery.BOARD_ID,
        generation=2, predecessor_receipt_id="receipt-1", declared_revision=workspace.declared_revision,
        workspace_state=workspace, observation_sha256=digest, idempotency_key="receipt-2")
    head = replace(head, board_id=delivery.BOARD_ID, generation=2, revision=2,
                   current_receipt_id=receipt.id, latest_receipt_id=receipt.id)
    origin = CommunitySqlAlchemyCodeInvestigationStore(session)
    await origin.create_request(opened)
    await origin.consume_request_append_receipt_and_advance_head(
        request=consumed, receipt=receipt, head=head, expected_head_revision=1)
    target_id = "target"
    if independent_target:
        target = await session.get(Target, target_id)
        values = {column.name: deepcopy(getattr(target, column.name)) for column in Target.__table__.columns}
        target_id = "independent-target"
        values.update(id=target_id, relative_path_hint="src/independent.py")
        session.add(Target(**values))
        await session.flush()
    session.add(Execution(id="execution-2", board_id=delivery.BOARD_ID, card_id="task",
        target_id=target_id, target_revision=1, result_investigation_receipt_id=receipt.id,
        source_ref=receipt.source_ref, disposition=disposition, result_declared_revision=workspace.declared_revision,
        result_workspace_state_id=workspace.workspace_state_id,
        actual_relative_path="src/independent.py" if independent_target else "src/file.py",
        justification="New observation", submitted_by="agent-1", received_at=now,
        payload_sha256="f" * 64, idempotency_key="execution-2"))
    await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("change", ["target_revision", "source_execution", "independent_target"])
async def test_currentness_is_scoped_and_never_rewrites_prior_delivery(ledger, change):
    session, store, _ = ledger
    implementation = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command("test", implementation_ids=[implementation["id"]]))
    await session.commit()
    assert (await store.projection(delivery.BOARD_ID, delivery.SPEC_ID))["allowed"]
    records = list(await session.scalars(select(Record)))
    before = {row.id: deepcopy(row.payload) for row in records}
    if change == "target_revision":
        targets = CommunitySqlAlchemyCodeTraceabilityStore(session)
        target = await targets.get_target(board_id=delivery.BOARD_ID, target_id="target")
        await targets.update_target(target=replace(target, revision=2,
            last_change_reason_sha256=sha256(b"Move implementation to revised path").hexdigest(),
            relative_path_hint="src/revised.py", updated_at=datetime.now(timezone.utc)), expected_revision=1)
        await session.commit()
    else:
        await advance_source(session, independent_target=change == "independent_target")
    # Force a fresh read rather than relying on the original identity-map rows.
    await session.close()
    current = await store.projection(delivery.BOARD_ID, delivery.SPEC_ID)
    assert current["allowed"] == (change == "independent_target")
    assert {row.id: row.payload for row in await session.scalars(select(Record))} == before
    if change != "independent_target":
        with pytest.raises(ValueError, match="accepted_committed"):
            await delivery.record(store, delivery.command(idempotency_key="reuse-old-proof"))
        await session.commit()
        assert {row.id: row.payload for row in await session.scalars(select(Record))} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("disposition", ["not_touched", "superseded"])
async def test_non_delivery_disposition_is_preserved_without_inventing_credit(ledger, disposition):
    session, store, _ = ledger
    await advance_source(session, disposition=disposition)
    await session.close()
    assert (await session.get(Execution, "execution-2")).disposition == disposition
    with pytest.raises(ValueError, match="accepted_committed"):
        await delivery.record(store, delivery.command(execution_id="execution-2"))
    await session.commit()
    assert not list(await session.scalars(select(Record)))
    projection = await store.projection(delivery.BOARD_ID, delivery.SPEC_ID)
    assert not projection["allowed"]
    assert not [row for row in projection["candidates"] if row["kind"] == "implementation"]
    assert (await session.get(Execution, "execution-2")).disposition == disposition


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("revision", ["0" * 40, "b" * 40, "a" * 40], ids=["lower-hash", "higher-hash", "same-base"])
@pytest.mark.parametrize("reverse", [False, True], ids=["old-first", "new-first"])
async def test_composite_proof_never_infers_ancestry_from_hash_or_observation_order(ledger, revision, reverse):
    session, store, _ = ledger
    await advance_source(session, independent_target=True, revision=revision)
    executions = ["execution", "execution-2"]
    if reverse:
        executions.reverse()
    # Both origin receipts are accepted/current for their own Target; the second
    # observation is newer. These facts do not establish a common commit base.
    request = delivery.command(execution_id=None, obligation_refs=[], bindings=[{
        "obligation_ref": "ac:ac-about", "contribution": "complete",
        "execution_refs": [{"execution_id": identity} for identity in executions],
    }])
    if revision == "a" * 40:
        saved = await delivery.record(store, request)
        await session.commit()
        assert len(list(await session.scalars(select(Record)))) == 1
        assert (await session.get(Record, saved["id"])).kind == "implementation"
    else:
        with pytest.raises(ValueError, match="delivery_execution_base_conflict"):
            await delivery.record(store, request)
        await session.commit()
        assert not list(await session.scalars(select(Record)))


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
@pytest.mark.parametrize("relationship", ["divergent", "revert"])
@pytest.mark.parametrize("reverse", [False, True])
async def test_actual_branch_and_revert_commits_do_not_imply_integrated_delivery(ledger, tmp_path, relationship, reverse):
    # Git is fixture mechanics only. The domain receives opaque commit identities;
    # it cannot substitute branch ancestry or restored file content for same base.
    repository = tmp_path / "commit-basis"
    repository.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repository),
            "-c", "user.name=Native Test", "-c", "user.email=native-test@example.invalid",
            "-c", "commit.gpgsign=false", *args], text=True, stderr=subprocess.PIPE).strip()

    git("init", "-b", "main")
    source = repository / "source.txt"
    source.write_text("baseline\n", encoding="utf-8")
    git("add", "source.txt")
    git("commit", "-m", "baseline")
    baseline = git("rev-parse", "HEAD")
    source.write_text("feature\n", encoding="utf-8")
    git("commit", "-am", "feature")
    feature = git("rev-parse", "HEAD")
    if relationship == "divergent":
        git("checkout", "-b", "other", baseline)
        source.write_text("independent\n", encoding="utf-8")
        git("commit", "-am", "independent")
        alternative = git("rev-parse", "HEAD")
        assert git("merge-base", feature, alternative) == baseline
    else:
        git("revert", "--no-edit", feature)
        alternative = git("rev-parse", "HEAD")
        assert source.read_text(encoding="utf-8") == "baseline\n"
        assert alternative != baseline and git("merge-base", feature, alternative) == feature
    session, store, _ = ledger
    saved = await delivery.record(store, delivery.command())
    snapshot = await store.load_card_snapshot(CardDeliveryScope(delivery.BOARD_ID, "task", delivery.SPEC_ID, 1))
    fact = next(item for item in snapshot.implementations if item.id == saved["id"])
    binding = fact.bindings[0]
    revisions = [feature, alternative]
    if reverse:
        revisions.reverse()
    proofs = tuple(replace(fact.executions[0], execution_id=f"proof-{index}",
        target_id=f"target-{index}", result_revision=revision) for index, revision in enumerate(revisions))
    contribution = next(item for item in fact.contributions if item.binding == binding)
    candidate = replace(fact, executions=proofs, contributions=(replace(contribution,
        execution_ids=tuple(item.execution_id for item in proofs)),))
    assert implementation_binding_proof_issue(candidate, binding) == "delivery_execution_base_conflict"
