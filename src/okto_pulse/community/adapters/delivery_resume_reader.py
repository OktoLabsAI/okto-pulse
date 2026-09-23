"""Card resume projection over canonical relational facts, without a graph read."""

import json
from dataclasses import replace

from sqlalchemy import func, select

from okto_pulse.community.adapters.sqlalchemy_models import ImplementationTargetRow as Target
from okto_pulse.core.domain.delivery_evidence import (
    CardDeliveryScope, evaluate_delivery_coverage, implementation_binding_proof_current,
)
from okto_pulse.core.domain.effective_delivery_coverage import implementation_scope_current
from okto_pulse.core.domain.delivery_progress import require_delivery_progress_mutable
from okto_pulse.core.domain.execution_plan import card_verification_plan


async def read_card_resume(store, query, *, actor_id):
    spec = await store._spec(query.board_id, query.spec_id)
    scope = CardDeliveryScope(query.board_id, query.card_id, query.spec_id, int(spec.edition))
    card, spec, _ = await store._card_scope_guard(scope)
    generation = await store._delivery_revision(scope)
    version = (card.policy_version, spec.version, card.status)
    plan = await store._execution_plan(spec)
    # Reuse only within this read. The final generation/version fence still
    # rejects concurrent changes; no cross-request or cross-principal cache.
    records = await store._card_records(scope)
    snapshot = await store.load_card_snapshot(scope, plan=plan, records=records)
    verification = card_verification_plan(plan, scope.card_id)
    scenario_ids = {row["scenario_id"] for row in verification["items"]}
    test_card_ids = sorted({identity for row in verification["items"] for identity in row["test_card_ids"]})
    tests_by_id = {fact.id: fact for fact in snapshot.tests}
    related_complete = verification["complete"] and plan is not None and plan.complete and len(test_card_ids) <= 20
    for test_card_id in test_card_ids[:20]:
        if test_card_id == scope.card_id:
            continue
        related = await store.load_card_snapshot(CardDeliveryScope(scope.board_id, test_card_id, scope.spec_id, scope.spec_edition), plan=plan)
        related_complete = related_complete and related.complete
        tests_by_id.update((fact.id, fact) for fact in related.tests if fact.scenario_id in scenario_ids)
    if plan is not None:
        snapshot = replace(snapshot, tests=tuple(tests_by_id.values()), complete=snapshot.complete and related_complete)
        snapshot = store._with_effective_context(snapshot, plan, records, spec, card)
    evaluation = evaluate_delivery_coverage(snapshot)
    progress = await store.progress_history(query.model_copy(update={"view": "progress"}), actor_id=actor_id)
    impact = await store._accumulated_impact(scope, records=records)
    target_filters = (
        Target.board_id == scope.board_id, Target.card_id == scope.card_id,
        Target.lifecycle_status == "active",
    )
    target_total = await store.session.scalar(select(func.count()).select_from(Target).where(*target_filters))
    targets = list((await store.session.scalars(select(Target).where(*target_filters).order_by(Target.id).limit(101))).all())
    try:
        require_delivery_progress_mutable(card)
        progress_eligible = True
    except ValueError:
        progress_eligible = False
    proofs = []
    for fact in snapshot.implementations[:20]:
        current = [binding.obligation_ref for binding in fact.bindings
                   if implementation_binding_proof_current(fact, binding)
                   and implementation_scope_current(snapshot, fact, binding)]
        proofs.append({
            "record_id": fact.id, "actor_id": fact.actor_id,
            "receipt_id": fact.receipt_id, "source_ref": fact.source_ref,
            "result_revision": fact.result_revision, "relative_path": fact.relative_path,
            "current_obligation_refs": current[:20], "current_obligation_total": len(current),
            "bindings_truncated": len(fact.bindings) > 20,
            "contributions": [{"obligation_ref": row.binding.obligation_ref,
                "declaration": row.contribution} for row in (fact.contributions or ())[:20]],
            "declaration_origin": "recorded" if fact.contributions is not None else "legacy_unknown",
        })
    card, spec, _ = await store._card_scope_guard(scope)
    if generation != await store._delivery_revision(scope) or version != (card.policy_version, spec.version, card.status):
        raise ValueError("delivery_resume_changed_retry")
    result = {
        "contract_version": "card-delivery-resume/v1",
        "board_id": scope.board_id, "card_id": scope.card_id, "spec_id": scope.spec_id,
        "edition": scope.spec_edition, "spec_version": spec.version,
        "card_version": card.policy_version, "delivery_revision": generation,
        "card_type": card.card_type, "status": card.status, "spec_status": spec.status,
        "title": card.title[:500], "progress_state_eligible": progress_eligible,
        "latest_checkpoint": progress["items"][0] if progress["items"] else None,
        "progress": progress, "accumulated_impact": impact,
        "obligations": {
            "complete": snapshot.complete, "total": len(evaluation.rows),
            "truncated": len(evaluation.rows) > 100,
            "items": [{"ref": row.obligation.binding.obligation_ref,
                "semantic_sha256": row.obligation.binding.semantic_sha256,
                "title": row.obligation.title[:500],
                "implementation_satisfied": row.implementation_satisfied,
                "test_satisfied": row.test_satisfied} for row in evaluation.rows[:100]],
        },
        "implementation_proofs": {"total": len(snapshot.implementations),
            "truncated": len(snapshot.implementations) > 20, "items": proofs},
        "verification_plan": {"complete": verification["complete"], "status": verification["status"],
            "total": len(verification["items"]), "truncated": len(verification["items"]) > 100,
            "test_card_total": len(test_card_ids), "test_cards_truncated": len(test_card_ids) > 20,
            "items": [{**row, "criterion_ids": row["criterion_ids"][:20], "test_card_ids": row["test_card_ids"][:20],
                "links_truncated": len(row["criterion_ids"]) > 20 or len(row["test_card_ids"]) > 20}
                for row in verification["items"][:100]]},
        "tests": {"scope": "card_and_related_obligations" if plan is not None else "this_card", "total": len(snapshot.tests),
            "total_exact": related_complete if plan is not None else False,
            "truncated": len(snapshot.tests) > 20,
            "items": [{"record_id": fact.id, "scenario_id": fact.scenario_id,
                "card_id": fact.card_id,
                "actor_id": fact.actor_id, "result": fact.result,
                "observes_this_card": bool(set(fact.verified_implementation_ids).intersection(item.id for item in snapshot.implementations)),
                "current_verified_run": fact.current_verified_run} for fact in snapshot.tests[:20]]},
        "targets": {"total": target_total, "truncated": len(targets) > 100,
            "items": [{"id": target.id, "revision": target.revision, "source_ref": target.source_ref,
                "relative_path": target.relative_path_hint, "resolution_id": target.current_resolution_id,
                "role": target.role} for target in targets[:100]]},
        "recovery": {"verified": False, "workspace_access": "unknown",
            "receipt_ownership_transferred": False, "unsubmitted_work": "unknown"},
        "pending_work": {"source": "declared_progress", "resolution_inferred": False,
            "history_truncated": progress["next_cursor"] is not None},
        "follow_up": {
            "targets": {"tool": "okto_pulse_list_implementation_targets", "board_id": scope.board_id,
                "card_id": scope.card_id, "lifecycle_status": "active", "limit": 50},
            "test_card_ledgers": [{"tool": "okto_pulse_get_delivery_evidence", "board_id": scope.board_id,
                "spec_id": scope.spec_id, "card_id": identity, "view": "ledger"} for identity in test_card_ids[:20]],
            "ledger": {"tool": "okto_pulse_get_delivery_evidence", "board_id": scope.board_id,
                "spec_id": scope.spec_id, "card_id": scope.card_id, "view": "ledger"},
            "progress": {"tool": "okto_pulse_get_delivery_evidence", "board_id": scope.board_id,
                "spec_id": scope.spec_id, "card_id": scope.card_id, "view": "progress"},
            "spec_rollup": {"tool": "okto_pulse_get_delivery_evidence", "board_id": scope.board_id,
                "spec_id": scope.spec_id},
            "transition_gates": {"tool": "okto_pulse_get_task_context", "board_id": scope.board_id,
                "card_id": scope.card_id, "profile": "full", "context_scope": "gate"},
        },
    }
    # Never shorten a progress page after constructing its cursor: that would
    # skip omitted notes. Other manifests retain totals and explicit truncation.
    result["response_limit_bytes"] = 128 * 1024
    result["response_truncated"] = False
    def oversized():
        # Reserve room for the use case's caller-specific action metadata.
        return len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > result["response_limit_bytes"] - 1024
    for name in ("targets", "obligations", "implementation_proofs", "tests", "verification_plan"):
        while result[name]["items"] and oversized():
            result[name]["items"].pop()
            result[name]["truncated"] = True
            result["response_truncated"] = True
    if oversized():
        result["accumulated_impact"] = {"status": impact["status"], "claim_only": True,
            "history_count": impact["history_count"], "detail_omitted": True}
        result["response_truncated"] = True
    if oversized():
        raise ValueError("delivery_resume_response_limit")
    return result
