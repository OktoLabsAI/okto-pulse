"""Relational delivery ledger. No source execution or graph-provider dependency."""

from dataclasses import asdict, replace
from datetime import datetime, timezone
import re
import uuid

from sqlalchemy import func, select, update

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
    CodeInvestigationHeadRow as SourceHead,
)
from okto_pulse.core.domain.delivery_evidence import (
    CardDeliveryScope,
    DeliveryBinding,
    DeliveryScope,
    DeliveryPhase,
    DeliveryEvidenceSnapshot,
    DeliveryObligation,
    ImplementationDeliveryFact,
    ImplementationExecutionProof,
    TestDeliveryFact,
    require_test_result_admission,
    DeliveryWaiverFact,
    evaluate_delivery_coverage,
    require_delivery_entry_card_type,
    require_delivery_batch_state,
    read_delivery_contributions,
    delivery_execution_ids,
    implementation_binding_proof_current,
    implementation_binding_proof_issue,
    implementation_binding_ready,
)
from okto_pulse.core.domain.enums import CardType, CardStatus, TestScenarioStatus
from okto_pulse.core.domain.execution_contract import execution_contract
from okto_pulse.core.domain.verification_report import parse_verification_report, verification_report_passing_criteria
from okto_pulse.core.domain.effective_delivery_coverage import (
    EffectiveDeliveryContext, ScopedTestFact, read_scoped_implementation, implementation_scope_current,
)
from okto_pulse.core.domain.delivery_progress import DeliveryProgress, progress_blocks_execution, progress_change_scope, require_delivery_progress_mutable
from okto_pulse.core.domain.delivery_selection import current_delivery_selection, seal_delivery_selection, current_delivery_report, report_reuses_impact, submitted_report_receipt
from okto_pulse.core.domain.delivery_impact import DeliveryImpactClaim, compose_delivery_impact, DeliveryImpactObservation, progress_affects_impact_source, require_impact_observation, reusable_impact_block
from okto_pulse.core.models.delivery_selection import DeliveryImpactBasis, DeliverySelectionManifest, DeliverySelectionInput
from okto_pulse.core.models.delivery_evidence import (
    CardDeliveryEvidenceCommand,
    CardDeliveryEvidenceBatchCommand,
    CardDeliveryEvidenceWriteCommand,
    DeliveryBatchEntryError,
    DeliveryEvidenceCommand,
)
from okto_pulse.core.models.code_traceability import ImplementationTargetExecutionSubmission
from okto_pulse.core.ports.delivery_evidence import DeliveryExecutionSubmitter
from okto_pulse.core.ports.test_evidence import resolve_test_evidence_write_verifier, require_supported_test_verification_method, supported_test_verification_methods
from okto_pulse.core.ports.delivery_inventory import (
    DeliveryInventoryPolicy,
    default_delivery_inventory_policy,
)
from okto_pulse.core.services.test_scenario_lifecycle import (
    compute_test_scenario_semantic_sha256,
)


class CommunityDeliveryEvidenceStore:
    def __init__(self, session, *, inventory: DeliveryInventoryPolicy | None = None):
        self.session = session
        self.inventory = inventory if inventory is not None else default_delivery_inventory_policy()
        self._locked = False

    async def _execution_plan(self, spec):
        if execution_contract(spec) is None:
            return None
        cards = list((await self.session.scalars(select(Card).where(
            Card.board_id == spec.board_id, Card.spec_id == spec.id,
        ).limit(5001))).all())
        fields = ('id', 'board_id', 'spec_id', 'card_type', 'status', 'archived',
                  'test_scenario_ids', 'title', 'description', 'details')
        return self.inventory.execution_plan(spec=spec,
            cards=[{field: getattr(card, field, None) for field in fields} for card in cards],
            admitted_methods=supported_test_verification_methods())

    def _planned_obligations(self, spec, plan, card=None):
        rows = plan.inventory.rows if card is None or card.card_type == CardType.TEST else plan.inventory.card_obligations(card.id)
        names = {item.binding.obligation_ref: item.title for item in self.inventory.spec_obligations(spec)}
        return tuple(DeliveryObligation(row.binding, names.get(row.binding.obligation_ref,
            card.title if card is not None else row.binding.obligation_ref)) for row in rows)

    def _with_effective_context(self, snapshot, plan, records, spec, card=None):
        if plan is None:
            return snapshot
        by_id = {record.id: record for record in records}
        inventory = plan.inventory
        if card is not None and card.card_type != CardType.TEST:
            inventory = replace(inventory, rows=tuple(replace(row,
                contributions=tuple(c for c in row.contributions if c.card_id == card.id))
                for row in inventory.card_obligations(card.id)))
        scenarios = {scenario.get('id'): scenario for scenario in (spec.test_scenarios or []) if isinstance(scenario, dict)}
        try:
            scoped = tuple(read_scoped_implementation(fact, by_id[fact.id].payload) for fact in snapshot.implementations)
        except (KeyError, ValueError, TypeError):
            return replace(snapshot, complete=False, effective_context=False)
        for fact in snapshot.tests:
            criteria = scenarios.get(fact.scenario_id, {}).get('linked_criteria')
            if (not isinstance(criteria, (tuple, list))
                or any(not isinstance(value, str) or not value for value in criteria)):
                return replace(snapshot, complete=False, effective_context=False)
        tests = []
        for fact in snapshot.tests:
            scenario = scenarios.get(fact.scenario_id, {})
            evidence = scenario.get('evidence') or {}
            passing = None
            if isinstance(evidence, dict) and evidence.get('evidence_class') == 'verification_report':
                passing = ()
                if fact.current_verified_run:
                    try:
                        passing = verification_report_passing_criteria(parse_verification_report(evidence.get('verification_report')))
                    except (TypeError, ValueError):
                        pass  # Malformed facts never acquire criterion credit.
            tests.append(ScopedTestFact(fact, tuple(scenario.get('linked_criteria') or ()),
                scenario.get('verification_method') or '', passing))
        return replace(snapshot, complete=snapshot.complete and plan.complete,
            effective_context=EffectiveDeliveryContext(inventory, scoped, tuple(tests), supported_test_verification_methods()))

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
        card = await self._get(Card, payload.get("card_id"))
        if card is None or card.board_id != scope.board_id or card.spec_id != scope.spec_id:
            return None
        contributions = read_delivery_contributions(payload, bindings)
        if payload.get("contribution_contract_version") == "card-binding-contribution/v2":
            identities = delivery_execution_ids(payload)
            proofs = []
            for identity in identities:
                proof = await self._execution_proof(identity, scope, card)
                if proof is not None:
                    proofs.append(proof)
            return ImplementationDeliveryFact(
                id=record.id, scope=scope, card_id=card.id,
                card_type=CardType(card.card_type), card_status=CardStatus(card.status),
                bindings=bindings, source_ref="", result_revision="", relative_path="",
                explanation=payload["justification"], receipt_id="",
                current_accepted_execution=len(proofs) == len(identities) and all(proof.current_accepted_execution for proof in proofs),
                actor_id=record.actor_id, contributions=contributions, executions=tuple(proofs),
                blocking_progress_ids=tuple(sorted({identity for proof in proofs for identity in proof.blocking_progress_ids}))[:20],
                blocking_progress_truncated=any(proof.blocking_progress_truncated for proof in proofs) or len({identity for proof in proofs for identity in proof.blocking_progress_ids}) > 20,
            )
        proof = await self._execution_proof(payload.get("execution_id"), scope, card)
        if proof is None:
            return None
        return ImplementationDeliveryFact(
            id=record.id, scope=scope, card_id=card.id,
            card_type=CardType(card.card_type), card_status=CardStatus(card.status),
            bindings=bindings, source_ref=proof.source_ref,
            result_revision=proof.result_revision, relative_path=proof.relative_path,
            explanation=payload["justification"], receipt_id=proof.execution_id,
            current_accepted_execution=proof.current_accepted_execution,
            actor_id=record.actor_id, symbol=proof.symbol, contributions=contributions,
            blocking_progress_ids=proof.blocking_progress_ids,
            blocking_progress_truncated=proof.blocking_progress_truncated,
        )

    async def _execution_proof(self, execution_id, scope, card):
        """The original receipt/head validator, shared by single and composed proofs."""
        execution = await self._get(Execution, execution_id)
        if execution is None or execution.board_id != scope.board_id or execution.card_id != card.id:
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
        progress = await self._active_material_progress(scope, card.id)
        blocking_progress_ids = tuple(record.id for record, declaration in progress if progress_blocks_execution(
            declaration, target_id=execution.target_id, source_ref=execution.source_ref,
            checkpoint_received_at=record.created_at,
            execution_observed_at=receipt.observed_at if receipt else None,
        ))
        # Receipt expiration concerns fresh source investigation, not the historical
        # existence of an accepted immutable delivery commit. Target heads and
        # explicit revocation still invalidate proof on every projection.
        return ImplementationExecutionProof(
            execution_id=execution.id,
            target_id=execution.target_id,
            target_revision=execution.target_revision,
            source_ref=execution.source_ref,
            result_revision=execution.result_declared_revision or "",
            relative_path=execution.actual_relative_path or "",
            current_accepted_execution=bool(valid) and not blocking_progress_ids,
            symbol=execution.actual_qualified_symbol,
            blocking_progress_ids=blocking_progress_ids[:20],
            blocking_progress_truncated=len(blocking_progress_ids) > 20,
        )

    async def _active_material_progress(self, scope: DeliveryScope, card_id: str):
        # Read the full relevant population, never the capped resume summary.
        filters = (CardRecord.board_id == scope.board_id, CardRecord.card_id == card_id,
                   CardRecord.spec_id == scope.spec_id, CardRecord.spec_edition == scope.edition)
        revoked = select(CardRecord.payload["record_id"].as_string()).where(
            *filters, CardRecord.kind == "revoke", CardRecord.actor_kind.in_(("human", "user")),
            CardRecord.payload["record_id"].as_string().is_not(None),
        )
        records = (await self.session.scalars(select(CardRecord).where(
            *filters, CardRecord.kind == "progress", CardRecord.id.not_in(revoked),
        ).order_by(CardRecord.created_at, CardRecord.id))).all()
        return [(record, declaration) for record in records
                if progress_change_scope(declaration := DeliveryProgress.model_validate(record.payload["progress"])) != "none"]

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
        # New records retain the authenticated outcome even when the live
        # scenario later changes. Legacy records keep their historical reader.
        recorded_result = payload.get("test_result", scenario.get("status"))
        evidence = scenario.get("evidence") or {}
        verifier = resolve_test_evidence_write_verifier()
        valid = (
            card.board_id == scope.board_id
            and card.spec_id == scope.spec_id
            and not card.archived
            and card.card_type == CardType.TEST
            and scenario["id"] in (card.test_scenario_ids or [])
            and recorded_result in {"passed", "failed"}
            and scenario.get("status") == recorded_result
            and isinstance(evidence, dict)
            and evidence.get("execution_receipt") == payload.get("test_receipt")
            and bool(payload.get("test_receipt"))
            and verifier is not None
        )
        if valid:
            try:
                require_supported_test_verification_method(scenario.get("verification_method"))
                from okto_pulse.core.domain.verification_report import require_evidence_method_binding
                require_evidence_method_binding(scenario.get("verification_method"), evidence)
            except ValueError:
                valid = False
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
                status=recorded_result,
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
                    (evidence["verification_report"]["observed_at"]
                     if evidence.get("evidence_class") == "verification_report"
                     else evidence["execution_attestation"]["executed_at"]).replace(
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
                    execution_ids = delivery_execution_ids(binding_record.payload, bindings)
                    if not execution_ids:
                        valid = False
                        break
                    for execution_id in execution_ids:
                        execution = await self.session.get(Execution, execution_id)
                        receipt = await self.session.get(Receipt, execution.result_investigation_receipt_id) if execution else None
                        if receipt is None or executed_at < receipt.observed_at:
                            valid = False
                            break
                    if not valid:
                        break
            except (KeyError, TypeError, ValueError):
                valid = False
        try:
            status = TestScenarioStatus(recorded_result or "draft")
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
        if execution_contract(spec) is not None:
            if int(spec.edition) != scope.edition:
                raise ValueError("delivery_edition_conflict")
            return (await self.load_rollup_snapshot(scope.board_id, scope.spec_id))[0]
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
            self.inventory.spec_obligations(spec),
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
                    # No card-status filter: the card-scoped surface records
                    # proof BEFORE completion (the done gate consumes it), so
                    # in-progress/validation cards' receipts must be pickable.
                    # Record-time fact validation stays the authority.
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
                    Card.status.in_((CardStatus.STARTED, CardStatus.IN_PROGRESS, CardStatus.DONE)),
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
                            "label": f"{card.title}: {scenario.get('title', scenario['id'])} · {fact.result.value}",
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
            "implementations": [{
                **asdict(fact),
                "admitted_obligation_refs": [binding.obligation_ref for binding in fact.bindings if implementation_binding_proof_current(fact, binding) and implementation_scope_current(snapshot, fact, binding)],
                "ready_obligation_refs": [binding.obligation_ref for binding in fact.bindings if implementation_binding_ready(fact, binding) and implementation_scope_current(snapshot, fact, binding)],
            } for fact in snapshot.implementations],
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
        command.require_exception_kind()
        if command.kind in {"waiver", "revoke"} and actor_kind not in {"human", "user"}:
            raise ValueError("delivery_human_authorization_required")
        scope = DeliveryScope(
            command.board_id, command.spec_id, command.expected_edition
        )
        await self.lock_scope(scope)
        request_digest = self.inventory.payload_digest(command.model_dump())
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
        plan = await self._execution_plan(spec)
        inventory = {
            o.binding.obligation_ref: o.binding for o in (
                self._planned_obligations(spec, plan) if plan is not None else self.inventory.spec_obligations(spec))
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
        # Proof admission exists only in record_card. The legacy table remains
        # readable and revocable; no historical bindings are rewritten here.
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

    async def load_card_snapshot(self, scope: CardDeliveryScope, *, plan=None, prospective_report=None):
        card, spec, spec_scope = await self._card_scope_guard(scope)
        records = await self._card_records(scope)
        revoked = {
            r.payload.get("record_id")
            for r in records
            if r.kind == "revoke" and r.actor_kind in {"human", "user"}
        }
        plan = plan if plan is not None else await self._execution_plan(spec)
        obligations = await self._snapshot_obligations(spec, card, plan=plan)
        selection_valid = True
        try:
            selected = current_delivery_selection(card, scope, obligations=obligations,
                record_hashes={record.id: self._selection_record_hash(record) for record in records},
                prospective_report=prospective_report)
        except ValueError:
            selected, selection_valid = set(), False
        implementations, tests = [], []
        for record in records:
            if record.id in revoked or record.kind == "revoke":
                continue
            if selected is not None and record.id not in selected:
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
        snapshot = DeliveryEvidenceSnapshot(
            spec_scope,
            obligations,
            tuple(implementations),
            tuple(tests),
            complete=selection_valid,
        )
        return self._with_effective_context(snapshot, plan, records, spec, card)

    def _selection_record_hash(self, record):
        # payload_sha256 is the request digest (possibly the whole batch), not
        # the fingerprint of its persisted, canonically resolved record.
        return self.inventory.payload_digest(dict(
            id=record.id, kind=record.kind, actor_id=record.actor_id, actor_kind=record.actor_kind,
            created_at=record.created_at.isoformat(), board_id=record.board_id,
            card_id=record.card_id, spec_id=record.spec_id, spec_edition=record.spec_edition,
            payload=record.payload,
        ))

    async def _fenced_selection(self, scope, selection, *, expected_status):
        if selection.expected_spec_edition != scope.spec_edition:
            raise ValueError("delivery_edition_conflict")
        await self.lock_scope(DeliveryScope(scope.board_id, scope.spec_id, scope.spec_edition))
        card, spec, _ = await self._card_scope_guard(scope)
        if card.policy_version != selection.expected_card_version or card.status != expected_status:
            raise ValueError("delivery_version_conflict")
        records = await self._card_records(scope)
        if len(records) != selection.expected_delivery_revision:
            raise ValueError("delivery_revision_conflict")
        revoked = {row.payload.get("record_id") for row in records
                   if row.kind == "revoke" and row.actor_kind in {"human", "user"}}
        wanted = set(selection.record_ids)
        selected = [row for row in records if row.id in wanted and row.id not in revoked
                    and row.kind in {"implementation", "test", "progress"}]
        if {row.id for row in selected} != wanted:
            raise ValueError("delivery_selection_record_unavailable")
        return card, spec, records, selected

    async def seal_selection(self, scope, selection, *, expected_status, impact, impact_basis=None):
        card, spec, records, selected = await self._fenced_selection(scope, selection, expected_status=expected_status)
        if bool(selection.reuse_impact) != (impact_basis is not None):
            raise ValueError("delivery_selection_impact_basis_required")
        return seal_delivery_selection(scope=scope, card_version=card.policy_version,
            revision=len(records), obligations=await self._snapshot_obligations(spec, card), impact=impact, impact_basis=impact_basis,
            records=[dict(id=row.id, kind=row.kind, sha256=self._selection_record_hash(row)) for row in selected])

    async def _observed_impact_basis(self, scope, source, *, original=None, fence=False):
        statement = select(SourceHead).where(SourceHead.board_id == scope.board_id, SourceHead.source_ref == source["source_ref"])
        head = (await self.session.scalars((statement.with_for_update() if fence else statement).execution_options(populate_existing=True))).one_or_none()
        receipt = None
        if head is not None and head.current_receipt_id:
            statement = select(Receipt).where(Receipt.id == head.current_receipt_id,
                Receipt.board_id == scope.board_id, Receipt.source_ref == source["source_ref"])
            if original is None:
                statement = statement.where(Receipt.subject_type == "card", Receipt.subject_id == scope.card_id)
            receipt = (await self.session.scalars((statement.with_for_update() if fence else statement).execution_options(populate_existing=True))).one_or_none()
        if original is not None:
            statement = select(Receipt).where(
                Receipt.id == original.observation_receipt_id, Receipt.board_id == scope.board_id,
                Receipt.source_ref == original.source_ref, Receipt.subject_type == "card", Receipt.subject_id == scope.card_id,
            )
            original_row = (await self.session.scalars((statement.with_for_update() if fence else statement).execution_options(populate_existing=True))).one_or_none()
            if (original_row is None
                or original_row.source_identity_digest != original.source_identity_sha256
                or (original_row.declared_revision or "").lower() != original.result_revision):
                raise ValueError("delivery_impact_observation_revoked_or_unavailable")
        # Read revocations after taking receipt locks; a concurrent revoker must
        # precede this read or wait for the report/completion transaction.
        revoked_ids = set((await self.session.scalars(select(Revocation.receipt_id).where(
            Revocation.board_id == scope.board_id,
            Revocation.receipt_id.in_([identity for identity in (
                receipt.id if receipt else None, original.observation_receipt_id if original else None) if identity]),
        ))).all())
        if original is not None and original.observation_receipt_id in revoked_ids:
            raise ValueError("delivery_impact_observation_revoked_or_unavailable")
        observation = DeliveryImpactObservation(receipt.id, receipt.source_ref, receipt.source_identity_digest,
            receipt.declared_revision, receipt.observed_at, bool(head.state == "current"
                and receipt.acceptance_status == "accepted" and receipt.declared_dirty is False and receipt.id not in revoked_ids)) if receipt else None
        progress = await self._active_material_progress(DeliveryScope(scope.board_id, scope.spec_id, scope.spec_edition), scope.card_id)
        target_ids = {identity for _, declaration in progress for identity in declaration.target_ids}
        target_sources = dict((await self.session.execute(select(Target.id, Target.source_ref).where(
            Target.board_id == scope.board_id, Target.card_id == scope.card_id, Target.id.in_(target_ids),
        ))).all()) if target_ids else {}
        checkpoints = tuple(row.created_at for row, declaration in progress
                            if progress_affects_impact_source(declaration, source["source_ref"], target_sources))
        require_impact_observation(source, observation, checkpoints,
                                  expected_identity=original.source_identity_sha256 if original else source["source_identity_sha256"])
        return DeliveryImpactBasis(source_ref=source["source_ref"], source_identity_sha256=observation.source_identity_sha256,
            base_revision=source["base_revision"], result_revision=source["result_revision"],
            observation_receipt_id=observation.receipt_id, record_ids=source["record_ids"]).model_dump(mode="json")

    async def resolve_selection_impact(self, scope, selection, *, expected_status):
        _, _, _, selected = await self._fenced_selection(scope, selection, expected_status=expected_status)
        projection = compose_delivery_impact(self._impact_claims(selected))
        impact = reusable_impact_block(projection)
        by_id = {row.id: row for row in selected}
        for source in projection["sources"]:
            identities = {by_id[identity].payload.get("_impact_source_identity_sha256") for identity in source["record_ids"]}
            if None in identities or len(identities) != 1:
                raise ValueError("delivery_impact_source_identity_unestablished")
            source["source_identity_sha256"] = next(iter(identities))
        basis = [await self._observed_impact_basis(scope, source, fence=True) for source in projection["sources"]]
        return dict(impact_evidence=impact.model_dump(mode="json", exclude_none=True), impact_basis=basis)

    async def report_impact_status(self, scope, *, for_update=False):
        if for_update:
            await self.lock_scope(DeliveryScope(scope.board_id, scope.spec_id, scope.spec_edition))
        card, spec, _ = await self._card_scope_guard(scope)
        report = current_delivery_report(card)
        if report is None or not report_reuses_impact(report):
            return dict(source="manual_or_absent", current=None, reason=None)
        try:
            records = await self._card_records(scope)
            current_delivery_selection(card, scope, obligations=await self._snapshot_obligations(spec, card),
                record_hashes={row.id: self._selection_record_hash(row) for row in records})
            manifest = DeliverySelectionManifest.model_validate(report["delivery_manifest"])
            for basis in manifest.impact_basis or ():
                await self._observed_impact_basis(scope, basis.model_dump(), original=basis, fence=for_update)
            return dict(source="accumulated", current=True, reason=None)
        except ValueError as exc:
            return dict(source="accumulated", current=False, reason=str(exc).split(":", 1)[0][:128])

    async def _selection_summary(self, scope):
        records = await self._card_records(scope)
        revoked = {row.payload.get("record_id") for row in records
                   if row.kind == "revoke" and row.actor_kind in {"human", "user"}}
        selectable = [row for row in records if row.kind != "revoke" and row.id not in revoked]
        return dict(total=len(selectable), truncated=len(selectable) > 200,
            records=[dict(id=row.id, kind=row.kind, summary=row.payload.get("justification", "")[:160])
                     for row in selectable[-200:]])

    def _impact_claims(self, records):
        claims = []
        for row in records:
            if row.kind != "progress":
                continue
            progress = DeliveryProgress.model_validate(row.payload["progress"])
            if progress.impact_delta is not None:
                claims.append(DeliveryImpactClaim(row.id, progress.source_state.source_ref,
                    progress.impact_base_revision, progress.source_state.declared_revision, progress.impact_delta))
        return tuple(claims)

    async def _accumulated_impact(self, scope):
        """All active declared deltas, independently of a frozen report selection.

        This read-only preview does not authenticate source bases, change the
        report's impact claim or decide readiness. Revoked entries remain history.
        """
        records = await self._card_records(scope)
        revoked = {row.payload.get("record_id") for row in records
                   if row.kind == "revoke" and row.actor_kind in {"human", "user"}}
        return compose_delivery_impact(self._impact_claims([row for row in records if row.id not in revoked]))

    async def _snapshot_obligations(self, spec, card, *, plan=None):
        """Obligation universe for one card's snapshot.

        Normal/bug cards derive obligations from their own links (the task
        DoD universe, FR-2). Test cards bind spec-level obligations — their
        scenario links are not ``linked_task_ids`` on spec entities — so
        their snapshot resolves the spec inventory (the rollup universe).
        """
        plan = plan if plan is not None else await self._execution_plan(spec)
        if plan is not None:
            return self._planned_obligations(spec, plan, card)
        raw_type = getattr(card, "card_type", None)
        card_type = str(getattr(raw_type, "value", raw_type or "normal"))
        if card_type == "test":
            return self.inventory.spec_obligations(spec)
        return self.inventory.card_obligations(spec, card)

    async def _record_inventory(self, spec, card):
        """Binding-resolution inventory for record_card (same rule)."""
        return await self._snapshot_obligations(spec, card)

    async def record_card(
        self, command: CardDeliveryEvidenceWriteCommand, *, actor_id, actor_kind,
        execution_submitter: DeliveryExecutionSubmitter | None = None,
    ):
        if isinstance(command, CardDeliveryEvidenceBatchCommand):
            return await self._record_card_batch(command, actor_id=actor_id, actor_kind=actor_kind,
                                                 execution_submitter=execution_submitter)
        if command.execution_submission is not None:
            # Receipt, binding and origin outbox share the same rollback boundary.
            await self.lock_scope(DeliveryScope(command.board_id, command.spec_id, command.expected_spec_edition))
            async with self.session.begin_nested():
                return await self._record_card_entry(command, actor_id=actor_id, actor_kind=actor_kind,
                                                      execution_submitter=execution_submitter)
        return await self._record_card_entry(command, actor_id=actor_id, actor_kind=actor_kind)

    async def _delivery_revision(self, scope):
        return int(await self.session.scalar(select(func.count()).select_from(CardRecord).where(
            CardRecord.board_id == scope.board_id, CardRecord.card_id == scope.card_id,
            CardRecord.spec_id == scope.spec_id, CardRecord.spec_edition == scope.spec_edition,
        )))

    async def record_card_report(self, command, *, actor_id, actor_kind, report_submitter, execution_submitter=None):
        batch = command.batch_command()
        scope = CardDeliveryScope(command.board_id, command.card_id, command.spec_id, batch.expected_spec_edition)
        await self.lock_scope(DeliveryScope(scope.board_id, scope.spec_id, scope.spec_edition))
        async with self.session.begin_nested():
            result = await self._record_card_batch(batch, actor_id=actor_id, actor_kind=actor_kind,
                execution_submitter=execution_submitter, submission_digest=self.inventory.payload_digest(command.model_dump()),
                expected_status=command.expected_card_status)
            ids = {row["id"] for row in result["entries"]}
            if not result["replayed"]:
                selection = DeliverySelectionInput(expected_card_version=batch.expected_card_version,
                    expected_spec_edition=batch.expected_spec_edition, expected_delivery_revision=result["delivery_revision"],
                    record_ids=sorted(ids | set(command.existing_record_ids)), reuse_impact=command.reuse_impact)
                await report_submitter(selection)
                await self.session.flush()
            card, _, _ = await self._card_scope_guard(scope)
            receipt = submitted_report_receipt(card, scope, ids, command.report.status.value)
            return {**result, "report": receipt}

    async def _record_card_batch(self, command, *, actor_id, actor_kind, execution_submitter=None,
                                 submission_digest=None, expected_status=None):
        if not actor_id or actor_kind not in {"human", "user", "agent"}:
            raise ValueError("delivery_authenticated_actor_required")
        scope = CardDeliveryScope(command.board_id, command.card_id, command.spec_id, command.expected_spec_edition)
        await self.lock_scope(DeliveryScope(scope.board_id, scope.spec_id, scope.spec_edition))
        card, _, _ = await self._card_scope_guard(scope)
        request_digest = self.inventory.payload_digest(command.model_dump())
        if submission_digest is not None:
            request_digest = self.inventory.payload_digest({"batch": request_digest, "report": submission_digest})
        filters = (
            CardRecord.board_id == scope.board_id, CardRecord.card_id == scope.card_id,
            CardRecord.actor_id == actor_id,
        )
        head = (await self.session.scalars(select(CardRecord).where(
            *filters, CardRecord.idempotency_key == command.idempotency_key,
        ))).one_or_none()
        if head is not None:
            receipt = head.payload.get("_batch", {}).get("receipt")
            if not receipt or head.payload_sha256 != request_digest or head.actor_kind != actor_kind:
                raise ValueError("delivery_idempotency_conflict")
            members = list((await self.session.scalars(select(CardRecord).where(
                *filters, CardRecord.id.in_([item["id"] for item in receipt["entries"]]),
            ))).all())
            expected = {(item["id"], item["client_ref"]) for item in receipt["entries"]}
            observed = {
                (item.id, item.payload.get("_batch", {}).get("client_ref"))
                for item in members
                if item.spec_id == scope.spec_id and item.spec_edition == scope.spec_edition
                and item.actor_kind == actor_kind and item.payload.get("_batch", {}).get("head_id") == head.id
            }
            if expected != observed or len(members) != len(receipt["entries"]):
                raise ValueError("delivery_batch_replay_incomplete")
            by_id = {item.id: item for item in members}
            response_entries = [dict(item) for item in receipt["entries"]]
            for entry, item in zip(command.entries, response_entries, strict=True):
                if entry.execution_submission is not None or entry.execution_client_ref is not None:
                    item["execution_id"] = by_id[item["id"]].payload["execution_id"]
            return {"entries": response_entries, "delivery_revision": receipt["delivery_revision"], "replayed": True}
        if card.policy_version != command.expected_card_version:
            raise ValueError("delivery_version_conflict")
        if expected_status is not None and card.status != expected_status:
            raise ValueError("delivery_report_status_conflict")
        require_delivery_batch_state(card)
        revision = await self._delivery_revision(scope)
        if revision != command.expected_delivery_revision:
            raise ValueError("delivery_revision_conflict")
        for index, entry in enumerate(command.entries):
            try:
                require_delivery_entry_card_type(card.card_type, entry.kind)
            except ValueError as exc:
                raise DeliveryBatchEntryError(index, entry.client_ref, str(exc)) from exc
        keys = [command.idempotency_key] + [
            "delivery-batch-entry:" + self.inventory.payload_digest({
                "batch_key": command.idempotency_key, "client_ref": entry.client_ref,
            }) for entry in command.entries[1:]
        ]
        if await self.session.scalar(select(CardRecord.id).where(
            *filters, CardRecord.idempotency_key.in_(keys),
        ).limit(1)):
            raise ValueError("delivery_idempotency_conflict")
        results = [
            {"client_ref": entry.client_ref, "id": "card_delivery_" + uuid.uuid4().hex}
            for entry in command.entries
        ]
        receipt = {"request_digest": request_digest, "entries": results, "delivery_revision": revision + len(results)}
        response_entries = [dict(item) for item in results]
        prior_results = {}
        # The first immutable entry carries the batch receipt; there is no
        # second journal or mutable batch head. A savepoint also protects a
        # caller that catches the error and later commits its outer UoW.
        async with self.session.begin_nested():
            for index, entry in enumerate(command.entries):
                try:
                    child = CardDeliveryEvidenceCommand(
                        board_id=scope.board_id, card_id=scope.card_id, spec_id=scope.spec_id,
                        expected_card_version=command.expected_card_version,
                        expected_spec_edition=scope.spec_edition, idempotency_key=keys[index],
                        **entry.resolved_fields(prior_results),
                    )
                    context = {"head_id": results[0]["id"], "client_ref": entry.client_ref}
                    if index == 0:
                        context["receipt"] = receipt
                    saved = await self._record_card_entry(child, actor_id=actor_id, actor_kind=actor_kind,
                                                  record_identity=results[index]["id"], batch_context=context,
                                                  execution_submitter=execution_submitter)
                    if "execution_id" in saved:
                        response_entries[index]["execution_id"] = saved["execution_id"]
                    elif entry.execution_client_ref is not None:
                        response_entries[index]["execution_id"] = child.execution_id
                    prior_results[entry.client_ref] = {
                        **saved, "execution_id": saved.get("execution_id", child.execution_id),
                    }
                except ValueError as exc:
                    raise DeliveryBatchEntryError(index, entry.client_ref, str(exc).split(":", 1)[0]) from exc
        return {"entries": response_entries, "delivery_revision": receipt["delivery_revision"], "replayed": False}

    async def _record_card_entry(
        self, command: CardDeliveryEvidenceCommand, *, actor_id, actor_kind,
        record_identity=None, batch_context=None, execution_submitter=None,
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
        request_digest = self.inventory.payload_digest(command.model_dump())
        if batch_context and "receipt" in batch_context:
            request_digest = batch_context["receipt"]["request_digest"]
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
                batch_context is not None or "_batch" in replay.payload
                or
                replay.payload_sha256 != request_digest
                or replay.actor_kind != actor_kind
            ):
                raise ValueError("delivery_idempotency_conflict")
            result = {"id": replay.id, "replayed": True}
            if command.execution_submission is not None:
                result["execution_id"] = replay.payload["execution_id"]
            return result
        card, spec, spec_scope = await self._card_scope_guard(scope)
        if card.policy_version != command.expected_card_version:
            raise ValueError("delivery_version_conflict")
        if command.progress_refs:
            reference_ids = {ref.record_id for ref in command.progress_refs}
            existing_ids = set((await self.session.scalars(select(CardRecord.id).where(
                CardRecord.id.in_(reference_ids), CardRecord.kind == "progress",
                CardRecord.board_id == scope.board_id, CardRecord.card_id == scope.card_id,
                CardRecord.spec_id == scope.spec_id, CardRecord.spec_edition == scope.spec_edition,
            ))).all())
            if reference_ids != existing_ids:
                raise ValueError("delivery_progress_reference_unavailable")
        inline = command.execution_submission
        if inline is not None:
            require_delivery_batch_state(card)
            require_delivery_entry_card_type(card.card_type, command.kind)
            if execution_submitter is None:
                raise ValueError("delivery_execution_submitter_unavailable")
            execution_id = await execution_submitter(ImplementationTargetExecutionSubmission(
                board_id=scope.board_id, card_id=scope.card_id,
                idempotency_key="delivery-inline:" + self.inventory.payload_digest({
                    "card_id": scope.card_id, "key": command.idempotency_key,
                }), justification=command.justification, **inline.model_dump(),
            ))
            command = command.model_copy(update={"execution_submission": None, "execution_id": execution_id})
        if command.kind == "progress":
            require_delivery_progress_mutable(card)
            progress = command.progress
            targets = list((await self.session.scalars(
                select(Target).where(
                    Target.board_id == scope.board_id,
                    Target.card_id == scope.card_id,
                    Target.id.in_(progress.target_ids),
                )
            )).all()) if progress.target_ids else []
            if {target.id for target in targets} != set(progress.target_ids):
                raise ValueError("delivery_progress_target_unavailable")
            source_ref = progress.source_state.source_ref
            if source_ref and any(target.source_ref != source_ref for target in targets):
                raise ValueError("delivery_progress_target_source_conflict")
            if source_ref and not await self.session.scalar(
                select(Receipt.id).where(
                    Receipt.board_id == scope.board_id,
                    Receipt.source_ref == source_ref,
                ).limit(1)
            ):
                raise ValueError("delivery_progress_source_unavailable")
        refs = command.selected_obligation_refs
        inventory = {
            o.binding.obligation_ref: o.binding
            for o in await self._record_inventory(spec, card)
        } if refs else {}
        if any(ref not in inventory for ref in refs):
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
        if batch_context is not None:
            payload["_batch"] = batch_context
        if command.kind == "progress" and command.progress.impact_delta is not None and command.progress.source_state.source_ref:
            # Server provenance only: request schemas cannot author this value.
            # Unestablished/conflicted identity stays unknown, never blocks the
            # checkpoint and never becomes reusable impact through inference.
            payload["_impact_source_identity_sha256"] = await self.session.scalar(
                select(Receipt.source_identity_digest).join(SourceHead, SourceHead.current_receipt_id == Receipt.id).where(
                    SourceHead.board_id == scope.board_id, SourceHead.source_ref == command.progress.source_state.source_ref,
                    SourceHead.state == "current", Receipt.board_id == scope.board_id,
                    Receipt.source_ref == command.progress.source_state.source_ref, Receipt.acceptance_status == "accepted",
                ).with_for_update()
            )
        if command.bindings is not None:
            payload["contribution_contract_version"] = "card-binding-contribution/v2" if command.composite_execution else "card-binding-contribution/v1"
            payload["contributions"] = [{
                "obligation_ref": item.obligation_ref, "contribution": item.contribution,
                **({"execution_ids": [ref.execution_id for ref in item.execution_refs]} if command.composite_execution else {}),
            } for item in command.bindings]
        payload["bindings"] = [
            asdict(inventory[ref]) for ref in refs
        ]
        plan = await self._execution_plan(spec) if command.kind in {"implementation", "test"} else None
        if plan is not None and command.kind == "implementation":
            if command.bindings is None:
                raise ValueError("delivery_contribution_declaration_required")
            rows = {row.binding.obligation_ref: row for row in plan.inventory.rows}
            scopes = []
            for ref in refs:
                row = rows[ref]
                contributions = [item for item in row.contributions if item.card_id == card.id]
                if row.blockers or len(contributions) != 1:
                    raise ValueError("delivery_contribution_allocation_unresolved")
                scopes.append(dict(obligation_ref=ref, scope_sha256=contributions[0].scope_sha256))
            payload["scope_contract_version"] = "card-contribution-scope/v1"
            payload["contribution_scopes"] = scopes
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
            payload["test_result"] = scenarios[0].get("status")
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
            id=record_identity or "card_delivery_" + uuid.uuid4().hex,
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
        bindings = tuple(inventory[ref] for ref in refs)
        if command.kind == "implementation":
            fact = await self._implementation(record, spec_scope, bindings)
            # Admission checks every named receipt without requiring Done or
            # upgrading a partial declaration. Completion uses the same proof
            # predicate plus declaration/lifecycle/review predicates.
            if fact is None:
                raise ValueError("delivery_accepted_committed_task_execution_required")
            for binding in bindings:
                issue = implementation_binding_proof_issue(fact, binding)
                if issue is not None:
                    raise ValueError(issue)
        elif command.kind == "test":
            fact = await self._test(record, spec_scope, bindings, spec)
            # Authenticate and bind the result now; final delivery credit is a
            # separate read predicate and still requires passing + Done.
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
                rollup_snapshot.obligations,
                selected_implementations,
                (fact,) if fact else (),
                complete=rollup_snapshot.complete,
            )
            if plan is not None:
                selected_records = list((await self.session.scalars(select(CardRecord).where(
                    CardRecord.board_id == scope.board_id, CardRecord.spec_id == scope.spec_id,
                    CardRecord.spec_edition == scope.spec_edition, CardRecord.id.in_(command.implementation_ids),
                ))).all())
                candidate = self._with_effective_context(candidate, plan, [*selected_records, record], spec)
            require_test_result_admission(candidate, fact)
        self.session.add(record)
        await self.session.flush()
        response = {"id": record.id, "replayed": False}
        if inline is not None:
            response["execution_id"] = command.execution_id
        return response

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

    async def card_resume(self, query, *, actor_id):
        from okto_pulse.community.adapters.delivery_resume_reader import read_card_resume

        return await read_card_resume(self, query, actor_id=actor_id)

    async def progress_history(self, query, *, actor_id):
        from okto_pulse.community.adapters.delivery_progress_reader import read_progress_history

        return await read_progress_history(self, query, actor_id=actor_id)

    async def _progress_summary(self, scope):
        """Bounded history for resumption; currentness reads the full population."""
        filters = (
            CardRecord.board_id == scope.board_id,
            CardRecord.card_id == scope.card_id,
            CardRecord.spec_id == scope.spec_id,
            CardRecord.spec_edition == scope.spec_edition,
            CardRecord.kind == "progress",
        )
        total = await self.session.scalar(select(func.count()).select_from(CardRecord).where(*filters))
        records = list((await self.session.scalars(
            select(CardRecord).where(*filters).order_by(
                CardRecord.created_at.desc(), CardRecord.id.desc(),
            ).limit(20)
        )).all())
        revoked = set((await self.session.scalars(
            select(CardRecord.payload["record_id"].as_string()).where(
                CardRecord.board_id == scope.board_id,
                CardRecord.card_id == scope.card_id,
                CardRecord.spec_id == scope.spec_id,
                CardRecord.spec_edition == scope.spec_edition,
                CardRecord.kind == "revoke",
                CardRecord.actor_kind.in_(("human", "user")),
                CardRecord.payload["record_id"].as_string().in_([record.id for record in records]),
            )
        )).all()) if records else set()
        targets = list((await self.session.scalars(select(Target).where(
            Target.board_id == scope.board_id, Target.card_id == scope.card_id,
            Target.lifecycle_status == "active",
        ).order_by(Target.id).limit(101))).all())
        return {
            "target_options": [{"id": target.id, "source_ref": target.source_ref,
                                "label": target.relative_path_hint or target.id} for target in targets[:100]],
            "targets_truncated": len(targets) > 100,
            "total": total,
            "truncated": total > len(records),
            "recovery_verified": False,
            "items": [
                {
                    "id": record.id, "actor_id": record.actor_id,
                    "revoked": record.id in revoked,
                    "created_at": record.created_at.isoformat(),
                    "summary": record.payload["justification"][:1000],
                    "remaining": record.payload["progress"]["remaining"][:1000],
                    "text_truncated": len(record.payload["justification"]) > 1000 or len(record.payload["progress"]["remaining"]) > 1000,
                    "source_state": record.payload["progress"]["source_state"],
                    "material_change": progress_change_scope(DeliveryProgress.model_validate(record.payload["progress"])),
                    "change_declaration_origin": record.payload["progress"].get("contract_version", "delivery-progress/v1"),
                    "target_ids": record.payload["progress"]["target_ids"][:10],
                    "targets_truncated": len(record.payload["progress"]["target_ids"]) > 10,
                }
                for record in reversed(records)
            ],
        }

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
        plan = await self._execution_plan(spec)
        obligations = self._planned_obligations(spec, plan) if plan is not None else self.inventory.spec_obligations(spec)
        implementations, tests, per_card = [], [], []
        complete = True
        for card in await self._linked_cards(board_id, spec_id):
            card_scope = CardDeliveryScope(
                board_id, card.id, spec_id, int(spec.edition)
            )
            snapshot = await self.load_card_snapshot(card_scope, plan=plan)
            complete = complete and snapshot.complete is True
            implementations.extend(snapshot.implementations)
            tests.extend(snapshot.tests)
            evaluation = evaluate_delivery_coverage(snapshot)
            per_card.append(
                {
                    "card_id": card.id,
                    "complete": snapshot.complete,
                    "title": card.title,
                    "card_version": card.policy_version,
                    "delivery_revision": await self._delivery_revision(card_scope),
                    "selection": await self._selection_summary(card_scope),
                    "accumulated_impact": await self._accumulated_impact(card_scope),
                    "report_impact": await self.report_impact_status(card_scope),
                    "progress": await self._progress_summary(card_scope),
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
                    "satisfied": snapshot.complete is True and bool(evaluation.rows)
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
            complete=complete,
        )
        if plan is not None:
            identities = [item.id for item in (*implementations, *tests)]
            records = list((await self.session.scalars(select(CardRecord).where(
                CardRecord.board_id == board_id, CardRecord.spec_id == spec_id,
                CardRecord.spec_edition == scope.edition, CardRecord.id.in_(identities),
            ))).all())
            snapshot = self._with_effective_context(snapshot, plan, records, spec)
        return snapshot, per_card
