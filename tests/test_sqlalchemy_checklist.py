"""Real-SQLite regression tests for the Community A3 checklist adapter."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from okto_pulse.community.adapters.sqlalchemy_checklist import (
    CommunitySqlAlchemyChecklist,
)
from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.community.adapters.sqlalchemy_default_board_configuration import (
    CommunitySqlAlchemyDefaultBoardConfigurationStore,
)
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
    CommunitySqlAlchemyDomainEventPublisher,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    CommunityUnitOfWork,
    build_community_unit_of_work_factory,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog,
    Base,
    Board,
    ChecklistBindingRow,
    ChecklistExecutionHeadRow,
    ChecklistExecutionRow,
    ChecklistItemResultRow,
    DefaultBoardConfiguration,
    DomainEventHandlerExecution,
    DomainEventRow,
    Spec,
)
from okto_pulse.core.domain.checklist import (
    SPECIFY_CHECKLIST_ITEM_IDS,
    SPECIFY_CHECKLIST_TEMPLATE_V1,
    ChecklistBinding,
    ChecklistItemOutcome,
    ChecklistItemResult,
    ChecklistMode,
    ChecklistPhase,
    ChecklistPreflight,
    ChecklistReceiptState,
    ChecklistSubmission,
    ChecklistTargetType,
)
from okto_pulse.core.domain.enums import SpecStatus
from okto_pulse.core.domain.permissions import get_builtin_presets
from okto_pulse.core.events import EventBus
from okto_pulse.core.events.handlers.checklist_binding_audit import (
    ChecklistBindingAuditHandler,
)
from okto_pulse.core.events.types import ChecklistBindingChanged
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.application.use_cases.admin_catalog import (
    DefaultBoardConfigCommand,
    GetBoardDefaultConfigDiffUseCase,
)
from okto_pulse.core.application.use_cases.create_board import (
    CreateBoardCommand,
    CreateBoardUseCase,
)
from okto_pulse.core.application.use_cases.mcp_board_crud import (
    McpGetBoardDefaultConfigDiffCommand,
    McpGetBoardDefaultConfigDiffUseCase,
)
from okto_pulse.core.models import BoardCreate
from okto_pulse.core.ports.checklist import (
    ChecklistListQuery,
    ChecklistSpecLifecycleConflict,
)
from okto_pulse.core.ports.default_board_configuration import (
    register_default_board_configuration_store,
)
from okto_pulse.core.ports.domain_event_delivery import (
    register_domain_event_publisher,
)
from okto_pulse.core.ports.relational_application import (
    register_relational_application_adapter,
)
from okto_pulse.core.runtime_registry import register_unit_of_work_factory
from okto_pulse.core.services.checklist import (
    ChecklistConflictError,
    ChecklistService,
)
from okto_pulse.core.services.default_board_configuration import (
    DefaultBoardConfigurationService,
)
from okto_pulse.core.services.ska_observability import (
    reset_ska_metric_samples_for_tests,
    ska_metric_samples,
)


_FULL_CONTROL_FLAGS = next(
    preset["flags"]
    for preset in get_builtin_presets()
    if preset["name"] == "Full Control"
)


def _full_control_actor(actor_id: str, source: str = "rest") -> ActorContext:
    return ActorContext(
        actor_id,
        source,
        actor_kind="human" if source == "rest" else "agent",
        permissions=_FULL_CONTROL_FLAGS,
        roles=("admin",),
    )

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 7, 27, 18, 0, tzinfo=timezone.utc)
BOARD_ID = "board-checklist"
SPEC_ID = "spec-checklist"


class _Ids:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self, prefix: str) -> str:
        self._value += 1
        return f"{prefix}_{self._value:04d}"


@pytest.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _foreign_keys(dbapi_connection, _record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        engine,
        sync_session_class=CommunitySemanticSession,
        expire_on_commit=False,
    )
    async with factory() as active:
        active.add(
            Board(
                id=BOARD_ID,
                name="A3 checklist",
                owner_id="owner-checklist",
                settings={},
            )
        )
        active.add(
            Spec(
                id=SPEC_ID,
                board_id=BOARD_ID,
                title="Curated checklist persistence",
                created_by="owner-checklist",
                status=SpecStatus.APPROVED,
                version=1,
            )
        )
        await active.commit()
        yield active
    await engine.dispose()


def _passing_items() -> tuple[ChecklistItemResult, ...]:
    return tuple(
        ChecklistItemResult(
            item_id=item_id,
            outcome=ChecklistItemOutcome.PASS,
            anchor=f"spec://{SPEC_ID}/{item_id}",
        )
        for item_id in SPECIFY_CHECKLIST_ITEM_IDS
    )


async def _preflight(
    adapter: CommunitySqlAlchemyChecklist,
) -> ChecklistPreflight:
    subject = await adapter.get_spec_snapshot(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
    )
    assert subject is not None
    binding = await adapter.get_binding(
        board_id=BOARD_ID,
        target_type=ChecklistTargetType.SPEC,
        phase=ChecklistPhase.SPEC_VALIDATION,
    )
    assert binding is not None
    current = await adapter.get_current(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        phase=ChecklistPhase.SPEC_VALIDATION,
    )
    return ChecklistPreflight(
        subject=subject,
        binding=binding,
        current_head_revision=0 if current is None else current[1].revision,
        current_head_receipt_id=None if current is None else current[1].receipt_id,
    )


async def _complete(
    service: ChecklistService,
    adapter: CommunitySqlAlchemyChecklist,
    *,
    start_key: str,
    submit_key: str,
):
    preflight = await _preflight(adapter)
    started = await service.start_execution(
        preflight=preflight,
        actor_id="agent-checklist",
        idempotency_key=start_key,
        persistence=adapter,
    )
    submission = ChecklistSubmission(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        spec_version=preflight.subject.spec_version,
        spec_edition=preflight.subject.spec_edition,
        content_digest=preflight.subject.content_digest,
        input_digest=preflight.subject.input_digest,
        template_version=started.execution.template_version,
        template_digest=started.execution.template_digest,
        binding_version=preflight.binding.version,
        binding_digest=preflight.binding.digest or "",
        expected_head_revision=preflight.current_head_revision,
        items=_passing_items(),
        idempotency_key=submit_key,
    )
    committed = await service.submit_started_execution(
        started.execution,
        submission,
        expected_execution_revision=1,
        actor_id="agent-checklist",
        preflight=preflight,
        persistence=adapter,
    )
    return started, submission, committed


async def test_complete_receipts_gate_pagination_and_off_zero_write(
    session: AsyncSession,
) -> None:
    adapter = CommunitySqlAlchemyChecklist(session)
    service = ChecklistService(id_factory=_Ids(), clock=lambda: NOW)

    advisory = service.prepare_binding(
        board_id=BOARD_ID,
        mode=ChecklistMode.ADVISORY,
        current_binding=None,
    )
    await service.apply_binding(
        advisory,
        previous_binding=None,
        persistence=adapter,
    )

    first_start, first_submission, first_commit = await _complete(
        service,
        adapter,
        start_key="start-1",
        submit_key="submit-1",
    )
    assert first_commit.replayed is False
    assert (
        await session.scalar(select(func.count(ChecklistItemResultRow.item_id)))
        == 10
    )

    replay = await service.submit_started_execution(
        first_start.execution,
        first_submission,
        expected_execution_revision=1,
        actor_id="agent-checklist",
        preflight=ChecklistPreflight(
            subject=(await _preflight(adapter)).subject,
            binding=advisory,
            current_head_revision=0,
            current_head_receipt_id=None,
        ),
        persistence=adapter,
    )
    assert replay.replayed is True
    assert replay.receipt_id == first_commit.receipt_id

    blocking = service.prepare_binding(
        board_id=BOARD_ID,
        mode=ChecklistMode.BLOCKING,
        current_binding=advisory,
    )
    assert blocking.digest == advisory.digest
    await service.apply_binding(
        blocking,
        previous_binding=advisory,
        persistence=adapter,
    )
    promoted_gate = await service.evaluate_spec_gate(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        persistence=adapter,
    )
    assert promoted_gate.allowed is True
    assert promoted_gate.reason == "checklist_satisfied"
    assert promoted_gate.currentness is not None
    assert promoted_gate.currentness.current is True
    assert promoted_gate.currentness.stale_reasons == ()
    assert (
        await session.scalar(select(func.count(ChecklistExecutionRow.id)))
        == 1
    )

    _second_start, _second_submission, second_commit = await _complete(
        service,
        adapter,
        start_key="start-2",
        submit_key="submit-2",
    )
    current_gate = await service.evaluate_spec_gate(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        persistence=adapter,
    )
    assert current_gate.allowed is True
    assert current_gate.reason == "checklist_satisfied"

    preflight = await _preflight(adapter)
    reset_ska_metric_samples_for_tests()
    statement_count = 0

    def _count_statements(*_args) -> None:
        nonlocal statement_count
        statement_count += 1

    event.listen(
        session.bind.sync_engine,
        "before_cursor_execute",
        _count_statements,
    )
    page = await service.list_executions(
        ChecklistListQuery(
            board_id=BOARD_ID,
            spec_id=SPEC_ID,
            offset=0,
            limit=1,
        ),
        current_subject=preflight.subject,
        current_binding=preflight.binding,
        head_receipt_id=second_commit.receipt_id,
        persistence=adapter,
    )
    event.remove(
        session.bind.sync_engine,
        "before_cursor_execute",
        _count_statements,
    )
    assert page.total == 2
    assert len(page.items) == 1
    assert page.items[0].receipt.id == second_commit.receipt_id
    assert page.items[0].is_head is True
    assert statement_count == 3
    sample = ska_metric_samples()[-1]
    assert sample["surface"] == "checklist_history"
    assert sample["subject_type"] == "spec"
    assert sample["outcome"] == "success"
    assert sample["value"] == 3

    reset_ska_metric_samples_for_tests()
    statement_count = 0
    event.listen(
        session.bind.sync_engine,
        "before_cursor_execute",
        _count_statements,
    )
    empty_page = await service.list_executions(
        ChecklistListQuery(
            board_id=BOARD_ID,
            spec_id=SPEC_ID,
            offset=99,
            limit=1,
        ),
        current_subject=preflight.subject,
        current_binding=preflight.binding,
        head_receipt_id=second_commit.receipt_id,
        persistence=adapter,
    )
    event.remove(
        session.bind.sync_engine,
        "before_cursor_execute",
        _count_statements,
    )
    assert empty_page.total == 2
    assert empty_page.items == ()
    assert statement_count == 3
    assert ska_metric_samples()[-1]["value"] == 3

    off = service.prepare_binding(
        board_id=BOARD_ID,
        mode=ChecklistMode.OFF,
        current_binding=blocking,
    )
    await service.apply_binding(
        off,
        previous_binding=blocking,
        persistence=adapter,
    )
    before = await session.scalar(select(func.count(ChecklistExecutionRow.id)))
    off_preflight = await _preflight(adapter)
    with pytest.raises(ChecklistConflictError, match="checklist_binding_off"):
        await service.start_execution(
            preflight=off_preflight,
            actor_id="agent-checklist",
            idempotency_key="off-must-not-write",
            persistence=adapter,
        )
    after = await session.scalar(select(func.count(ChecklistExecutionRow.id)))
    assert before == after == 2
    off_gate = await service.evaluate_spec_gate(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        persistence=adapter,
    )
    assert off_gate.allowed is True
    assert off_gate.reason == "checklist_off"


async def test_write_fence_refreshes_preloaded_spec_lifecycle(
    session: AsyncSession,
) -> None:
    """A stale ORM identity cannot admit a checklist write after validation."""

    adapter = CommunitySqlAlchemyChecklist(session)
    service = ChecklistService(id_factory=_Ids(), clock=lambda: NOW)
    advisory = service.prepare_binding(
        board_id=BOARD_ID,
        mode=ChecklistMode.ADVISORY,
        current_binding=None,
    )
    await service.apply_binding(
        advisory,
        previous_binding=None,
        persistence=adapter,
    )
    preflight = await _preflight(adapter)
    execution = service.prepare_execution_start(
        preflight=preflight,
        actor_id="agent-checklist",
        idempotency_key="stale-spec-fence",
    )

    # Simulate a lifecycle winner without synchronizing the Session identity
    # map; the adapter's locked reread must refresh the cached approved row.
    await session.execute(
        update(Spec)
        .where(Spec.id == SPEC_ID, Spec.board_id == BOARD_ID)
        .values(status=SpecStatus.VALIDATED)
        .execution_options(synchronize_session=False)
    )

    with pytest.raises(ChecklistSpecLifecycleConflict):
        await adapter.start_execution_cas(execution)
    assert await session.scalar(select(func.count(ChecklistExecutionRow.id))) == 0


async def test_current_head_refreshes_preloaded_identity(
    session: AsyncSession,
) -> None:
    adapter = CommunitySqlAlchemyChecklist(session)
    service = ChecklistService(id_factory=_Ids(), clock=lambda: NOW)
    blocking = service.prepare_binding(
        board_id=BOARD_ID,
        mode=ChecklistMode.BLOCKING,
        current_binding=None,
    )
    await service.apply_binding(
        blocking,
        previous_binding=None,
        persistence=adapter,
    )
    await _complete(
        service,
        adapter,
        start_key="fresh-head-start",
        submit_key="fresh-head-submit",
    )
    await session.execute(
        update(ChecklistExecutionHeadRow)
        .where(
            ChecklistExecutionHeadRow.board_id == BOARD_ID,
            ChecklistExecutionHeadRow.spec_id == SPEC_ID,
            ChecklistExecutionHeadRow.phase
            == ChecklistPhase.SPEC_VALIDATION.value,
        )
        .values(revision=7)
        .execution_options(synchronize_session=False)
    )

    current = await adapter.get_current(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        phase=ChecklistPhase.SPEC_VALIDATION,
    )
    assert current is not None
    assert current[1].revision == 7


async def test_new_spec_edition_resets_current_head_and_projects_prior_history(
    session: AsyncSession,
) -> None:
    adapter = CommunitySqlAlchemyChecklist(session)
    service = ChecklistService(id_factory=_Ids(), clock=lambda: NOW)
    blocking = service.prepare_binding(
        board_id=BOARD_ID,
        mode=ChecklistMode.BLOCKING,
        current_binding=None,
    )
    await service.apply_binding(
        blocking,
        previous_binding=None,
        persistence=adapter,
    )
    _first_start, _first_submission, first_commit = await _complete(
        service,
        adapter,
        start_key="edition-1-start",
        submit_key="edition-1-submit",
    )
    assert first_commit.spec_edition == 1

    spec = await session.get(Spec, SPEC_ID)
    assert spec is not None
    spec.status = SpecStatus.DRAFT
    spec.edition = 2
    spec.version += 1
    head = await session.get(
        ChecklistExecutionHeadRow,
        (BOARD_ID, SPEC_ID, ChecklistPhase.SPEC_VALIDATION.value),
    )
    assert head is not None
    await session.delete(head)
    await session.flush()
    spec.status = SpecStatus.APPROVED
    spec.version += 1
    await session.flush()

    snapshot = await adapter.get_spec_snapshot(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
    )
    assert snapshot is not None and snapshot.spec_edition == 2
    probe = ChecklistSubmission(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        spec_version=snapshot.spec_version,
        spec_edition=snapshot.spec_edition,
        content_digest=snapshot.content_digest,
        input_digest=snapshot.input_digest,
        template_version=SPECIFY_CHECKLIST_TEMPLATE_V1.version,
        template_digest=SPECIFY_CHECKLIST_TEMPLATE_V1.digest,
        binding_version=blocking.version,
        binding_digest=blocking.digest or "",
        expected_head_revision=0,
        items=_passing_items(),
        idempotency_key="edition-2-probe",
    )
    reopened_current = await adapter.get_current(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        phase=ChecklistPhase.SPEC_VALIDATION,
        spec_edition=2,
    )
    assert reopened_current is None
    reopened_previous = await adapter.list_executions(
        ChecklistListQuery(
            board_id=BOARD_ID,
            spec_id=SPEC_ID,
            offset=0,
            limit=25,
            current_spec_edition=2,
            state=ChecklistReceiptState.PREVIOUS,
        )
    )
    assert reopened_previous.total == 1
    assert reopened_previous.items[0].id == first_commit.receipt_id
    reopened_gate = await service.evaluate_spec_gate(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        persistence=adapter,
    )
    assert reopened_gate.allowed is False
    assert reopened_gate.reason == "checklist_receipt_required"

    preflight = await adapter.resolve_checklist_preflight(
        probe,
        actor_id="agent-checklist",
    )
    assert preflight.current_head_revision == 0
    assert preflight.current_head_receipt_id is None

    _second_start, _second_submission, second_commit = await _complete(
        service,
        adapter,
        start_key="edition-2-start",
        submit_key="edition-2-submit",
    )
    assert second_commit.spec_edition == 2
    assert second_commit.head_revision == 1

    current = await adapter.get_current(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        phase=ChecklistPhase.SPEC_VALIDATION,
        spec_edition=2,
    )
    assert current is not None
    assert current[0].id == second_commit.receipt_id
    assert current[0].spec_edition == 2

    previous = await adapter.list_executions(
        ChecklistListQuery(
            board_id=BOARD_ID,
            spec_id=SPEC_ID,
            offset=0,
            limit=25,
            current_spec_edition=2,
            state=ChecklistReceiptState.PREVIOUS,
        )
    )
    assert previous.total == 1
    assert previous.items[0].id == first_commit.receipt_id
    assert previous.items[0].spec_edition == 1


async def test_mode_promotion_replays_open_execution_and_submits_without_reexecution(
    session: AsyncSession,
) -> None:
    adapter = CommunitySqlAlchemyChecklist(session)
    service = ChecklistService(id_factory=_Ids(), clock=lambda: NOW)
    advisory = service.prepare_binding(
        board_id=BOARD_ID,
        mode=ChecklistMode.ADVISORY,
        current_binding=None,
    )
    await service.apply_binding(
        advisory,
        previous_binding=None,
        persistence=adapter,
    )
    advisory_preflight = await _preflight(adapter)
    started = await service.start_execution(
        preflight=advisory_preflight,
        actor_id="agent-checklist",
        idempotency_key="promotion-start",
        persistence=adapter,
    )

    blocking = service.prepare_binding(
        board_id=BOARD_ID,
        mode=ChecklistMode.BLOCKING,
        current_binding=advisory,
    )
    await service.apply_binding(
        blocking,
        previous_binding=advisory,
        persistence=adapter,
    )
    blocking_preflight = await _preflight(adapter)
    replay = await service.start_execution(
        preflight=blocking_preflight,
        actor_id="agent-checklist",
        idempotency_key="promotion-start",
        persistence=adapter,
    )

    assert replay.replayed is True
    assert replay.execution.id == started.execution.id
    assert replay.execution.binding_mode is ChecklistMode.ADVISORY
    assert (
        await session.scalar(select(func.count(ChecklistExecutionRow.id)))
        == 1
    )
    submission = ChecklistSubmission(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        spec_version=started.execution.spec_version,
        spec_edition=started.execution.spec_edition,
        content_digest=started.execution.content_digest,
        input_digest=started.execution.input_digest,
        template_version=started.execution.template_version,
        template_digest=started.execution.template_digest,
        binding_version=started.execution.binding_version,
        binding_digest=started.execution.binding_digest,
        expected_head_revision=blocking_preflight.current_head_revision,
        items=_passing_items(),
        idempotency_key="promotion-submit",
    )
    committed = await service.submit_started_execution(
        started.execution,
        submission,
        expected_execution_revision=1,
        actor_id="agent-checklist",
        preflight=blocking_preflight,
        persistence=adapter,
    )
    receipt = await adapter.get_receipt(
        board_id=BOARD_ID,
        receipt_id=committed.receipt_id,
    )
    assert receipt is not None
    assert receipt.binding_version == 2
    assert receipt.binding_mode is ChecklistMode.BLOCKING
    gate = await service.evaluate_spec_gate(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        persistence=adapter,
    )
    assert gate.allowed is True
    assert gate.reason == "checklist_satisfied"
    assert gate.currentness is not None and gate.currentness.current is True


async def test_mode_revisions_racing_both_adapter_fences_remain_semantic(
    session: AsyncSession,
) -> None:
    adapter = CommunitySqlAlchemyChecklist(session)
    service = ChecklistService(id_factory=_Ids(), clock=lambda: NOW)
    advisory_v1 = service.prepare_binding(
        board_id=BOARD_ID,
        mode=ChecklistMode.ADVISORY,
        current_binding=None,
    )
    await service.apply_binding(
        advisory_v1,
        previous_binding=None,
        persistence=adapter,
    )
    advisory_preflight = await _preflight(adapter)
    execution = service.prepare_execution_start(
        preflight=advisory_preflight,
        actor_id="agent-checklist",
        idempotency_key="fence-race-start",
    )

    blocking_v2 = service.prepare_binding(
        board_id=BOARD_ID,
        mode=ChecklistMode.BLOCKING,
        current_binding=advisory_v1,
    )
    await service.apply_binding(
        blocking_v2,
        previous_binding=advisory_v1,
        persistence=adapter,
    )
    started = await adapter.start_execution_cas(execution)
    assert started.replayed is False
    assert started.execution.binding_mode is ChecklistMode.ADVISORY

    blocking_preflight = await _preflight(adapter)
    submission = ChecklistSubmission(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        spec_version=execution.spec_version,
        spec_edition=execution.spec_edition,
        content_digest=execution.content_digest,
        input_digest=execution.input_digest,
        template_version=execution.template_version,
        template_digest=execution.template_digest,
        binding_version=execution.binding_version,
        binding_digest=execution.binding_digest,
        expected_head_revision=0,
        items=_passing_items(),
        idempotency_key="fence-race-submit",
    )
    bundle = service.prepare_execution(
        submission,
        actor_id="agent-checklist",
        preflight=blocking_preflight,
    )
    advisory_v3 = service.prepare_binding(
        board_id=BOARD_ID,
        mode=ChecklistMode.ADVISORY,
        current_binding=blocking_v2,
    )
    await service.apply_binding(
        advisory_v3,
        previous_binding=blocking_v2,
        persistence=adapter,
    )
    committed = await adapter.submit_execution_cas(
        execution,
        expected_revision=1,
        bundle=bundle,
    )
    assert committed.replayed is False

    blocking_v4 = service.prepare_binding(
        board_id=BOARD_ID,
        mode=ChecklistMode.BLOCKING,
        current_binding=advisory_v3,
    )
    await service.apply_binding(
        blocking_v4,
        previous_binding=advisory_v3,
        persistence=adapter,
    )
    gate = await service.evaluate_spec_gate(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        persistence=adapter,
    )
    assert gate.allowed is True
    assert gate.reason == "checklist_satisfied"
    assert gate.currentness is not None and gate.currentness.current is True


async def test_persisted_off_v1_announces_revision_one_and_allows_next_cas(
    session: AsyncSession,
) -> None:
    adapter = CommunitySqlAlchemyChecklist(session)
    service = ChecklistService()
    synthetic = ChecklistBinding.synthetic_off(board_id=BOARD_ID)
    persisted_off = service.prepare_binding(
        board_id=BOARD_ID,
        mode=ChecklistMode.OFF,
        current_binding=synthetic,
    )
    await service.apply_binding(
        persisted_off,
        previous_binding=synthetic,
        persistence=adapter,
    )

    reloaded_off = await adapter.get_binding(
        board_id=BOARD_ID,
        target_type=ChecklistTargetType.SPEC,
        phase=ChecklistPhase.SPEC_VALIDATION,
    )
    assert reloaded_off is not None
    assert reloaded_off.mode is ChecklistMode.OFF
    assert reloaded_off.version == reloaded_off.revision == 1
    assert reloaded_off.is_synthetic is False

    advisory = service.prepare_binding(
        board_id=BOARD_ID,
        mode=ChecklistMode.ADVISORY,
        current_binding=reloaded_off,
    )
    applied = await service.apply_binding(
        advisory,
        previous_binding=reloaded_off,
        persistence=adapter,
    )
    assert applied.version == applied.revision == 2
    assert (
        await adapter.get_binding(
            board_id=BOARD_ID,
            target_type=ChecklistTargetType.SPEC,
            phase=ChecklistPhase.SPEC_VALIDATION,
        )
        == applied
    )


async def test_create_board_atomically_bootstraps_advisory_binding(
    session: AsyncSession,
) -> None:
    # The autouse test seam starts every case with an intentionally empty
    # runtime-value registry. Production runtimes clone the import-time handler
    # baseline; register the same handler in this isolated test runtime.
    EventBus.register_handler(ChecklistBindingChanged.event_type)(
        ChecklistBindingAuditHandler
    )
    register_relational_application_adapter(
        CommunityRelationalApplicationAdapter()
    )
    register_default_board_configuration_store(
        CommunitySqlAlchemyDefaultBoardConfigurationStore()
    )
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    uow = CommunityUnitOfWork(session)
    result = await CreateBoardUseCase().execute(
        CreateBoardCommand(BoardCreate(name="Checklist bootstrap")),
        actor=_full_control_actor("new-board-owner"),
        uow=uow,
    )

    binding = await CommunitySqlAlchemyChecklist(session).get_binding(
        board_id=result.board.id,
        target_type=ChecklistTargetType.SPEC,
        phase=ChecklistPhase.SPEC_VALIDATION,
    )
    assert binding is not None
    assert binding.mode is ChecklistMode.ADVISORY
    assert binding.version == 1
    activity = await session.scalar(
        select(ActivityLog).where(
            ActivityLog.board_id == result.board.id,
            ActivityLog.action == "spec_checklist_binding_changed",
        )
    )
    assert activity is not None
    assert activity.details == {
        "target_type": "spec",
        "phase": "spec_validation",
        "template_version": "/specify/v1",
        "mode": "advisory",
        "binding_version": 1,
        "binding_digest": binding.digest,
        "previous_mode": None,
        "previous_binding_version": None,
        "change_source": "board_bootstrap",
    }
    domain_event = await session.scalar(
        select(DomainEventRow).where(
            DomainEventRow.board_id == result.board.id,
            DomainEventRow.event_type == "checklist.binding_changed.v1",
        )
    )
    assert domain_event is not None
    assert domain_event.payload_json == {
        "event_schema_version": 1,
        "target_type": "spec",
        "phase": "spec_validation",
        "template_version": "/specify/v1",
        "mode": "advisory",
        "binding_version": 1,
        "binding_digest": binding.digest,
        "previous_mode": None,
        "previous_binding_version": None,
        "change_source": "board_bootstrap",
    }
    delivery = await session.scalar(
        select(DomainEventHandlerExecution).where(
            DomainEventHandlerExecution.event_id == domain_event.id,
        )
    )
    assert delivery is not None
    assert delivery.handler_name == "ChecklistBindingAuditHandler"
    assert delivery.status == "pending"

    # The fixture board predates this use case and remains binding-less: no
    # retropropagation or read-time materialization.
    legacy = await CommunitySqlAlchemyChecklist(session).get_binding(
        board_id=BOARD_ID,
        target_type=ChecklistTargetType.SPEC,
        phase=ChecklistPhase.SPEC_VALIDATION,
    )
    assert legacy is None


@pytest.mark.parametrize("configured_mode", ["off", "blocking"])
async def test_create_board_materializes_active_default_checklist_mode(
    session: AsyncSession,
    configured_mode: str,
) -> None:
    EventBus.register_handler(ChecklistBindingChanged.event_type)(
        ChecklistBindingAuditHandler
    )
    register_relational_application_adapter(
        CommunityRelationalApplicationAdapter()
    )
    register_default_board_configuration_store(
        CommunitySqlAlchemyDefaultBoardConfigurationStore()
    )
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())

    await DefaultBoardConfigurationService(session).create_version(
        settings_payload={},
        actor="default-admin",
        spec_checklist_mode=configured_mode,
        activate=True,
    )
    result = await CreateBoardUseCase().execute(
        CreateBoardCommand(BoardCreate(name=f"Checklist {configured_mode}")),
        actor=_full_control_actor("new-board-owner"),
        uow=CommunityUnitOfWork(session),
    )

    binding = await CommunitySqlAlchemyChecklist(session).get_binding(
        board_id=result.board.id,
        target_type=ChecklistTargetType.SPEC,
        phase=ChecklistPhase.SPEC_VALIDATION,
    )
    assert binding is not None
    assert binding.mode.value == configured_mode
    assert binding.version == 1
    assert result.board.default_config_snapshot["spec_checklist"] == {
        "mode": configured_mode,
        "template_version_id": "/specify/v1",
    }


async def test_create_board_projects_legacy_null_default_mode_as_advisory(
    session: AsyncSession,
) -> None:
    EventBus.register_handler(ChecklistBindingChanged.event_type)(
        ChecklistBindingAuditHandler
    )
    register_relational_application_adapter(
        CommunityRelationalApplicationAdapter()
    )
    register_default_board_configuration_store(
        CommunitySqlAlchemyDefaultBoardConfigurationStore()
    )
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    session.add(
        DefaultBoardConfiguration(
            id="legacy-null-checklist-default",
            version=1,
            status="active",
            is_active=True,
            scope="global",
            settings_payload={},
            spec_checklist_mode=None,
            created_by="legacy-admin",
        )
    )
    await session.commit()

    result = await CreateBoardUseCase().execute(
        CreateBoardCommand(BoardCreate(name="Checklist legacy null")),
        actor=_full_control_actor("new-board-owner"),
        uow=CommunityUnitOfWork(session),
    )

    binding = await CommunitySqlAlchemyChecklist(session).get_binding(
        board_id=result.board.id,
        target_type=ChecklistTargetType.SPEC,
        phase=ChecklistPhase.SPEC_VALIDATION,
    )
    assert binding is not None
    assert binding.mode is ChecklistMode.ADVISORY
    assert result.board.default_config_snapshot["spec_checklist"] == {
        "mode": "advisory",
        "template_version_id": "/specify/v1",
    }


async def test_default_config_diff_includes_local_checklist_mode_override(
    session: AsyncSession,
) -> None:
    EventBus.register_handler(ChecklistBindingChanged.event_type)(
        ChecklistBindingAuditHandler
    )
    register_relational_application_adapter(
        CommunityRelationalApplicationAdapter()
    )
    register_default_board_configuration_store(
        CommunitySqlAlchemyDefaultBoardConfigurationStore()
    )
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    register_unit_of_work_factory(
        build_community_unit_of_work_factory(
            async_sessionmaker(session.bind, expire_on_commit=False)
        )
    )

    await DefaultBoardConfigurationService(session).create_version(
        settings_payload={},
        actor="default-admin",
        spec_checklist_mode="blocking",
        activate=True,
    )
    actor = _full_control_actor("diff-board-owner")
    request_uow = CommunityUnitOfWork(session, actor=actor)
    result = await CreateBoardUseCase().execute(
        CreateBoardCommand(BoardCreate(name="Checklist diff")),
        actor=actor,
        uow=request_uow,
    )

    adapter = CommunitySqlAlchemyChecklist(session)
    current = await adapter.get_binding(
        board_id=result.board.id,
        target_type=ChecklistTargetType.SPEC,
        phase=ChecklistPhase.SPEC_VALIDATION,
    )
    assert current is not None
    local = ChecklistService().prepare_binding(
        board_id=result.board.id,
        mode=ChecklistMode.OFF,
        current_binding=current,
    )
    await ChecklistService().apply_binding(
        local,
        previous_binding=current,
        persistence=adapter,
    )
    await session.commit()

    diff = await GetBoardDefaultConfigDiffUseCase().execute(
        DefaultBoardConfigCommand(board_id=result.board.id),
        actor=actor,
        uow=request_uow,
    )
    checklist_field = next(
        field
        for field in diff.data["fields"]
        if field["field"] == "spec_checklist_mode"
    )
    assert checklist_field == {
        "field": "spec_checklist_mode",
        "template_value": "blocking",
        "current_value": "off",
        "state": "overridden",
    }

    mcp_diff = await McpGetBoardDefaultConfigDiffUseCase().execute(
        McpGetBoardDefaultConfigDiffCommand(result.board.id),
        actor=ActorContext(
            "diff-board-agent",
            "mcp",
            actor_kind="agent",
            board_id=result.board.id,
            permissions=["*"],
        ),
        uow=request_uow,
    )
    assert mcp_diff.data == diff.data


async def test_board_bootstrap_rolls_back_board_binding_history_and_outbox(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_pulse.core.events as events

    register_relational_application_adapter(
        CommunityRelationalApplicationAdapter()
    )
    register_default_board_configuration_store(
        CommunitySqlAlchemyDefaultBoardConfigurationStore()
    )
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    before = {
        "boards": await session.scalar(select(func.count(Board.id))),
        "bindings": await session.scalar(
            select(func.count(ChecklistBindingRow.version))
        ),
        "activities": await session.scalar(select(func.count(ActivityLog.id))),
        "events": await session.scalar(select(func.count(DomainEventRow.id))),
    }

    async def _fail_publish(*_args, **_kwargs) -> None:
        raise RuntimeError("forced_checklist_outbox_failure")

    monkeypatch.setattr(events, "publish", _fail_publish)
    uow = CommunityUnitOfWork(session)
    with pytest.raises(RuntimeError, match="forced_checklist_outbox_failure"):
        await CreateBoardUseCase().execute(
            CreateBoardCommand(BoardCreate(name="Must rollback atomically")),
            actor=_full_control_actor("rollback-owner"),
            uow=uow,
        )
    await session.rollback()

    after = {
        "boards": await session.scalar(select(func.count(Board.id))),
        "bindings": await session.scalar(
            select(func.count(ChecklistBindingRow.version))
        ),
        "activities": await session.scalar(select(func.count(ActivityLog.id))),
        "events": await session.scalar(select(func.count(DomainEventRow.id))),
    }
    assert after == before
