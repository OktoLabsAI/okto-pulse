"""Native transactional storage for planning-conclusion replay."""
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.models.schemas import SpecValidationConclusionResponse
from okto_pulse.core.ports.spec_validation_receipts import (
    SpecValidationIdempotencyConflict,
    SpecValidationOperationReceipt,
    SpecValidationReceiptScope,
)
from .sqlalchemy_models import Board, Spec, SpecValidationOperationReceiptRow


class CommunitySpecValidationReceiptStore:
    def __init__(self, session: AsyncSession, realm_scope: RealmScope):
        self._session = session
        self._realm_scope = realm_scope

    async def _require_scope(self, scope: SpecValidationReceiptScope) -> None:
        present = await self._session.scalar(
            select(Spec.id).join(Board, Board.id == Spec.board_id).where(
                Spec.id == scope.spec_id, Spec.board_id == scope.board_id,
                Board.realm_id == self._realm_scope.realm_id,
            )
        )
        if present is None:
            raise SpecValidationIdempotencyConflict("Spec receipt scope is unavailable.")

    async def get(self, scope: SpecValidationReceiptScope) -> SpecValidationOperationReceipt | None:
        await self._require_scope(scope)
        row = await self._session.get(SpecValidationOperationReceiptRow, (
            scope.board_id, scope.spec_id, scope.actor_id, scope.actor_kind, scope.idempotency_key,
        ))
        if row is None:
            return None
        return SpecValidationOperationReceipt(
            scope=scope, request_digest=row.request_digest,
            result=SpecValidationConclusionResponse.model_validate(row.result),
        )

    async def save(self, receipt: SpecValidationOperationReceipt) -> None:
        existing = await self.get(receipt.scope)
        if existing is not None:
            if existing != receipt:
                raise SpecValidationIdempotencyConflict("Idempotency key already used for another conclusion.")
            return
        scope = receipt.scope
        self._session.add(SpecValidationOperationReceiptRow(
            board_id=scope.board_id, spec_id=scope.spec_id,
            actor_id=scope.actor_id, actor_kind=scope.actor_kind,
            idempotency_key=scope.idempotency_key, request_digest=receipt.request_digest,
            result=receipt.result.model_dump(mode="json"),
        ))
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise SpecValidationIdempotencyConflict("Concurrent conclusion receipt; retry the original request.") from exc
