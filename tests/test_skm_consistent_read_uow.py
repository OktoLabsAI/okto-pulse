"""SK-M transport-neutral consistent-read UnitOfWork dialect contracts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    CommunityUnitOfWork,
)
from okto_pulse.core.repositories.interfaces.unit_of_work import (
    ConsistentReadContractError,
)


class _Connection:
    def __init__(
        self,
        *,
        isolation: str,
        physical_transaction_active: bool = False,
    ) -> None:
        self.isolation = isolation
        self.physical_transaction_active = physical_transaction_active
        self.driver_statements: list[str] = []

    async def run_sync(self, operation):  # noqa: ANN001, ANN201
        sync_connection = SimpleNamespace(
            connection=SimpleNamespace(
                driver_connection=SimpleNamespace(
                    in_transaction=self.physical_transaction_active
                )
            )
        )
        return operation(sync_connection)

    async def exec_driver_sql(self, statement: str) -> None:
        self.driver_statements.append(statement)
        if statement == "BEGIN":
            self.physical_transaction_active = True

    async def get_isolation_level(self) -> str:
        return self.isolation


class _Session:
    def __init__(
        self,
        dialect: str,
        *,
        isolation: str = "READ COMMITTED",
        transaction_active: bool = False,
        physical_transaction_active: bool = False,
    ) -> None:
        self.info: dict[str, object] = {}
        self._bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect))
        self._transaction_active = transaction_active
        self.connection_value = _Connection(
            isolation=isolation,
            physical_transaction_active=physical_transaction_active,
        )
        self.connection_options: list[dict[str, Any] | None] = []

    def get_bind(self):  # noqa: ANN201
        return self._bind

    def in_transaction(self) -> bool:
        return self._transaction_active

    async def connection(
        self, *, execution_options: dict[str, Any] | None = None
    ) -> _Connection:
        self.connection_options.append(execution_options)
        self._transaction_active = True
        if execution_options is not None:
            requested = execution_options.get("isolation_level")
            if isinstance(requested, str):
                self.connection_value.isolation = requested
        return self.connection_value


def _uow(session: _Session) -> CommunityUnitOfWork:
    # Isolate the transaction contract from repository/service construction.
    uow = object.__new__(CommunityUnitOfWork)
    uow._session = session  # type: ignore[attr-defined]
    return uow


@pytest.mark.asyncio
async def test_sqlite_consistent_read_uses_deferred_begin_and_is_idempotent() -> None:
    session = _Session("sqlite")
    uow = _uow(session)

    await uow.begin_consistent_read()
    await uow.begin_consistent_read()

    assert session.connection_options == [None]
    assert session.connection_value.driver_statements == ["BEGIN"]
    assert "BEGIN IMMEDIATE" not in session.connection_value.driver_statements


@pytest.mark.asyncio
async def test_postgresql_consistent_read_sets_isolation_before_first_statement() -> (
    None
):
    session = _Session("postgresql")

    await _uow(session).begin_consistent_read()

    assert session.connection_options == [
        {"isolation_level": "REPEATABLE READ"}
    ]
    assert session.connection_value.isolation == "REPEATABLE READ"


@pytest.mark.asyncio
async def test_postgresql_existing_read_committed_transaction_fails_closed() -> None:
    session = _Session(
        "postgresql",
        isolation="READ COMMITTED",
        transaction_active=True,
        physical_transaction_active=True,
    )

    with pytest.raises(
        ConsistentReadContractError,
        match="consistent_read_incompatible_active_transaction",
    ):
        await _uow(session).begin_consistent_read()

    # An active transaction is inspected, never upgraded after a statement.
    assert session.connection_options == [None]


@pytest.mark.asyncio
async def test_postgresql_existing_repeatable_read_transaction_is_reused() -> None:
    session = _Session(
        "postgresql",
        isolation="REPEATABLE READ",
        transaction_active=True,
        physical_transaction_active=True,
    )

    await _uow(session).begin_consistent_read()
    await _uow(session).begin_consistent_read()

    assert session.connection_options == [None]


@pytest.mark.asyncio
async def test_unknown_dialect_fails_closed_without_opening_a_connection() -> None:
    session = _Session("unknown")

    with pytest.raises(
        ConsistentReadContractError,
        match="consistent_read_dialect_unsupported",
    ):
        await _uow(session).begin_consistent_read()

    assert session.connection_options == []
