"""Production relational projection and whole-batch preparation stay read-only."""

import pytest
from sqlalchemy import event

from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    CommunitySqlAlchemyApplicationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_structured_spec import (
    CommunitySqlAlchemyStructuredSpecStore,
)
from okto_pulse.core.ports.application_persistence import (
    register_application_persistence_port,
)
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.permission_policy import (
    builtin_permission_presets,
    resolve_effective_permissions,
)
from okto_pulse.core.ports.structured_spec import register_structured_spec_store
from okto_pulse.core.services.spec_structured_entities import (
    PreparedIntegrationRequirementCreates,
    StructuredSpecEntityCommand,
    StructuredSpecEntityService,
)

from test_architecture_candidates_integration import adopted_context as adopted_context


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["valid", "stale_edition", "invalid_last_ir"])
async def test_real_adapter_preserves_edition_and_prepares_without_sql_writes(
    adopted_context, case
):
    db = adopted_context
    db.info["realm_scope"] = RealmScope.local()
    store = CommunitySqlAlchemyStructuredSpecStore()
    register_structured_spec_store(store)
    register_application_persistence_port(CommunitySqlAlchemyApplicationPersistence())
    before = await store.get(db, spec_id="spec")
    assert before.edition == 2
    preset = next(
        item for item in builtin_permission_presets() if item["name"] == "Spec"
    )
    command = StructuredSpecEntityCommand(
        spec_id="spec",
        board_id="board",
        actor_id="author",
        entity_type="integration_requirement",
        operation="create",
        expected_spec_version=before.version,
        expected_spec_edition=1 if case == "stale_edition" else 2,
        permission_set=resolve_effective_permissions(None, preset["flags"], None),
    )
    payloads = [
        {"title": "Publish an event", "integration_type": "event"},
        {"title": "Import a file", "integration_type": "file"},
    ]
    if case == "invalid_last_ir":
        payloads[1]["linked_api_contracts"] = ["api_missing"]
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
    try:
        prepared = await StructuredSpecEntityService(
            db
        ).prepare_integration_requirement_creates(command, payloads)
        await db.commit()
        db.expire_all()
        after = await store.get(db, spec_id="spec")
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", capture)
    assert after == before
    assert statements and all(
        sql.lstrip().upper().startswith("SELECT") for sql in statements
    )
    if case == "valid":
        assert isinstance(prepared, PreparedIntegrationRequirementCreates)
        assert prepared.expected_spec_edition == 2
        assert len(set(prepared.entity_ids)) == 2
        assert all(item.startswith("ir_") for item in prepared.entity_ids)
        assert [
            item["integration_type"]
            for item in prepared.update_data["integration_requirements"]
        ] == ["event", "file"]
        assert all("id" not in item for item in payloads)
    else:
        assert prepared.success is False
        assert prepared.error_code == (
            "version_conflict" if case == "stale_edition" else "link_target_invalid"
        )
