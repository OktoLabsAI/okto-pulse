"""Run existing lifecycle writers inside one caller-owned SQLite transaction.

Internal migration mechanism only. This does not acquire the offline writer
window, commit a terminal receipt or admit runtime. The caller owns those gates.
Each engine scope and ORM session uses savepoints, so existing writer commits
cannot publish a partial lifecycle. Unsupported engine operations fail closed.
"""

from contextlib import asynccontextmanager
import re

from sqlalchemy import event
from sqlalchemy.exc import InvalidRequestError

from okto_pulse.core.ports.relational_runtime import database_runtime_scope
from .sqlalchemy_database import CommunityDatabaseRuntime, build_community_session_factory


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    async def _start(self):
        if self.connection.in_transaction():
            raise InvalidRequestError("schema_transaction_scope_already_started")
        await self.connection._ensure()
        return self

    def __await__(self):
        return self._start().__await__()

    async def __aenter__(self):
        return await self._start()

    async def __aexit__(self, kind, value, traceback):
        if kind is None:
            await self.commit()
        else:
            await self.rollback()

    async def commit(self):
        await self.connection.commit()

    async def rollback(self):
        await self.connection.rollback()


class _Connection:
    def __init__(self, owner):
        self.owner = owner
        self.transaction = None
        self.closed = False

    @property
    def dialect(self):
        return self.owner.dialect

    async def _ensure(self):
        if self.closed or not self.owner.in_transaction():
            raise RuntimeError("schema_transaction_scope_closed")
        if not self.in_transaction():
            self.transaction = await self.owner.begin_nested()

    def in_transaction(self):
        return self.transaction is not None and self.transaction.is_active

    def begin(self):
        return _Transaction(self)

    async def commit(self):
        if self.in_transaction():
            await self.transaction.commit()
        self.transaction = None

    async def rollback(self):
        if self.in_transaction():
            await self.transaction.rollback()
        self.transaction = None

    async def close(self):
        await self.rollback()
        self.closed = True

    async def execute(self, *args, **kwargs):
        await self._ensure()
        return await self.owner.execute(*args, **kwargs)

    async def exec_driver_sql(self, statement, *args, **kwargs):
        await self._ensure()
        # The outer owner already holds BEGIN IMMEDIATE. Existing migration
        # helpers repeat this reservation when operating on a normal engine.
        # Do not generalize this to arbitrary transaction-control statements.
        if re.fullmatch(r"\s*BEGIN(?:\s+(?:IMMEDIATE|DEFERRED|TRANSACTION))?\s*;?\s*", statement, re.IGNORECASE):
            return await self.owner.exec_driver_sql("SELECT 1 WHERE 0")
        return await self.owner.exec_driver_sql(statement, *args, **kwargs)

    async def run_sync(self, *args, **kwargs):
        await self._ensure()
        return await self.owner.run_sync(*args, **kwargs)

    async def scalar(self, *args, **kwargs):
        return (await self.execute(*args, **kwargs)).scalar()

    async def scalars(self, *args, **kwargs):
        return (await self.execute(*args, **kwargs)).scalars()


class _Engine:
    # Filesystem/graph mutations cannot join the relational transaction.
    permits_external_schema_effects = False

    def __init__(self, owner):
        self.owner = owner
        self.dialect = owner.dialect
        self.url = owner.engine.url

    @asynccontextmanager
    async def connect(self):
        connection = _Connection(self.owner)
        try:
            yield connection
        finally:
            await connection.close()

    @asynccontextmanager
    async def begin(self):
        async with self.connect() as connection:
            async with connection.begin():
                yield connection


@asynccontextmanager
async def schema_transaction_runtime(connection):
    """Reserve a fresh connection and bind writers without committing it.

    Caller must supply a fresh connection. Verify dependencies, run the writers,
    and record the receipt inside this scope; commit only after it exits. The
    enclosing connection owner must roll back if any operation fails.
    """
    if connection.dialect.name != "sqlite" or connection.in_transaction():
        raise ValueError("schema_transaction_fresh_sqlite_connection_required")
    # A PRAGMA inside BEGIN cannot enable enforcement. Establish it before
    # reserving the writer, including when a caller supplies an unconfigured
    # engine, and never disable it for the lifecycle's convenience.
    await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    enabled = (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()
    await connection.rollback()
    if enabled != 1:
        raise ValueError("schema_transaction_foreign_keys_required")
    await connection.exec_driver_sql("BEGIN IMMEDIATE")
    boundary_attempted = False

    def refuse_boundary(*args):
        nonlocal boundary_attempted
        boundary_attempted = True
        raise RuntimeError("schema_transaction_outer_boundary_forbidden")

    def refuse_sql_boundary(conn, cursor, statement, parameters, context, many):
        # Also guard sync callbacks and ORM text statements; a connection
        # facade alone would not stop an explicit SQL COMMIT/ROLLBACK.
        cleaned = re.sub(r"--[^\r\n]*(?:\r?\n|$)|/\*[\s\S]*?\*/", " ", statement)
        command = re.sub(r"\s+", " ", cleaned.strip()).upper()
        if (re.match(r"(?:COMMIT|END)\b", command)
                or re.match(r"ROLLBACK\b", command) and not re.match(r"ROLLBACK(?: TRANSACTION)? TO\b", command)):
            refuse_boundary()
        # ORM writers can request the same reservation through Session.execute,
        # bypassing the engine facade. The actual outer BEGIN IMMEDIATE already
        # owns it; retain stricter/unknown SQL as an error, never weaken it.
        if re.fullmatch(r"BEGIN(?: (?:IMMEDIATE|DEFERRED|TRANSACTION))?\s*;?", command):
            return "SELECT 1 WHERE 0", parameters
        return statement, parameters

    sync = connection.sync_connection
    listeners = (("commit", refuse_boundary), ("rollback", refuse_boundary), ("before_cursor_execute", refuse_sql_boundary))
    installed = []
    try:
        for name, handler in listeners:
            event.listen(sync, name, handler, **({"retval": True} if name == "before_cursor_execute" else {}))
            installed.append((name, handler))
        factory = build_community_session_factory(connection)
        factory.configure(join_transaction_mode="create_savepoint")
        runtime = CommunityDatabaseRuntime(_Engine(connection), factory)
        with database_runtime_scope(runtime=runtime):
            yield runtime
        if boundary_attempted:
            raise RuntimeError("schema_transaction_outer_boundary_forbidden")
        if not connection.in_transaction() or connection.in_nested_transaction():
            raise RuntimeError("schema_transaction_scope_leaked")
    finally:
        for name, handler in installed:
            event.remove(sync, name, handler)
        if boundary_attempted:
            # SQLAlchemy deactivates its transaction object even when a commit
            # event rejects the native commit. Discard that connection so its
            # actual uncommitted SQLite transaction cannot later escape through
            # an apparently idle SQLAlchemy connection or pool reuse.
            await connection.invalidate()
