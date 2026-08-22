"""Permission-aware projection helpers for parent-list open-Q&A counts.

The count is useful list metadata, but it still discloses that questions exist.
Resolve all requested leaves once for the board, then either expose the exact
``answered_at IS NULL`` aggregate or omit it from the wire projection.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from okto_pulse.core.application.use_cases.authorization import (
    PermissionRequirement,
    decide_authorization,
    resolve_actor_permissions,
)


async def resolve_board_projection_permissions(
    *,
    actor: Any,
    uow: Any,
    board_id: str,
    permission_leaves: Iterable[str],
) -> dict[str, bool]:
    """Resolve a board's requested projection leaves in one permission lookup."""

    leaves = tuple(dict.fromkeys(permission_leaves))
    if not leaves:
        return {}
    permissions = await resolve_actor_permissions(actor, uow, board_id)
    return {
        leaf: decide_authorization(
            actor,
            PermissionRequirement(leaf),
            permissions,
        ).allowed
        for leaf in leaves
    }


def project_open_qa_count(
    values: Mapping[str, Any],
    *,
    can_read_qa: bool,
) -> dict[str, Any]:
    """Copy a row projection and disclose its Q&A count only when allowed."""

    projected = dict(values)
    if not can_read_qa:
        projected.pop("open_qa_count", None)
        return projected
    projected["open_qa_count"] = int(projected.get("open_qa_count") or 0)
    return projected


def redact_open_qa_count_records(
    records: Sequence[Any],
    *,
    can_read_qa: bool,
) -> Sequence[Any]:
    """Set a legacy record count to ``None`` so its schema omits the field."""

    if can_read_qa:
        return records
    for record in records:
        attach = getattr(record, "attach", None)
        if callable(attach):
            attach("open_qa_count", None)
        else:
            setattr(record, "open_qa_count", None)
    return records


__all__ = [
    "project_open_qa_count",
    "redact_open_qa_count_records",
    "resolve_board_projection_permissions",
]
