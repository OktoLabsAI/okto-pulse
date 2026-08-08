"""Community SQLAlchemy structured spec store."""

from __future__ import annotations

import copy
from typing import Any, Sequence

from sqlalchemy.orm.attributes import flag_modified

from okto_pulse.community.adapters.sqlalchemy_models import Spec
from okto_pulse.core.ports.structured_spec import StructuredSpecRecord


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
    ) -> None:
        row = await context.get(Spec, record.id)
        if row is None:
            raise LookupError(f"Spec {record.id!r} disappeared during mutation")
        for field_name in changed_fields:
            setattr(row, field_name, copy.deepcopy(getattr(record, field_name)))
            flag_modified(row, field_name)
        row.version = record.version
        await context.flush()


__all__ = ["CommunitySqlAlchemyStructuredSpecStore"]
