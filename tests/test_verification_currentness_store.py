"""Real isolated SQLite authority and ledger tests; never live Delivery proof."""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, func, insert, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog, Base, Board, Spec, VerificationAcceptanceRow as Acceptance,
    VerificationBasisHeadRow as Head, VerificationReservationRow as Reservation,
    VerificationScenarioStateRow as State,
)
from okto_pulse.community.adapters.sqlalchemy_verification_currentness import (
    CommunityVerificationCurrentnessStore as Store,
    CommunityVerificationReservationPort as ReservationPort,
)
from okto_pulse.community.adapters.test_evidence import (
    CommunityEvidenceLedger, CommunityTestEvidenceWriteVerifier,
    CommunityTestVerificationReportIssuer,
)
from okto_pulse.core.domain.enums import SpecStatus
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.domain.verification_currentness import (
    MAX_VERIFICATION_COUNTER, VerificationCurrentnessError, VerificationScope,
    VerificationHistoryCursor, VerificationHistoryQuery,
    canonical_scenario_sha256, result_from_authenticated_evidence,
)
from okto_pulse.core.ports.test_evidence import TestVerificationReportRequest as ReportRequest
from okto_pulse.core.ports.verification_currentness import VerificationReservationRequest
from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256
from verification_report_fixtures import report


SCOPE = VerificationScope(board_id="board", spec_id="spec", spec_edition=2, scenario_id="ts")


@pytest_asyncio.fixture
async def authority(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "authority.db"))

    @event.listens_for(engine.sync_engine, "connect")
    def pragmas(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, info={"realm_scope": RealmScope.local()})
    scenario = {"id": "ts", "given": "Source is available", "when": "Inspected", "then": "Public ports only",
                "verification_method": "inspection", "linked_criteria": ["ac-1"], "status": "ready"}
    criteria = [{"id": "ac-1", "text": "Public ports only"}]
    digest = canonical_scenario_sha256(compute_test_scenario_semantic_sha256(
        board_id="board", spec_id="spec", scenario=scenario, acceptance_criteria=criteria))
    async with factory.begin() as session:
        await session.execute(insert(Board).values(id="board", name="Isolated", owner_id="actor", realm_id="local"))
        await session.execute(insert(Spec).values(id="spec", board_id="board", title="Isolated spec", created_by="actor",
            architecture_adoption={}, status=SpecStatus.IN_PROGRESS, edition=2, version=7,
            test_scenarios=[scenario], acceptance_criteria=criteria))
        # Generation creation belongs to R5.3. This fixture seeds that explicit
        # committed state; reserve/read must not repair a missing authority row.
        await session.execute(insert(State).values(**SCOPE.model_dump(), generation=1,
            scenario_sha256=digest, verification_method="inspection"))
    ledger = CommunityEvidenceLedger(evidence_root=tmp_path / "evidence")
    verifier = CommunityTestEvidenceWriteVerifier(ledger=ledger)
    provider = ReservationPort(factory)

    async def issue(revision="revision-a", outcome="passed", *, actor_id="actor", actor_kind="human", companions=(), observations=1):
        async with factory() as session:
            version = await session.scalar(select(Spec.version).where(Spec.id == "spec"))
        reserved = await provider.reserve(VerificationReservationRequest(SCOPE, digest, "inspection",
            actor_id, actor_kind, version))
        value = report("inspection")
        value["sources"][0]["revision"] = revision
        value["inspection_procedure"]["revision"] = revision
        value["sources"].extend({"reference": source, "revision": rev, "sha256": "b" * 64} for source, rev in companions)
        value["result"] = value["observations"][0]["outcome"] = outcome
        if observations != 1:
            value["observations"] = [{**value["observations"][0], "observation_id": f"o-{index}"}
                                     for index in range(observations)]
        issued = await CommunityTestVerificationReportIssuer(ledger=ledger).admit(ReportRequest(
            board_id="board", spec_id="spec", scenario_id="ts", scenario_sha256="sha256:" + digest,
            actor_id=actor_id, report=value, verification_context=reserved.context))
        normalized = result_from_authenticated_evidence(issued.evidence, actor_id=actor_id, actor_kind=actor_kind)
        return SimpleNamespace(result=normalized, evidence=issued.evidence,
            status=outcome if outcome in {"passed", "failed"} else "ready", version=version)

    async def publish(issued, *, commit=True):
        async with factory() as session:
            value = await Store(session, verifier=verifier).publish(issued.result,
                evidence=issued.evidence, evidence_status=issued.status, expected_spec_version=issued.version)
            if commit:
                await session.commit()
            else:
                await session.rollback()
            return value

    async def read(*, accepted_ids=()):
        async with factory() as session:
            return await Store(session, verifier=verifier).read_current(board_id="board", spec_id="spec",
                spec_edition=2, scenario_ids=("ts",), accepted_result_ids=accepted_ids)

    yield SimpleNamespace(engine=engine, factory=factory, ledger=ledger, verifier=verifier,
        provider=provider, digest=digest, issue=issue, publish=publish, read=read)
    await engine.dispose()


@pytest_asyncio.fixture
async def history_authority(authority):
    from okto_pulse.community.adapters.sqlite_verification_currentness import (
        install_verification_currentness_functions, install_verification_currentness_triggers,
    )

    @event.listens_for(authority.engine.sync_engine, "connect")
    def functions(connection, _):
        install_verification_currentness_functions(connection)

    async with authority.engine.begin() as connection:
        def install(sync):
            install_verification_currentness_functions(sync.connection)
            install_verification_currentness_triggers(sync)
        await connection.run_sync(install)
    return authority


async def history(authority, *, scope=SCOPE, limit=2, cursor=None):
    async with authority.factory() as session:
        return await Store(session, verifier=authority.verifier).read_history(
            VerificationHistoryQuery(scope=scope, limit=limit, cursor=cursor))


@pytest.mark.asyncio
async def test_history_pages_every_acceptance_without_selecting_old_passing_credit(authority):
    accepted = []
    for revision, outcome in [("a", "passed"), ("b", "passed"), ("b", "failed"),
                              ("c", "inconclusive"), ("a", "passed")]:
        accepted.append((await authority.publish(await authority.issue(revision, outcome))).acceptance)
    pages, cursor = [], None
    while True:
        page = await history(authority, cursor=cursor)
        assert page.complete and len(page.results) <= 2 and not page.reason_codes
        pages.append(page)
        if not page.has_more:
            break
        cursor = page.next_cursor
    rows = [row for page in pages for row in page.results]
    assert [row.accepted_result_id for row in rows] == [row.accepted_result_id for row in reversed(accepted)]
    assert [row.result.context.ordinal for row in rows] == [5, 4, 3, 2, 1]
    assert [row.current for row in rows] == [True, True, True, False, False]
    assert [row.result.outcome for row in rows] == ["passed", "inconclusive", "failed", "passed", "passed"]
    assert {page.source_revision for page in pages} == {6}
    assert all(page.spec_edition_at_read == 2 for page in pages)
    assert pages[-1].next_cursor is None


@pytest.mark.asyncio
async def test_history_empty_scope_is_distinct_from_missing_native_authority(authority):
    empty = await history(authority)
    assert empty.complete and not empty.results and empty.has_more is False
    async with authority.factory.begin() as session:
        await session.execute(delete(State))
    missing = await history(authority)
    assert not missing.complete and missing.has_more is None and not missing.results
    assert missing.reason_codes == ("verification_currentness_unavailable",)


@pytest.mark.asyncio
async def test_history_keyset_uses_scope_index_and_only_authenticates_requested_page(authority):
    from sqlalchemy import text

    for ordinal in range(6):
        await authority.publish(await authority.issue(str(ordinal)))
    observed = []

    @event.listens_for(authority.engine.sync_engine, "before_cursor_execute")
    def capture(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().startswith("SELECT"):
            observed.append((statement, parameters))

    calls = []
    original = authority.verifier.verify
    authority.verifier.verify = lambda **kwargs: (calls.append(kwargs["evidence"]), original(**kwargs))[1]
    try:
        first = await history(authority)
        second = await history(authority, cursor=first.next_cursor)
        assert first.complete and second.complete and len(calls) == 4
    finally:
        authority.verifier.verify = original
        event.remove(authority.engine.sync_engine, "before_cursor_execute", capture)
    statements = [(sql, params) for sql, params in observed if "LEFT OUTER JOIN verification_reservations" in sql]
    assert len(statements) == 2
    assert "(verification_acceptances.generation, verification_acceptances.ordinal) <" in statements[1][0]
    async with authority.engine.connect() as connection:
        for sql, params in statements:
            plan = " ".join(str(row) for row in await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, params))
            assert "ix_verification_acceptance_scope" in plan
            assert "SCAN verification_acceptances" not in plan and "USE TEMP B-TREE" not in plan
            assert params[-2:] == (3, 0)  # LIMIT page+1; never a growing OFFSET.
        assert await connection.scalar(text("SELECT count(*) FROM verification_acceptances")) == 6


@pytest.mark.asyncio
async def test_history_unused_reservation_and_replay_keep_cursor_valid_but_publication_stales_it(authority):
    first = await authority.issue("a")
    await authority.publish(first)
    second = await authority.issue("b")
    await authority.publish(second)
    page = await history(authority, limit=1)
    pending = await authority.issue("c")
    assert (await authority.publish(second)).replayed
    assert (await history(authority, limit=1, cursor=page.next_cursor)).complete
    await authority.publish(pending)
    stale = await history(authority, limit=1, cursor=page.next_cursor)
    assert not stale.complete and stale.has_more is None and stale.next_cursor is None
    assert stale.reason_codes == ("verification_history_cursor_stale",)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["reset", "delete", "edition"])
async def test_history_survives_native_reset_deletion_and_edition_as_noncurrent(history_authority, native_owner, mutation):
    authority = history_authority
    await native_owner.publish(await authority.issue("a"))
    accepted = await native_owner.publish(await authority.issue("b"))
    page = await history(authority, limit=1)
    assert page.complete and page.results[0].accepted_result_id == accepted["accepted_result_id"]
    async with authority.factory.begin() as session:
        spec = await session.get(Spec, "spec")
        scenario = {**spec.test_scenarios[0], "status": "ready"}
        scenario.pop("evidence", None)
        values = {"edition": 3} if mutation == "edition" else {"test_scenarios": [] if mutation == "delete" else [scenario]}
        await session.execute(update(Spec).where(Spec.id == "spec").values(**values))
    stale = await history(authority, limit=1, cursor=page.next_cursor)
    assert not stale.complete and stale.reason_codes == ("verification_history_cursor_stale",)
    read = await history(authority, limit=100)
    assert read.complete and len(read.results) == 2 and not any(row.current for row in read.results)


@pytest.mark.asyncio
async def test_history_pins_one_wal_snapshot_while_a_new_result_commits(authority, monkeypatch):
    old = await authority.issue()
    old_accepted = await authority.publish(old)
    newer = await authority.issue(outcome="failed")
    published = False
    async with authority.factory() as session:
        execute = session.execute

        async def concurrent_publish(statement, *args, **kwargs):
            nonlocal published
            result = await execute(statement, *args, **kwargs)
            if not published and "FROM verification_scenario_states" in str(statement):
                published = True
                await authority.publish(newer)
            return result

        monkeypatch.setattr(session, "execute", concurrent_publish)
        old_page = await Store(session, verifier=authority.verifier).read_history(VerificationHistoryQuery(scope=SCOPE))
        assert published and old_page.complete
        assert [row.accepted_result_id for row in old_page.results] == [old_accepted.acceptance.accepted_result_id]
        assert old_page.results[0].current and old_page.results[0].result.outcome == "passed"
        assert session.in_transaction()  # The read neither commits nor ends its caller's transaction.
    latest = await history(authority)
    assert latest.complete and latest.results[0].result.outcome == "failed" and latest.results[0].current
    assert not latest.results[1].current


@pytest.mark.asyncio
async def test_current_heads_and_revision_share_one_wal_snapshot(authority, monkeypatch):
    old = await authority.issue()
    accepted = await authority.publish(old)
    newer = await authority.issue(outcome="failed")
    published = False
    async with authority.factory() as session:
        execute = session.execute

        async def concurrent_publish(statement, *args, **kwargs):
            nonlocal published
            result = await execute(statement, *args, **kwargs)
            if not published and "FROM verification_scenario_states" in str(statement):
                published = True
                await authority.publish(newer)
            return result

        monkeypatch.setattr(session, "execute", concurrent_publish)
        read = await Store(session, verifier=authority.verifier).read_current(board_id="board",
            spec_id="spec", spec_edition=2, scenario_ids=("ts",))
        assert published and read.complete and read.source_revision == 2
        assert len(read.results) == 1 and read.results[0].accepted_result_id == accepted.acceptance.accepted_result_id
    latest = await authority.read()
    assert latest.complete and latest.source_revision == 3 and latest.results[0].result.outcome == "failed"


@pytest.mark.asyncio
async def test_history_cannot_commit_caller_writes_or_repair_persistent_state(authority):
    await authority.publish(await authority.issue())
    async with authority.factory() as session:
        await session.execute(update(Board).where(Board.id == "board").values(name="Uncommitted"))
        page = await Store(session, verifier=authority.verifier).read_history(VerificationHistoryQuery(scope=SCOPE))
        assert page.complete and session.in_transaction()
        await session.rollback()
    async with authority.factory() as session:
        assert (await session.get(Board, "board")).name == "Isolated"


@pytest.mark.asyncio
async def test_history_reauthenticates_persisted_payload_and_fails_explicitly(authority):
    accepted = (await authority.publish(await authority.issue())).acceptance
    async with authority.factory.begin() as session:
        bad = deepcopy(accepted.evidence)
        bad["verification_report"]["conclusion"] = "Forged"
        await session.execute(update(Acceptance).values(evidence=bad))
    page = await history(authority)
    assert not page.complete and page.has_more is None and not page.results
    assert page.reason_codes == ("verification_receipt_invalid",)


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", ["MAX_FACTS", "MAX_SCENARIOS"])
async def test_history_capacity_never_becomes_empty_success(authority, monkeypatch, bound):
    import okto_pulse.community.adapters.sqlalchemy_verification_currentness as adapter

    await authority.publish(await authority.issue())
    monkeypatch.setattr(adapter, bound, 0)
    page = await history(authority)
    assert not page.complete and page.has_more is None and not page.results
    assert page.reason_codes == ("verification_resolution_limit",)


@pytest.mark.asyncio
async def test_history_rejects_missing_anchor_and_wrong_scope(authority):
    await authority.publish(await authority.issue("a"))
    await authority.publish(await authority.issue("b"))
    first = await history(authority, limit=1)
    cursor = VerificationHistoryCursor.model_validate({**first.next_cursor.model_dump(), "before_acceptance_id": "missing"})
    invalid = await history(authority, limit=1, cursor=cursor)
    assert not invalid.complete and invalid.reason_codes == ("verification_history_cursor_invalid",)
    foreign = VerificationScope(**{**SCOPE.model_dump(), "board_id": "private"})
    with pytest.raises(ValueError, match="verification_history_cursor_invalid"):
        VerificationHistoryQuery(scope=foreign, limit=1, cursor=first.next_cursor)
    assert not (await history(authority, scope=foreign)).complete


@pytest.mark.parametrize("limit", [0, -1, 101, True, 1.5, "2"])
def test_history_query_rejects_invalid_page_bounds(limit):
    with pytest.raises(ValueError):
        VerificationHistoryQuery(scope=SCOPE, limit=limit)


@pytest.mark.parametrize("field,value", [("before_ordinal", 0), ("source_revision", True),
    ("before_generation", -1), ("spec_edition_at_read", 1), ("before_acceptance_id", ""), ("page_size", 101)])
def test_history_cursor_rejects_invalid_internal_keyset(field, value):
    with pytest.raises(ValueError):
        VerificationHistoryCursor.model_validate({"scope": SCOPE, "spec_edition_at_read": 2,
            "source_revision": 1, "page_size": 2, "before_generation": 1,
            "before_ordinal": 1, "before_acceptance_id": "accepted", field: value})


@pytest.mark.asyncio
async def test_reservation_is_durable_releases_writer_and_does_not_change_read_revision(authority):
    a = await authority.issue()
    # A different connection can acquire a writer while the test is external.
    async with authority.factory.begin() as session:
        state = await session.get(State, tuple(SCOPE.model_dump().values()))
        assert state.last_ordinal == 1 and state.revision == 1 and state.summary_ordinal == 0
        assert await session.scalar(select(func.count()).select_from(Reservation)) == 1
        await session.execute(update(Board).where(Board.id == "board").values(name="Writer released"))
        assert a.result.context.ordinal == 1
    read = await authority.read()
    assert read.complete and not read.results


@pytest.mark.asyncio
async def test_concurrent_reservations_have_unique_monotonic_server_order(authority):
    issued = await asyncio.gather(*(authority.issue(f"revision-{i}") for i in range(8)))
    assert sorted(item.result.context.ordinal for item in issued) == list(range(1, 9))
    assert len({item.result.context.reservation_id for item in issued}) == 8


@pytest.mark.asyncio
async def test_independent_bases_and_delayed_results_do_not_roll_back_global_summary(authority):
    a, b = await authority.issue("a"), await authority.issue("b", "failed")
    latest = await authority.publish(b)
    earlier = await authority.publish(a)
    assert latest.replace_operational_summary and not earlier.replace_operational_summary
    read = await authority.read()
    assert read.complete and len(read.results) == 2
    assert {r.result.outcome for r in read.results} == {"passed", "failed"}
    async with authority.factory() as session:
        assert (await session.get(State, tuple(SCOPE.model_dump().values()))).summary_ordinal == 2


@pytest.mark.asyncio
async def test_same_basis_failure_supersedes_pass_and_exact_old_replay_stays_noncurrent(authority):
    first = await authority.issue()
    accepted = await authority.publish(first)
    failure = await authority.issue(outcome="failed")
    failed = await authority.publish(failure)
    replay = await authority.publish(first)
    assert replay.replayed and not replay.acceptance.current
    assert replay.acceptance.accepted_result_id == accepted.acceptance.accepted_result_id
    read = await authority.read(accepted_ids=(accepted.acceptance.accepted_result_id,))
    assert read.complete and len(read.results) == 2
    assert [r.result.outcome for r in read.results if r.current] == ["failed"]
    assert failed.acceptance.current


@pytest.mark.asyncio
async def test_delayed_unaccepted_success_cannot_replace_newer_failure(authority):
    older = await authority.issue()
    newer = await authority.issue(outcome="failed")
    await authority.publish(newer)
    with pytest.raises(VerificationCurrentnessError, match="verification_result_superseded"):
        await authority.publish(older)
    assert [r.result.outcome for r in (await authority.read()).results] == ["failed"]


@pytest.mark.asyncio
async def test_owner_rollback_discards_acceptance_head_and_summary_together(authority):
    issued = await authority.issue()
    await authority.publish(issued, commit=False)
    async with authority.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Acceptance)) == 0
        assert await session.scalar(select(func.count()).select_from(Head)) == 0
        state = await session.get(State, tuple(SCOPE.model_dump().values()))
        assert state.summary_ordinal == 0 and state.revision == 1
        assert await session.scalar(select(func.count()).select_from(Reservation)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["version", "generation", "semantic", "method", "deleted"])
async def test_publication_rechecks_actual_storage_and_never_repairs_it(authority, change):
    issued = await authority.issue()
    async with authority.factory.begin() as session:
        if change == "version":
            await session.execute(update(Spec).values(version=8))
        else:
            values = {"generation": 2} if change == "generation" else (
                {"scenario_sha256": "f" * 64} if change == "semantic" else (
                    {"verification_method": "demonstration"} if change == "method" else {"deleted": True}))
            await session.execute(update(State).values(**values))
    with pytest.raises(VerificationCurrentnessError, match="verification_currentness_conflict|verification_context_stale"):
        await authority.publish(issued)
    async with authority.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Acceptance)) == 0


@pytest.mark.asyncio
async def test_signed_payload_and_persisted_result_tampering_fail_closed(authority):
    issued = await authority.issue()
    original = deepcopy(issued.evidence)
    issued.evidence["verification_context"]["ordinal"] += 1
    with pytest.raises(VerificationCurrentnessError, match="verification_receipt_invalid"):
        await authority.publish(issued)
    issued.evidence = original
    accepted = await authority.publish(issued)
    async with authority.factory.begin() as session:
        changed = issued.result.model_dump(mode="json")
        changed["outcome"] = "failed"
        await session.execute(update(Acceptance).where(Acceptance.id == accepted.acceptance.accepted_result_id).values(result=changed))
    read = await authority.read()
    assert not read.complete and read.reason_codes == ("verification_currentness_unavailable",)


@pytest.mark.asyncio
async def test_counter_exhaustion_does_not_wrap_or_issue_a_reservation(authority):
    async with authority.factory.begin() as session:
        await session.execute(update(State).values(last_ordinal=MAX_VERIFICATION_COUNTER))
    with pytest.raises(VerificationCurrentnessError, match="verification_counter_exhausted"):
        await authority.issue()
    async with authority.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Reservation)) == 0


@pytest.mark.asyncio
async def test_reader_uses_head_index_and_explicit_history_ids_not_all_history(authority):
    old = await authority.publish(await authority.issue())
    await authority.publish(await authority.issue(outcome="failed"))
    statements = []

    def capture(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append((statement, parameters))

    event.listen(authority.engine.sync_engine, "before_cursor_execute", capture)
    try:
        read = await authority.read()
    finally:
        event.remove(authority.engine.sync_engine, "before_cursor_execute", capture)
    assert read.complete and len(read.results) == 1 and len(statements) == 3
    query, parameters = next(item for item in statements if "FROM verification_basis_heads" in item[0])
    async with authority.engine.connect() as connection:
        plan = (await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + query, parameters)).all()
    assert not any("SCAN verification_acceptances" in row[-1] for row in plan)
    historical = await authority.read(accepted_ids=(old.acceptance.accepted_result_id,))
    assert historical.complete and len(historical.results) == 2


@pytest.mark.asyncio
async def test_bounds_and_missing_requested_history_are_explicitly_incomplete(authority, monkeypatch):
    from okto_pulse.community.adapters import sqlalchemy_verification_currentness as module

    assert module.MAX_SCENARIOS == 5000 and module.MAX_FACTS == 20000
    await authority.publish(await authority.issue("a"))
    await authority.publish(await authority.issue("b"))
    missing = await authority.read(accepted_ids=("foreign-or-missing",))
    assert not missing.complete and missing.reason_codes == ("verification_acceptance_not_found",)
    monkeypatch.setattr(module, "MAX_FACTS", 3)
    limited = await authority.read()
    assert not limited.complete and limited.reason_codes == ("verification_resolution_limit",)


@pytest.mark.asyncio
async def test_real_5000_scenario_boundary_and_overflow_use_native_generation_states(history_authority):
    authority = history_authority
    async with authority.factory.begin() as session:
        spec = await session.get(Spec, "spec")
        original = deepcopy(spec.test_scenarios[0])
        scenarios = [original] + [{**original, "id": f"scenario-{index}"} for index in range(1, 5000)]
        await session.execute(update(Spec).where(Spec.id == "spec").values(test_scenarios=scenarios))
        assert await session.scalar(select(func.count()).select_from(State)) == 5000
    accepted = (await authority.publish(await authority.issue())).acceptance
    async with authority.factory() as session:
        read = await Store(session, verifier=authority.verifier).read_current(board_id="board",
            spec_id="spec", spec_edition=2, scenario_ids=())
        assert read.complete and len(read.results) == 1 and read.source_revision == 5001
        assert read.results[0].accepted_result_id == accepted.accepted_result_id
    async with authority.factory.begin() as session:
        await session.execute(update(Spec).where(Spec.id == "spec").values(
            test_scenarios=[*scenarios, {**original, "id": "scenario-5000"}]))
        assert await session.scalar(select(func.count()).select_from(State)) == 5001
    async with authority.factory() as session:
        overflow = await Store(session, verifier=authority.verifier).read_current(board_id="board",
            spec_id="spec", spec_edition=2, scenario_ids=())
    assert not overflow.complete and overflow.reason_codes == ("verification_resolution_limit",)
    assert not overflow.results  # No zero-success projection for an over-limit population.


@pytest.mark.asyncio
async def test_real_20000_expanded_fact_boundary_authenticates_every_retained_basis(authority):
    companions = tuple((f"source-{index}", "revision") for index in range(19))
    for index in range(165):
        await authority.publish(await authority.issue(f"basis-{index}", companions=companions, observations=100))
    await authority.publish(await authority.issue("last-supported", companions=companions, observations=14))
    # 165 * (1 acceptance + 20 sources + 100 observations) + (1 + 20 + 14).
    assert 165 * 121 + 35 == 20000
    supported = await authority.read()
    assert supported.complete and len(supported.results) == 166
    assert sum(1 + len(row.result.basis.sources) + len(row.evidence["verification_report"]["observations"])
               for row in supported.results) == 20000
    assert all(row.current for row in supported.results)
    await authority.publish(await authority.issue("overflow"))
    overflow = await authority.read()
    assert not overflow.complete and overflow.reason_codes == ("verification_resolution_limit",)
    async with authority.factory() as session:
        # Storage remains append-only and unbounded by the read limit.
        assert await session.scalar(select(func.count()).select_from(Acceptance)) == 167
        assert await session.scalar(select(func.count()).select_from(Head)) == 167


@pytest.mark.asyncio
async def test_replay_after_reset_is_rejected_even_when_semantics_are_restored(authority):
    issued = await authority.issue()
    await authority.publish(issued)
    async with authority.factory.begin() as session:
        await session.execute(update(State).values(generation=3, revision=3))
    with pytest.raises(VerificationCurrentnessError, match="verification_context_stale"):
        await authority.publish(issued)
    read = await authority.read()
    assert read.complete and not read.results


@pytest.mark.asyncio
async def test_complete_source_set_keeps_different_companion_revisions_independent(authority):
    a = await authority.issue(companions=(("source-companion", "a"),))
    b = await authority.issue(companions=(("source-companion", "b"),), outcome="failed")
    await authority.publish(a)
    await authority.publish(b)
    read = await authority.read()
    assert read.complete and len(read.results) == 2
    assert all(len(item.result.basis.sources) == 2 for item in read.results)
    assert len({item.result.basis.digest for item in read.results}) == 2


@pytest.fixture
def native_owner(authority, monkeypatch):
    """Keep real scope, receipt, storage and transaction owners; isolate guideline policy."""
    from okto_pulse.community.adapters.sqlalchemy_application_persistence import CommunitySqlAlchemyApplicationPersistence
    from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
    from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
    from okto_pulse.core.application.use_cases.base import ActorContext
    from okto_pulse.core.ports import test_evidence, verification_currentness
    from okto_pulse.core.services import main

    adapter = CommunitySqlAlchemyApplicationPersistence()
    monkeypatch.setattr(main, "get_application_persistence_port", lambda: adapter)
    monkeypatch.setattr(test_evidence, "resolve_test_evidence_write_verifier", lambda: authority.verifier)
    monkeypatch.setattr(verification_currentness, "require_verification_currentness_store",
                        lambda session: Store(session, verifier=authority.verifier))

    async def guideline_policy(self, **kwargs):
        return None

    monkeypatch.setattr(main.GuidelineService, "enforce_policy_transition", guideline_policy)
    owner_factory = build_community_session_factory(authority.engine)

    async def publish(issued, *, actor_kind="human"):
        async with owner_factory() as session:
            actor = ActorContext("actor", "mcp", actor_kind=actor_kind, board_id="board", realm_id="local")
            async with CommunityUnitOfWork(session, actor=actor, application_persistence=adapter):
                return await main.SpecService(session).set_test_scenario_status("spec", "actor", "ts",
                    issued.status, issued.evidence, actor_kind=actor_kind)

    return SimpleNamespace(publish=publish, module=main, adapter=adapter)


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_kind", ["human", "agent"])
async def test_shared_owner_commits_authority_status_and_actual_actor_audit(authority, native_owner, actor_kind):
    issued = await authority.issue(actor_kind=actor_kind)
    response = await native_owner.publish(issued, actor_kind=actor_kind)
    assert response["verification_current"] and response["operational_summary_updated"]
    async with authority.factory() as session:
        spec = await session.get(Spec, "spec")
        assert spec.test_scenarios[0]["status"] == "passed"
        assert spec.test_scenarios[0]["evidence"] == issued.evidence
        acceptance = await session.get(Acceptance, response["accepted_result_id"])
        assert acceptance is not None
        audit = await session.scalar(select(ActivityLog).where(ActivityLog.action == "test_scenario_result_accepted"))
        assert audit.actor_id == "actor" and audit.actor_type == actor_kind
        assert audit.details["accepted_result_id"] == acceptance.id
        assert audit.details["actor_kind"] == actor_kind


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_shared_owner_audit_failure_rolls_back_every_publication_write(authority, native_owner, monkeypatch, failure):
    issued = await authority.issue()

    async def fail_audit(*args, **kwargs):
        raise failure("audit unavailable")

    monkeypatch.setattr(native_owner.module, "_application_add", fail_audit)
    with pytest.raises(failure, match="audit unavailable"):
        await native_owner.publish(issued)
    async with authority.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Acceptance)) == 0
        assert await session.scalar(select(func.count()).select_from(Head)) == 0
        assert (await session.get(Spec, "spec")).test_scenarios[0]["status"] == "ready"
        state = await session.get(State, tuple(SCOPE.model_dump().values()))
        assert state.revision == 1 and state.summary_ordinal == 0


@pytest.mark.asyncio
async def test_shared_owner_delayed_other_basis_and_replay_leave_summary_and_audit_stable(authority, native_owner):
    older = await authority.issue("old-base")
    newer = await authority.issue("new-base", "failed")
    failed = await native_owner.publish(newer)
    delayed = await native_owner.publish(older)
    assert failed["operational_summary_updated"] and not delayed["operational_summary_updated"]
    assert delayed["new_status"] == "failed"
    replay = await native_owner.publish(older)
    assert replay["replayed"] and replay["accepted_result_id"] == delayed["accepted_result_id"]
    async with authority.factory() as session:
        assert (await session.get(Spec, "spec")).test_scenarios[0]["evidence"] == newer.evidence
        assert await session.scalar(select(func.count()).select_from(Acceptance)) == 2
        assert await session.scalar(select(func.count()).select_from(ActivityLog).where(
            ActivityLog.action == "test_scenario_result_accepted")) == 2


@pytest.mark.asyncio
async def test_shared_owner_cannot_reinterpret_authenticated_actor_kind(authority, native_owner):
    issued = await authority.issue(actor_kind="human")
    with pytest.raises(VerificationCurrentnessError, match="verification_currentness_conflict|verification_reservation_invalid"):
        await native_owner.publish(issued, actor_kind="agent")
    assert not (await authority.read()).results


@pytest.mark.asyncio
async def test_shared_owner_new_failure_preserves_other_basis_without_manual_reset(authority, native_owner):
    await native_owner.publish(await authority.issue("a"))
    await native_owner.publish(await authority.issue("b"))
    failure = await native_owner.publish(await authority.issue("b", "failed"))
    assert failure["new_status"] == "failed"
    read = await authority.read()
    assert read.complete and len(read.results) == 2
    assert {item.result.basis.sources[0].revision: item.result.outcome for item in read.results} == {
        "a": "passed", "b": "failed"}
    assert {item.result.context.generation for item in read.results} == {1}


@pytest.mark.asyncio
async def test_result_publication_retains_state_permissions_authentication_and_locked_spec_guard(
    authority, native_owner,
):
    from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
    from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
    from okto_pulse.core.application.use_cases.base import ActorContext, PermissionDeniedError
    from okto_pulse.core.application.use_cases.spec_crud import SetTestScenarioStatusCommand, SetTestScenarioStatusUseCase
    from okto_pulse.core.domain.permissions import PermissionSet
    from okto_pulse.core.services.test_scenario_lifecycle import (
        StatusNotMutableError, is_test_scenario_status_transition_allowed,
    )

    await native_owner.publish(await authority.issue("a"))
    newer = await authority.issue("a", "failed")
    factory = build_community_session_factory(authority.engine)

    async def attempt(*, allowed, evidence=None):
        actor = ActorContext("actor", "mcp", actor_kind="human", board_id="board", realm_id="local",
            permissions=PermissionSet({"spec": {"tests": {"execute": True, "update_status": True}},
                "test_scenario": {"interact_in": {"passed": allowed}}}))
        async with CommunityUnitOfWork(factory(), actor=actor, application_persistence=native_owner.adapter) as uow:
            return await SetTestScenarioStatusUseCase().execute(
                SetTestScenarioStatusCommand("spec", "ts", "failed", evidence or newer.evidence), actor=actor, uow=uow)

    with pytest.raises(PermissionDeniedError):
        await attempt(allowed=False)
    tampered = deepcopy(newer.evidence)
    tampered["verification_context"]["ordinal"] += 1
    with pytest.raises(ValueError, match="evidence_unverified"):
        await attempt(allowed=True, evidence=tampered)
    async with authority.factory.begin() as session:
        await session.execute(update(Spec).where(Spec.id == "spec").values(status=SpecStatus.DONE))
    with pytest.raises(StatusNotMutableError):
        await attempt(allowed=True)
    assert not is_test_scenario_status_transition_allowed("passed", "failed")
    read = await authority.read()
    assert read.complete and [item.result.outcome for item in read.results] == ["passed"]


@pytest.mark.asyncio
async def test_concurrent_same_basis_publication_cannot_finish_with_older_success(authority):
    older = await authority.issue()
    newer = await authority.issue(outcome="failed")
    results = await asyncio.gather(authority.publish(newer), authority.publish(older), return_exceptions=True)
    assert not isinstance(results[0], BaseException)
    if isinstance(results[1], BaseException):
        assert isinstance(results[1], VerificationCurrentnessError)
        assert results[1].code == "verification_result_superseded"
    read = await authority.read()
    assert read.complete and len(read.results) == 1 and read.results[0].result.outcome == "failed"
    assert read.results[0].result.context.ordinal == 2


@pytest.mark.asyncio
async def test_missing_generation_is_not_created_by_reservation_or_read(authority):
    async with authority.factory.begin() as session:
        await session.execute(delete(State))
    with pytest.raises(VerificationCurrentnessError, match="verification_currentness_unavailable"):
        await authority.issue()
    read = await authority.read()
    assert not read.complete and read.reason_codes == ("verification_currentness_unavailable",)
    async with authority.factory() as session:
        assert await session.scalar(select(func.count()).select_from(State)) == 0


@pytest.mark.asyncio
async def test_composed_runtime_admits_and_publishes_through_real_use_cases(authority, tmp_path, monkeypatch):
    from okto_pulse.community.adapters import sqlalchemy_database
    from okto_pulse.community.adapters.relational_effects import register_community_relational_effects
    from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork, CommunityUnitOfWorkFactory
    from okto_pulse.core.application.use_cases.base import ActorContext
    from okto_pulse.core.application.use_cases.spec_crud import (
        AdmitTestVerificationReportCommand, AdmitTestVerificationReportUseCase,
        SetTestScenarioStatusCommand, SetTestScenarioStatusUseCase,
    )
    from okto_pulse.core.domain.permissions import PermissionSet
    from okto_pulse.core.services.main import GuidelineService

    factory = sqlalchemy_database.build_community_session_factory(authority.engine)
    monkeypatch.setattr(sqlalchemy_database, "get_session_factory", lambda: factory)
    register_community_relational_effects(settings=SimpleNamespace(
        data_dir=tmp_path / "composed", port=8100, environment="isolated-test"))

    async def guideline_policy(self, **kwargs):
        return None

    monkeypatch.setattr(GuidelineService, "enforce_policy_transition", guideline_policy)
    actor = ActorContext("actor", "mcp", actor_kind="human", board_id="board", realm_id="local",
        permissions=PermissionSet({"spec": {"tests": {"execute": True, "update_status": True}},
            "test_scenario": {"interact_in": {"ready": True, "passed": True, "failed": True},
                              "move": {"ready_to_passed": True}}}))
    async with CommunityUnitOfWork(factory(), actor=actor) as uow:
        issued = await AdmitTestVerificationReportUseCase().execute(
            AdmitTestVerificationReportCommand("spec", "ts", report()), actor=actor, uow=uow)
    assert issued.evidence["verification_context"]["generation"] == 1
    async with CommunityUnitOfWork(factory(), actor=actor) as uow:
        published = await SetTestScenarioStatusUseCase().execute(
            SetTestScenarioStatusCommand("spec", "ts", "passed", issued.evidence), actor=actor, uow=uow)
    async with factory() as session:
        read = await Store(session).read_current(board_id="board", spec_id="spec", spec_edition=2, scenario_ids=("ts",))
        assert read.complete and len(read.results) == 1
        assert read.results[0].result.actor_kind == "human"
        assert read.results[0].result.actor_id == "actor"
        assert (await session.get(Spec, "spec")).test_scenarios[0]["status"] == "passed"
    assert published is not None

    # The real MCP response must retain the acceptance identity, so the agent
    # can associate Delivery without another read or a guessed receipt lookup.
    from okto_pulse.core.mcp import server

    async def authenticated_agent(board_id):
        assert board_id == "board"
        return SimpleNamespace(agent_id="agent", agent_name="Isolated agent", realm_id="local",
                               permissions=actor.permissions)

    monkeypatch.setattr(server, "_get_agent_ctx", authenticated_agent)
    monkeypatch.setattr(server, "get_unit_of_work_factory_for_mcp", lambda: CommunityUnitOfWorkFactory(factory))
    failure = report()
    failure["result"] = failure["observations"][0]["outcome"] = "failed"
    admitted = json.loads(await server.okto_pulse_admit_test_verification_report.fn(
        board_id="board", spec_id="spec", scenario_id="ts", report=failure))
    assert admitted.get("success") is True, admitted
    args = dict(board_id="board", spec_id="spec", scenario_id="ts", status="failed",
                evidence=json.dumps(admitted["evidence"]))
    response = json.loads(await server.okto_pulse_update_test_scenario_status.fn(**args))
    assert response.get("success") is True, response
    assert response["accepted_result_id"] and response["verification_current"]
    assert response["operational_summary_updated"] and not response["replayed"]
    replay = json.loads(await server.okto_pulse_update_test_scenario_status.fn(**args))
    assert replay["replayed"] and replay["accepted_result_id"] == response["accepted_result_id"]
    async with factory() as session:
        read = await Store(session).read_current(board_id="board", spec_id="spec", spec_edition=2, scenario_ids=("ts",))
        assert read.complete and len(read.results) == 1
        assert read.results[0].result.outcome == "failed"
        assert read.results[0].result.actor_id == "agent" and read.results[0].result.actor_kind == "agent"
