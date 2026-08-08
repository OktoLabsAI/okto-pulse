"""Community SQLAlchemy source-of-truth adapter for the A3 Spec checklist.

The adapter is transaction-bound: methods may flush to observe constraints,
but transaction ownership remains with the caller's unit of work.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.community.adapters.sqlalchemy_models import (
    ChecklistBindingHeadRow,
    ChecklistBindingRow,
    ChecklistExecutionHeadRow,
    ChecklistExecutionRow,
    ChecklistItemResultRow,
    ChecklistReceiptRow,
    ChecklistTemplateVersionRow,
    Spec,
    SpecQAItem,
)
from okto_pulse.community.adapters.ska_observability import (
    observed_ska_projection,
)
from okto_pulse.core.domain.checklist import (
    SPECIFY_CHECKLIST_TEMPLATE_V1,
    ChecklistBinding,
    ChecklistCommitResult,
    ChecklistExecution,
    ChecklistExecutionHead,
    ChecklistExecutionStartResult,
    ChecklistExecutionStatus,
    ChecklistItemOutcome,
    ChecklistItemResult,
    ChecklistMode,
    ChecklistPage,
    ChecklistPhase,
    ChecklistPreflight,
    ChecklistReceipt,
    ChecklistReceiptSource,
    ChecklistSpecSnapshot,
    ChecklistSubmission,
    ChecklistTargetType,
    ChecklistWriteBundle,
)
from okto_pulse.core.domain.quality_assessment import AssessmentSubjectType
from okto_pulse.core.domain.quality_canonicalization import (
    SEMANTIC_FIELD_MANIFEST_V1,
    canonical_sha256,
    clarification_digest_v1,
    semantic_content_digest_v1,
)
from okto_pulse.core.ports.checklist import (
    ChecklistBindingConflict,
    ChecklistContentDigestConflict,
    ChecklistHeadRevisionConflict,
    ChecklistIdempotencyConflict,
    ChecklistInputDigestConflict,
    ChecklistListQuery,
    ChecklistPersistenceError,
    ChecklistSpecLifecycleConflict,
    ChecklistSpecVersionConflict,
    ChecklistTemplateConflict,
    ChecklistExecutionRevisionConflict,
    ChecklistExecutionStateConflict,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _template_items_payload() -> list[dict[str, object]]:
    return [
        {
            "item_id": item.item_id,
            "title_en": item.title_en,
            "title_pt": item.title_pt,
            "description_en": item.description_en,
            "description_pt": item.description_pt,
            "allow_na": item.allow_na,
        }
        for item in SPECIFY_CHECKLIST_TEMPLATE_V1.items
    ]


def _qa_payload(row: SpecQAItem) -> dict[str, object]:
    return {
        "id": row.id,
        "revision": 1,
        "question": row.question,
        "question_type": row.question_type,
        "choices": row.choices or [],
        "allow_free_text": bool(row.allow_free_text),
        "answer": row.answer,
        "selected": row.selected or [],
        "answered_at": row.answered_at,
        "lifecycle": "active",
        "tombstoned": False,
    }


class CommunitySqlAlchemyChecklist:
    """Concrete append-only A3 persistence with transaction-local CAS."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _ensure_template(self) -> None:
        template = SPECIFY_CHECKLIST_TEMPLATE_V1
        row = await self._session.get(ChecklistTemplateVersionRow, template.version)
        if row is None:
            self._session.add(
                ChecklistTemplateVersionRow(
                    version=template.version,
                    template_id=template.template_id,
                    digest=template.digest,
                    items_json=_template_items_payload(),
                    created_at=_now(),
                )
            )
            try:
                await self._session.flush()
            except IntegrityError as exc:
                raise ChecklistTemplateConflict(
                    "checklist_template_insert_conflict"
                ) from exc
            return
        if (
            row.template_id != template.template_id
            or row.digest != template.digest
            or row.items_json != _template_items_payload()
        ):
            raise ChecklistTemplateConflict("checklist_template_identity_conflict")

    async def get_spec_snapshot(
        self,
        *,
        board_id: str,
        spec_id: str,
    ) -> ChecklistSpecSnapshot | None:
        row = (
            await self._session.execute(
                select(Spec).where(
                    Spec.id == spec_id,
                    Spec.board_id == board_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        payload = {
            field: getattr(row, field, None)
            for field in SEMANTIC_FIELD_MANIFEST_V1[
                AssessmentSubjectType.SPEC.value
            ]
        }
        qa_rows = list(
            (
                await self._session.execute(
                    select(SpecQAItem)
                    .where(SpecQAItem.spec_id == spec_id)
                    .order_by(SpecQAItem.id)
                )
            )
            .scalars()
            .all()
        )
        content_digest = semantic_content_digest_v1(
            AssessmentSubjectType.SPEC,
            payload,
        )
        clarification_digest = clarification_digest_v1(
            tuple(_qa_payload(item) for item in qa_rows)
        )
        input_digest = canonical_sha256(
            {
                "contract_version": "checklist-spec-input/v1",
                "content_digest": content_digest,
                "clarification_digest": clarification_digest,
            }
        )
        return ChecklistSpecSnapshot(
            board_id=row.board_id,
            spec_id=row.id,
            spec_version=row.version,
            content_digest=content_digest,
            input_digest=input_digest,
            status=_enum_value(row.status),
            archived=bool(row.archived),
        )

    async def get_binding(
        self,
        *,
        board_id: str,
        target_type: ChecklistTargetType,
        phase: ChecklistPhase,
    ) -> ChecklistBinding | None:
        head = (
            await self._session.execute(
                select(ChecklistBindingHeadRow).where(
                    ChecklistBindingHeadRow.board_id == board_id,
                    ChecklistBindingHeadRow.target_type == target_type.value,
                    ChecklistBindingHeadRow.phase == phase.value,
                )
            )
        ).scalar_one_or_none()
        if head is None:
            return None
        row = (
            await self._session.execute(
                select(ChecklistBindingRow).where(
                    ChecklistBindingRow.board_id == board_id,
                    ChecklistBindingRow.target_type == target_type.value,
                    ChecklistBindingRow.phase == phase.value,
                    ChecklistBindingRow.version == head.version,
                )
            )
        ).scalar_one_or_none()
        if row is None or row.digest != head.digest:
            raise ChecklistPersistenceError("checklist_binding_head_corrupt")
        try:
            return ChecklistBinding(
                board_id=row.board_id,
                target_type=ChecklistTargetType(row.target_type),
                phase=ChecklistPhase(row.phase),
                template_version=row.template_version,
                mode=ChecklistMode(row.mode),
                version=row.version,
                revision=head.version,
                digest=row.digest,
            )
        except (TypeError, ValueError) as exc:
            raise ChecklistPersistenceError("checklist_binding_row_corrupt") from exc

    async def apply_binding_cas(
        self,
        binding: ChecklistBinding,
        *,
        expected_version: int,
        expected_digest: str | None,
    ) -> ChecklistBinding:
        await self._ensure_template()
        if binding.revision != binding.version:
            raise ChecklistBindingConflict(
                "checklist_binding_synthetic_persist_forbidden"
            )
        head = (
            await self._session.execute(
                select(ChecklistBindingHeadRow).where(
                    ChecklistBindingHeadRow.board_id == binding.board_id,
                    ChecklistBindingHeadRow.target_type
                    == binding.target_type.value,
                    ChecklistBindingHeadRow.phase == binding.phase.value,
                )
            )
        ).scalar_one_or_none()
        if expected_version == 0:
            if head is not None or expected_digest is not None or binding.version != 1:
                raise ChecklistBindingConflict(
                    "checklist_binding_initial_conflict"
                )
        elif (
            head is None
            or head.version != expected_version
            or head.digest != expected_digest
            or binding.version != expected_version + 1
        ):
            raise ChecklistBindingConflict("checklist_binding_predecessor_conflict")

        now = _now()
        self._session.add(
            ChecklistBindingRow(
                board_id=binding.board_id,
                target_type=binding.target_type.value,
                phase=binding.phase.value,
                version=binding.version,
                digest=binding.digest,
                mode=binding.mode.value,
                template_version=binding.template_version,
                created_at=now,
            )
        )
        # Flush the immutable predecessor before inserting/updating the head.
        # The ORM has no relationship edge between these deliberately plain
        # rows, so relying on unit-of-work ordering is not portable.
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise ChecklistBindingConflict(
                "checklist_binding_insert_conflict"
            ) from exc
        if head is None:
            self._session.add(
                ChecklistBindingHeadRow(
                    board_id=binding.board_id,
                    target_type=binding.target_type.value,
                    phase=binding.phase.value,
                    version=binding.version,
                    digest=binding.digest,
                    updated_at=now,
                )
            )
        else:
            head.version = binding.version
            head.digest = binding.digest
            head.updated_at = now
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise ChecklistBindingConflict("checklist_binding_cas_conflict") from exc
        return binding

    async def start_execution_cas(
        self,
        execution: ChecklistExecution,
    ) -> ChecklistExecutionStartResult:
        existing = (
            await self._session.execute(
                select(ChecklistExecutionRow).where(
                    ChecklistExecutionRow.board_id == execution.board_id,
                    ChecklistExecutionRow.idempotency_key
                    == execution.idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.request_digest != execution.request_digest:
                raise ChecklistIdempotencyConflict(
                    "checklist_execution_idempotency_conflict"
                )
            return ChecklistExecutionStartResult(
                execution=self._execution(existing),
                replayed=True,
            )

        await self._validate_execution_fences(execution)
        await self._ensure_template()
        self._session.add(
            ChecklistExecutionRow(
                id=execution.id,
                board_id=execution.board_id,
                spec_id=execution.spec_id,
                spec_version=execution.spec_version,
                content_digest=execution.content_digest,
                input_digest=execution.input_digest,
                template_version=execution.template_version,
                template_digest=execution.template_digest,
                binding_version=execution.binding_version,
                binding_digest=execution.binding_digest,
                binding_mode=execution.binding_mode.value,
                request_digest=execution.request_digest,
                idempotency_key=execution.idempotency_key,
                created_by=execution.created_by,
                created_at=execution.created_at,
                revision=execution.revision,
                status=execution.status.value,
                receipt_id=execution.receipt_id,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise ChecklistIdempotencyConflict(
                "checklist_execution_insert_conflict"
            ) from exc
        return ChecklistExecutionStartResult(execution=execution)

    async def _validate_execution_fences(
        self,
        execution: ChecklistExecution,
    ) -> None:
        snapshot = await self.get_spec_snapshot(
            board_id=execution.board_id,
            spec_id=execution.spec_id,
        )
        if snapshot is None:
            raise ChecklistSpecVersionConflict("checklist_spec_missing")
        if snapshot.archived:
            raise ChecklistSpecLifecycleConflict("checklist_spec_archived")
        if snapshot.spec_version != execution.spec_version:
            raise ChecklistSpecVersionConflict("checklist_spec_version_mismatch")
        if snapshot.content_digest != execution.content_digest:
            raise ChecklistContentDigestConflict(
                "checklist_content_digest_mismatch"
            )
        if snapshot.input_digest != execution.input_digest:
            raise ChecklistInputDigestConflict("checklist_input_digest_mismatch")
        binding = await self.get_binding(
            board_id=execution.board_id,
            target_type=ChecklistTargetType.SPEC,
            phase=ChecklistPhase.SPEC_VALIDATION,
        )
        # Mode/version are governance observations recorded on the execution,
        # not semantic fences.  A current OFF mode still closes admission.
        if (
            binding is None
            or binding.mode is ChecklistMode.OFF
            or binding.digest != execution.binding_digest
            or binding.template_version != execution.template_version
            or execution.template_digest != SPECIFY_CHECKLIST_TEMPLATE_V1.digest
        ):
            raise ChecklistBindingConflict("checklist_binding_fence_mismatch")

    def _execution(self, row: ChecklistExecutionRow) -> ChecklistExecution:
        try:
            return ChecklistExecution(
                id=row.id,
                board_id=row.board_id,
                spec_id=row.spec_id,
                spec_version=row.spec_version,
                content_digest=row.content_digest,
                input_digest=row.input_digest,
                template_version=row.template_version,
                template_digest=row.template_digest,
                binding_version=row.binding_version,
                binding_digest=row.binding_digest,
                binding_mode=ChecklistMode(row.binding_mode),
                request_digest=row.request_digest,
                idempotency_key=row.idempotency_key,
                created_by=row.created_by,
                created_at=row.created_at,
                revision=row.revision,
                status=ChecklistExecutionStatus(row.status),
                receipt_id=row.receipt_id,
            )
        except (TypeError, ValueError) as exc:
            raise ChecklistPersistenceError(
                "checklist_execution_row_corrupt"
            ) from exc

    async def get_execution(
        self,
        *,
        board_id: str,
        spec_id: str,
        execution_id: str,
    ) -> ChecklistExecution | None:
        row = (
            await self._session.execute(
                select(ChecklistExecutionRow).where(
                    ChecklistExecutionRow.id == execution_id,
                    ChecklistExecutionRow.board_id == board_id,
                    ChecklistExecutionRow.spec_id == spec_id,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else self._execution(row)

    async def submit_execution_cas(
        self,
        execution: ChecklistExecution,
        *,
        expected_revision: int,
        bundle: ChecklistWriteBundle,
    ) -> ChecklistCommitResult:
        row = (
            await self._session.execute(
                select(ChecklistExecutionRow).where(
                    ChecklistExecutionRow.id == execution.id,
                    ChecklistExecutionRow.board_id == execution.board_id,
                    ChecklistExecutionRow.spec_id == execution.spec_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise ChecklistExecutionStateConflict("checklist_execution_missing")
        if row.status == ChecklistExecutionStatus.SUBMITTED.value:
            receipt = (
                await self._session.execute(
                    select(ChecklistReceiptRow).where(
                        ChecklistReceiptRow.execution_id == row.id
                    )
                )
            ).scalar_one_or_none()
            if (
                receipt is None
                or receipt.request_digest != bundle.request_digest
                or receipt.idempotency_key != bundle.receipt.idempotency_key
            ):
                raise ChecklistExecutionStateConflict(
                    "checklist_execution_already_submitted"
                )
            return self._commit_result(receipt, replayed=True)
        if row.status != ChecklistExecutionStatus.OPEN.value:
            raise ChecklistExecutionStateConflict(
                "checklist_execution_state_conflict"
            )
        if row.revision != expected_revision:
            raise ChecklistExecutionRevisionConflict(
                "checklist_execution_revision_conflict"
            )
        if self._execution(row) != execution:
            raise ChecklistExecutionStateConflict(
                "checklist_execution_identity_conflict"
            )

        result = await self._apply_receipt_bundle(
            bundle,
            execution_id=execution.id,
        )
        row.status = ChecklistExecutionStatus.SUBMITTED.value
        row.revision += 1
        row.receipt_id = result.receipt_id
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise ChecklistExecutionStateConflict(
                "checklist_execution_submit_conflict"
            ) from exc
        return result

    async def apply_execution_cas(
        self,
        bundle: ChecklistWriteBundle,
    ) -> ChecklistCommitResult:
        return await self._apply_receipt_bundle(bundle, execution_id=None)

    async def _apply_receipt_bundle(
        self,
        bundle: ChecklistWriteBundle,
        *,
        execution_id: str | None,
    ) -> ChecklistCommitResult:
        receipt = bundle.receipt
        if receipt.idempotency_key is not None:
            existing = (
                await self._session.execute(
                    select(ChecklistReceiptRow).where(
                        ChecklistReceiptRow.board_id == receipt.board_id,
                        ChecklistReceiptRow.idempotency_key
                        == receipt.idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if (
                    existing.request_digest != bundle.request_digest
                    or existing.execution_id != execution_id
                ):
                    raise ChecklistIdempotencyConflict(
                        "checklist_receipt_idempotency_conflict"
                    )
                return self._commit_result(existing, replayed=True)

        await self._validate_bundle_fences(bundle)
        self._session.add(
            ChecklistReceiptRow(
                id=receipt.id,
                board_id=receipt.board_id,
                spec_id=receipt.spec_id,
                execution_id=execution_id,
                spec_version=receipt.spec_version,
                content_digest=receipt.content_digest,
                input_digest=receipt.input_digest,
                template_version=receipt.template_version,
                template_digest=receipt.template_digest,
                binding_version=receipt.binding_version,
                binding_digest=receipt.binding_digest,
                binding_mode=receipt.binding_mode.value,
                source=receipt.source.value,
                request_digest=receipt.request_digest,
                idempotency_key=receipt.idempotency_key,
                manual_checklist_ref=receipt.manual_checklist_ref,
                predecessor_receipt_id=receipt.predecessor_receipt_id,
                created_by=receipt.created_by,
                created_at=receipt.created_at,
                head_revision=receipt.head_revision,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise ChecklistIdempotencyConflict(
                "checklist_receipt_insert_conflict"
            ) from exc
        for index, item in enumerate(receipt.items):
            self._session.add(
                ChecklistItemResultRow(
                    receipt_id=receipt.id,
                    item_id=item.item_id,
                    execution_id=execution_id,
                    outcome=item.outcome.value,
                    anchor=item.anchor,
                    rationale=item.rationale,
                    order_index=index,
                )
            )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise ChecklistIdempotencyConflict(
                "checklist_item_result_insert_conflict"
            ) from exc
        await self._advance_head(bundle)
        return ChecklistCommitResult(
            board_id=receipt.board_id,
            spec_id=receipt.spec_id,
            spec_version=receipt.spec_version,
            receipt_id=receipt.id,
            request_digest=receipt.request_digest,
            head_revision=receipt.head_revision,
        )

    async def _validate_bundle_fences(
        self,
        bundle: ChecklistWriteBundle,
    ) -> None:
        receipt = bundle.receipt
        snapshot = await self.get_spec_snapshot(
            board_id=receipt.board_id,
            spec_id=receipt.spec_id,
        )
        if snapshot is None:
            raise ChecklistSpecVersionConflict("checklist_spec_missing")
        if (
            snapshot.archived != bundle.expected_spec_archived
            or snapshot.status != bundle.expected_spec_status
        ):
            raise ChecklistSpecLifecycleConflict(
                "checklist_spec_lifecycle_mismatch"
            )
        if snapshot.spec_version != bundle.expected_spec_version:
            raise ChecklistSpecVersionConflict("checklist_spec_version_mismatch")
        if snapshot.content_digest != bundle.expected_content_digest:
            raise ChecklistContentDigestConflict(
                "checklist_content_digest_mismatch"
            )
        if snapshot.input_digest != bundle.expected_input_digest:
            raise ChecklistInputDigestConflict("checklist_input_digest_mismatch")
        if (
            bundle.expected_template_version
            != SPECIFY_CHECKLIST_TEMPLATE_V1.version
            or bundle.expected_template_digest
            != SPECIFY_CHECKLIST_TEMPLATE_V1.digest
        ):
            raise ChecklistTemplateConflict("checklist_template_fence_mismatch")
        await self._ensure_template()
        binding = await self.get_binding(
            board_id=receipt.board_id,
            target_type=ChecklistTargetType.SPEC,
            phase=ChecklistPhase.SPEC_VALIDATION,
        )
        # The immutable receipt keeps the preflight mode/version for audit.
        # Only executable identity (digest) and a current non-OFF policy fence
        # admission against a concurrent governance revision.
        if (
            binding is None
            or binding.mode is ChecklistMode.OFF
            or binding.digest != bundle.expected_binding_digest
        ):
            raise ChecklistBindingConflict("checklist_binding_fence_mismatch")
        head = (
            await self._session.execute(
                select(ChecklistExecutionHeadRow).where(
                    ChecklistExecutionHeadRow.board_id == receipt.board_id,
                    ChecklistExecutionHeadRow.spec_id == receipt.spec_id,
                    ChecklistExecutionHeadRow.phase
                    == ChecklistPhase.SPEC_VALIDATION.value,
                )
            )
        ).scalar_one_or_none()
        if bundle.expected_head_revision == 0:
            if head is not None or bundle.expected_head_receipt_id is not None:
                raise ChecklistHeadRevisionConflict(
                    "checklist_head_initial_conflict"
                )
        elif (
            head is None
            or head.revision != bundle.expected_head_revision
            or head.receipt_id != bundle.expected_head_receipt_id
            or receipt.predecessor_receipt_id
            != bundle.expected_head_receipt_id
        ):
            raise ChecklistHeadRevisionConflict(
                "checklist_head_predecessor_conflict"
            )

    async def _advance_head(self, bundle: ChecklistWriteBundle) -> None:
        next_head = bundle.next_head
        head = (
            await self._session.execute(
                select(ChecklistExecutionHeadRow).where(
                    ChecklistExecutionHeadRow.board_id == next_head.board_id,
                    ChecklistExecutionHeadRow.spec_id == next_head.spec_id,
                    ChecklistExecutionHeadRow.phase == next_head.phase.value,
                )
            )
        ).scalar_one_or_none()
        if head is None:
            if bundle.expected_head_revision != 0:
                raise ChecklistHeadRevisionConflict("checklist_head_missing")
            self._session.add(
                ChecklistExecutionHeadRow(
                    board_id=next_head.board_id,
                    spec_id=next_head.spec_id,
                    phase=next_head.phase.value,
                    receipt_id=next_head.receipt_id,
                    revision=next_head.revision,
                    updated_at=next_head.updated_at,
                )
            )
        else:
            if (
                head.revision != bundle.expected_head_revision
                or head.receipt_id != bundle.expected_head_receipt_id
            ):
                raise ChecklistHeadRevisionConflict(
                    "checklist_head_advance_conflict"
                )
            head.receipt_id = next_head.receipt_id
            head.revision = next_head.revision
            head.updated_at = next_head.updated_at
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise ChecklistHeadRevisionConflict(
                "checklist_head_cas_conflict"
            ) from exc

    async def get_current(
        self,
        *,
        board_id: str,
        spec_id: str,
        phase: ChecklistPhase,
    ) -> tuple[ChecklistReceipt, ChecklistExecutionHead] | None:
        row = (
            await self._session.execute(
                select(ChecklistExecutionHeadRow).where(
                    ChecklistExecutionHeadRow.board_id == board_id,
                    ChecklistExecutionHeadRow.spec_id == spec_id,
                    ChecklistExecutionHeadRow.phase == phase.value,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        receipt = await self.get_receipt(
            board_id=board_id,
            receipt_id=row.receipt_id,
        )
        if receipt is None:
            raise ChecklistPersistenceError("checklist_head_receipt_missing")
        return (
            receipt,
            ChecklistExecutionHead(
                board_id=row.board_id,
                spec_id=row.spec_id,
                phase=ChecklistPhase(row.phase),
                receipt_id=row.receipt_id,
                revision=row.revision,
                updated_at=row.updated_at,
            ),
        )

    async def get_receipt(
        self,
        *,
        board_id: str,
        receipt_id: str,
    ) -> ChecklistReceipt | None:
        row = (
            await self._session.execute(
                select(ChecklistReceiptRow).where(
                    ChecklistReceiptRow.id == receipt_id,
                    ChecklistReceiptRow.board_id == board_id,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else await self._receipt(row)

    @staticmethod
    def _receipt_from_rows(
        row: ChecklistReceiptRow,
        item_rows: list[ChecklistItemResultRow]
        | tuple[ChecklistItemResultRow, ...],
    ) -> ChecklistReceipt:
        try:
            return ChecklistReceipt(
                id=row.id,
                board_id=row.board_id,
                spec_id=row.spec_id,
                spec_version=row.spec_version,
                content_digest=row.content_digest,
                input_digest=row.input_digest,
                template_version=row.template_version,
                template_digest=row.template_digest,
                binding_version=row.binding_version,
                binding_digest=row.binding_digest,
                binding_mode=ChecklistMode(row.binding_mode),
                items=tuple(
                    ChecklistItemResult(
                        item_id=item.item_id,
                        outcome=ChecklistItemOutcome(item.outcome),
                        anchor=item.anchor,
                        rationale=item.rationale,
                    )
                    for item in item_rows
                ),
                source=ChecklistReceiptSource(row.source),
                request_digest=row.request_digest,
                created_by=row.created_by,
                created_at=row.created_at,
                head_revision=row.head_revision,
                idempotency_key=row.idempotency_key,
                manual_checklist_ref=row.manual_checklist_ref,
                predecessor_receipt_id=row.predecessor_receipt_id,
            )
        except (TypeError, ValueError) as exc:
            raise ChecklistPersistenceError("checklist_receipt_row_corrupt") from exc

    async def _receipt(self, row: ChecklistReceiptRow) -> ChecklistReceipt:
        item_rows = list(
            (
                await self._session.execute(
                    select(ChecklistItemResultRow)
                    .where(ChecklistItemResultRow.receipt_id == row.id)
                    .order_by(ChecklistItemResultRow.order_index)
                )
            )
            .scalars()
            .all()
        )
        return self._receipt_from_rows(row, item_rows)

    async def lookup_checklist_replay(
        self,
        *,
        board_id: str,
        idempotency_key: str,
        actor_id: str,
    ) -> ChecklistCommitResult | None:
        row = (
            await self._session.execute(
                select(ChecklistReceiptRow).where(
                    ChecklistReceiptRow.board_id == board_id,
                    ChecklistReceiptRow.idempotency_key == idempotency_key,
                    ChecklistReceiptRow.created_by == actor_id,
                    ChecklistReceiptRow.source
                    == ChecklistReceiptSource.NATIVE.value,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else self._commit_result(row, replayed=True)

    async def resolve_checklist_preflight(
        self,
        submission: ChecklistSubmission,
        *,
        actor_id: str,
    ) -> ChecklistPreflight:
        del actor_id
        snapshot = await self.get_spec_snapshot(
            board_id=submission.board_id,
            spec_id=submission.spec_id,
        )
        if snapshot is None:
            raise ChecklistSpecVersionConflict("checklist_spec_missing")
        binding = await self.get_binding(
            board_id=submission.board_id,
            target_type=ChecklistTargetType.SPEC,
            phase=ChecklistPhase.SPEC_VALIDATION,
        )
        if binding is None:
            binding = ChecklistBinding.synthetic_off(
                board_id=submission.board_id,
            )
        current = await self.get_current(
            board_id=submission.board_id,
            spec_id=submission.spec_id,
            phase=ChecklistPhase.SPEC_VALIDATION,
        )
        return ChecklistPreflight(
            subject=snapshot,
            binding=binding,
            current_head_revision=0 if current is None else current[1].revision,
            current_head_receipt_id=(
                None if current is None else current[1].receipt_id
            ),
        )

    @observed_ska_projection(
        surface="checklist_history",
        subject_type="spec",
        query_count=3,
    )
    async def list_executions(
        self,
        query: ChecklistListQuery,
    ) -> ChecklistPage[ChecklistReceipt]:
        where = (
            ChecklistReceiptRow.board_id == query.board_id,
            ChecklistReceiptRow.spec_id == query.spec_id,
        )
        total = int(
            (
                await self._session.execute(
                    select(func.count(ChecklistReceiptRow.id)).where(*where)
                )
            ).scalar_one()
        )
        rows = list(
            (
                await self._session.execute(
                    select(ChecklistReceiptRow)
                    .where(*where)
                    .order_by(
                        ChecklistReceiptRow.created_at.desc(),
                        ChecklistReceiptRow.id.desc(),
                    )
                    .offset(query.offset)
                    .limit(query.limit)
                )
            )
            .scalars()
            .all()
        )
        items_by_receipt: dict[str, list[ChecklistItemResultRow]] = defaultdict(
            list
        )
        item_rows = (
            (
                await self._session.execute(
                    select(ChecklistItemResultRow)
                    .where(
                        ChecklistItemResultRow.receipt_id.in_(
                            tuple(row.id for row in rows)
                        )
                    )
                    .order_by(
                        ChecklistItemResultRow.receipt_id,
                        ChecklistItemResultRow.order_index,
                    )
                )
            )
            .scalars()
            .all()
        )
        for item_row in item_rows:
            items_by_receipt[item_row.receipt_id].append(item_row)
        return ChecklistPage(
            items=tuple(
                self._receipt_from_rows(
                    row,
                    items_by_receipt.get(row.id, ()),
                )
                for row in rows
            ),
            total=total,
            offset=query.offset,
            limit=query.limit,
        )

    @staticmethod
    def _commit_result(
        row: ChecklistReceiptRow,
        *,
        replayed: bool,
    ) -> ChecklistCommitResult:
        return ChecklistCommitResult(
            board_id=row.board_id,
            spec_id=row.spec_id,
            spec_version=row.spec_version,
            receipt_id=row.id,
            request_digest=row.request_digest,
            head_revision=row.head_revision,
            replayed=replayed,
        )


__all__ = ["CommunitySqlAlchemyChecklist"]
