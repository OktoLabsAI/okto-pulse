"""Bounded relational progress reads; cursors are scope checks, not authority."""

import base64
import binascii
import json

from sqlalchemy import and_, func, or_, select

from okto_pulse.community.adapters.sqlalchemy_models import CardDeliveryEvidenceRecordRow as Record
from okto_pulse.core.domain.delivery_evidence import CardDeliveryScope
from okto_pulse.core.domain.delivery_progress import DeliveryProgress, progress_change_scope


def progress_item(record, revoked, *, detail=False):
    payload = record.payload
    progress = DeliveryProgress.model_validate(payload["progress"])
    summary, remaining = payload["justification"], progress.remaining
    item = {
        "id": record.id, "actor_id": record.actor_id, "actor_kind": record.actor_kind,
        "created_at": record.created_at.isoformat(), "revoked": record.id in revoked,
        "summary": summary if detail else summary[:1000],
        "remaining": remaining if detail else remaining[:1000],
        "text_truncated": not detail and (len(summary) > 1000 or len(remaining) > 1000),
        "source_state": progress.source_state.model_dump(),
        "material_change": progress_change_scope(progress),
        "change_declaration_origin": progress.contract_version,
        "target_ids": progress.target_ids if detail else progress.target_ids[:10],
        "targets_truncated": not detail and len(progress.target_ids) > 10,
    }
    if detail:
        item["progress"] = payload["progress"]
    return item


async def read_progress_history(store, query, *, actor_id):
    session = store.session
    spec = await store._spec(query.board_id, query.spec_id)
    scope = CardDeliveryScope(query.board_id, query.card_id, query.spec_id, int(spec.edition))
    card, spec, _ = await store._card_scope_guard(scope)
    version = (spec.version, card.policy_version, card.status)
    generation = await store._delivery_revision(scope)
    binding = [query.board_id, query.card_id, query.spec_id, scope.spec_edition,
               actor_id, query.limit, generation, *version]
    filters = (Record.board_id == scope.board_id, Record.card_id == scope.card_id,
               Record.spec_id == scope.spec_id, Record.spec_edition == scope.spec_edition)
    progress_filters = (*filters, Record.kind == "progress")
    total = await session.scalar(select(func.count()).select_from(Record).where(*progress_filters))
    statement = select(Record).where(*progress_filters)
    if query.cursor:
        try:
            decoded = json.loads(base64.b64decode(query.cursor.encode("ascii"), altchars=b"-_", validate=True))
            if not isinstance(decoded, dict) or set(decoded) != {"v", "scope", "last"}:
                raise ValueError()
            if type(decoded["v"]) is not int or decoded["v"] != 1 or decoded["scope"] != binding:
                raise ValueError()
            if not isinstance(decoded["last"], str) or not 1 <= len(decoded["last"]) <= 512:
                raise ValueError()
            anchor = await session.scalar(statement.where(Record.id == decoded["last"]))
            if anchor is None:
                raise ValueError()
        except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
            raise ValueError("delivery_history_cursor_invalid_or_stale") from None
        statement = statement.where(or_(Record.created_at < anchor.created_at,
            and_(Record.created_at == anchor.created_at, Record.id < anchor.id)))
    if query.record_id:
        statement = statement.where(Record.id == query.record_id)
    records = list((await session.scalars(statement.order_by(
        Record.created_at.desc(), Record.id.desc()).limit(query.limit + 1 if not query.record_id else 1))).all())
    if query.record_id and not records:
        raise ValueError("delivery_history_record_unavailable")
    more = len(records) > query.limit
    records = records[:query.limit]
    revoked = set((await session.scalars(select(Record.payload["record_id"].as_string()).where(
        *filters, Record.kind == "revoke", Record.actor_kind.in_(("human", "user")),
        Record.payload["record_id"].as_string().in_([row.id for row in records]),
    ))).all()) if records else set()
    # A concurrent append/revoke or edition change must not yield a mixed page.
    card, spec, _ = await store._card_scope_guard(scope)
    if generation != await store._delivery_revision(scope) or version != (spec.version, card.policy_version, card.status):
        raise ValueError("delivery_history_changed_retry")
    cursor = None
    if more:
        cursor = base64.urlsafe_b64encode(json.dumps({"v": 1, "scope": binding,
            "last": records[-1].id}, separators=(",", ":")).encode()).decode()
    return {
        "board_id": scope.board_id, "card_id": scope.card_id, "spec_id": scope.spec_id,
        "edition": scope.spec_edition, "card_version": card.policy_version,
        "status": card.status, "delivery_revision": generation,
        "total": total, "next_cursor": cursor, "recovery_verified": False,
        "order": "newest_first", "detail": bool(query.record_id),
        "items": [progress_item(row, revoked, detail=bool(query.record_id)) for row in records],
    }
