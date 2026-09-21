"""Atomic lifecycle completion bound to the exact physical-cut checkpoint.

This is still an offline checkpoint, not runtime admission. Its replay validates
the frozen candidate; normal user writes after eventual activation need a
separate admission contract rather than replaying this migration snapshot.
"""

from dataclasses import asdict, dataclass
import hashlib
import re

from .data_bootstrapper import make_community_data_bootstrapper
from .card_validation_retirement import _load_cards
from .relational_schema_lifecycle import CommunityRelationalSchemaLifecycleOrchestrator
from .relational_schema_migrator import make_community_relational_schema_migrator
from .relational_schema_transaction import schema_transaction_runtime
from .retirement_data_journal import ensure_retirement_data_journal, read_retirement_data_journal, record_retirement_stage
from .retirement_schema_cutover import SchemaRetirementCheckpoint
from .retirement_schema_storage import RETIRED_TABLES, _TEMP, _objects, require_cut_schema, surviving_data
from .sprint_retirement_archive import _encode


def _digest(value):
    return hashlib.sha256(_encode(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class BootstrapRetirementCheckpoint:
    migration_id: str
    schema_checkpoint_sha256: str
    lifecycle_sha256: str
    after_schema_sha256: str
    data_sha256: str
    cards: int

    def __post_init__(self):
        if (type(self.migration_id) is not str or not 1 <= len(self.migration_id) <= 128
                or any(type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
                    for value in (self.schema_checkpoint_sha256, self.lifecycle_sha256, self.after_schema_sha256, self.data_sha256))
                or type(self.cards) is not int or not 0 <= self.cards <= 100_000):
            raise ValueError("retirement_bootstrap_checkpoint_invalid")


def _snapshot(connection):
    objects = _objects(connection)
    if (any(name.casefold() in {*RETIRED_TABLES, _TEMP} for _, name, _, _ in objects)
            or any(row[1].casefold() == 'sprint_id' for row in connection.exec_driver_sql('PRAGMA table_xinfo("cards")'))
            or connection.exec_driver_sql('PRAGMA foreign_key_check').fetchmany(1)):
        raise ValueError("retirement_bootstrap_target_invalid")
    data = surviving_data(connection)
    return {'after_schema_sha256': _digest(objects), 'data_sha256': _digest(data), 'cards': data['cards']['count']}


def _lifecycle():
    migrator = make_community_relational_schema_migrator()
    bootstrapper = make_community_data_bootstrapper()
    fingerprint = _digest({"schema": asdict(migrator.plan(target="community-sqlite")),
        "bootstrap": asdict(bootstrapper.plan(target="community-sqlite"))})
    return CommunityRelationalSchemaLifecycleOrchestrator(migrator=migrator, bootstrapper=bootstrapper), fingerprint


async def complete_retirement_bootstrap(engine, run, *, verify_dependency):
    """Caller holds the continuous schema/startup/graph writer window.

    The required dependency verifier checks retained archives, graph/outbox,
    permission evidence and surviving authority on this same SQL connection.
    """
    if engine.dialect.name != 'sqlite' or not callable(verify_dependency):
        raise ValueError('retirement_bootstrap_coordinator_required')
    orchestrator, fingerprint = _lifecycle()
    async with engine.connect() as connection:
        try:
            async with schema_transaction_runtime(connection):
                records = await read_retirement_data_journal(connection, run)
                if len(records) not in {8, 9}:
                    raise ValueError('retirement_bootstrap_dependencies_incomplete')
                schema = SchemaRetirementCheckpoint(**records[7]['payload'])
                await verify_dependency(connection)
                if len(records) == 9:
                    receipt = BootstrapRetirementCheckpoint(**records[8]['payload'])
                    if (receipt.schema_checkpoint_sha256 != _digest(asdict(schema))
                            or receipt.lifecycle_sha256 != fingerprint
                            or await connection.run_sync(_snapshot) != {key: getattr(receipt, key)
                                for key in ('after_schema_sha256', 'data_sha256', 'cards')}):
                        raise ValueError('retirement_bootstrap_replay_mismatch')
                else:
                    # In particular, a max-7 historical journal remains part
                    # of this original hash. Validate it before expanding DDL.
                    await connection.run_sync(lambda sync: require_cut_schema(sync, schema))
                    card_content = {row['id']: {key: value for key, value in row.items() if key != 'position'}
                        for row in await _load_cards(connection, linked_only=False)}
                    await ensure_retirement_data_journal(connection)
                    await orchestrator.initialize_schema()
                    if {row['id']: {key: value for key, value in row.items() if key != 'position'}
                            for row in await _load_cards(connection, linked_only=False)} != card_content:
                        raise ValueError('retirement_bootstrap_card_content_changed')
                    values = await connection.run_sync(_snapshot)
                    if values['cards'] != schema.cards:
                        raise ValueError('retirement_bootstrap_card_population_changed')
                    receipt = BootstrapRetirementCheckpoint(run.migration_id, _digest(asdict(schema)), fingerprint, **values)
                await record_retirement_stage(connection, run, 'bootstrap', receipt, replay=len(records) == 9)
                await verify_dependency(connection)
                if await connection.run_sync(_snapshot) != {key: getattr(receipt, key)
                        for key in ('after_schema_sha256', 'data_sha256', 'cards')}:
                    raise ValueError('retirement_bootstrap_write_mismatch')
                complete = await read_retirement_data_journal(connection, run)
                if complete[:len(records)] != records or len(complete) != 9:
                    raise ValueError('retirement_bootstrap_checkpoint_changed')
            await connection.commit()
            return receipt
        except BaseException:
            await connection.rollback()
            raise
