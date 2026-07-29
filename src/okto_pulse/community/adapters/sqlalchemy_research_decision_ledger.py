"""Transaction-bound SQLAlchemy adapter for the SK-A Research Decision Ledger."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import TypeAlias

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.community.adapters.sqlalchemy_models import (
    DomainEventHandlerExecution,
    DomainEventRow,
    Refinement,
    ResearchDecisionDerivationRow,
    ResearchDecisionEntryRow,
    ResearchDecisionHeadRow,
    ResearchDecisionHistoryRow,
    ResearchDecisionIdempotencyRow,
    ResearchDecisionOutboxRow,
    ResearchDecisionSnapshotRow,
)
from okto_pulse.community.adapters.ska_observability import (
    observed_ska_projection,
)
from okto_pulse.core.domain.enums import RefinementStatus
from okto_pulse.core.domain.research_decision_ledger import (
    RESEARCH_DECISION_LEDGER_CONTRACT_VERSION,
    ResearchDecisionAnchor,
    ResearchDecisionAnchorType,
    ResearchDecisionCommitResult,
    ResearchDecisionContent,
    ResearchDecisionCursor,
    ResearchDecisionDerivation,
    ResearchDecisionDerivationRef,
    ResearchDecisionEntry,
    ResearchDecisionFrozenHead,
    ResearchDecisionHead,
    ResearchDecisionLedgerSnapshot,
    ResearchDecisionOffsetPage,
    ResearchDecisionPage,
    ResearchDecisionStatus,
    ResearchDecisionWriteBundle,
    research_decision_content_digest,
)
from okto_pulse.core.ports.research_decision_ledger import (
    ResearchDecisionHeadRevisionConflict,
    ResearchDecisionIdempotencyConflict,
    ResearchDecisionImmutableEntryConflict,
    ResearchDecisionLedgerPersistencePort,
    ResearchDecisionListQuery,
    ResearchDecisionOffsetListQuery,
    ResearchDecisionPersistenceError,
    ResearchDecisionRefinementLifecycleConflict,
    ResearchDecisionRefinementStatusConflict,
    ResearchDecisionRefinementVersionConflict,
    ResearchDecisionScopeConflict,
)

FaultInjector: TypeAlias = Callable[[str], Awaitable[None] | None]

_CONSOLIDATION_HANDLER_NAME = "ConsolidationEnqueuer"


def _entry_from_row(row: ResearchDecisionEntryRow) -> ResearchDecisionEntry:
    return ResearchDecisionEntry(
        id=row.id,
        ledger_id=row.ledger_id,
        board_id=row.board_id,
        refinement_id=row.refinement_id,
        refinement_version=row.refinement_version,
        predecessor_entry_id=row.predecessor_entry_id,
        content=ResearchDecisionContent(
            unknown=row.unknown,
            status=ResearchDecisionStatus(row.status),
            anchor=ResearchDecisionAnchor(
                anchor_type=ResearchDecisionAnchorType(row.anchor_type),
                anchor_ref=row.anchor_ref,
            ),
            evidence_refs=tuple(row.evidence_refs or ()),
            alternatives=tuple(row.alternatives or ()),
            decision=row.decision,
            rationale=row.rationale,
            confidence=row.confidence,
            evidence_absence_justification=row.evidence_absence_justification,
        ),
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _head_from_row(row: ResearchDecisionHeadRow) -> ResearchDecisionHead:
    return ResearchDecisionHead(
        ledger_id=row.ledger_id,
        board_id=row.board_id,
        refinement_id=row.refinement_id,
        current_entry_id=row.current_entry_id,
        revision=row.revision,
        refinement_version=row.refinement_version,
        status=ResearchDecisionStatus(row.status),
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )


class CommunitySqlAlchemyResearchDecisionLedger(
    ResearchDecisionLedgerPersistencePort
):
    """SQLite-backed RDL source of truth that never owns the outer transaction."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self._session = session
        self._fault_injector = fault_injector

    async def _fault(self, stage: str) -> None:
        if self._fault_injector is None:
            return
        result = self._fault_injector(stage)
        if inspect.isawaitable(result):
            await result

    async def get_entry(
        self,
        *,
        board_id: str,
        refinement_id: str,
        entry_id: str,
    ) -> ResearchDecisionEntry | None:
        row = await self._session.scalar(
            select(ResearchDecisionEntryRow).where(
                ResearchDecisionEntryRow.id == entry_id,
                ResearchDecisionEntryRow.board_id == board_id,
                ResearchDecisionEntryRow.refinement_id == refinement_id,
            )
        )
        return _entry_from_row(row) if row is not None else None

    async def get_current(
        self,
        *,
        board_id: str,
        refinement_id: str,
        ledger_id: str,
    ) -> tuple[ResearchDecisionEntry, ResearchDecisionHead] | None:
        result = await self._session.execute(
            select(ResearchDecisionEntryRow, ResearchDecisionHeadRow)
            .join(
                ResearchDecisionEntryRow,
                ResearchDecisionEntryRow.id
                == ResearchDecisionHeadRow.current_entry_id,
            )
            .where(
                ResearchDecisionHeadRow.ledger_id == ledger_id,
                ResearchDecisionHeadRow.board_id == board_id,
                ResearchDecisionHeadRow.refinement_id == refinement_id,
                ResearchDecisionEntryRow.board_id == board_id,
                ResearchDecisionEntryRow.refinement_id == refinement_id,
            )
        )
        row = result.one_or_none()
        if row is None:
            return None
        entry_row, head_row = row
        return _entry_from_row(entry_row), _head_from_row(head_row)

    async def list_current_heads(
        self,
        *,
        board_id: str,
        refinement_id: str,
    ) -> tuple[ResearchDecisionHead, ...]:
        rows = (
            await self._session.scalars(
                select(ResearchDecisionHeadRow)
                .where(
                    ResearchDecisionHeadRow.board_id == board_id,
                    ResearchDecisionHeadRow.refinement_id == refinement_id,
                )
                .order_by(ResearchDecisionHeadRow.ledger_id)
            )
        ).all()
        return tuple(_head_from_row(row) for row in rows)

    async def list_current_entries_with_heads(
        self,
        *,
        board_id: str,
        refinement_id: str,
    ) -> tuple[
        tuple[ResearchDecisionEntry, ResearchDecisionHead],
        ...,
    ]:
        rows = (
            await self._session.execute(
                select(ResearchDecisionEntryRow, ResearchDecisionHeadRow)
                .join(
                    ResearchDecisionEntryRow,
                    ResearchDecisionEntryRow.id
                    == ResearchDecisionHeadRow.current_entry_id,
                )
                .where(
                    ResearchDecisionHeadRow.board_id == board_id,
                    ResearchDecisionHeadRow.refinement_id == refinement_id,
                    ResearchDecisionEntryRow.board_id == board_id,
                    ResearchDecisionEntryRow.refinement_id == refinement_id,
                )
                .order_by(ResearchDecisionHeadRow.ledger_id)
            )
        ).all()
        return tuple(
            (_entry_from_row(entry_row), _head_from_row(head_row))
            for entry_row, head_row in rows
        )

    @observed_ska_projection(
        surface="research_decisions",
        subject_type="refinement",
        query_count=1,
    )
    async def list_entries(
        self,
        query: ResearchDecisionListQuery,
    ) -> ResearchDecisionPage[ResearchDecisionEntry]:
        statement = select(ResearchDecisionEntryRow).where(
            ResearchDecisionEntryRow.board_id == query.board_id,
            ResearchDecisionEntryRow.refinement_id == query.refinement_id,
        )
        if query.ledger_id is not None:
            statement = statement.where(
                ResearchDecisionEntryRow.ledger_id == query.ledger_id
            )
        if query.status is not None:
            statement = statement.where(
                ResearchDecisionEntryRow.status == query.status.value
            )
        if query.cursor is not None:
            statement = statement.where(
                or_(
                    ResearchDecisionEntryRow.created_at < query.cursor.created_at,
                    and_(
                        ResearchDecisionEntryRow.created_at
                        == query.cursor.created_at,
                        ResearchDecisionEntryRow.id < query.cursor.entry_id,
                    ),
                )
            )
        rows = (
            await self._session.scalars(
                statement.order_by(
                    ResearchDecisionEntryRow.created_at.desc(),
                    ResearchDecisionEntryRow.id.desc(),
                ).limit(query.limit + 1)
            )
        ).all()
        has_more = len(rows) > query.limit
        entries = tuple(_entry_from_row(row) for row in rows[: query.limit])
        next_cursor = (
            ResearchDecisionCursor(
                created_at=entries[-1].created_at,
                entry_id=entries[-1].id,
            )
            if has_more and entries
            else None
        )
        return ResearchDecisionPage(
            items=entries,
            limit=query.limit,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    @observed_ska_projection(
        surface="research_decisions",
        subject_type="refinement",
        query_count=3,
    )
    async def list_entries_offset(
        self,
        query: ResearchDecisionOffsetListQuery,
    ) -> ResearchDecisionOffsetPage[ResearchDecisionEntry]:
        base_conditions = (
            ResearchDecisionEntryRow.board_id == query.board_id,
            ResearchDecisionEntryRow.refinement_id == query.refinement_id,
        )
        filtered_conditions = list(base_conditions)
        if query.ledger_id is not None:
            filtered_conditions.append(
                ResearchDecisionEntryRow.ledger_id == query.ledger_id
            )
        if query.status is not None:
            filtered_conditions.append(
                ResearchDecisionEntryRow.status == query.status.value
            )
        total_overall = int(
            await self._session.scalar(
                select(func.count())
                .select_from(ResearchDecisionEntryRow)
                .where(*base_conditions)
            )
            or 0
        )
        total_filtered = int(
            await self._session.scalar(
                select(func.count())
                .select_from(ResearchDecisionEntryRow)
                .where(*filtered_conditions)
            )
            or 0
        )
        rows = (
            await self._session.scalars(
                select(ResearchDecisionEntryRow)
                .where(*filtered_conditions)
                .order_by(
                    ResearchDecisionEntryRow.created_at.desc(),
                    ResearchDecisionEntryRow.id.desc(),
                )
                .offset(query.offset)
                .limit(query.limit)
            )
        ).all()
        return ResearchDecisionOffsetPage(
            items=tuple(_entry_from_row(row) for row in rows),
            offset=query.offset,
            limit=query.limit,
            total_filtered=total_filtered,
            total_overall=total_overall,
        )

    async def get_snapshot(
        self,
        *,
        board_id: str,
        refinement_id: str,
        snapshot_id: str,
    ) -> ResearchDecisionLedgerSnapshot | None:
        row = await self._session.scalar(
            select(ResearchDecisionSnapshotRow).where(
                ResearchDecisionSnapshotRow.id == snapshot_id,
                ResearchDecisionSnapshotRow.board_id == board_id,
                ResearchDecisionSnapshotRow.refinement_id == refinement_id,
            )
        )
        if row is None:
            return None
        try:
            heads = tuple(
                ResearchDecisionFrozenHead(
                    ledger_id=item["ledger_id"],
                    entry_id=item["entry_id"],
                    head_revision=item["head_revision"],
                    head_refinement_version=item["head_refinement_version"],
                    status=ResearchDecisionStatus(item["status"]),
                    content_digest=item["content_digest"],
                )
                for item in (row.heads_json or ())
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ResearchDecisionPersistenceError(
                "research_decision_snapshot_content_digest_invalid"
            ) from exc
        await self._validate_content_digests(
            board_id=row.board_id,
            refinement_id=row.refinement_id,
            references=heads,
        )
        return ResearchDecisionLedgerSnapshot(
            id=row.id,
            board_id=row.board_id,
            refinement_id=row.refinement_id,
            refinement_version=row.refinement_version,
            heads=heads,
            created_at=row.created_at,
        )

    async def get_snapshot_for_version(
        self,
        *,
        board_id: str,
        refinement_id: str,
        refinement_version: int,
    ) -> ResearchDecisionLedgerSnapshot | None:
        row = await self._session.scalar(
            select(ResearchDecisionSnapshotRow).where(
                ResearchDecisionSnapshotRow.board_id == board_id,
                ResearchDecisionSnapshotRow.refinement_id == refinement_id,
                ResearchDecisionSnapshotRow.refinement_version
                == refinement_version,
            )
        )
        if row is None:
            return None
        return await self.get_snapshot(
            board_id=board_id,
            refinement_id=refinement_id,
            snapshot_id=row.id,
        )

    async def get_derivation(
        self,
        *,
        board_id: str,
        spec_id: str,
        spec_version: int,
    ) -> ResearchDecisionDerivation | None:
        row = await self._session.scalar(
            select(ResearchDecisionDerivationRow).where(
                ResearchDecisionDerivationRow.board_id == board_id,
                ResearchDecisionDerivationRow.spec_id == spec_id,
                ResearchDecisionDerivationRow.spec_version == spec_version,
            )
        )
        if row is None:
            return None
        try:
            references = tuple(
                ResearchDecisionDerivationRef(
                    source_snapshot_id=item["source_snapshot_id"],
                    source_refinement_id=item["source_refinement_id"],
                    source_refinement_version=item[
                        "source_refinement_version"
                    ],
                    ledger_id=item["ledger_id"],
                    entry_id=item["entry_id"],
                    head_revision=item["head_revision"],
                    status=ResearchDecisionStatus(item["status"]),
                    content_digest=item["content_digest"],
                )
                for item in (row.references_json or ())
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ResearchDecisionPersistenceError(
                "research_decision_derivation_content_digest_invalid"
            ) from exc
        await self._validate_content_digests(
            board_id=row.board_id,
            refinement_id=row.source_refinement_id,
            references=references,
        )
        return ResearchDecisionDerivation(
            board_id=row.board_id,
            spec_id=row.spec_id,
            spec_version=row.spec_version,
            source_refinement_id=row.source_refinement_id,
            source_refinement_version=row.source_refinement_version,
            source_snapshot_id=row.source_snapshot_id,
            references=references,
        )

    async def _validate_content_digests(
        self,
        *,
        board_id: str,
        refinement_id: str,
        references: tuple[
            ResearchDecisionFrozenHead | ResearchDecisionDerivationRef,
            ...,
        ],
    ) -> None:
        if not references:
            return
        entry_ids = tuple(reference.entry_id for reference in references)
        rows = (
            await self._session.scalars(
                select(ResearchDecisionEntryRow).where(
                    ResearchDecisionEntryRow.board_id == board_id,
                    ResearchDecisionEntryRow.refinement_id == refinement_id,
                    ResearchDecisionEntryRow.id.in_(entry_ids),
                )
            )
        ).all()
        entries = {row.id: _entry_from_row(row) for row in rows}
        if len(entries) != len(set(entry_ids)):
            raise ResearchDecisionPersistenceError(
                "research_decision_content_digest_entry_missing"
            )
        for reference in references:
            entry = entries[reference.entry_id]
            if (
                research_decision_content_digest(entry.content)
                != reference.content_digest
            ):
                raise ResearchDecisionPersistenceError(
                    "research_decision_content_digest_mismatch"
                )

    async def resolve_idempotent_result(
        self,
        *,
        board_id: str,
        refinement_id: str,
        idempotency_key: str,
        request_digest: str,
    ) -> ResearchDecisionCommitResult | None:
        binding = await self._session.scalar(
            select(ResearchDecisionIdempotencyRow).where(
                ResearchDecisionIdempotencyRow.board_id == board_id,
                ResearchDecisionIdempotencyRow.refinement_id == refinement_id,
                ResearchDecisionIdempotencyRow.idempotency_key
                == idempotency_key,
            )
        )
        if binding is None:
            return None
        if binding.request_digest != request_digest:
            raise ResearchDecisionIdempotencyConflict(
                "research_decision_idempotency_conflict"
            )
        entry = await self.get_entry(
            board_id=board_id,
            refinement_id=refinement_id,
            entry_id=binding.entry_id,
        )
        if entry is None:
            raise ResearchDecisionPersistenceError(
                "research_decision_idempotency_entry_missing"
            )
        return ResearchDecisionCommitResult(
            entry=entry,
            head=ResearchDecisionHead(
                ledger_id=binding.ledger_id,
                board_id=board_id,
                refinement_id=refinement_id,
                current_entry_id=binding.entry_id,
                revision=binding.head_revision,
                refinement_version=binding.refinement_version,
                status=entry.status,
                updated_by=binding.actor_id,
                updated_at=binding.occurred_at,
            ),
            refinement_version=binding.refinement_version,
            history_id=binding.history_id,
            event_id=binding.event_id,
            outbox_id=binding.outbox_id,
            replayed=True,
        )

    async def apply_bundle_cas(
        self,
        bundle: ResearchDecisionWriteBundle,
    ) -> ResearchDecisionCommitResult:
        if not isinstance(bundle, ResearchDecisionWriteBundle):
            raise ResearchDecisionPersistenceError(
                "research_decision_write_bundle_invalid"
            )
        replay = await self._resolve_bundle_replay(bundle)
        if replay is not None:
            return replay

        try:
            async with self._session.begin_nested():
                await self._assert_append_head_absent(bundle)
                await self._advance_refinement_version(bundle)
                await self._fault("after_refinement_version")

                self._session.add(
                    ResearchDecisionEntryRow(
                        id=bundle.entry.id,
                        ledger_id=bundle.entry.ledger_id,
                        board_id=bundle.entry.board_id,
                        refinement_id=bundle.entry.refinement_id,
                        refinement_version=bundle.entry.refinement_version,
                        predecessor_entry_id=bundle.entry.predecessor_entry_id,
                        unknown=bundle.entry.content.unknown,
                        status=bundle.entry.content.status.value,
                        anchor_type=bundle.entry.content.anchor.anchor_type.value,
                        anchor_ref=bundle.entry.content.anchor.anchor_ref,
                        evidence_refs=list(bundle.entry.content.evidence_refs),
                        alternatives=list(bundle.entry.content.alternatives),
                        decision=bundle.entry.content.decision,
                        rationale=bundle.entry.content.rationale,
                        confidence=bundle.entry.content.confidence,
                        evidence_absence_justification=(
                            bundle.entry.content.evidence_absence_justification
                        ),
                        created_by=bundle.entry.created_by,
                        created_at=bundle.entry.created_at,
                    )
                )
                await self._session.flush()
                await self._fault("after_entry")

                await self._advance_head(bundle)
                await self._session.flush()
                await self._fault("after_head")

                self._session.add(
                    ResearchDecisionHistoryRow(
                        id=bundle.history.id,
                        action=bundle.history.action.value,
                        board_id=bundle.history.board_id,
                        refinement_id=bundle.history.refinement_id,
                        ledger_id=bundle.history.ledger_id,
                        entry_id=bundle.history.entry_id,
                        predecessor_entry_id=(
                            bundle.history.predecessor_entry_id
                        ),
                        from_refinement_version=(
                            bundle.history.from_refinement_version
                        ),
                        to_refinement_version=(
                            bundle.history.to_refinement_version
                        ),
                        actor_id=bundle.history.actor_id,
                        created_at=bundle.history.created_at,
                    )
                )
                await self._session.flush()
                await self._fault("after_history")

                event_payload = {
                    "contract_version": RESEARCH_DECISION_LEDGER_CONTRACT_VERSION,
                    "event_schema_version": 1,
                    "entry_id": bundle.entry.id,
                    "head_revision": bundle.next_head.revision,
                    "ledger_id": bundle.entry.ledger_id,
                    "refinement_id": bundle.entry.refinement_id,
                    "refinement_version": bundle.entry.refinement_version,
                    "status": bundle.entry.status.value,
                }
                self._session.add(
                    DomainEventRow(
                        id=bundle.event.id,
                        event_type=bundle.event.event_type.value,
                        board_id=bundle.event.board_id,
                        actor_id=bundle.event.actor_id,
                        actor_type=bundle.event.actor_type,
                        payload_json=event_payload,
                        occurred_at=bundle.event.occurred_at,
                    )
                )
                await self._session.flush()
                self._session.add(
                    DomainEventHandlerExecution(
                        event_id=bundle.event.id,
                        handler_name=_CONSOLIDATION_HANDLER_NAME,
                        status="pending",
                        attempts=0,
                    )
                )
                await self._session.flush()
                await self._fault("after_event")

                self._session.add(
                    ResearchDecisionOutboxRow(
                        id=bundle.outbox.id,
                        event_id=bundle.outbox.event_id,
                        event_type=bundle.outbox.event_type.value,
                        partition_key=bundle.outbox.partition_key,
                        payload_json=event_payload,
                        status="pending",
                        available_at=bundle.outbox.available_at,
                    )
                )
                await self._session.flush()
                await self._fault("after_outbox")

                if bundle.idempotency_key is not None:
                    self._session.add(
                        ResearchDecisionIdempotencyRow(
                            board_id=bundle.entry.board_id,
                            refinement_id=bundle.entry.refinement_id,
                            idempotency_key=bundle.idempotency_key,
                            request_digest=bundle.request_digest,
                            ledger_id=bundle.entry.ledger_id,
                            entry_id=bundle.entry.id,
                            head_revision=bundle.next_head.revision,
                            refinement_version=bundle.entry.refinement_version,
                            history_id=bundle.history.id,
                            event_id=bundle.event.id,
                            outbox_id=bundle.outbox.id,
                            actor_id=bundle.entry.created_by,
                            occurred_at=bundle.entry.created_at,
                        )
                    )
                    await self._session.flush()
                    await self._fault("after_idempotency")
        except ResearchDecisionPersistenceError:
            raise
        except IntegrityError as exc:
            message = str(exc.orig).lower()
            if "research_decision_entry_immutable" in message:
                raise ResearchDecisionImmutableEntryConflict() from exc
            replay = await self._resolve_bundle_replay(bundle)
            if replay is not None:
                return replay
            if "refinement_version" in message:
                raise ResearchDecisionRefinementVersionConflict() from exc
            raise ResearchDecisionHeadRevisionConflict() from exc
        except SQLAlchemyError as exc:
            raise ResearchDecisionPersistenceError(str(exc)) from exc

        return ResearchDecisionCommitResult(
            entry=bundle.entry,
            head=bundle.next_head,
            refinement_version=bundle.version_bump.resulting_version,
            history_id=bundle.history.id,
            event_id=bundle.event.id,
            outbox_id=bundle.outbox.id,
        )

    async def save_snapshot(
        self,
        snapshot: ResearchDecisionLedgerSnapshot,
    ) -> ResearchDecisionLedgerSnapshot:
        if not isinstance(snapshot, ResearchDecisionLedgerSnapshot):
            raise ResearchDecisionPersistenceError(
                "research_decision_snapshot_invalid"
            )
        await self._validate_content_digests(
            board_id=snapshot.board_id,
            refinement_id=snapshot.refinement_id,
            references=snapshot.heads,
        )
        existing = await self.get_snapshot_for_version(
            board_id=snapshot.board_id,
            refinement_id=snapshot.refinement_id,
            refinement_version=snapshot.refinement_version,
        )
        if existing is not None:
            if existing.heads == snapshot.heads:
                return existing
            raise ResearchDecisionImmutableEntryConflict(
                "research_decision_snapshot_conflict"
            )
        try:
            async with self._session.begin_nested():
                self._session.add(
                    ResearchDecisionSnapshotRow(
                        id=snapshot.id,
                        board_id=snapshot.board_id,
                        refinement_id=snapshot.refinement_id,
                        refinement_version=snapshot.refinement_version,
                        heads_json=[
                            {
                                "ledger_id": head.ledger_id,
                                "entry_id": head.entry_id,
                                "head_revision": head.head_revision,
                                "head_refinement_version": (
                                    head.head_refinement_version
                                ),
                                "status": head.status.value,
                                "content_digest": head.content_digest,
                            }
                            for head in snapshot.heads
                        ],
                        created_at=snapshot.created_at,
                    )
                )
                await self._session.flush()
        except IntegrityError as exc:
            existing = await self.get_snapshot_for_version(
                board_id=snapshot.board_id,
                refinement_id=snapshot.refinement_id,
                refinement_version=snapshot.refinement_version,
            )
            if existing is not None and existing.heads == snapshot.heads:
                return existing
            raise ResearchDecisionImmutableEntryConflict(
                "research_decision_snapshot_conflict"
            ) from exc
        except SQLAlchemyError as exc:
            raise ResearchDecisionPersistenceError(str(exc)) from exc
        return snapshot

    async def save_derivation(
        self,
        derivation: ResearchDecisionDerivation,
    ) -> ResearchDecisionDerivation:
        if not isinstance(derivation, ResearchDecisionDerivation):
            raise ResearchDecisionPersistenceError(
                "research_decision_derivation_invalid"
            )
        await self._validate_content_digests(
            board_id=derivation.board_id,
            refinement_id=derivation.source_refinement_id,
            references=derivation.references,
        )
        existing = await self.get_derivation(
            board_id=derivation.board_id,
            spec_id=derivation.spec_id,
            spec_version=derivation.spec_version,
        )
        if existing is not None:
            if existing == derivation:
                return existing
            raise ResearchDecisionImmutableEntryConflict(
                "research_decision_derivation_conflict"
            )
        references = [
            {
                "source_snapshot_id": reference.source_snapshot_id,
                "source_refinement_id": reference.source_refinement_id,
                "source_refinement_version": (
                    reference.source_refinement_version
                ),
                "ledger_id": reference.ledger_id,
                "entry_id": reference.entry_id,
                "head_revision": reference.head_revision,
                "status": reference.status.value,
                "content_digest": reference.content_digest,
            }
            for reference in derivation.references
        ]
        try:
            async with self._session.begin_nested():
                self._session.add(
                    ResearchDecisionDerivationRow(
                        board_id=derivation.board_id,
                        spec_id=derivation.spec_id,
                        spec_version=derivation.spec_version,
                        source_refinement_id=(
                            derivation.source_refinement_id
                        ),
                        source_refinement_version=(
                            derivation.source_refinement_version
                        ),
                        source_snapshot_id=derivation.source_snapshot_id,
                        references_json=references,
                    )
                )
                await self._session.flush()
        except IntegrityError as exc:
            existing = await self.get_derivation(
                board_id=derivation.board_id,
                spec_id=derivation.spec_id,
                spec_version=derivation.spec_version,
            )
            if existing is not None and existing == derivation:
                return existing
            raise ResearchDecisionImmutableEntryConflict(
                "research_decision_derivation_conflict"
            ) from exc
        except SQLAlchemyError as exc:
            raise ResearchDecisionPersistenceError(str(exc)) from exc
        return derivation

    async def _resolve_bundle_replay(
        self,
        bundle: ResearchDecisionWriteBundle,
    ) -> ResearchDecisionCommitResult | None:
        if bundle.idempotency_key is None or bundle.request_digest is None:
            return None
        return await self.resolve_idempotent_result(
            board_id=bundle.entry.board_id,
            refinement_id=bundle.entry.refinement_id,
            idempotency_key=bundle.idempotency_key,
            request_digest=bundle.request_digest,
        )

    async def _assert_append_head_absent(
        self,
        bundle: ResearchDecisionWriteBundle,
    ) -> None:
        if bundle.expected_head_revision != 0:
            return
        existing = await self._session.scalar(
            select(ResearchDecisionHeadRow.ledger_id).where(
                ResearchDecisionHeadRow.ledger_id == bundle.entry.ledger_id
            )
        )
        if existing is not None:
            raise ResearchDecisionHeadRevisionConflict()

    async def _advance_refinement_version(
        self,
        bundle: ResearchDecisionWriteBundle,
    ) -> None:
        result = await self._session.execute(
            update(Refinement)
            .where(
                Refinement.id == bundle.entry.refinement_id,
                Refinement.board_id == bundle.entry.board_id,
                Refinement.version == bundle.version_bump.expected_version,
                Refinement.status == bundle.expected_refinement_status,
                Refinement.archived.is_(bundle.expected_refinement_archived),
            )
            .values(
                version=bundle.version_bump.resulting_version,
                updated_at=bundle.entry.created_at,
            )
            .execution_options(synchronize_session=False)
        )
        if int(result.rowcount or 0) == 1:
            return
        row = await self._session.scalar(
            select(Refinement).where(
                Refinement.id == bundle.entry.refinement_id,
            )
        )
        if row is None or row.board_id != bundle.entry.board_id:
            raise ResearchDecisionScopeConflict()
        if bool(row.archived) != bundle.expected_refinement_archived:
            raise ResearchDecisionRefinementLifecycleConflict()
        if row.status is not RefinementStatus.DRAFT:
            raise ResearchDecisionRefinementStatusConflict()
        raise ResearchDecisionRefinementVersionConflict()

    async def _advance_head(self, bundle: ResearchDecisionWriteBundle) -> None:
        if bundle.expected_head_revision == 0:
            self._session.add(
                ResearchDecisionHeadRow(
                    ledger_id=bundle.next_head.ledger_id,
                    board_id=bundle.next_head.board_id,
                    refinement_id=bundle.next_head.refinement_id,
                    current_entry_id=bundle.next_head.current_entry_id,
                    revision=bundle.next_head.revision,
                    refinement_version=bundle.next_head.refinement_version,
                    status=bundle.next_head.status.value,
                    updated_by=bundle.next_head.updated_by,
                    updated_at=bundle.next_head.updated_at,
                )
            )
            return
        result = await self._session.execute(
            update(ResearchDecisionHeadRow)
            .where(
                ResearchDecisionHeadRow.ledger_id == bundle.next_head.ledger_id,
                ResearchDecisionHeadRow.board_id == bundle.next_head.board_id,
                ResearchDecisionHeadRow.refinement_id
                == bundle.next_head.refinement_id,
                ResearchDecisionHeadRow.revision
                == bundle.expected_head_revision,
                ResearchDecisionHeadRow.current_entry_id
                == bundle.expected_head_entry_id,
            )
            .values(
                current_entry_id=bundle.next_head.current_entry_id,
                revision=bundle.next_head.revision,
                refinement_version=bundle.next_head.refinement_version,
                status=bundle.next_head.status.value,
                updated_by=bundle.next_head.updated_by,
                updated_at=bundle.next_head.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        if int(result.rowcount or 0) != 1:
            raise ResearchDecisionHeadRevisionConflict()


__all__ = ["CommunitySqlAlchemyResearchDecisionLedger"]
