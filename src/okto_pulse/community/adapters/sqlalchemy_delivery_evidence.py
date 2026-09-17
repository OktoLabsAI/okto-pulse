"""Relational delivery ledger. No source execution or graph-provider dependency."""

from dataclasses import asdict
from datetime import datetime, timezone
import re
import uuid

from sqlalchemy import select, update

from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    Card,
    Spec,
    DeliveryEvidenceRecordRow as Record,
    CardDeliveryEvidenceRecordRow as CardRecord,
    ImplementationTargetExecutionRecordRow as Execution,
    ImplementationTargetRow as Target,
    CodeInvestigationReceiptRow as Receipt,
    CodeInvestigationReceiptRevocationRow as Revocation,
)
from okto_pulse.core.domain.delivery_evidence import (
    CardDeliveryScope,
    DeliveryBinding,
    DeliveryScope,
    DeliveryPhase,
    DeliveryEvidenceSnapshot,
    ImplementationDeliveryFact,
    TestDeliveryFact,
    DeliveryWaiverFact,
    evaluate_delivery_coverage,
)
from okto_pulse.core.domain.enums import CardType, CardStatus, TestScenarioStatus
from okto_pulse.core.models.delivery_evidence import (
    CardDeliveryEvidenceCommand,
    DeliveryEvidenceCommand,
)
from okto_pulse.core.ports.test_evidence import resolve_test_evidence_write_verifier
from okto_pulse.core.services.delivery_evidence import (
    card_delivery_inventory,
    delivery_digest,
    delivery_inventory,
)
from okto_pulse.core.services.test_scenario_lifecycle import (
    compute_test_scenario_semantic_sha256,
)


class CommunityDeliveryEvidenceStore:
    def __init__(self, session):
        self.session = session
        self._locked = False

    async def _get(self, model, identity):
        if identity is None:
            return None
        return await self.session.get(
            model, identity, populate_existing=True, with_for_update=self._locked
        )

    async def _spec(self, board_id, spec_id):
        spec = (
            await self.session.execute(
                select(Spec)
                .where(Spec.id == spec_id, Spec.board_id == board_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if spec is None or spec.archived:
            raise ValueError("delivery_spec_not_found")
        return spec

    async def lock_scope(self, scope):
        # Same board no-op write fence as Code Traceability receipt mutations.
        # SQLite serializes writers; a server-backed engine locks this board row
        # until commit. Community itself deliberately supports only SQLite.
        result = await self.session.execute(
            update(Board)
            .where(Board.id == scope.board_id)
            .values(id=Board.id)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise ValueError("delivery_board_not_found")
        self._locked = True
        spec = await self._spec(scope.board_id, scope.spec_id)
        if int(spec.edition) != scope.edition:
            raise ValueError("delivery_edition_conflict")

    async def _records(self, scope):
        return list(
            (
                await self.session.scalars(
                    select(Record)
                    .where(
                        Record.board_id == scope.board_id,
                        Record.spec_id == scope.spec_id,
                        Record.edition == scope.edition,
                    )
                    .order_by(Record.created_at, Record.id)
                )
            ).all()
        )

    async def _implementation(self, record, scope, bindings):
        payload = record.payload
        execution = await self._get(Execution, payload.get("execution_id"))
        card = await self._get(Card, payload.get("card_id"))
        if execution is None or card is None:
            return None
        target = await self._get(Target, execution.target_id)
        receipt = await self._get(Receipt, execution.result_investigation_receipt_id)
        latest = (
            await self.session.scalars(
                select(Execution.id)
                .where(
                    Execution.board_id == scope.board_id,
                    Execution.target_id == execution.target_id,
                )
                .order_by(Execution.received_at.desc(), Execution.id.desc())
                .limit(1)
            )
        ).first()
        revoked = (
            await self.session.scalars(
                select(Revocation.id).where(
                    Revocation.receipt_id == execution.result_investigation_receipt_id
                )
            )
        ).first()
        valid = (
            card.board_id == scope.board_id
            and card.spec_id == scope.spec_id
            and not card.archived
            and execution.board_id == scope.board_id
            and execution.card_id == card.id
            and latest == execution.id
            and execution.disposition in {"touched", "created", "deleted", "replaced"}
            and target is not None
            and target.board_id == scope.board_id
            and target.card_id == card.id
            and target.lifecycle_status == "active"
            and target.revision == execution.target_revision
            and receipt is not None
            and receipt.board_id == scope.board_id
            and receipt.subject_type == "card"
            and receipt.subject_id == card.id
            and receipt.acceptance_status == "accepted"
            and revoked is None
            and receipt.source_ref == execution.source_ref
            and receipt.declared_revision == execution.result_declared_revision
            and receipt.declared_dirty is False
            and re.fullmatch(
                r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}",
                execution.result_declared_revision or "",
            )
            is not None
        )
        # Receipt expiration concerns fresh source investigation, not the historical
        # existence of an accepted immutable delivery commit. Target heads and
        # explicit revocation still invalidate proof on every projection.
        return ImplementationDeliveryFact(
            id=record.id,
            scope=scope,
            card_id=card.id,
            card_type=CardType(card.card_type),
            card_status=CardStatus(card.status),
            bindings=bindings,
            source_ref=execution.source_ref,
            result_revision=execution.result_declared_revision or "",
            relative_path=execution.actual_relative_path or "",
            explanation=payload["justification"],
            receipt_id=execution.id,
            current_accepted_execution=bool(valid),
            actor_id=record.actor_id,
            symbol=execution.actual_qualified_symbol,
        )

    async def _test(self, record, scope, bindings, spec):
        payload = record.payload
        card = await self._get(Card, payload.get("card_id"))
        if card is None:
            return None
        matches = [
            s
            for s in (spec.test_scenarios or [])
            if isinstance(s, dict) and s.get("id") == payload.get("scenario_id")
        ]
        if len(matches) != 1:
            return None
        scenario = matches[0]
        evidence = scenario.get("evidence") or {}
        verifier = resolve_test_evidence_write_verifier()
        valid = (
            card.board_id == scope.board_id
            and card.spec_id == scope.spec_id
            and not card.archived
            and card.card_type == CardType.TEST
            and scenario["id"] in (card.test_scenario_ids or [])
            and scenario.get("status") == "passed"
            and isinstance(evidence, dict)
            and evidence.get("execution_receipt") == payload.get("test_receipt")
            and bool(payload.get("test_receipt"))
            and verifier is not None
        )
        if valid:
            digest = compute_test_scenario_semantic_sha256(
                board_id=scope.board_id,
                spec_id=scope.spec_id,
                scenario=scenario,
                acceptance_criteria=spec.acceptance_criteria or [],
            )
            valid = verifier.verify(
                board_id=scope.board_id,
                spec_id=scope.spec_id,
                status="passed",
                scenario_id=scenario["id"],
                scenario_sha256=digest,
                actor_id=None,
                evidence=evidence,
            ).verified
        if valid and payload.get("implementation_ids"):
            # Card-ledger test records reference card-ledger implementation ids
            # (CardRecord rows); legacy spec records reference the legacy table.
            binding_model = CardRecord if isinstance(record, CardRecord) else Record
            binding_scope_fields = (
                ("board_id", "spec_id", "spec_edition")
                if binding_model is CardRecord
                else ("board_id", "spec_id", "edition")
            )
            try:
                executed_at = datetime.fromisoformat(
                    evidence["execution_attestation"]["executed_at"].replace(
                        "Z", "+00:00"
                    )
                )
                for implementation_id in payload["implementation_ids"]:
                    binding_record = await self.session.get(
                        binding_model, implementation_id
                    )
                    if (
                        binding_record is None
                        or binding_record.kind != "implementation"
                        or tuple(
                            getattr(binding_record, field)
                            for field in binding_scope_fields
                        )
                        != (scope.board_id, scope.spec_id, scope.edition)
                    ):
                        valid = False
                        break
                    execution = await self.session.get(
                        Execution, binding_record.payload.get("execution_id")
                    )
                    receipt = (
                        await self.session.get(
                            Receipt, execution.result_investigation_receipt_id
                        )
                        if execution
                        else None
                    )
                    if receipt is None or executed_at < receipt.observed_at:
                        valid = False
                        break
            except (KeyError, TypeError, ValueError):
                valid = False
        try:
            status = TestScenarioStatus(scenario.get("status", "draft"))
        except ValueError:
            status = TestScenarioStatus.DRAFT
        return TestDeliveryFact(
            id=record.id,
            scope=scope,
            card_id=card.id,
            card_type=CardType(card.card_type),
            card_status=CardStatus(card.status),
            bindings=bindings,
            scenario_id=scenario["id"],
            result=status,
            receipt_id=payload.get("test_receipt") or "",
            current_verified_run=bool(valid),
            actor_id=record.actor_id,
            verified_implementation_ids=tuple(payload.get("implementation_ids", [])),
        )

    async def load_snapshot(self, scope):
        spec = await self._spec(scope.board_id, scope.spec_id)
        if int(spec.edition) != scope.edition:
            raise ValueError("delivery_edition_conflict")
        records = await self._records(scope)
        revoked = {
            r.payload.get("record_id")
            for r in records
            if r.kind == "revoke" and r.actor_kind in {"human", "user"}
        }
        implementations, tests, waivers = [], [], []
        for record in records:
            if record.id in revoked or record.kind == "revoke":
                continue
            bindings = tuple(
                DeliveryBinding(**b) for b in record.payload.get("bindings", [])
            )
            if record.kind == "implementation":
                fact = await self._implementation(record, scope, bindings)
                if fact is not None:
                    implementations.append(fact)
            elif record.kind == "test":
                fact = await self._test(record, scope, bindings, spec)
                if fact is not None:
                    tests.append(fact)
            elif record.kind == "waiver":
                for binding in bindings:
                    waivers.append(
                        DeliveryWaiverFact(
                            id=f"{record.id}:{binding.obligation_ref}",
                            scope=scope,
                            binding=binding,
                            phase=DeliveryPhase(record.payload["phase"]),
                            justification=record.payload["justification"],
                            actor_id=record.actor_id,
                            authorization_receipt_id=record.id,
                            current_authorized=record.actor_kind in {"human", "user"},
                        )
                    )
        return DeliveryEvidenceSnapshot(
            scope,
            delivery_inventory(spec),
            tuple(implementations),
            tuple(tests),
            tuple(waivers),
            complete=True,
        )

    async def projection(self, board_id, spec_id):
        spec = await self._spec(board_id, spec_id)
        scope = DeliveryScope(board_id, spec_id, int(spec.edition))
        # Spec-scoped READ returns the card-ledger rollup (FR-4): the spec
        # projection is a derivation, not a recording surface. The response
        # keeps the legacy shape and adds the per_card block (RDL-3).
        snapshot, per_card = await self.load_rollup_snapshot(board_id, spec_id)
        evaluation = evaluate_delivery_coverage(snapshot)
        records = await self._records(scope)
        revoked = {r.payload.get("record_id") for r in records if r.kind == "revoke"}
        candidates = []
        executions = (
            await self.session.scalars(
                select(Execution)
                .join(Card, Card.id == Execution.card_id)
                .where(
                    Execution.board_id == board_id,
                    Card.spec_id == spec_id,
                    Card.board_id == board_id,
                    Card.archived.is_(False),
                    Card.status == CardStatus.DONE,
                )
            )
        ).all()
        for execution in executions:
            candidate = Record(
                id=execution.id,
                actor_id=execution.submitted_by,
                payload={
                    "card_id": execution.card_id,
                    "execution_id": execution.id,
                    "justification": execution.justification,
                },
            )
            fact = await self._implementation(candidate, scope, ())
            if (
                fact
                and fact.current_accepted_execution
                and fact.card_type in {CardType.NORMAL, CardType.BUG}
            ):
                candidates.append(
                    {
                        "kind": "implementation",
                        "id": execution.id,
                        "card_id": execution.card_id,
                        # Card CAS fence for the card-scoped record surface.
                        "card_version": int(
                            getattr(
                                await self._get(Card, execution.card_id),
                                "policy_version",
                                1,
                            )
                            or 1
                        ),
                        "label": f"{execution.actual_relative_path} @ {execution.result_declared_revision}",
                    }
                )
        test_cards = (
            await self.session.scalars(
                select(Card).where(
                    Card.board_id == board_id,
                    Card.spec_id == spec_id,
                    Card.card_type == CardType.TEST,
                    Card.status == CardStatus.DONE,
                    Card.archived.is_(False),
                )
            )
        ).all()
        for card in test_cards:
            for scenario in spec.test_scenarios or []:
                if scenario.get("id") not in (card.test_scenario_ids or []):
                    continue
                candidate = Record(
                    id=card.id + ":" + scenario["id"],
                    actor_id="projection",
                    payload={
                        "card_id": card.id,
                        "scenario_id": scenario["id"],
                        "test_receipt": (scenario.get("evidence") or {}).get(
                            "execution_receipt"
                        ),
                    },
                )
                fact = await self._test(candidate, scope, (), spec)
                if fact and fact.current_verified_run:
                    candidates.append(
                        {
                            "kind": "test",
                            "id": scenario["id"],
                            "card_id": card.id,
                            "card_version": int(card.policy_version or 1),
                            "label": f"{card.title}: {scenario.get('title', scenario['id'])}",
                        }
                    )
        return {
            "board_id": board_id,
            "spec_id": spec_id,
            "edition": scope.edition,
            "version": spec.version,
            "status": str(getattr(spec.status, "value", spec.status)),
            "allowed": evaluation.allowed,
            "complete": snapshot.complete,
            "blockers": list(evaluation.blockers),
            "rejected_record_ids": list(evaluation.rejected_record_ids),
            "rows": [
                {
                    **asdict(row),
                    "implementation_satisfied": row.implementation_satisfied,
                    "test_satisfied": row.test_satisfied,
                }
                for row in evaluation.rows
            ],
            "implementations": [asdict(fact) for fact in snapshot.implementations],
            "tests": [asdict(fact) for fact in snapshot.tests],
            "candidates": candidates,
            "per_card": per_card,
            "records": [
                {
                    "id": r.id,
                    "kind": r.kind,
                    "actor_id": r.actor_id,
                    "created_at": r.created_at.isoformat(),
                    "revoked": r.id in revoked,
                    "payload": r.payload,
                }
                for r in records
            ],
        }

    async def record(self, command: DeliveryEvidenceCommand, *, actor_id, actor_kind):
        if not actor_id or actor_kind not in {"human", "user", "agent"}:
            raise ValueError("delivery_authenticated_actor_required")
        if command.kind in {"waiver", "revoke"} and actor_kind not in {"human", "user"}:
            raise ValueError("delivery_human_authorization_required")
        scope = DeliveryScope(
            command.board_id, command.spec_id, command.expected_edition
        )
        await self.lock_scope(scope)
        request_digest = delivery_digest(command.model_dump())
        replay = (
            await self.session.scalars(
                select(Record).where(
                    Record.board_id == scope.board_id,
                    Record.spec_id == scope.spec_id,
                    Record.actor_id == actor_id,
                    Record.idempotency_key == command.idempotency_key,
                )
            )
        ).one_or_none()
        if replay is not None:
            if (
                replay.payload_sha256 != request_digest
                or replay.actor_kind != actor_kind
            ):
                raise ValueError("delivery_idempotency_conflict")
            return {"id": replay.id, "replayed": True}
        spec = await self._spec(scope.board_id, scope.spec_id)
        if spec.version != command.expected_version:
            raise ValueError("delivery_version_conflict")
        inventory = {
            o.binding.obligation_ref: o.binding for o in delivery_inventory(spec)
        }
        if any(ref not in inventory for ref in command.obligation_refs):
            raise ValueError("delivery_obligation_not_found")
        payload = command.model_dump(
            exclude={
                "board_id",
                "spec_id",
                "idempotency_key",
                "expected_version",
                "expected_edition",
            }
        )
        payload["bindings"] = [
            asdict(inventory[ref]) for ref in command.obligation_refs
        ]
        if command.kind == "test":
            scenarios = [
                s
                for s in spec.test_scenarios or []
                if s.get("id") == command.scenario_id
            ]
            if len(scenarios) != 1:
                raise ValueError("delivery_test_scenario_not_found")
            payload["test_receipt"] = (scenarios[0].get("evidence") or {}).get(
                "execution_receipt"
            )
        if command.kind == "revoke":
            target = await self.session.get(Record, command.record_id)
            if (
                target is None
                or (target.board_id, target.spec_id, target.edition)
                != (scope.board_id, scope.spec_id, scope.edition)
                or target.kind == "revoke"
            ):
                raise ValueError("delivery_record_not_found")
        record = Record(
            id="delivery_" + uuid.uuid4().hex,
            board_id=scope.board_id,
            spec_id=scope.spec_id,
            edition=scope.edition,
            kind=command.kind,
            actor_id=actor_id,
            actor_kind=actor_kind,
            idempotency_key=command.idempotency_key,
            payload_sha256=request_digest,
            payload=payload,
            created_at=datetime.now(timezone.utc),
        )
        # Validate the candidate before inserting it. Rejected requests never leave
        # a partially accepted binding even if the caller catches the exception.
        bindings = tuple(inventory[ref] for ref in command.obligation_refs)
        existing = await self.load_snapshot(scope)
        if command.kind == "implementation":
            fact = await self._implementation(record, scope, bindings)
            candidate = DeliveryEvidenceSnapshot(
                scope, existing.obligations, (fact,) if fact else (), complete=True
            )
            result = evaluate_delivery_coverage(candidate)
            if fact is None or record.id in result.rejected_record_ids:
                raise ValueError("delivery_accepted_committed_task_execution_required")
        elif command.kind == "test":
            fact = await self._test(record, scope, bindings, spec)
            selected_implementations = tuple(
                i
                for i in existing.implementations
                if i.id in command.implementation_ids
            )
            candidate = DeliveryEvidenceSnapshot(
                scope,
                existing.obligations,
                selected_implementations,
                (fact,) if fact else (),
                complete=True,
            )
            result = evaluate_delivery_coverage(candidate)
            matching = {
                r.obligation.binding.obligation_ref
                for r in result.rows
                if record.id in r.test_ids
            }
            if matching != set(command.obligation_refs):
                raise ValueError(
                    "delivery_current_verified_test_and_implementation_required"
                )
            valid_ids = {
                f.id for f in existing.implementations if f.current_accepted_execution
            }
            if not set(command.implementation_ids) <= valid_ids:
                raise ValueError("delivery_implementation_scope_invalid")
        self.session.add(record)
        await self.session.flush()
        return {"id": record.id, "replayed": False}

    # ------------------------------------------------------------------
    # Card-scoped delivery ledger (per-task re-anchoring). The authenticated
    # fact validators above are reused untouched; only the addressing scope
    # and the CAS fence (card policy_version) change. Waivers stay on the
    # spec rollup surface and are human-only.
    # ------------------------------------------------------------------

    async def _card(self, board_id, card_id):
        card = await self._get(Card, card_id)
        if card is None or card.board_id != board_id:
            raise ValueError("delivery_card_not_found")
        return card

    async def _card_scope_guard(self, scope: CardDeliveryScope):
        """Validate card↔spec ownership and return (card, spec, spec scope)."""
        card = await self._card(scope.board_id, scope.card_id)
        spec = await self._spec(scope.board_id, scope.spec_id)
        if card.spec_id != scope.spec_id or int(spec.edition) != scope.spec_edition:
            raise ValueError("delivery_edition_conflict")
        return card, spec, DeliveryScope(scope.board_id, scope.spec_id, scope.spec_edition)

    async def _card_records(self, scope: CardDeliveryScope):
        return list(
            (
                await self.session.scalars(
                    select(CardRecord)
                    .where(
                        CardRecord.board_id == scope.board_id,
                        CardRecord.card_id == scope.card_id,
                        CardRecord.spec_id == scope.spec_id,
                        CardRecord.spec_edition == scope.spec_edition,
                    )
                    .order_by(CardRecord.created_at, CardRecord.id)
                )
            ).all()
        )

    async def load_card_snapshot(self, scope: CardDeliveryScope):
        card, spec, spec_scope = await self._card_scope_guard(scope)
        records = await self._card_records(scope)
        revoked = {
            r.payload.get("record_id")
            for r in records
            if r.kind == "revoke" and r.actor_kind in {"human", "user"}
        }
        implementations, tests = [], []
        for record in records:
            if record.id in revoked or record.kind == "revoke":
                continue
            bindings = tuple(
                DeliveryBinding(**b) for b in record.payload.get("bindings", [])
            )
            if record.kind == "implementation":
                fact = await self._implementation(record, spec_scope, bindings)
                if fact is not None:
                    implementations.append(fact)
            elif record.kind == "test":
                fact = await self._test(record, spec_scope, bindings, spec)
                if fact is not None:
                    tests.append(fact)
        return DeliveryEvidenceSnapshot(
            spec_scope,
            self._snapshot_obligations(spec, card),
            tuple(implementations),
            tuple(tests),
            complete=True,
        )

    @staticmethod
    def _snapshot_obligations(spec, card):
        """Obligation universe for one card's snapshot.

        Normal/bug cards derive obligations from their own links (the task
        DoD universe, FR-2). Test cards bind spec-level obligations — their
        scenario links are not ``linked_task_ids`` on spec entities — so
        their snapshot resolves the spec inventory (the rollup universe).
        """
        raw_type = getattr(card, "card_type", None)
        card_type = str(getattr(raw_type, "value", raw_type or "normal"))
        if card_type == "test":
            return delivery_inventory(spec)
        return card_delivery_inventory(spec, card)

    @staticmethod
    def _record_inventory(spec, card):
        """Binding-resolution inventory for record_card (same rule)."""
        raw_type = getattr(card, "card_type", None)
        card_type = str(getattr(raw_type, "value", raw_type or "normal"))
        if card_type == "test":
            return delivery_inventory(spec)
        return card_delivery_inventory(spec, card)

    async def record_card(
        self, command: CardDeliveryEvidenceCommand, *, actor_id, actor_kind
    ):
        if not actor_id or actor_kind not in {"human", "user", "agent"}:
            raise ValueError("delivery_authenticated_actor_required")
        if command.kind == "revoke" and actor_kind not in {"human", "user"}:
            raise ValueError("delivery_human_authorization_required")
        scope = CardDeliveryScope(
            command.board_id,
            command.card_id,
            command.spec_id,
            command.expected_spec_edition,
        )
        # Same board no-op write fence as the spec ledger serializes writers.
        await self.lock_scope(
            DeliveryScope(command.board_id, command.spec_id, command.expected_spec_edition)
        )
        request_digest = delivery_digest(command.model_dump())
        replay = (
            await self.session.scalars(
                select(CardRecord).where(
                    CardRecord.board_id == scope.board_id,
                    CardRecord.card_id == scope.card_id,
                    CardRecord.actor_id == actor_id,
                    CardRecord.idempotency_key == command.idempotency_key,
                )
            )
        ).one_or_none()
        if replay is not None:
            if (
                replay.payload_sha256 != request_digest
                or replay.actor_kind != actor_kind
            ):
                raise ValueError("delivery_idempotency_conflict")
            return {"id": replay.id, "replayed": True}
        card, spec, spec_scope = await self._card_scope_guard(scope)
        if card.policy_version != command.expected_card_version:
            raise ValueError("delivery_version_conflict")
        inventory = {
            o.binding.obligation_ref: o.binding
            for o in self._record_inventory(spec, card)
        }
        if any(ref not in inventory for ref in command.obligation_refs):
            raise ValueError("delivery_obligation_not_found")
        payload = command.model_dump(
            exclude={
                "board_id",
                "card_id",
                "spec_id",
                "idempotency_key",
                "expected_card_version",
                "expected_spec_edition",
            }
        )
        payload["card_id"] = card.id
        payload["bindings"] = [
            asdict(inventory[ref]) for ref in command.obligation_refs
        ]
        if command.kind == "test":
            scenarios = [
                s
                for s in spec.test_scenarios or []
                if s.get("id") == command.scenario_id
            ]
            if len(scenarios) != 1:
                raise ValueError("delivery_test_scenario_not_found")
            payload["test_receipt"] = (scenarios[0].get("evidence") or {}).get(
                "execution_receipt"
            )
        if command.kind == "revoke":
            target = await self.session.get(CardRecord, command.record_id)
            if (
                target is None
                or (
                    target.board_id,
                    target.card_id,
                    target.spec_id,
                    target.spec_edition,
                )
                != (
                    scope.board_id,
                    scope.card_id,
                    scope.spec_id,
                    scope.spec_edition,
                )
                or target.kind == "revoke"
            ):
                raise ValueError("delivery_record_not_found")
        record = CardRecord(
            id="card_delivery_" + uuid.uuid4().hex,
            board_id=scope.board_id,
            card_id=scope.card_id,
            spec_id=scope.spec_id,
            spec_edition=scope.spec_edition,
            kind=command.kind,
            actor_id=actor_id,
            actor_kind=actor_kind,
            idempotency_key=command.idempotency_key,
            payload_sha256=request_digest,
            payload=payload,
            created_at=datetime.now(timezone.utc),
        )
        # Validate the candidate before inserting it. Rejected requests never
        # leave a partially accepted binding even if the caller catches the
        # exception — same contract as the spec ledger's record().
        bindings = tuple(inventory[ref] for ref in command.obligation_refs)
        existing = await self.load_card_snapshot(scope)
        if command.kind == "implementation":
            fact = await self._implementation(record, spec_scope, bindings)
            candidate = DeliveryEvidenceSnapshot(
                spec_scope, existing.obligations, (fact,) if fact else (), complete=True
            )
            result = evaluate_delivery_coverage(candidate)
            if fact is None or record.id in result.rejected_record_ids:
                # evaluate_delivery_coverage only credits facts whose card is
                # already DONE. The card-scoped surface records the task's own
                # proof BEFORE completion (AC ac_c41b1fa3: record, then the
                # done move passes), so a chain-valid fact (accepted committed
                # execution) is acceptable here; its evaluator validity
                # completes with the DONE status, and the card gate plus the
                # spec rollup re-evaluate with the final status. Any other
                # rejection (broken receipt chain, scope mismatch) still
                # refuses the insert — no partial binding ever persists.
                if fact is None or not fact.current_accepted_execution:
                    raise ValueError(
                        "delivery_accepted_committed_task_execution_required"
                    )
        elif command.kind == "test":
            fact = await self._test(record, spec_scope, bindings, spec)
            # The test-phase join is cross-card by design (BR-5): a test card
            # verifies implementations recorded on OTHER cards, so the
            # candidate is evaluated against the spec ROLLUP implementations,
            # mirroring the legacy spec-ledger validation exactly.
            rollup_snapshot, _ = await self.load_rollup_snapshot(
                command.board_id, command.spec_id
            )
            selected_implementations = tuple(
                i
                for i in rollup_snapshot.implementations
                if i.id in command.implementation_ids
            )
            candidate = DeliveryEvidenceSnapshot(
                spec_scope,
                delivery_inventory(spec),
                selected_implementations,
                (fact,) if fact else (),
                complete=True,
            )
            result = evaluate_delivery_coverage(candidate)
            matching = {
                r.obligation.binding.obligation_ref
                for r in result.rows
                if record.id in r.test_ids
            }
            if matching != set(command.obligation_refs):
                raise ValueError(
                    "delivery_current_verified_test_and_implementation_required"
                )
            valid_ids = {
                f.id
                for f in rollup_snapshot.implementations
                if f.current_accepted_execution
            }
            if not set(command.implementation_ids) <= valid_ids:
                raise ValueError("delivery_implementation_scope_invalid")
        self.session.add(record)
        await self.session.flush()
        return {"id": record.id, "replayed": False}

    # ------------------------------------------------------------------
    # Spec rollup (FR-4): the spec projection derives from the card ledgers.
    # Waivers stay on the legacy spec ledger as the human-only, rollup-level
    # exception surface (BR-3); implementation/test proof is aggregated from
    # every linked card's snapshot.
    # ------------------------------------------------------------------

    async def _linked_cards(self, board_id, spec_id):
        return list(
            (
                await self.session.scalars(
                    select(Card)
                    .where(
                        Card.board_id == board_id,
                        Card.spec_id == spec_id,
                        Card.archived.is_(False),
                    )
                    .order_by(Card.id)
                )
            ).all()
        )

    async def _rollup_waivers(self, scope):
        """Active human-authorized waivers from the legacy spec ledger."""
        records = await self._records(scope)
        revoked = {
            r.payload.get("record_id")
            for r in records
            if r.kind == "revoke" and r.actor_kind in {"human", "user"}
        }
        waivers = []
        for record in records:
            if record.kind != "waiver" or record.id in revoked:
                continue
            if record.actor_kind not in {"human", "user"}:
                continue
            for binding in (
                DeliveryBinding(**b) for b in record.payload.get("bindings", [])
            ):
                waivers.append(
                    DeliveryWaiverFact(
                        id=f"{record.id}:{binding.obligation_ref}",
                        scope=scope,
                        binding=binding,
                        phase=DeliveryPhase(record.payload["phase"]),
                        justification=record.payload["justification"],
                        actor_id=record.actor_id,
                        authorization_receipt_id=record.id,
                        current_authorized=True,
                    )
                )
        return waivers

    async def load_rollup_snapshot(self, board_id, spec_id):
        """Aggregate the card-ledger snapshots of a spec into one snapshot.

        Returns ``(DeliveryEvidenceSnapshot, per_card)`` where per_card carries
        each linked card's derived obligations and implementation satisfaction
        (FR-8 name-first surfaces read the obligation titles from rows).
        """
        spec = await self._spec(board_id, spec_id)
        scope = DeliveryScope(board_id, spec_id, int(spec.edition))
        obligations = delivery_inventory(spec)
        implementations, tests, per_card = [], [], []
        for card in await self._linked_cards(board_id, spec_id):
            card_scope = CardDeliveryScope(
                board_id, card.id, spec_id, int(spec.edition)
            )
            snapshot = await self.load_card_snapshot(card_scope)
            implementations.extend(snapshot.implementations)
            tests.extend(snapshot.tests)
            evaluation = evaluate_delivery_coverage(snapshot)
            per_card.append(
                {
                    "card_id": card.id,
                    "title": card.title,
                    "card_type": str(
                        getattr(card.card_type, "value", card.card_type)
                    ),
                    "status": str(getattr(card.status, "value", card.status)),
                    "obligations": [
                        {
                            "ref": row.obligation.binding.obligation_ref,
                            "title": row.obligation.title,
                            "implementation_satisfied": (
                                row.implementation_satisfied
                            ),
                        }
                        for row in evaluation.rows
                    ],
                    "satisfied": bool(evaluation.rows)
                    and all(row.implementation_satisfied for row in evaluation.rows),
                }
            )
        waivers = await self._rollup_waivers(scope)
        snapshot = DeliveryEvidenceSnapshot(
            scope,
            obligations,
            tuple(implementations),
            tuple(tests),
            tuple(waivers),
            complete=True,
        )
        return snapshot, per_card
