"""One-shot 0.3.3 → card-ledger delivery evidence migration (copy-not-move).

The legacy ``delivery_evidence_records`` table is append-only (triggers abort
UPDATE/DELETE), so the migration COPIES bindings and revoke tombstones into
``card_delivery_evidence_records`` with ``migrated_from`` provenance and never
edits a legacy row (BR-6). Waivers stay on the legacy ledger: they are the
human-only, rollup-level exception surface and the rollup reads them there
(BR-3). The runner is transactional per board — a ``needs_relink`` obligation
or a verdict divergence aborts with no partial writes — and idempotent by
origin identity, so re-running skips already-migrated rows.
"""

from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import (
    CommunityDeliveryEvidenceStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Card,
    DeliveryEvidenceRecordRow as Record,
    CardDeliveryEvidenceRecordRow as CardRecord,
    Spec,
)
from okto_pulse.core.domain.delivery_evidence import (
    CardDeliveryScope,
    DeliveryScope,
    evaluate_delivery_coverage,
)
from okto_pulse.core.services.delivery_evidence import delivery_digest

MIGRATION_ACTOR_ID = "system:delivery-migration"


class DeliveryMigrationAborted(ValueError):
    """Transactional abort: nothing was copied for the board."""

    def __init__(self, reason: str, report: dict):
        super().__init__(f"delivery_migration_aborted: {reason}")
        self.report = report


def _verdict(evaluation) -> dict:
    return {
        "allowed": evaluation.allowed,
        "blockers": sorted(evaluation.blockers),
        "rows": {
            row.obligation.binding.obligation_ref: (
                row.implementation_satisfied,
                row.test_satisfied,
            )
            for row in evaluation.rows
        },
    }


async def migrate_board_delivery_evidence(
    session,
    board_id: str,
    *,
    dry_run: bool = False,
) -> dict:
    """Migrate one board's legacy delivery ledger into the card ledger.

    Returns a report: per-spec copied/skipped counts, plus the before/after
    verdict identity check. Raises :class:`DeliveryMigrationAborted` (before
    any write when ``dry_run`` is false the caller rolls back; with
    ``dry_run`` the abort report is still raised but nothing was staged).
    """
    store = CommunityDeliveryEvidenceStore(session)
    legacy = list(
        (
            await session.scalars(
                select(Record)
                .where(Record.board_id == board_id)
                .order_by(Record.spec_id, Record.edition, Record.created_at, Record.id)
            )
        ).all()
    )
    spec_ids = sorted({row.spec_id for row in legacy})
    already = {
        row.migrated_from.get("record_id")
        for row in (
            await session.scalars(
                select(CardRecord).where(CardRecord.board_id == board_id)
            )
        ).all()
        if row.migrated_from
    }
    report: dict = {
        "board_id": board_id,
        "specs": {},
        "copied": 0,
        "skipped_waivers": 0,
        "skipped_existing": 0,
        "aborted": False,
    }
    for spec_id in spec_ids:
        spec = await session.get(Spec, spec_id)
        if spec is None or spec.archived:
            continue
        scope = DeliveryScope(board_id, spec_id, int(spec.edition))
        before = _verdict(
            evaluate_delivery_coverage(await store.load_snapshot(scope))
        )
        rows = [r for r in legacy if r.spec_id == spec_id]
        spec_report = {"records": len(rows), "copied": 0, "needs_relink": []}
        id_map: dict[str, str] = {}
        card_cache: dict[str, Card] = {}
        staged: list[CardRecord] = []
        for row in rows:
            if row.kind == "waiver":
                # Waivers remain on the legacy rollup surface (BR-3).
                report["skipped_waivers"] += 1
                continue
            if row.id in already:
                report["skipped_existing"] += 1
                continue
            if row.kind == "revoke":
                target_old = row.payload.get("record_id")
                target_new = id_map.get(target_old)
                if target_new is None:
                    # Waiver revocation or pre-migration target: stays legacy.
                    continue
                origin_card = next(iter(staged_ids(staged, target_new)), None)
                new_row = _copy_row(
                    row,
                    board_id=board_id,
                    card_id=origin_card,
                    spec_id=spec_id,
                    edition=int(spec.edition),
                    payload={**row.payload, "record_id": target_new},
                )
                staged.append(new_row)
                spec_report["copied"] += 1
                report["copied"] += 1
                continue
            card_id = row.payload.get("card_id")
            card = card_cache.get(card_id)
            if card is None:
                card = await session.get(Card, card_id)
                card_cache[card_id] = card
            if card is None or card.board_id != board_id:
                # Card gone: the fact is already invalid in the legacy
                # projection (no card → no fact); skip, do not resurrect.
                report["skipped_existing"] += 1
                continue
            card_scope = CardDeliveryScope(
                board_id, card.id, spec_id, int(spec.edition)
            )
            inventory = store._record_inventory(spec, card)
            known = {o.binding.obligation_ref for o in inventory}
            missing = sorted(
                {
                    b.get("obligation_ref")
                    for b in row.payload.get("bindings", [])
                    if b.get("obligation_ref") not in known
                }
            )
            if missing:
                spec_report["needs_relink"].append(
                    {
                        "record_id": row.id,
                        "card_id": card.id,
                        "missing_obligation_refs": missing,
                    }
                )
                continue
            new_row = _copy_row(
                row,
                board_id=board_id,
                card_id=card.id,
                spec_id=spec_id,
                edition=int(spec.edition),
                payload=row.payload,
            )
            id_map[row.id] = new_row.id
            new_row._migration_target_card = card.id  # type: ignore[attr-defined]
            staged.append(new_row)
            spec_report["copied"] += 1
            report["copied"] += 1
        if spec_report["needs_relink"]:
            report["aborted"] = True
            report["specs"][spec_id] = spec_report
            raise DeliveryMigrationAborted("needs_relink", report)
        if not dry_run:
            for new_row in staged:
                session.add(new_row)
            await session.flush()
        after = _verdict(
            evaluate_delivery_coverage(
                (await store.load_rollup_snapshot(board_id, spec_id))[0]
            )
        )
        if after != before:
            report["aborted"] = True
            report["specs"][spec_id] = {
                **spec_report,
                "verdict_before": before,
                "verdict_after": after,
            }
            raise DeliveryMigrationAborted("verdict_divergence", report)
        report["specs"][spec_id] = spec_report
    return report


def staged_ids(staged, target_new: str):
    """Resolve the owning card of a copied record id among staged rows."""
    for row in staged:
        if row.id == target_new:
            yield getattr(row, "_migration_target_card", None)


def _copy_row(
    row: Record,
    *,
    board_id: str,
    card_id: str,
    spec_id: str,
    edition: int,
    payload: dict,
) -> CardRecord:
    import uuid

    return CardRecord(
        id="card_delivery_" + uuid.uuid4().hex,
        board_id=board_id,
        card_id=card_id,
        spec_id=spec_id,
        spec_edition=edition,
        kind=row.kind,
        actor_id=row.actor_id,
        actor_kind=row.actor_kind,
        idempotency_key=row.idempotency_key,
        payload_sha256=delivery_digest(payload),
        payload=payload,
        migrated_from={
            "spec_id": row.spec_id,
            "edition": int(row.edition),
            "record_id": row.id,
        },
        created_at=datetime.now(timezone.utc),
    )


async def migrate_all_boards(session) -> list[dict]:
    """Idempotent startup sweep: migrate every board with legacy records."""
    from okto_pulse.community.adapters.sqlalchemy_models import Board

    board_ids = list(
        (
            await session.scalars(
                select(Record.board_id).distinct()
            )
        ).all()
    )
    reports = []
    for board_id in board_ids:
        board = await session.get(Board, board_id)
        if board is None:
            continue
        reports.append(
            await migrate_board_delivery_evidence(session, board_id)
        )
    return reports
