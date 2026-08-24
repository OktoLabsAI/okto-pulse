"""Community SQLAlchemy structured spec store."""

from __future__ import annotations

import copy
from typing import Any, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified

from okto_pulse.community.adapters.sqlalchemy_models import (
    Card,
    CodeEvidenceRow,
    CodeEvidenceSpecLinkRow,
    ProjectStructureMutationReceiptRow,
    Spec,
)
from okto_pulse.core.domain.enums import CardType
from okto_pulse.core.ports.structured_spec import (
    ProjectStructureMutationReceipt,
    ProjectStructureMutationPersistenceResult,
    ProjectStructureMutationPersistenceState,
    StructuredSpecRecord,
)


_JSON_FIELDS = (
    "functional_requirements",
    "business_rules",
    "technical_requirements",
    "decisions",
    "acceptance_criteria",
    "api_contracts",
    "integration_requirements",
    "observability_requirements",
    "test_scenarios",
    "project_structure",
)


def _record(row: Any) -> StructuredSpecRecord:
    values = {
        # Preserve stored NULL versus an authored empty list. Requirement-lint
        # currentness is rederived from the persisted Spec, so normalizing an
        # untouched NULL here would make the structured writer stage a
        # different semantic snapshot and its receipt would be stale at birth.
        field_name: copy.deepcopy(getattr(row, field_name, None))
        for field_name in _JSON_FIELDS
    }
    return StructuredSpecRecord(
        id=str(row.id),
        board_id=str(row.board_id),
        status=row.status,
        version=int(row.version),
        archived=bool(row.archived),
        title=row.title,
        description=row.description,
        context=row.context,
        project_structure_revision=int(
            getattr(row, "project_structure_revision", None) or 0
        ),
        project_structure_digest=getattr(row, "project_structure_digest", None),
        **values,
    )


class CommunitySqlAlchemyStructuredSpecStore:
    async def get(
        self,
        context: Any,
        *,
        spec_id: str,
    ) -> StructuredSpecRecord | None:
        row = await context.get(Spec, spec_id)
        return _record(row) if row is not None else None

    async def save(
        self,
        context: Any,
        record: StructuredSpecRecord,
        *,
        changed_fields: Sequence[str],
        expected_version: int | None = None,
    ) -> None:
        row = await context.get(Spec, record.id)
        if row is None:
            raise LookupError(f"Spec {record.id!r} disappeared during mutation")
        if expected_version is not None and int(row.version) != expected_version:
            raise RuntimeError("structured_spec_version_conflict")
        for field_name in changed_fields:
            setattr(row, field_name, copy.deepcopy(getattr(record, field_name)))
            flag_modified(row, field_name)
        row.version = record.version
        await context.flush()

    async def get_project_structure_receipt(
        self,
        context: Any,
        *,
        spec_id: str,
        idempotency_key: str,
    ) -> ProjectStructureMutationReceipt | None:
        row = await context.get(
            ProjectStructureMutationReceiptRow,
            (spec_id, idempotency_key),
        )
        if row is None:
            return None
        return ProjectStructureMutationReceipt(
            spec_id=str(row.spec_id),
            idempotency_key=str(row.idempotency_key),
            request_digest=str(row.request_digest),
            result=copy.deepcopy(row.result),
        )

    async def save_project_structure_receipt(
        self,
        context: Any,
        receipt: ProjectStructureMutationReceipt,
    ) -> None:
        existing = await context.get(
            ProjectStructureMutationReceiptRow,
            (receipt.spec_id, receipt.idempotency_key),
        )
        if existing is not None:
            if (
                existing.request_digest != receipt.request_digest
                or existing.result != receipt.result
            ):
                raise RuntimeError("project_structure_idempotency_conflict")
            return
        context.add(
            ProjectStructureMutationReceiptRow(
                spec_id=receipt.spec_id,
                idempotency_key=receipt.idempotency_key,
                request_digest=receipt.request_digest,
                result=copy.deepcopy(receipt.result),
            )
        )
        try:
            await context.flush()
        except IntegrityError as exc:
            raise RuntimeError("project_structure_idempotency_conflict") from exc

    async def save_project_structure_mutation(
        self,
        context: Any,
        record: StructuredSpecRecord,
        *,
        expected_spec_version: int,
        expected_project_structure_revision: int,
        bump_spec_version: bool,
        changed_fields: Sequence[str],
        receipt: ProjectStructureMutationReceipt,
    ) -> ProjectStructureMutationPersistenceResult:
        allowed = {
            "project_structure",
            "project_structure_revision",
            "project_structure_digest",
        }
        changed = set(changed_fields)
        if changed not in (set(), allowed):
            raise ValueError("project_structure_changed_fields_invalid")
        existing = await self.get_project_structure_receipt(
            context,
            spec_id=receipt.spec_id,
            idempotency_key=receipt.idempotency_key,
        )
        if existing is not None:
            state = (
                ProjectStructureMutationPersistenceState.REPLAYED
                if existing.request_digest == receipt.request_digest
                else ProjectStructureMutationPersistenceState.IDEMPOTENCY_CONFLICT
            )
            return ProjectStructureMutationPersistenceResult(state, existing)
        expected_record_version = expected_spec_version + int(bump_spec_version)
        if record.version != expected_record_version:
            raise ValueError("project_structure_record_version_invalid")
        expected_record_revision = expected_project_structure_revision + int(
            bool(changed)
        )
        if record.project_structure_revision != expected_record_revision:
            raise ValueError("project_structure_record_revision_invalid")

        try:
            async with context.begin_nested():
                context.add(
                    ProjectStructureMutationReceiptRow(
                        spec_id=receipt.spec_id,
                        idempotency_key=receipt.idempotency_key,
                        request_digest=receipt.request_digest,
                        result=copy.deepcopy(receipt.result),
                    )
                )
                await context.flush()
                values: dict[str, Any]
                if changed:
                    values = {
                        "project_structure": copy.deepcopy(record.project_structure),
                        "project_structure_revision": record.project_structure_revision,
                        "project_structure_digest": record.project_structure_digest,
                        "version": record.version,
                        "updated_at": func.now(),
                    }
                else:
                    # A no-op batch still claims its receipt against the exact
                    # observed version, without creating false content/history
                    # currentness through an updated timestamp.
                    values = {"version": Spec.version}
                statement = (
                    update(Spec)
                    .where(
                        Spec.id == record.id,
                        Spec.version == expected_spec_version,
                        func.coalesce(Spec.project_structure_revision, 0)
                        == expected_project_structure_revision,
                    )
                    .values(**values)
                    .execution_options(synchronize_session=False)
                )
                updated = await context.execute(statement)
                if updated.rowcount != 1:
                    raise _ProjectStructureVersionConflict
        except _ProjectStructureVersionConflict:
            return ProjectStructureMutationPersistenceResult(
                ProjectStructureMutationPersistenceState.VERSION_CONFLICT
            )
        except IntegrityError:
            # The savepoint has been rolled back, leaving the caller's UoW
            # usable. Under READ COMMITTED the winning durable receipt is now
            # visible and determines exact replay versus key reuse conflict.
            winner = await self.get_project_structure_receipt(
                context,
                spec_id=receipt.spec_id,
                idempotency_key=receipt.idempotency_key,
            )
            if winner is None:
                return ProjectStructureMutationPersistenceResult(
                    ProjectStructureMutationPersistenceState.IDEMPOTENCY_CONFLICT
                )
            state = (
                ProjectStructureMutationPersistenceState.REPLAYED
                if winner.request_digest == receipt.request_digest
                else ProjectStructureMutationPersistenceState.IDEMPOTENCY_CONFLICT
            )
            return ProjectStructureMutationPersistenceResult(state, winner)
        return ProjectStructureMutationPersistenceResult(
            ProjectStructureMutationPersistenceState.APPLIED,
            copy.deepcopy(receipt),
        )

    async def validate_project_structure_references(
        self,
        context: Any,
        *,
        board_id: str,
        spec_id: str,
        task_ids: Sequence[str],
        test_ids: Sequence[str],
        evidence_ids: Sequence[str],
    ) -> None:
        task_set = {str(value) for value in task_ids}
        test_set = {str(value) for value in test_ids}
        if task_set & test_set:
            raise ValueError("project_structure_reference_role_conflict")
        card_ids = task_set | test_set
        if card_ids:
            rows = (
                await context.execute(
                    select(Card.id, Card.card_type).where(
                        Card.id.in_(card_ids),
                        Card.board_id == board_id,
                        Card.spec_id == spec_id,
                        Card.archived.is_(False),
                    )
                )
            ).all()
            cards = {str(row.id): row.card_type for row in rows}
            if set(cards) != card_ids:
                raise ValueError("project_structure_card_reference_invalid")
            if any(cards[test_id] != CardType.TEST for test_id in test_set):
                raise ValueError("project_structure_test_reference_invalid")
            if any(cards[task_id] != CardType.NORMAL for task_id in task_set):
                raise ValueError("project_structure_task_reference_invalid")
        evidence_set = {str(value) for value in evidence_ids}
        if evidence_set:
            direct = set(
                (
                    await context.execute(
                        select(CodeEvidenceRow.id).where(
                            CodeEvidenceRow.id.in_(evidence_set),
                            CodeEvidenceRow.board_id == board_id,
                            CodeEvidenceRow.spec_id == spec_id,
                            CodeEvidenceRow.lifecycle_status == "active",
                        )
                    )
                ).scalars()
            )
            linked = set(
                (
                    await context.execute(
                        select(CodeEvidenceSpecLinkRow.evidence_id)
                        .join(
                            CodeEvidenceRow,
                            CodeEvidenceRow.id
                            == CodeEvidenceSpecLinkRow.evidence_id,
                        )
                        .where(
                            CodeEvidenceSpecLinkRow.evidence_id.in_(evidence_set),
                            CodeEvidenceSpecLinkRow.board_id == board_id,
                            CodeEvidenceSpecLinkRow.spec_id == spec_id,
                            CodeEvidenceRow.board_id == board_id,
                            CodeEvidenceRow.lifecycle_status == "active",
                        )
                    )
                ).scalars()
            )
            if {str(value) for value in direct | linked} != evidence_set:
                raise ValueError("project_structure_evidence_reference_invalid")


class _ProjectStructureVersionConflict(Exception):
    """Rollback-only sentinel scoped to the atomic mutation savepoint."""


__all__ = ["CommunitySqlAlchemyStructuredSpecStore"]
