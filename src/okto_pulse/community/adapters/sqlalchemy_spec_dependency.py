"""SQLAlchemy authority for operational precedence between Specs (SK-M)."""

from __future__ import annotations

import base64
import json
import logging
from time import perf_counter
import uuid
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    Spec,
    SpecDependencyBoardLock,
    SpecDependency,
    SpecDependencyOperation,
)
from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    statement_budget,
)
from okto_pulse.core.domain.datetime_utils import normalize_utc_datetime
from okto_pulse.core.domain.enums import SpecStatus
from okto_pulse.core.domain.spec_dependency import (
    SpecDependencyBlocker,
    SpecDependencyCapabilities,
    SpecDependencyDirection,
    SpecDependencyLifecycleFilter,
    SpecDependencyLineageFilter,
    SpecDependencyListItem,
    SpecDependencyListQuery,
    SpecDependencyMutationReceipt,
    SpecDependencyOperationError,
    SpecDependencyPage,
    SpecDependencyReadiness,
    SpecDependencyRecord,
    SpecDependencySatisfactionFilter,
    SpecDependencySpecSnapshot,
    normalize_spec_dependency_blocker_limit,
    spec_dependency_is_satisfied,
)


logger = logging.getLogger(__name__)


def _status(value: object) -> SpecStatus:
    return value if isinstance(value, SpecStatus) else SpecStatus(str(value))


def _snapshot(row: Spec) -> SpecDependencySpecSnapshot:
    return SpecDependencySpecSnapshot(
        id=str(row.id),
        board_id=str(row.board_id),
        title=str(row.title),
        status=_status(row.status),
        edition=int(row.edition),
        version=int(row.version),
        archived=bool(row.archived),
        ideation_id=str(row.ideation_id) if row.ideation_id else None,
        last_started_edition=(
            int(row.last_started_edition)
            if row.last_started_edition is not None
            else None
        ),
    )


def _record(row: SpecDependency) -> SpecDependencyRecord:
    return SpecDependencyRecord(
        id=str(row.id),
        board_id=str(row.board_id),
        source_spec_id=str(row.dependent_spec_id),
        target_spec_id=str(row.prerequisite_spec_ref),
        created_at=normalize_utc_datetime(row.created_at),
        created_by=str(row.created_by_id),
        created_by_type=str(row.created_by_type),
        created_by_name=str(row.created_by_name),
        source_version_on_create=int(row.source_version_on_create),
        source_status_on_create=_status(row.source_status_on_create),
        target_status_on_create=_status(row.target_status_on_create),
        target_version_on_create=int(row.target_version_on_create),
        resolved_on_create=bool(row.resolved_on_create),
        retrospective=bool(row.retrospective),
        removed_at=(
            normalize_utc_datetime(row.removed_at)
            if row.removed_at is not None
            else None
        ),
        removed_by=str(row.removed_by_id) if row.removed_by_id else None,
        removed_by_type=(str(row.removed_by_type) if row.removed_by_type else None),
        removed_by_name=(str(row.removed_by_name) if row.removed_by_name else None),
        removal_reason=str(row.removal_reason) if row.removal_reason else None,
        source_version_on_remove=(
            int(row.removed_at_spec_version)
            if row.removed_at_spec_version is not None
            else None
        ),
        add_idempotency_key=str(row.add_idempotency_key),
        remove_idempotency_key=(
            str(row.remove_idempotency_key) if row.remove_idempotency_key else None
        ),
    )


def _record_payload(record: SpecDependencyRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "board_id": record.board_id,
        "source_spec_id": record.source_spec_id,
        "target_spec_id": record.target_spec_id,
        "created_at": record.created_at.isoformat(),
        "created_by": record.created_by,
        "created_by_type": record.created_by_type,
        "created_by_name": record.created_by_name,
        "source_version_on_create": record.source_version_on_create,
        "source_status_on_create": record.source_status_on_create.value,
        "target_status_on_create": record.target_status_on_create.value,
        "target_version_on_create": record.target_version_on_create,
        "resolved_on_create": record.resolved_on_create,
        "retrospective": record.retrospective,
        "removed_at": record.removed_at.isoformat() if record.removed_at else None,
        "removed_by": record.removed_by,
        "removed_by_type": record.removed_by_type,
        "removed_by_name": record.removed_by_name,
        "removal_reason": record.removal_reason,
        "source_version_on_remove": record.source_version_on_remove,
        "add_idempotency_key": record.add_idempotency_key,
        "remove_idempotency_key": record.remove_idempotency_key,
    }


def _record_from_payload(payload: dict[str, Any]) -> SpecDependencyRecord:
    return SpecDependencyRecord(
        id=str(payload["id"]),
        board_id=str(payload["board_id"]),
        source_spec_id=str(payload["source_spec_id"]),
        target_spec_id=str(payload["target_spec_id"]),
        created_at=normalize_utc_datetime(
            datetime.fromisoformat(payload["created_at"])
        ),
        created_by=str(payload["created_by"]),
        created_by_type=str(payload.get("created_by_type") or "user"),
        created_by_name=payload.get("created_by_name"),
        source_version_on_create=int(payload["source_version_on_create"]),
        source_status_on_create=_status(payload["source_status_on_create"]),
        target_status_on_create=_status(payload["target_status_on_create"]),
        target_version_on_create=int(payload["target_version_on_create"]),
        resolved_on_create=bool(payload["resolved_on_create"]),
        retrospective=bool(payload.get("retrospective", False)),
        removed_at=(
            normalize_utc_datetime(datetime.fromisoformat(payload["removed_at"]))
            if payload.get("removed_at")
            else None
        ),
        removed_by=payload.get("removed_by"),
        removed_by_type=payload.get("removed_by_type"),
        removed_by_name=payload.get("removed_by_name"),
        removal_reason=payload.get("removal_reason"),
        source_version_on_remove=(
            int(payload["source_version_on_remove"])
            if payload.get("source_version_on_remove") is not None
            else None
        ),
        add_idempotency_key=payload.get("add_idempotency_key"),
        remove_idempotency_key=payload.get("remove_idempotency_key"),
    )


def _snapshot_payload(snapshot: SpecDependencySpecSnapshot) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "board_id": snapshot.board_id,
        "title": snapshot.title,
        "status": snapshot.status.value,
        "edition": snapshot.edition,
        "version": snapshot.version,
        "archived": snapshot.archived,
        "ideation_id": snapshot.ideation_id,
        "last_started_edition": snapshot.last_started_edition,
    }


def _snapshot_from_payload(payload: dict[str, Any]) -> SpecDependencySpecSnapshot:
    return SpecDependencySpecSnapshot(
        id=str(payload["id"]),
        board_id=str(payload["board_id"]),
        title=str(payload["title"]),
        status=_status(payload["status"]),
        edition=int(payload["edition"]),
        version=int(payload["version"]),
        archived=bool(payload.get("archived", False)),
        ideation_id=payload.get("ideation_id"),
        last_started_edition=(
            int(payload["last_started_edition"])
            if payload.get("last_started_edition") is not None
            else None
        ),
    )


def _cursor_identity(query: SpecDependencyListQuery) -> dict[str, Any]:
    return {
        "b": query.board_id,
        "s": query.spec_id,
        "d": query.direction.value,
        "l": query.lifecycle.value,
        "f": query.satisfaction.value,
        "g": query.lineage.value,
        "r": query.retrospective,
        "st": [status.value for status in query.related_statuses],
    }


def _encode_cursor(query: SpecDependencyListQuery, row: SpecDependency) -> str:
    payload = {
        "v": 1,
        "q": _cursor_identity(query),
        "t": normalize_utc_datetime(row.created_at).isoformat(),
        "i": str(row.id),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(
    query: SpecDependencyListQuery,
) -> tuple[datetime, str] | None:
    if query.cursor is None:
        return None
    try:
        raw = query.cursor + "=" * (-len(query.cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw).decode())
        if (
            not isinstance(payload, dict)
            or payload.get("v") != 1
            or payload.get("q") != _cursor_identity(query)
            or not isinstance(payload.get("i"), str)
        ):
            raise ValueError("cursor contract mismatch")
        timestamp = normalize_utc_datetime(datetime.fromisoformat(payload["t"]))
        return timestamp, payload["i"]
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise SpecDependencyOperationError(
            "invalid_cursor",
            "The Spec dependency cursor is invalid for this query.",
        ) from exc


class CommunitySqlAlchemySpecDependency:
    """Transaction-bound implementation of ``SpecDependencyPersistencePort``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _ensure_sqlite_read_snapshot(self) -> bool:
        """Pin SQLite's physical read transaction without taking a writer lock.

        Python's sqlite3 driver uses legacy transaction control through 3.15:
        SQLAlchemy may have an ORM transaction after an authorization SELECT
        while SQLite itself has not received ``BEGIN``.  In that state every
        SELECT in a paginated projection may observe a different WAL commit.

        ``BEGIN`` (deferred, deliberately *not* ``BEGIN IMMEDIATE``) establishes
        one WAL snapshot at the next SELECT and still allows another connection
        to commit.  If the caller's UoW already owns a physical transaction we
        keep it; issuing a nested ``BEGIN`` would be both invalid and needless.
        The transaction remains caller-owned and is committed/rolled back by
        the existing UoW lifecycle.

        PostgreSQL is intentionally left untouched here: changing isolation
        after the UoW's authorization read is illegal.  A PostgreSQL runtime
        must enter its read UoW at REPEATABLE READ (or use a one-statement
        projection) before calling this adapter.
        """

        bind = self._session.get_bind()
        if bind is None or bind.dialect.name != "sqlite":
            return False
        connection = await self._session.connection()

        def physical_transaction_active(sync_connection: Connection) -> bool:
            driver_connection = getattr(
                sync_connection.connection, "driver_connection", None
            )
            return bool(getattr(driver_connection, "in_transaction", False))

        if not await connection.run_sync(physical_transaction_active):
            await connection.exec_driver_sql("BEGIN")
        return True

    async def acquire_board_graph_lock(self, board_id: str) -> None:
        bind = self._session.get_bind()
        dialect = bind.dialect.name if bind is not None else ""
        if dialect == "sqlite":
            # SQLite has no row-level FOR UPDATE.  An idempotent UPSERT against
            # a technical row obtains its cross-process writer lock without
            # mutating the Board and firing product/discovery triggers.
            statement = sqlite_insert(SpecDependencyBoardLock).values(board_id=board_id)
            await self._session.execute(
                statement.on_conflict_do_update(
                    index_elements=[SpecDependencyBoardLock.board_id],
                    set_={"board_id": statement.excluded.board_id},
                )
            )
            return
        await self._session.execute(
            select(Board.id).where(Board.id == board_id).with_for_update()
        )

    async def get_spec_snapshot(
        self,
        *,
        board_id: str,
        spec_id: str,
        for_update: bool = False,
    ) -> SpecDependencySpecSnapshot | None:
        statement = select(Spec).where(
            Spec.board_id == board_id,
            Spec.id == spec_id,
        ).execution_options(populate_existing=True)
        if for_update and (
            not self._session.bind or self._session.bind.dialect.name != "sqlite"
        ):
            statement = statement.with_for_update()
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return _snapshot(row) if row is not None else None

    async def lookup_mutation_replay(
        self,
        *,
        board_id: str,
        operation: str,
        idempotency_key: str,
        actor_id: str,
        actor_type: str,
    ) -> SpecDependencyMutationReceipt | None:
        row = (
            await self._session.execute(
                select(SpecDependencyOperation).where(
                    SpecDependencyOperation.board_id == board_id,
                    SpecDependencyOperation.operation == operation,
                    SpecDependencyOperation.idempotency_key == idempotency_key,
                    SpecDependencyOperation.actor_id == actor_id,
                    SpecDependencyOperation.actor_type == actor_type,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        payload = dict(row.result_payload)
        dependency = _record_from_payload(dict(payload["dependency"]))
        persisted_satisfaction = payload.get("satisfied")
        return SpecDependencyMutationReceipt(
            operation=str(row.operation),
            dependency=dependency,
            source_spec=_snapshot_from_payload(dict(payload["source_spec"])),
            request_digest=str(row.request_digest),
            satisfied=(
                persisted_satisfaction
                if isinstance(persisted_satisfaction, bool)
                else dependency.resolved_on_create
            ),
            replayed=True,
        )

    async def get_dependency(
        self, *, board_id: str, dependency_id: str
    ) -> SpecDependencyRecord | None:
        row = (
            await self._session.execute(
                select(SpecDependency).where(
                    SpecDependency.board_id == board_id,
                    SpecDependency.id == dependency_id,
                )
            )
        ).scalar_one_or_none()
        return _record(row) if row is not None else None

    async def find_active_dependency(
        self,
        *,
        board_id: str,
        source_spec_id: str,
        target_spec_id: str,
    ) -> SpecDependencyRecord | None:
        row = (
            await self._session.execute(
                select(SpecDependency).where(
                    SpecDependency.board_id == board_id,
                    SpecDependency.dependent_spec_id == source_spec_id,
                    SpecDependency.prerequisite_spec_ref == target_spec_id,
                    SpecDependency.active.is_(True),
                )
            )
        ).scalar_one_or_none()
        return _record(row) if row is not None else None

    async def list_active_board_edges(
        self, *, board_id: str
    ) -> tuple[SpecDependencyRecord, ...]:
        rows = (
            (
                await self._session.execute(
                    select(SpecDependency)
                    .where(
                        SpecDependency.board_id == board_id,
                        SpecDependency.active.is_(True),
                    )
                    .order_by(SpecDependency.id)
                )
            )
            .scalars()
            .all()
        )
        return tuple(_record(row) for row in rows)

    async def list_active_blockers(
        self,
        *,
        board_id: str,
        source_spec_id: str,
        limit: int = 100,
    ) -> tuple[SpecDependencyBlocker, ...]:
        rows = (
            await self._session.execute(
                select(SpecDependency, Spec)
                .join(Spec, Spec.id == SpecDependency.prerequisite_spec_id)
                .where(
                    SpecDependency.board_id == board_id,
                    SpecDependency.dependent_spec_id == source_spec_id,
                    SpecDependency.active.is_(True),
                    or_(
                        Spec.status != SpecStatus.DONE.value,
                        Spec.archived.is_(True),
                    ),
                )
                .order_by(SpecDependency.created_at.desc(), SpecDependency.id.desc())
                .limit(normalize_spec_dependency_blocker_limit(limit))
            )
        ).all()
        return tuple(
            SpecDependencyBlocker(
                dependency_id=str(dependency.id),
                source_spec_id=str(dependency.dependent_spec_id),
                target_spec_id=str(dependency.prerequisite_spec_ref),
                target_title=str(target.title),
                target_status=_status(target.status),
                target_edition=int(target.edition),
                target_version=int(target.version),
                target_archived=bool(target.archived),
            )
            for dependency, target in rows
        )

    async def list_incoming_active(
        self,
        *,
        board_id: str,
        target_spec_ids: Sequence[str],
        exclude_source_spec_ids: Sequence[str] = (),
        limit: int = 100,
    ) -> tuple[SpecDependencyRecord, ...]:
        target_ids = tuple(dict.fromkeys(str(value) for value in target_spec_ids))
        if not target_ids:
            return ()
        statement = select(SpecDependency).where(
            SpecDependency.board_id == board_id,
            SpecDependency.prerequisite_spec_ref.in_(target_ids),
            SpecDependency.active.is_(True),
        )
        excluded = tuple(dict.fromkeys(str(value) for value in exclude_source_spec_ids))
        if excluded:
            statement = statement.where(
                SpecDependency.dependent_spec_id.not_in(excluded)
            )
        # The lifecycle guard requests one probe row beyond the 100-item
        # public detail ceiling. Preserve that row so Core can distinguish an
        # exact total from a truncated lower bound without an unbounded read.
        rows = (
            (
                await self._session.execute(
                    statement.order_by(
                        SpecDependency.created_at.desc(), SpecDependency.id.desc()
                    ).limit(max(1, min(int(limit), 101)))
                )
            )
            .scalars()
            .all()
        )
        return tuple(_record(row) for row in rows)

    def _page_statement(self, query: SpecDependencyListQuery):
        source = aliased(Spec, name="dependency_source_spec")
        target = aliased(Spec, name="dependency_target_spec")
        statement = (
            select(SpecDependency, source, target)
            .join(
                source,
                and_(
                    source.id == SpecDependency.dependent_spec_id,
                    source.board_id == SpecDependency.board_id,
                ),
            )
            .outerjoin(
                target,
                and_(
                    target.id == SpecDependency.prerequisite_spec_ref,
                    target.board_id == SpecDependency.board_id,
                ),
            )
        )
        predicates: list[Any] = [SpecDependency.board_id == query.board_id]
        if query.direction is SpecDependencyDirection.OUTGOING:
            predicates.append(SpecDependency.dependent_spec_id == query.spec_id)
            related = target
        else:
            predicates.append(SpecDependency.prerequisite_spec_ref == query.spec_id)
            related = source
        if query.lifecycle is SpecDependencyLifecycleFilter.ACTIVE:
            predicates.append(SpecDependency.active.is_(True))
        elif query.lifecycle is SpecDependencyLifecycleFilter.REMOVED:
            predicates.append(SpecDependency.active.is_(False))

        target_available = and_(
            target.id.is_not(None),
            target.archived.is_(False),
        )
        target_satisfied = and_(
            target_available,
            target.status == SpecStatus.DONE.value,
        )
        if query.satisfaction is SpecDependencySatisfactionFilter.SATISFIED:
            predicates.append(target_satisfied)
        elif query.satisfaction is SpecDependencySatisfactionFilter.BLOCKING:
            predicates.append(~target_satisfied)
        if query.retrospective is not None:
            predicates.append(
                SpecDependency.retrospective.is_(bool(query.retrospective))
            )
        if query.related_statuses:
            related_status = (
                func.coalesce(related.status, SpecDependency.target_status_on_create)
                if query.direction is SpecDependencyDirection.OUTGOING
                else related.status
            )
            predicates.append(
                related_status.in_(
                    tuple(status.value for status in query.related_statuses)
                )
            )
        same_ideation = func.coalesce(
            source.ideation_id
            == func.coalesce(
                target.ideation_id, SpecDependency.target_ideation_id_on_create
            ),
            False,
        )
        if query.lineage is SpecDependencyLineageFilter.SAME_IDEATION:
            predicates.append(same_ideation.is_(True))
        elif query.lineage is SpecDependencyLineageFilter.CROSS_IDEATION:
            predicates.append(same_ideation.is_(False))
        return statement.where(*predicates), source, target, predicates

    async def list_page(self, query: SpecDependencyListQuery) -> SpecDependencyPage:
        """Execute the keyset page under a constant, observable SQL budget."""

        started = perf_counter()
        outcome = "success"
        query_count = 0
        try:
            # Transaction control is deliberately outside the four-query data
            # budget.  A fresh SQLite UoW therefore consumes at most five real
            # statements (BEGIN + four SELECTs), while subsequent calls reuse
            # the caller-owned snapshot and still execute four data queries.
            await self._ensure_sqlite_read_snapshot()
            async with statement_budget(self._session, 4) as budget:
                try:
                    page = await self._list_page(query)
                finally:
                    query_count = budget.used
                if query_count != 4:
                    raise RuntimeError(
                        "spec_dependency_page_query_budget_mismatch: "
                        f"used {query_count}, expected 4"
                    )
                return page
        except Exception:
            outcome = "error"
            raise
        finally:
            logger.info(
                "spec_dependency.page outcome=%s queries=%s",
                outcome,
                query_count,
                extra={
                    "event": "spec_dependency.page",
                    "metric_name": "spec_dependency_page_query_count",
                    "value": query_count,
                    "query_count": query_count,
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                    "direction": query.direction.value,
                    "outcome": outcome,
                },
            )

    async def _list_page(self, query: SpecDependencyListQuery) -> SpecDependencyPage:
        # This DTO field is an application-derived authorization outcome.  An
        # older Core DTO or malformed direct caller therefore remains denied.
        remove_authorized = getattr(query, "can_manage_dependencies", False) is True
        statement, source_alias, target_alias, predicates = self._page_statement(query)
        cursor = _decode_cursor(query)
        if cursor is not None:
            timestamp, row_id = cursor
            statement = statement.where(
                or_(
                    SpecDependency.created_at < timestamp,
                    and_(
                        SpecDependency.created_at == timestamp,
                        SpecDependency.id < row_id,
                    ),
                )
            )
        total_query = (
            select(func.count(SpecDependency.id))
            .select_from(SpecDependency)
            .join(
                source_alias,
                and_(
                    source_alias.id == SpecDependency.dependent_spec_id,
                    source_alias.board_id == SpecDependency.board_id,
                ),
            )
            .outerjoin(
                target_alias,
                and_(
                    target_alias.id == SpecDependency.prerequisite_spec_ref,
                    target_alias.board_id == SpecDependency.board_id,
                ),
            )
            .where(*predicates)
        )
        anchor_and_total = (
            await self._session.execute(
                select(
                    Spec,
                    total_query.scalar_subquery().label("dependency_total"),
                )
                .where(
                    Spec.board_id == query.board_id,
                    Spec.id == query.spec_id,
                )
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if anchor_and_total is None:
            raise SpecDependencyOperationError(
                "dependency_target_unavailable",
                "The requested Spec is unavailable.",
            )
        anchor_row, total_value = anchor_and_total
        anchor = _snapshot(anchor_row)
        total = int(total_value)
        rows = (
            await self._session.execute(
                statement.order_by(
                    SpecDependency.created_at.desc(), SpecDependency.id.desc()
                ).limit(query.limit + 1)
            )
        ).all()
        has_more = len(rows) > query.limit
        visible = rows[: query.limit]
        items: list[SpecDependencyListItem] = []
        for dependency, source, target in visible:
            if query.direction is SpecDependencyDirection.OUTGOING:
                if target is None:
                    related_snapshot = SpecDependencySpecSnapshot(
                        id=str(dependency.prerequisite_spec_ref),
                        board_id=str(dependency.board_id),
                        title=str(dependency.target_title_on_create),
                        status=_status(dependency.target_status_on_create),
                        edition=int(dependency.target_edition_on_create),
                        version=int(dependency.target_version_on_create),
                        archived=True,
                        ideation_id=dependency.target_ideation_id_on_create,
                    )
                else:
                    related_snapshot = _snapshot(target)
            else:
                related_snapshot = _snapshot(source)
            target_status = (
                _status(target.status)
                if target is not None
                else _status(dependency.target_status_on_create)
            )
            source_ideation = source.ideation_id
            target_ideation = (
                target.ideation_id
                if target is not None
                else dependency.target_ideation_id_on_create
            )
            items.append(
                SpecDependencyListItem(
                    dependency=_record(dependency),
                    direction=query.direction,
                    related_spec=related_snapshot,
                    satisfied=(
                        target is not None
                        and not bool(target.archived)
                        and spec_dependency_is_satisfied(target_status)
                    ),
                    retrospective=bool(dependency.retrospective),
                    same_ideation=(
                        source_ideation is not None
                        and source_ideation == target_ideation
                    ),
                    capabilities=SpecDependencyCapabilities(
                        can_remove=(
                            remove_authorized
                            and bool(dependency.active)
                            and not bool(source.archived)
                            and query.direction is SpecDependencyDirection.OUTGOING
                        ),
                        can_navigate=not related_snapshot.archived,
                        removal_blocked_reason=(
                            "dependency_removed"
                            if not bool(dependency.active)
                            else "incoming_dependency_read_only"
                            if query.direction is SpecDependencyDirection.INCOMING
                            else "source_archived"
                            if bool(source.archived)
                            else "permission_denied"
                            if not remove_authorized
                            else None
                        ),
                    ),
                )
            )
        next_cursor = (
            _encode_cursor(query, visible[-1][0]) if has_more and visible else None
        )
        return SpecDependencyPage(
            items=tuple(items),
            total=total,
            next_cursor=next_cursor,
            readiness=await self._get_readiness_for_snapshot(
                snapshot=anchor,
                blocker_limit=100,
            ),
        )

    async def get_readiness(
        self, *, board_id: str, spec_id: str, blocker_limit: int = 100
    ) -> SpecDependencyReadiness:
        snapshot = await self.get_spec_snapshot(board_id=board_id, spec_id=spec_id)
        if snapshot is None:
            raise SpecDependencyOperationError(
                "dependency_target_unavailable",
                "The requested Spec is unavailable.",
            )
        return await self._get_readiness_for_snapshot(
            snapshot=snapshot,
            blocker_limit=blocker_limit,
        )

    async def _get_readiness_for_snapshot(
        self,
        *,
        snapshot: SpecDependencySpecSnapshot,
        blocker_limit: int,
    ) -> SpecDependencyReadiness:
        target = aliased(Spec)
        base = and_(
            SpecDependency.board_id == snapshot.board_id,
            SpecDependency.dependent_spec_id == snapshot.id,
            SpecDependency.active.is_(True),
        )
        archived_blocker = target.archived.is_(True)
        unfinished_blocker = and_(
            target.archived.is_(False),
            target.status != SpecStatus.DONE.value,
        )
        counts = (
            await self._session.execute(
                select(
                    func.count(SpecDependency.id).label("active_count"),
                    func.count(SpecDependency.id)
                    .filter(
                        or_(
                            target.status != SpecStatus.DONE.value,
                            target.archived.is_(True),
                        )
                    )
                    .label("blocking_count"),
                    func.count(SpecDependency.id)
                    .filter(archived_blocker)
                    .label("archived_blocking_count"),
                    func.count(SpecDependency.id)
                    .filter(unfinished_blocker)
                    .label("unfinished_blocking_count"),
                )
                .select_from(SpecDependency)
                .join(target, target.id == SpecDependency.prerequisite_spec_id)
                .where(base)
            )
        ).one()
        active_count = int(counts.active_count)
        blocking_count = int(counts.blocking_count)
        archived_blocking_count = int(counts.archived_blocking_count)
        unfinished_blocking_count = int(counts.unfinished_blocking_count)
        blockers = await self.list_active_blockers(
            board_id=snapshot.board_id,
            source_spec_id=snapshot.id,
            limit=blocker_limit,
        )
        return SpecDependencyReadiness(
            spec_id=snapshot.id,
            board_id=snapshot.board_id,
            current_edition=snapshot.edition,
            last_started_edition=snapshot.last_started_edition,
            active_dependency_count=active_count,
            blocking_count=blocking_count,
            archived_blocking_count=archived_blocking_count,
            unfinished_blocking_count=unfinished_blocking_count,
            blockers_truncated=blocking_count > len(blockers),
            blockers=blockers,
        )

    async def list_board_readiness(
        self,
        *,
        board_id: str,
        blocker_limit_per_spec: int = 100,
    ) -> tuple[SpecDependencyReadiness, ...]:
        """Load board readiness in two statements, independent of Spec count."""

        blocker_limit = normalize_spec_dependency_blocker_limit(
            blocker_limit_per_spec
        )
        target = aliased(Spec, name="board_readiness_target")
        archived_blocker = target.archived.is_(True)
        unfinished_blocker = and_(
            target.archived.is_(False),
            target.status != SpecStatus.DONE.value,
        )
        summary_rows = (
            await self._session.execute(
                select(
                    Spec,
                    func.count(SpecDependency.id).label("active_count"),
                    func.count(SpecDependency.id)
                    .filter(
                        or_(
                            target.status != SpecStatus.DONE.value,
                            target.archived.is_(True),
                        )
                    )
                    .label("blocking_count"),
                    func.count(SpecDependency.id)
                    .filter(archived_blocker)
                    .label("archived_blocking_count"),
                    func.count(SpecDependency.id)
                    .filter(unfinished_blocker)
                    .label("unfinished_blocking_count"),
                )
                .outerjoin(
                    SpecDependency,
                    and_(
                        SpecDependency.board_id == Spec.board_id,
                        SpecDependency.dependent_spec_id == Spec.id,
                        SpecDependency.active.is_(True),
                    ),
                )
                .outerjoin(
                    target,
                    target.id == SpecDependency.prerequisite_spec_id,
                )
                .where(Spec.board_id == board_id, Spec.archived.is_(False))
                .group_by(Spec.id)
                .order_by(Spec.id)
            )
        ).all()
        if not summary_rows:
            return ()

        ranked_blockers = (
            select(
                SpecDependency.id.label("dependency_id"),
                SpecDependency.dependent_spec_id.label("source_spec_id"),
                SpecDependency.prerequisite_spec_ref.label("target_spec_id"),
                target.title.label("target_title"),
                target.status.label("target_status"),
                target.edition.label("target_edition"),
                target.version.label("target_version"),
                target.archived.label("target_archived"),
                func.row_number()
                .over(
                    partition_by=SpecDependency.dependent_spec_id,
                    order_by=(
                        SpecDependency.created_at.desc(),
                        SpecDependency.id.desc(),
                    ),
                )
                .label("blocker_rank"),
            )
            .join(target, target.id == SpecDependency.prerequisite_spec_id)
            .where(
                SpecDependency.board_id == board_id,
                SpecDependency.active.is_(True),
                or_(
                    target.status != SpecStatus.DONE.value,
                    target.archived.is_(True),
                ),
            )
            .subquery("ranked_spec_dependency_blockers")
        )
        blocker_rows = (
            (
                await self._session.execute(
                    select(ranked_blockers)
                    .where(ranked_blockers.c.blocker_rank <= blocker_limit)
                    .order_by(
                        ranked_blockers.c.source_spec_id,
                        ranked_blockers.c.blocker_rank,
                    )
                )
            )
            .mappings()
            .all()
        )
        blockers_by_source: dict[str, list[SpecDependencyBlocker]] = {}
        for row in blocker_rows:
            blockers_by_source.setdefault(str(row["source_spec_id"]), []).append(
                SpecDependencyBlocker(
                    dependency_id=str(row["dependency_id"]),
                    source_spec_id=str(row["source_spec_id"]),
                    target_spec_id=str(row["target_spec_id"]),
                    target_title=str(row["target_title"]),
                    target_status=_status(row["target_status"]),
                    target_edition=int(row["target_edition"]),
                    target_version=int(row["target_version"]),
                    target_archived=bool(row["target_archived"]),
                )
            )
        return tuple(
            SpecDependencyReadiness(
                spec_id=snapshot.id,
                board_id=snapshot.board_id,
                current_edition=snapshot.edition,
                last_started_edition=snapshot.last_started_edition,
                active_dependency_count=int(active_count),
                blocking_count=int(blocking_count),
                archived_blocking_count=int(archived_blocking_count),
                unfinished_blocking_count=int(unfinished_blocking_count),
                blockers_truncated=(
                    int(blocking_count)
                    > len(blockers_by_source.get(snapshot.id, ()))
                ),
                blockers=tuple(blockers_by_source.get(snapshot.id, ())),
            )
            for (
                spec_row,
                active_count,
                blocking_count,
                archived_blocking_count,
                unfinished_blocking_count,
            ) in summary_rows
            for snapshot in (_snapshot(spec_row),)
        )

    async def insert_dependency(
        self,
        dependency: SpecDependencyRecord,
        *,
        request_digest: str,
    ) -> None:
        target = await self.get_spec_snapshot(
            board_id=dependency.board_id,
            spec_id=dependency.target_spec_id,
        )
        source = await self.get_spec_snapshot(
            board_id=dependency.board_id,
            spec_id=dependency.source_spec_id,
        )
        if target is None or source is None:
            raise SpecDependencyOperationError(
                "dependency_target_unavailable",
                "A Spec required by the dependency is unavailable.",
            )
        self._session.add(
            SpecDependency(
                id=dependency.id,
                board_id=dependency.board_id,
                dependent_spec_id=dependency.source_spec_id,
                prerequisite_spec_id=dependency.target_spec_id,
                prerequisite_spec_ref=dependency.target_spec_id,
                active=True,
                resolved_on_create=dependency.resolved_on_create,
                retrospective=dependency.retrospective,
                introduced_at_spec_version=dependency.source_version_on_create,
                source_version_on_create=dependency.source_version_on_create,
                source_status_on_create=dependency.source_status_on_create.value,
                target_status_on_create=dependency.target_status_on_create.value,
                target_version_on_create=dependency.target_version_on_create,
                target_title_on_create=target.title,
                target_edition_on_create=target.edition,
                target_ideation_id_on_create=target.ideation_id,
                add_idempotency_key=dependency.add_idempotency_key,
                add_request_digest=request_digest,
                created_at=dependency.created_at,
                created_by_id=dependency.created_by,
                created_by_type=dependency.created_by_type,
                created_by_name=dependency.created_by_name or dependency.created_by,
            )
        )
        await self._session.flush()

    async def tombstone_dependency(
        self,
        *,
        board_id: str,
        dependency_id: str,
        removed_at: datetime,
        removed_by: str,
        removed_by_type: str,
        removed_by_name: str | None,
        removal_reason: str,
        source_version_on_remove: int,
        idempotency_key: str,
        request_digest: str,
    ) -> SpecDependencyRecord:
        row = (
            await self._session.execute(
                select(SpecDependency).where(
                    SpecDependency.board_id == board_id,
                    SpecDependency.id == dependency_id,
                    SpecDependency.active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise SpecDependencyOperationError(
                "spec_dependency_not_found",
                "The active Spec dependency was not found.",
            )
        row.active = False
        row.prerequisite_spec_id = None
        row.removed_at = removed_at
        row.removed_by_id = removed_by
        row.removed_by_type = removed_by_type
        row.removed_by_name = removed_by_name or removed_by
        row.removal_reason = removal_reason.strip()
        row.remove_idempotency_key = idempotency_key
        row.remove_request_digest = request_digest
        row.removed_at_spec_version = source_version_on_remove
        await self._session.flush()
        return _record(row)

    async def bump_source_spec_version(
        self,
        *,
        board_id: str,
        spec_id: str,
        expected_version: int,
        expected_edition: int,
    ) -> SpecDependencySpecSnapshot | None:
        result = await self._session.execute(
            update(Spec)
            .where(
                Spec.board_id == board_id,
                Spec.id == spec_id,
                Spec.version == expected_version,
                Spec.edition == expected_edition,
            )
            .values(version=Spec.version + 1)
        )
        if int(result.rowcount or 0) != 1:
            return None
        return await self.get_spec_snapshot(board_id=board_id, spec_id=spec_id)

    async def store_mutation_receipt(
        self,
        receipt: SpecDependencyMutationReceipt,
        *,
        idempotency_key: str,
        actor_id: str,
        actor_type: str,
        actor_name: str | None,
    ) -> None:
        self._session.add(
            SpecDependencyOperation(
                id=str(uuid.uuid4()),
                board_id=receipt.dependency.board_id,
                dependent_spec_ref=receipt.dependency.source_spec_id,
                dependency_ref=receipt.dependency.id,
                operation=receipt.operation,
                idempotency_key=idempotency_key,
                request_digest=receipt.request_digest,
                actor_id=actor_id,
                actor_type=actor_type,
                actor_name=actor_name or actor_id,
                expected_spec_version=receipt.source_spec.version - 1,
                resulting_spec_version=receipt.source_spec.version,
                result_payload={
                    "dependency": _record_payload(receipt.dependency),
                    "source_spec": _snapshot_payload(receipt.source_spec),
                    "satisfied": receipt.satisfied,
                },
                created_at=receipt.dependency.removed_at
                or receipt.dependency.created_at,
            )
        )
        await self._session.flush()

    async def mark_spec_edition_started(
        self,
        *,
        board_id: str,
        spec_id: str,
        expected_edition: int,
    ) -> SpecDependencySpecSnapshot | None:
        result = await self._session.execute(
            update(Spec)
            .where(
                Spec.board_id == board_id,
                Spec.id == spec_id,
                Spec.edition == expected_edition,
            )
            .values(last_started_edition=expected_edition)
        )
        if int(result.rowcount or 0) != 1:
            return None
        return await self.get_spec_snapshot(board_id=board_id, spec_id=spec_id)


__all__ = ["CommunitySqlAlchemySpecDependency"]
