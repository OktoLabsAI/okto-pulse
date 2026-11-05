"""Native SQL/ORM mutation fences with real signed observations in isolated DBs."""

from copy import deepcopy
import json

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.readiness_invalidation_schema import install_readiness_invalidation_triggers
from okto_pulse.community.adapters.current_relational_schema import current_schema_contract, initialize_current_schema
from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_session_factory, install_community_sqlite_pragmas,
)
from okto_pulse.community.adapters.sqlite_verification_currentness import (
    install_verification_currentness_functions, install_verification_currentness_triggers, scenario_facts,
)
from okto_pulse.core.domain.verification_currentness import MAX_VERIFICATION_COUNTER, VerificationCurrentnessError
import test_verification_currentness_store as existing
from test_verification_currentness_store import Acceptance, Reservation, Spec, State

base_authority = existing.authority
native_owner = existing.native_owner


@pytest_asyncio.fixture
async def authority(base_authority):
    engine = base_authority.engine

    @event.listens_for(engine.sync_engine, "connect")
    def functions(connection, _):
        install_verification_currentness_functions(connection)

    async with engine.begin() as connection:
        def install(sync):
            install_verification_currentness_functions(sync.connection)
            install_verification_currentness_triggers(sync)
            install_readiness_invalidation_triggers(sync)
        await connection.run_sync(install)
    return base_authority


async def states(authority):
    async with authority.factory() as session:
        rows = (await session.execute(select(State.__table__).order_by(State.spec_edition, State.scenario_id))).mappings()
        return [dict(row) for row in rows]


async def scenario_edit(authority, change, *, orm=False, commit=True):
    factory = build_community_session_factory(authority.engine) if orm else authority.factory
    async with factory() as session:
        spec = await session.get(Spec, "spec")
        scenarios, criteria = deepcopy(spec.test_scenarios), deepcopy(spec.acceptance_criteria)
        change(scenarios, criteria)
        if orm:
            spec.test_scenarios, spec.acceptance_criteria = scenarios, criteria
        else:
            await session.execute(update(Spec).where(Spec.id == "spec").values(
                test_scenarios=scenarios, acceptance_criteria=criteria))
        await (session.commit() if commit else session.rollback())


async def readiness_revision(authority):
    async with authority.factory() as session:
        return (await session.scalar(text("SELECT source_revision FROM readiness_scope WHERE id='spec:2'"))) or 0


@pytest.mark.asyncio
async def test_native_insert_initializes_scenario_authority_and_scope(authority):
    async with authority.factory.begin() as session:
        spec = await session.get(Spec, "spec")
        await session.execute(insert(Spec).values(id="new", board_id="board", title="New", created_by="actor",
            architecture_adoption={}, edition=1, version=1, status="draft", test_scenarios=spec.test_scenarios,
            acceptance_criteria=spec.acceptance_criteria))
        created = await session.get(State, ("board", "new", 1, "ts"))
        assert created.generation == created.revision == 1 and not created.deleted
        assert created.scenario_sha256 != authority.digest  # Spec identity participates.


@pytest.mark.asyncio
@pytest.mark.parametrize("orm", [False, True], ids=["bulk", "orm"])
@pytest.mark.parametrize("change", ["semantic", "method", "criterion", "reset", "remove_evidence"])
async def test_all_native_writers_invalidate_only_affected_scenario(authority, native_owner, orm, change):
    issued = await authority.issue()
    await native_owner.publish(issued)
    before = (await states(authority))[0]
    def edit(scenarios, criteria):
        if change == "semantic":
            scenarios[0]["then"] = "Different required behavior"
        elif change == "method":
            scenarios[0]["verification_method"] = "demonstration"
        elif change == "criterion":
            criteria[0]["text"] = "Changed criterion"
        elif change == "reset":
            scenarios[0]["status"] = "ready"
        else:
            scenarios[0].pop("evidence")
    await scenario_edit(authority, edit, orm=orm)
    after = (await states(authority))[0]
    assert after["generation"] == before["generation"] + 1
    assert after["last_ordinal"] == after["summary_ordinal"] == 0
    assert not (await authority.read()).results
    with pytest.raises(VerificationCurrentnessError, match="verification_context_stale|verification_currentness_conflict"):
        await authority.publish(issued)


@pytest.mark.asyncio
async def test_new_outcomes_preserve_other_bases_including_incomplete_ready_summary(authority, native_owner):
    accepted = await native_owner.publish(await authority.issue("a"))
    await native_owner.publish(await authority.issue("b"))
    await native_owner.publish(await authority.issue("b", "failed"))
    await native_owner.publish(await authority.issue("c", "inconclusive"))
    read = await authority.read()
    assert read.complete and len(read.results) == 3
    assert {row.result.outcome for row in read.results} == {"passed", "failed", "inconclusive"}
    assert next(row for row in read.results if row.accepted_result_id == accepted["accepted_result_id"]).current
    assert (await states(authority))[0]["generation"] == 1


@pytest.mark.asyncio
async def test_exact_replay_and_unused_reservation_do_not_churn_readiness(authority, native_owner):
    issued = await authority.issue()
    await native_owner.publish(issued)
    before, revision = await states(authority), await readiness_revision(authority)
    assert (await native_owner.publish(issued))["replayed"]
    assert await states(authority) == before
    assert await readiness_revision(authority) == revision
    await authority.issue("unused")
    after = (await states(authority))[0]
    assert after["last_ordinal"] == before[0]["last_ordinal"] + 1
    assert after["revision"] == before[0]["revision"]
    assert await readiness_revision(authority) == revision


@pytest.mark.asyncio
async def test_edit_restore_identical_semantics_never_revives_proof(authority, native_owner):
    issued = await authority.issue()
    await native_owner.publish(issued)
    await scenario_edit(authority, lambda s, c: s[0].update(then="Changed"))
    await scenario_edit(authority, lambda s, c: s[0].update(then="Public ports only"))
    state = (await states(authority))[0]
    assert state["generation"] == 3 and state["scenario_sha256"] == authority.digest
    assert not (await authority.read()).results
    with pytest.raises(VerificationCurrentnessError, match="verification_context_stale|verification_currentness_conflict"):
        await authority.publish(issued)


@pytest.mark.asyncio
async def test_unrelated_scenario_criterion_and_cosmetic_edits_preserve_current_fact(authority, native_owner):
    await native_owner.publish(await authority.issue())
    def append(scenarios, criteria):
        scenarios.append({**scenarios[0], "id": "other", "linked_criteria": [], "status": "draft", "evidence": None})
        criteria.append({"id": "other-ac", "text": "Independent"})
    await scenario_edit(authority, append)
    before = next(s for s in await states(authority) if s["scenario_id"] == "ts")
    def edit(scenarios, criteria):
        scenarios[0]["title"] = "Editorial title"
        scenarios[1]["then"] = "Other behavior"
        criteria[1]["text"] = "Changed independent criterion"
        criteria[0]["linked_task_ids"] = ["task"]
    await scenario_edit(authority, edit)
    after = next(s for s in await states(authority) if s["scenario_id"] == "ts")
    assert after == before and len((await authority.read()).results) == 1


@pytest.mark.asyncio
async def test_noop_does_not_advance_generation_or_policy_epoch(authority):
    before = await states(authority)
    async with authority.factory() as session:
        epoch = (await session.get(Spec, "spec")).test_scenario_policy_epoch
    await scenario_edit(authority, lambda s, c: None, orm=True)
    assert await states(authority) == before
    async with authority.factory() as session:
        assert (await session.get(Spec, "spec")).test_scenario_policy_epoch == epoch


@pytest.mark.asyncio
async def test_reset_preserves_existing_policy_epoch_and_history(authority, native_owner):
    await native_owner.publish(await authority.issue())
    revision = await readiness_revision(authority)
    async with authority.factory() as session:
        epoch = (await session.get(Spec, "spec")).test_scenario_policy_epoch
    await scenario_edit(authority, lambda s, c: s[0].update(status="ready"), orm=True)
    assert await readiness_revision(authority) == revision + 1
    async with authority.factory() as session:
        assert (await session.get(Spec, "spec")).test_scenario_policy_epoch == epoch + 1
        assert (await session.execute(select(Acceptance))).first() is not None
        assert (await session.execute(select(Reservation))).first() is not None


@pytest.mark.asyncio
async def test_scenario_delete_recreate_retains_tombstone_and_old_receipt_is_stale(authority, native_owner):
    issued = await authority.issue()
    await native_owner.publish(issued)
    async with authority.factory() as session:
        original = deepcopy((await session.get(Spec, "spec")).test_scenarios)
    await scenario_edit(authority, lambda s, c: s.clear())
    assert (await states(authority))[0]["deleted"]
    await scenario_edit(authority, lambda s, c: s.extend(original))
    restored = (await states(authority))[0]
    assert restored["generation"] == 3 and not restored["deleted"]
    assert not (await authority.read()).results


@pytest.mark.asyncio
async def test_spec_delete_recreate_and_edition_return_cannot_restore_old_authority(authority, native_owner):
    issued = await authority.issue()
    await native_owner.publish(issued)
    async with authority.factory.begin() as session:
        original = dict((await session.execute(select(Spec.__table__).where(Spec.id == "spec"))).mappings().one())
        await session.execute(delete(Spec).where(Spec.id == "spec"))
    assert (await states(authority))[0]["deleted"]
    async with authority.factory.begin() as session:
        # Preserve SQL NULL by omitting absent optional fields, rather than
        # rebinding them as a JSON literal null during isolated recreation.
        await session.execute(insert(Spec).values(**{key: value for key, value in original.items() if value is not None}))
    assert (await states(authority))[0]["generation"] == 3
    async with authority.factory.begin() as session:
        await session.execute(update(Spec).values(edition=3))
    assert [(s["spec_edition"], s["deleted"]) for s in await states(authority)] == [(2, True), (3, False)]
    # Preserve the existing monotonic started-edition guard too.
    with pytest.raises(DBAPIError, match="spec_dependency_started_edition_invalid"):
        async with authority.factory.begin() as session:
            await session.execute(update(Spec).values(edition=2))
    assert (await states(authority))[0]["generation"] == 4
    assert not (await authority.read()).complete


@pytest.mark.asyncio
async def test_linked_criterion_removal_invalidates_even_unresolved_editing_state(authority, native_owner):
    await native_owner.publish(await authority.issue())
    await scenario_edit(authority, lambda s, c: c.clear())
    assert (await states(authority))[0]["generation"] == 2
    assert not (await authority.read()).complete
    await scenario_edit(authority, lambda s, c: c.append({"id": "ac-1", "text": "Public ports only"}))
    assert (await states(authority))[0]["generation"] == 3
    assert not (await authority.read()).results


@pytest.mark.asyncio
async def test_generation_and_readiness_rollback_with_source_write(authority, native_owner):
    await native_owner.publish(await authority.issue())
    before, revision = await states(authority), await readiness_revision(authority)
    await scenario_edit(authority, lambda s, c: s[0].update(status="ready"), commit=False)
    assert await states(authority) == before
    assert await readiness_revision(authority) == revision
    assert len((await authority.read()).results) == 1


@pytest.mark.asyncio
async def test_counter_exhaustion_rejects_entire_native_edit(authority):
    async with authority.factory.begin() as session:
        await session.execute(update(State).values(generation=MAX_VERIFICATION_COUNTER))
    with pytest.raises(DBAPIError, match="verification_counter_exhausted"):
        await scenario_edit(authority, lambda s, c: s[0].update(then="Cannot commit"))
    async with authority.factory() as session:
        assert (await session.get(Spec, "spec")).test_scenarios[0]["then"] == "Public ports only"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [Reservation, Acceptance])
@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_persisted_verification_history_is_immutable(authority, native_owner, model, operation):
    await native_owner.publish(await authority.issue())
    with pytest.raises(DBAPIError, match="verification_history_immutable"):
        async with authority.factory.begin() as session:
            await session.execute(update(model).values(ordinal=99) if operation == "update" else delete(model))


@pytest.mark.asyncio
@pytest.mark.parametrize("before_issuance", [True, False])
async def test_reset_during_external_work_prevents_acceptance(authority, monkeypatch, before_issuance):
    if before_issuance:
        original = existing.CommunityTestVerificationReportIssuer.admit
        async def reset_then_admit(issuer, request):
            await scenario_edit(authority, lambda s, c: s[0].update(status="draft"))
            return await original(issuer, request)
        monkeypatch.setattr(existing.CommunityTestVerificationReportIssuer, "admit", reset_then_admit)
    issued = await authority.issue()
    if not before_issuance:
        await scenario_edit(authority, lambda s, c: s[0].update(status="draft"))
    with pytest.raises(VerificationCurrentnessError, match="verification_context_stale"):
        await authority.publish(issued)
    assert not (await authority.read()).results
    async with authority.factory() as session:
        assert (await session.execute(select(Acceptance))).first() is None


@pytest.mark.asyncio
async def test_copied_receipt_with_changed_body_cannot_claim_publication_exception(authority, native_owner):
    await native_owner.publish(await authority.issue())
    def tamper(scenarios, criteria):
        scenarios[0]["evidence"]["verification_report"]["observations"][0]["outcome"] = "failed"
    await scenario_edit(authority, tamper)
    assert (await states(authority))[0]["generation"] == 2
    assert not (await authority.read()).results


@pytest.mark.asyncio
async def test_readiness_failure_aborts_source_and_generation_together(authority, native_owner):
    await native_owner.publish(await authority.issue())
    before = await states(authority)
    async with authority.engine.begin() as connection:
        await connection.exec_driver_sql("CREATE TRIGGER fail_readiness BEFORE UPDATE ON readiness_job "
            "BEGIN SELECT RAISE(ABORT,'isolated_readiness_failure'); END")
    with pytest.raises(DBAPIError, match="isolated_readiness_failure"):
        await scenario_edit(authority, lambda s, c: s[0].update(then="Must roll back"))
    assert await states(authority) == before
    assert len((await authority.read()).results) == 1


@pytest.mark.asyncio
async def test_bulk_fact_projection_is_materialized_once_per_statement(authority):
    def populate(scenarios, criteria):
        scenarios.extend({**scenarios[0], "id": f"scenario-{i}"} for i in range(512))
    await scenario_edit(authority, populate)
    calls = []
    def counted(*args):
        calls.append(1)
        return scenario_facts(*args)
    async with authority.engine.begin() as connection:
        await connection.run_sync(lambda sync: sync.connection.create_function(
            "pulse_verification_scenario_facts", 5, counted, deterministic=True))
        raw = (await connection.exec_driver_sql("SELECT test_scenarios FROM specs WHERE id='spec'")).scalar_one()
        scenarios = json.loads(raw)
        scenarios[0]["then"] = "Only this scenario changes"
        await connection.exec_driver_sql("UPDATE specs SET test_scenarios=? WHERE id='spec'", (json.dumps(scenarios),))
    assert len(calls) == 2  # UPDATE existing states and INSERT new states, never N-squared.
    rows = await states(authority)
    assert sum(row["generation"] == 2 for row in rows) == 1


@pytest.mark.asyncio
async def test_fresh_native_runtime_registers_functions_on_every_pooled_connection(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "native.db"))
    install_community_sqlite_pragmas(engine)
    try:
        await initialize_current_schema(engine, current_schema_contract())
        # Hold the first checkout while using a second physical connection.
        async with engine.connect() as first, engine.begin() as second:
            assert await first.scalar(text("SELECT pulse_verification_scenario_facts('b','s','[]','[]','[]')")) == "[]"
            await second.exec_driver_sql("INSERT INTO boards(id,name,owner_id,realm_id) VALUES('b','Board','actor','local')")
            await second.exec_driver_sql("INSERT INTO specs(id,board_id,title,status,version,created_by,architecture_adoption,test_scenarios) "
                "VALUES('s','b','Spec','draft',1,'actor','{}',?)", (json.dumps([{"id": "ts", "status": "draft"}]),))
            assert (await second.exec_driver_sql("SELECT generation FROM verification_scenario_states "
                "WHERE board_id='b' AND spec_id='s' AND scenario_id='ts'")).scalar_one() == 1
    finally:
        await engine.dispose()
