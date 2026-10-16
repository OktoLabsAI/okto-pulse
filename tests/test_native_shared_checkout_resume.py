"""A successor combines persisted resume context with an accessible checkout.

Filesystem/Git access belongs to the external actor represented by the test;
Pulse never certifies access or transfers the original receipt's ownership.
"""
import subprocess
from types import SimpleNamespace

import pytest
from sqlalchemy import update

import test_delivery_evidence_integration as delivery
from test_delivery_progress import command as progress_command
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import CommunityDeliveryEvidenceStore
from okto_pulse.community.adapters.sqlalchemy_models import Card, ImplementationTargetExecutionRecordRow
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.application.use_cases.delivery_evidence import GetDeliveryEvidenceUseCase
from okto_pulse.core.models.delivery_evidence import DeliveryEvidenceReadQuery

ledger = delivery.ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
async def test_successor_resumes_shared_checkout_from_durable_context_without_full_history(ledger, tmp_path, monkeypatch):
    checkout = tmp_path / "shared-checkout"
    checkout.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(checkout),
            "-c", "user.name=Resume Test", "-c", "user.email=resume-test@example.invalid",
            "-c", "commit.gpgsign=false", *args], text=True, stderr=subprocess.PIPE).strip()

    git("init", "-b", "main")
    source = checkout / "src" / "file.py"
    source.parent.mkdir()
    source.write_text("def normalize(value):\n    return value\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "Shared baseline")
    baseline = git("rev-parse", "HEAD")
    source.write_text("def normalize(value):\n    return value.strip()\n", encoding="utf-8")

    session, store, _ = ledger
    proof = await delivery.record(store, delivery.command())
    await session.execute(update(Card).where(Card.id == "task").values(status="in_progress"))
    checkpoint = progress_command(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, card_id="task",
        idempotency_key="shared-work", justification="src/file.py now strips surrounding whitespace",
        progress={"material_change": "targets", "target_ids": ["target"],
            "source_state": {"source_ref": "source-main", "declared_revision": baseline,
                "workspace_state": "dirty", "recoverability": "external_workspace"},
            "remaining": "Add case normalization and verify the acceptance criteria"})
    saved = await store.record_card(checkpoint, actor_id="agent-1", actor_kind="agent")
    await store.record_card(progress_command(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID,
        card_id="task", idempotency_key="handoff-note", justification="Ready for successor",
        progress={"material_change": "none", "source_state": {"workspace_state": "unknown",
            "recoverability": "unknown"}, "remaining": "Continue the recorded work"}),
        actor_id="agent-1", actor_kind="agent")
    await session.commit()
    engine = session.bind
    await session.close()

    async with build_community_session_factory(engine)() as successor:
        reader = CommunityDeliveryEvidenceStore(successor)

        async def no_whole_spec(*args, **kwargs):
            raise AssertionError("Resume must not fetch a whole-Spec rollup")

        monkeypatch.setattr(reader, "load_rollup_snapshot", no_whole_spec)
        actor = ActorContext(actor_id="successor", actor_kind="agent", source="mcp",
            board_id=delivery.BOARD_ID, permissions=["code_traceability.evidence.read"])
        result = await GetDeliveryEvidenceUseCase().execute(DeliveryEvidenceReadQuery(
            board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, card_id="task", view="resume"),
            actor=actor, uow=SimpleNamespace(services=SimpleNamespace(delivery_evidence=reader)))
        assert result["latest_checkpoint"]["summary"] == "Ready for successor"
        durable = next(row for row in result["progress"]["items"] if row["id"] == saved["id"])
        assert durable["actor_id"] == "agent-1"
        assert durable["remaining"] == checkpoint.progress.remaining
        target = next(row for row in result["targets"]["items"] if row["id"] in durable["target_ids"])
        assert target["source_ref"] == durable["source_state"]["source_ref"]
        assert git("rev-parse", "HEAD") == durable["source_state"]["declared_revision"]
        # The external successor has the same checkout; the response itself
        # supplies the path, source/base declaration and outstanding work.
        assert "return value.strip()" in (checkout / target["relative_path"]).read_text(encoding="utf-8")
        assert result["pending_work"]["resolution_inferred"] is False
        assert not result["pending_work"]["history_truncated"]
        assert not result["recovery"]["verified"]
        assert result["recovery"]["workspace_access"] == "unknown"
        assert not result["recovery"]["receipt_ownership_transferred"]
        prior = next(row for row in result["implementation_proofs"]["items"] if row["record_id"] == proof["id"])
        assert prior["actor_id"] == "agent-1" and prior["current_obligation_refs"] == []
        assert (await successor.get(ImplementationTargetExecutionRecordRow, "execution")).submitted_by == "agent-1"
        assert not any(row["implementation_satisfied"] for row in result["obligations"]["items"])
        assert result["verification_plan"]["items"]
        assert not result["actions"]["record_progress"]  # Read grant does not confer write authority.
