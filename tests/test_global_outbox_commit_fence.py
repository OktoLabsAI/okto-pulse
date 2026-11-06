"""Real SQL transactions and file leases at the acknowledgement commit boundary."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from datetime import datetime, timezone

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from okto_pulse.community.adapters.coordination import CommunityLocalWriteLockPort
from okto_pulse.community.adapters.current_relational_schema import (
    current_schema_contract,
    initialize_current_schema,
)
from okto_pulse.community.adapters.sqlalchemy_database import install_community_sqlite_pragmas
from okto_pulse.community.adapters.sqlalchemy_global_outbox import CommunitySqlAlchemyGlobalOutboxStore
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    GlobalDiscoveryDeliveryLedger,
    GlobalUpdateOutbox,
)
from okto_pulse.core.kg.global_discovery_writer import (
    GLOBAL_DISCOVERY_WRITER_SCOPE,
    GlobalDiscoveryWriterFenceLost,
    GlobalDiscoveryWriterLease,
)
from okto_pulse.core.ports.coordination import register_coordination_providers


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", [
    "valid", "no_lease", "entry", "before_commit", "after_flush_postexec",
    "connection_commit", "cancel", "failure",
])
async def test_acknowledgement_commit_requires_current_owner(tmp_path, boundary):
    """A refused commit leaves both persisted facts unchanged, not merely an error."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ack.db'}")
    install_community_sqlite_pragmas(engine)
    register_coordination_providers(
        write_lock_port=CommunityLocalWriteLockPort(kg_base_dir=tmp_path / "leases")
    )
    lease = None
    listeners = []
    fired = []
    try:
        await initialize_current_schema(engine, current_schema_contract())
        async with AsyncSession(engine) as seed:
            seed.add(Board(id="r9-board", realm_id="local", name="R9", owner_id="r9"))
            await seed.flush()
            seed.add(GlobalUpdateOutbox(
                id="r9-outbox", event_id="r9-event", board_id="r9-board",
                session_id="r9-session", event_type="consolidation_committed",
                payload={}, retry_count=0,
            ))
            seed.add(GlobalDiscoveryDeliveryLedger(
                delivery_key="r9-delivery", board_id="r9-board", artifact_type="spec",
                artifact_id="r9-spec", generation=1, delete_event_id="r9-delete",
                state="outbox_persisted", attempt=0, attempt_event_key="r9-event",
            ))
            await seed.commit()

        if boundary != "no_lease":
            lease = GlobalDiscoveryWriterLease.acquire(operation="r9_commit_test")

        def inject(*_):
            fired.append(boundary)
            if boundary == "cancel":
                raise asyncio.CancelledError("injected commit cancellation")
            if boundary == "failure":
                raise RuntimeError("injected commit failure")
            assert lease.lock.release(
                board_id=GLOBAL_DISCOVERY_WRITER_SCOPE, owner_token=lease.owner_token,
            )

        async with engine.connect() as connection:
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                row = await session.get(GlobalUpdateOutbox, "r9-outbox")
                ledger = await session.get(GlobalDiscoveryDeliveryLedger, "r9-delivery")
                row.processed_at = datetime.now(timezone.utc)
                ledger.state = "delivered"
                if boundary in {"before_commit", "after_flush_postexec", "cancel", "failure"}:
                    name = boundary if boundary in {"before_commit", "after_flush_postexec"} else "before_commit"
                    event.listen(session.sync_session, name, inject)
                    listeners.append((session.sync_session, name, inject))
                elif boundary == "connection_commit":
                    event.listen(connection.sync_connection, "commit", inject)
                    listeners.append((connection.sync_connection, "commit", inject))

                expected = (asyncio.CancelledError if boundary == "cancel" else
                            RuntimeError if boundary == "failure" else GlobalDiscoveryWriterFenceLost)
                expectation = nullcontext() if boundary == "valid" else pytest.raises(expected)
                try:
                    with expectation:
                        with lease.guard() if lease else nullcontext():
                            if boundary == "entry":
                                inject()
                            await CommunitySqlAlchemyGlobalOutboxStore().commit(session)
                finally:
                    for target, name, callback in listeners:
                        event.remove(target, name, callback)
                    listeners.clear()
                    await session.rollback()

                if boundary not in {"valid", "no_lease"}:
                    assert fired == [boundary]
                # A separate session reads committed state, never the ORM identity map.
                async with AsyncSession(engine) as observer:
                    persisted = await observer.get(GlobalUpdateOutbox, "r9-outbox")
                    delivered = await observer.get(GlobalDiscoveryDeliveryLedger, "r9-delivery")
                    assert (persisted.processed_at is not None) == (boundary == "valid")
                    assert persisted.retry_count == 0
                    assert delivered.state == ("delivered" if boundary == "valid" else "outbox_persisted")

                # The adapter must remove its fence listener even on failure. Manual
                # relational recovery on this same connection needs no graph writer.
                await session.execute(select(GlobalUpdateOutbox.id))
                await session.commit()
                async with AsyncSession(engine) as observer:
                    persisted = await observer.get(GlobalUpdateOutbox, "r9-outbox")
                    delivered = await observer.get(GlobalDiscoveryDeliveryLedger, "r9-delivery")
                    assert (persisted.processed_at is not None) == (boundary == "valid")
                    assert delivered.state == ("delivered" if boundary == "valid" else "outbox_persisted")
    finally:
        for target, name, callback in listeners:
            event.remove(target, name, callback)
        if lease is not None:
            lease.release()
        await engine.dispose()
