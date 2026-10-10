"""Native Spec-ledger persistence for authenticated Decision observations."""

from dataclasses import replace
from datetime import datetime, timezone
import uuid

from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, SpecHistory, DeliveryEvidenceRecordRow as Record
from okto_pulse.core.domain.decision_review import (
    DecisionReviewFact, decision_authors, decision_inspection_basis, resolve_decision_inspection,
)
from okto_pulse.core.domain.delivery_evidence import DeliveryScope, evaluate_delivery_coverage
from okto_pulse.core.domain.delivery_inventory import delivery_digest
from okto_pulse.core.models.decision_review import DecisionReviewCommand
from okto_pulse.core.services.reviewer_separation import evaluate_decision_reviewer_separation, resolve_reviewer_separation_mode


class CommunityDecisionReviews:
    def __init__(self, delivery):
        self.delivery = delivery
        self.session = delivery.session

    async def records(self, scope):
        records = list((await self.session.scalars(select(Record).where(
            Record.board_id == scope.board_id, Record.spec_id == scope.spec_id,
        ).order_by(Record.created_at, Record.id).limit(10001))).all())
        if len(records) > 10000:
            raise ValueError("decision_review_history_unavailable")
        return records

    async def material(self, spec, snapshot, *, actor_id=None):
        board = await self.session.get(Board, spec.board_id, populate_existing=True)
        mode, _ = resolve_reviewer_separation_mode(board)
        records = await self.records(snapshot.scope)
        histories = list((await self.session.scalars(select(SpecHistory).where(
            SpecHistory.spec_id == spec.id).order_by(SpecHistory.version, SpecHistory.created_at).limit(10001))).all())
        if len(histories) > 10000:
            raise ValueError("decision_review_authorship_unavailable")
        cards = list((await self.session.scalars(select(Card).where(Card.board_id == spec.board_id,
            Card.spec_id == spec.id, Card.archived.is_(False)).limit(5001))).all())
        if len(cards) > 5000:
            raise ValueError("decision_review_scope_unavailable")
        revoked = {r.payload.get("record_id") for r in records if r.kind == "revoke"}
        bases, facts, separations = {}, [], {}
        by_decision = {d["id"]: d for d in spec.decisions}
        authors = {}
        scopes = {}
        executors = {}
        for row in snapshot.effective_context.inventory.rows:
            plan = row.decision_plan
            if row.family != "decision" or not plan or not plan.complete or not plan.verification.inspection:
                continue
            decision_id = plan.decision_id
            bases[decision_id] = decision_inspection_basis(spec=spec, snapshot=snapshot, row=row, separation_mode=mode)
            authors[decision_id] = decision_authors(by_decision[decision_id], histories)
            full = any(ref.kind == "spec" for ref in plan.verification.inspection.scope_refs)
            refs = {ref.id for ref in plan.verification.inspection.scope_refs if ref.kind == "obligation"}
            card_ids = {c.card_id for item in snapshot.effective_context.inventory.rows
                        if item.binding.obligation_ref in refs for c in item.contributions}
            card_ids.update(f.card_id for f in snapshot.tests if any(b.obligation_ref in refs for b in f.bindings))
            scopes[decision_id] = cards if full else [c for c in cards if c.id in card_ids]
            executors[decision_id] = tuple({f.actor_id for f in (*snapshot.implementations, *snapshot.tests)
                if full or any(b.obligation_ref in refs for b in f.bindings)})
            if actor_id:
                separations[decision_id] = evaluate_decision_reviewer_separation(board=board, reviewer_id=actor_id,
                    author_ids=authors[decision_id][0], authors_known=authors[decision_id][1], cards=scopes[decision_id],
                    executor_ids=executors[decision_id]).to_dict()
        history = []
        for record in records:
            if record.kind != "decision_review":
                continue
            submitted = DecisionReviewCommand.model_validate(record.payload.get("submission"))
            if ((submitted.board_id, submitted.spec_id, submitted.expected_edition) !=
                (spec.board_id, spec.id, record.edition) or delivery_digest(submitted.model_dump(mode="json")) != record.payload_sha256):
                raise ValueError("decision_review_receipt_invalid")
            for entry in submitted.entries:
                if record.edition != spec.edition:
                    continue
                authority = False
                if entry.decision_id in bases:
                    known_authors, known = authors[entry.decision_id]
                    authority = evaluate_decision_reviewer_separation(board=board, reviewer_id=record.actor_id,
                        author_ids=known_authors, authors_known=known, cards=scopes[entry.decision_id],
                        executor_ids=executors[entry.decision_id]).allowed
                facts.append(DecisionReviewFact(record.id, entry.decision_id, entry.expected_scope_sha256,
                    entry.result, tuple(entry.reconciles), record.id in revoked, authority))
            history.append({"id": record.id, "edition": record.edition, "actor_id": record.actor_id, "actor_kind": record.actor_kind,
                "created_at": record.created_at.isoformat(), "revoked": record.id in revoked,
                "observations": record.payload["observations"]})
        states = tuple(resolve_decision_inspection(identity, basis["scope_sha256"], facts)
                       for identity, basis in bases.items())
        revision = delivery_digest([(r.id, r.payload_sha256) for r in records if r.edition == spec.edition])
        return bases, states, history, revision, separations

    async def attach(self, spec, snapshot):
        _, states, _, _, _ = await self.material(spec, snapshot)
        return replace(snapshot, effective_context=replace(snapshot.effective_context, decision_inspections=states))

    async def read(self, query, *, actor_id):
        spec = await self.delivery._spec(query.board_id, query.spec_id)
        snapshot, _ = await self.delivery.load_rollup_snapshot(query.board_id, query.spec_id, include_reviews=False)
        bases, states, history, revision, separations = await self.material(spec, snapshot, actor_id=actor_id)
        snapshot = replace(snapshot, effective_context=replace(snapshot.effective_context, decision_inspections=states))
        evaluation = evaluate_delivery_coverage(snapshot)
        return {"board_id": spec.board_id, "spec_id": spec.id, "edition": spec.edition, "version": spec.version,
            "review_revision": revision, "complete": snapshot.complete,
            "decisions": [{"decision_id": r.obligation.binding.obligation_ref.removeprefix("decision:"),
                "status": r.decision_verification_status, "record_ids": list(r.decision_review_ids),
                "basis": bases.get(r.obligation.binding.obligation_ref.removeprefix("decision:")),
                "separation": separations.get(r.obligation.binding.obligation_ref.removeprefix("decision:"))}
                for r in evaluation.rows if getattr(r, "decision_verification_status", None) is not None],
            "history": history, "observation_authority": "authenticated_reviewer_declaration"}

    async def record(self, command, *, actor_id, actor_kind):
        if not actor_id or actor_kind not in {"human", "user", "agent"}:
            raise ValueError("decision_review_authenticated_actor_required")
        scope = DeliveryScope(command.board_id, command.spec_id, command.expected_edition)
        await self.delivery.lock_scope(scope)
        digest = delivery_digest(command.model_dump(mode="json"))
        replay = (await self.session.scalars(select(Record).where(Record.board_id == command.board_id,
            Record.spec_id == command.spec_id, Record.actor_id == actor_id,
            Record.idempotency_key == command.idempotency_key))).one_or_none()
        if replay is not None:
            if replay.kind != "decision_review" or replay.payload_sha256 != digest or replay.actor_kind != actor_kind:
                raise ValueError("decision_review_idempotency_conflict")
            return {"id": replay.id, "replayed": True}
        spec = await self.delivery._spec(command.board_id, command.spec_id)
        if spec.version != command.expected_version:
            raise ValueError("decision_review_version_conflict")
        if str(getattr(spec.status, "value", spec.status)) in {"done", "cancelled"}:
            raise ValueError("decision_review_spec_frozen")
        snapshot, _ = await self.delivery.load_rollup_snapshot(command.board_id, command.spec_id, include_reviews=False)
        bases, states, _, revision, separations = await self.material(spec, snapshot, actor_id=actor_id)
        if revision != command.expected_review_revision:
            raise ValueError("decision_review_revision_conflict")
        by_id = {state.decision_id: state for state in states}
        observations = []
        for entry in command.entries:
            basis = bases.get(entry.decision_id)
            if basis is None or basis["scope_sha256"] != entry.expected_scope_sha256:
                raise ValueError("decision_review_scope_conflict")
            if not separations[entry.decision_id]["allowed"]:
                raise ValueError("decision_review_reviewer_separation_required")
            if sorted((s.model_dump(mode="json") for s in entry.sources), key=lambda s: s["reference"]) != sorted(basis["sources"], key=lambda s: s["reference"]):
                raise ValueError("decision_review_source_unresolved")
            heads = set(by_id[entry.decision_id].record_ids)
            # Explicit reconciliation must account for all current conclusions;
            # a client cannot selectively hide one contradictory observation.
            if entry.reconciles and set(entry.reconciles) != heads:
                raise ValueError("decision_review_reconciliation_conflict")
            observations.append({**entry.model_dump(mode="json"), "expected": basis["expected"],
                "separation": separations[entry.decision_id]})
        record = Record(id="decision_review_" + uuid.uuid4().hex, board_id=command.board_id,
            spec_id=command.spec_id, edition=command.expected_edition, kind="decision_review",
            actor_id=actor_id, actor_kind=actor_kind, idempotency_key=command.idempotency_key,
            payload_sha256=digest, payload={"submission": command.model_dump(mode="json"), "observations": observations},
            created_at=datetime.now(timezone.utc))
        self.session.add(record)
        await self.session.flush()
        return {"id": record.id, "replayed": False, "observations": observations}
