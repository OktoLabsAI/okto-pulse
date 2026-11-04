"""Typed human declarations in the existing immutable Card ledger.

Physical kind=progress preserves the strict storage format. The payload variant
is human_submission, not delivery-progress/v2: it makes no material-code claim.
No table rebuild, history rewrite or additional receipt journal is needed.
"""

from datetime import datetime, timezone
import uuid

from sqlalchemy import select, update

from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog, Board, CardDeliveryEvidenceRecordRow as Record, DomainEventRow,
)
from okto_pulse.core.domain.delivery_evidence import CardDeliveryScope, implementation_binding_proof_current
from okto_pulse.core.domain.delivery_assessment import aggregate_delivery_metrics
from okto_pulse.core.domain.effective_delivery_coverage import EvaluatedDeliverySnapshot
from okto_pulse.core.domain.delivery_selection import current_delivery_selection
from okto_pulse.core.models.delivery_selection import DeliverySelectionInput
from okto_pulse.core.models.human_task_submission import HumanSubmissionConflict, HumanSubmissionReceipt
from okto_pulse.core.models.schemas import CardMove
from okto_pulse.core.services.task_delivery_summary import present_task_delivery_summary


def is_human_submission(payload):
    return isinstance(payload, dict) and payload.get("kind") == "human_submission"


def _conflict(reason, message, *, technical=False):
    return HumanSubmissionConflict(reason, message,
        next_action="resolve_delivery_conditions" if technical else "refresh_and_retry",
        next_owner="technical_owner" if technical else "user")


class CommunityHumanTaskSubmissions:
    def __init__(self, store):
        self.store = store
        self.session = store.session

    async def record(self, command, *, actor_id, actor_kind, report_submitter=None):
        if not actor_id or actor_kind not in {"human", "user"}:
            raise ValueError("human_submission_human_authorization_required")
        # Serialize before replay, but do not compare the *new* edition/CAS until
        # after exact replay. An authorized historical retry returns its receipt.
        fence = await self.session.execute(update(Board).where(Board.id == command.board_id)
            .values(id=Board.id).execution_options(synchronize_session=False))
        if fence.rowcount != 1:
            raise ValueError("delivery_board_not_found")
        self.store._locked = True
        card = await self.store._card(command.board_id, command.card_id)
        spec = await self.store._spec(command.board_id, command.spec_id)
        if card.archived or card.spec_id != spec.id:
            raise _conflict("scope_changed", "The task has changed. Refresh it before submitting.")
        digest = command.intent_digest()
        head = await self.session.scalar(select(Record).where(
            Record.board_id == command.board_id, Record.card_id == command.card_id,
            Record.actor_id == actor_id, Record.idempotency_key == command.idempotency_key))
        if head is not None:
            if (head.payload_sha256 != digest or head.actor_kind != actor_kind
                    or head.spec_id != command.spec_id or not is_human_submission(head.payload)):
                raise _conflict("idempotency_conflict", "This submission was already used for different content.")
            receipt = head.payload["human_submission"]["receipt"]
            if receipt["intent_sha256"] != digest or receipt["actor_id"] != actor_id:
                raise _conflict("receipt_unavailable", "The previous submission could not be confirmed.")
            return {**head.payload["human_submission"]["result"], "replayed": True}
        if card.policy_version != command.expected_card_version or spec.edition != command.expected_spec_edition:
            raise _conflict("version_changed", "The task or Spec has changed. Refresh and review your text.")
        if card.status != "in_progress" or card.card_type not in {"normal", "bug", "test"}:
            raise _conflict("lifecycle_changed", "Submission is available only for tasks in progress.")
        if command.supersedes_submission_id:
            prior = await self.session.scalar(select(Record).where(
                Record.id == command.supersedes_submission_id, Record.board_id == command.board_id,
                Record.card_id == command.card_id, Record.spec_id == command.spec_id, Record.actor_id == actor_id))
            if prior is None or not is_human_submission(prior.payload):
                raise _conflict("supersession_unavailable", "The record to correct is not available to you.")
        scope = CardDeliveryScope(command.board_id, command.card_id, command.spec_id, spec.edition)
        before_version = card.policy_version
        submission_id, receipt_id = "human_submission_" + uuid.uuid4().hex, "human_receipt_" + uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        async with self.session.begin_nested():
            report_link = None
            if command.action == "delivery_report":
                if report_submitter is None:
                    raise ValueError("human_submission_report_writer_unavailable")
                report = await self.prepare_report(command, card=card, scope=scope)
                await report_submitter(report)
                await self.session.flush()
                card = await self.store._card(command.board_id, command.card_id)
                if not card.conclusions or card.status != command.requested_status:
                    raise ValueError("human_submission_report_not_persisted")
                report_link = {"index": len(card.conclusions) - 1,
                    "manifest_sha256": card.conclusions[-1]["delivery_manifest"]["sha256"]}
            receipt = HumanSubmissionReceipt(id=receipt_id, submission_id=submission_id,
                board_id=command.board_id, card_id=command.card_id, spec_id=command.spec_id,
                spec_edition=spec.edition, actor_id=actor_id, actor_kind=actor_kind, received_at=now,
                action=command.action, before_card_version=before_version, after_card_version=card.policy_version,
                content_sha256=command.content_digest(), intent_sha256=digest,
                supersedes_submission_id=command.supersedes_submission_id)
            result = {"contract_version": "human-task-submission-result/v1",
                "submission_id": submission_id, "receipt_id": receipt_id,
                "card_version": card.policy_version, "spec_edition": spec.edition,
                "status": card.status.value if hasattr(card.status, "value") else card.status, "replayed": False,
                "summary": present_task_delivery_summary(None, board_id=command.board_id,
                    spec_id=command.spec_id, card_id=command.card_id, spec_edition=spec.edition,
                    card_version=card.policy_version, status=getattr(card.status, "value", card.status), freshness="updating",
                    actor_kind=actor_kind, allowed_operations=frozenset()).model_dump(mode="json")}
            self.session.add(Record(id=submission_id, board_id=command.board_id, card_id=command.card_id,
                spec_id=command.spec_id, spec_edition=spec.edition, kind="progress", actor_id=actor_id,
                actor_kind=actor_kind, idempotency_key=command.idempotency_key, payload_sha256=digest,
                payload={"kind": "human_submission", "card_id": command.card_id, "bindings": [],
                    "justification": command.content, "human_submission": {
                        "contract_version": command.contract_version, "action": command.action,
                        "remaining": command.remaining, "receipt": receipt.model_dump(mode="json"),
                        "report_link": report_link, "result": result}}, created_at=now))
            await self.session.flush()
            details = {"submission_id": submission_id, "receipt_id": receipt_id,
                "card_id": command.card_id, "spec_id": command.spec_id, "action": command.action,
                "content_sha256": receipt.content_sha256}
            self.session.add(ActivityLog(board_id=command.board_id, card_id=command.card_id,
                actor_id=actor_id, actor_type="user", actor_name=actor_id,
                action="task.human_submission_accepted", details=details))
            self.session.add(DomainEventRow(board_id=command.board_id, actor_id=actor_id,
                actor_type="user", event_type="task.human_submission_accepted", payload_json=details))
            await self.session.flush()
        return result

    async def prepare_report(self, command, *, card, scope):
        """Select existing current facts. Missing/ambiguous facts never become UI inputs."""
        records = await self.store._card_records(scope)
        snapshot = await self.store.load_card_snapshot(scope, records=records)
        if not snapshot.complete:
            raise _conflict("proof_unavailable", "Delivery verification is not available yet.", technical=True)
        evaluation = EvaluatedDeliverySnapshot(snapshot)
        assessment = (evaluation.scenarios(card_id=card.id, scenario_ids=tuple(card.test_scenario_ids or ()))
            if card.card_type == "test" else evaluation.obligations(card_id=card.id, card_type=card.card_type))
        metrics = aggregate_delivery_metrics(assessment)
        phase = metrics.verification if card.card_type == "test" else metrics.implementation
        responsibilities = getattr(snapshot.effective_context, "task_requirements", ())
        excepted = any(item.card_id == card.id and resolution.decision in {"optional", "waived"}
                       for item, resolution in responsibilities)
        if not excepted and (phase.status == "unknown" or phase.unresolved != 0):
            raise _conflict("proof_required", "The technical owner must complete the evidence for this task before submission.", technical=True)
        selected_ids = {fact.id for fact in snapshot.implementations
            if fact.bindings and all(implementation_binding_proof_current(fact, binding) for binding in fact.bindings)}
        selected_ids.update(fact.id for fact in snapshot.tests if fact.current_verified_run)
        revoked = {row.payload.get("record_id") for row in records if row.kind == "revoke"}
        # Existing technical impact declarations remain explicit and qualified
        # through resolve_selection_impact. Human prose is never mapped to none.
        impact_rows = [row for row in records if row.kind == "progress" and row.id not in revoked
            and not is_human_submission(row.payload) and (row.payload.get("progress") or {}).get("impact_delta")]
        selected_ids.update(row.id for row in impact_rows)
        if len(selected_ids) > 200:
            raise _conflict("proof_selection_ambiguous", "The technical owner must review the delivery evidence.", technical=True)
        assessment = None
        obligations = await self.store._snapshot_obligations(await self.store._spec(scope.board_id, scope.spec_id), card)
        hashes = {row.id: self.store._selection_record_hash(row) for row in records}
        for prior in reversed(card.conclusions or []):
            manifest = prior.get("delivery_manifest") if isinstance(prior, dict) else None
            if not isinstance(manifest, dict) or manifest.get("card_version") != card.policy_version:
                continue
            try:
                current_delivery_selection(card, scope, obligations=obligations,
                    record_hashes=hashes, prospective_report=prior)
            except ValueError:
                continue
            assessment = {name: prior.get(name) for name in (
                "completeness", "completeness_justification", "drift", "drift_justification")}
            break
        # A validation is usable only with a current, integrity-checked report
        # basis. Never revive an older pass after a newer failed assessment.
        latest = next((row for row in reversed(card.validations or []) if isinstance(row, dict)
                       and row.get("expected_subject_version") == card.policy_version), None)
        if latest is not None:
            if latest.get("outcome") != "success":
                assessment = None
            elif assessment is not None:
                assessment = {"completeness": latest.get("estimated_completeness"),
                    "completeness_justification": latest.get("completeness_justification"),
                    "drift": latest.get("estimated_drift"), "drift_justification": latest.get("drift_justification")}
        if assessment is None or any(value is None for value in assessment.values()):
            raise _conflict("assessment_required", "The technical owner must record the delivery assessment before submission.", technical=True)
        selection = DeliverySelectionInput(expected_card_version=card.policy_version,
            expected_spec_edition=scope.spec_edition, expected_delivery_revision=len(records),
            record_ids=sorted(selected_ids), reuse_impact=bool(impact_rows))
        # Resolve now for an actionable error; the native writer fences it again.
        if impact_rows:
            try:
                await self.store.resolve_selection_impact(scope, selection, expected_status="in_progress")
            except ValueError as exc:
                raise _conflict("impact_confirmation_required", "The technical owner must confirm the impact of the changes.", technical=True) from exc
        return CardMove(status=command.requested_status, conclusion=command.content,
                        delivery_selection=selection, **assessment)
