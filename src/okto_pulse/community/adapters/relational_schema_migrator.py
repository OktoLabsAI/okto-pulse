"""Community adapter for the ``RelationalSchemaMigrator`` port (spec R16-B).

This adapter is the Community-edition concrete implementation of the
``okto_pulse.core.ports.RelationalSchemaMigrator`` Protocol (R16-A). It models
the *effective* ordering of ``okto_pulse.core.infra.database.init_db`` as an
ordered, declarative ledger of :class:`MigrationStep` — WITHOUT moving,
reordering, removing or re-implementing any ``_migrate_*`` function
(register-before-remove). ``init_db`` remains the single source of truth; this
adapter only *describes* and *replays* the same steps.

Layering (br/ac of R16-A + R16-B):
  * The module top-level imports ONLY the pure ``core.ports`` contract (DTOs +
    Protocol). It does NOT import SQLAlchemy, ``infra.database``, any
    ``_migrate_*`` function or the engine at import time.
  * ``make_community_relational_schema_migrator`` (the composition factory)
    imports ``core.infra.database`` LAZILY and binds the real callables — so
    ``core`` never imports ``community`` and the adapter stays import-light.

Ledger scope (br_e16ff5a1):
  * EXACTLY ONE ``create_all_boundary`` step (``Base.metadata.create_all``).
  * Every ``async def _migrate_*`` in ``init_db`` is a schema step.
  * ``_seed_builtin_presets`` / ``_reconcile_*`` / ``_bootstrap_default_discovery_intents``
    are DATA bootstrap (``data_bootstrap_boundary``) and are deliberately
    EXCLUDED — a schema plan must never silently absorb data seeding.
  * Nuance: ``_migrate_agent_permissions`` is an ``async def _migrate_*`` that
    executes in ``init_db`` AFTER ``_seed_builtin_presets`` (in the bootstrap
    region) yet is a real schema migration — it is classified ``post_create_all``
    (see its ``metadata['runs_in_bootstrap_region']``).

Failure semantics are fail-closed (the port's ``MigrationResult.__post_init__``
enforces it): an invalid plan or a failing step yields a structured
``failed``/``partial`` result that can NEVER report ``success``.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable, Mapping

from okto_pulse.core.ports import (
    MIGRATION_PHASES,
    MigrationPlan,
    MigrationResult,
    MigrationStep,
    MigrationStepResult,
    SchemaMigrationError,
)

__all__ = [
    "CommunityRelationalSchemaMigrator",
    "build_community_migration_ledger",
    "make_community_relational_schema_migrator",
    "CREATE_ALL_BOUNDARY_STEP_ID",
]

#: step_id used for the single ``Base.metadata.create_all`` boundary step.
CREATE_ALL_BOUNDARY_STEP_ID = "create_all_boundary"

_PHASE_RANK = {phase: idx for idx, phase in enumerate(MIGRATION_PHASES)}

# A callable per step: zero-arg, sync OR async; raises on failure. May return
# the sentinel ``"skipped"`` to signal an explicit no-op (otherwise -> applied).
StepCallable = Callable[[], "Awaitable[object] | object"]


# ---------------------------------------------------------------------------
# Canonical ledger — the declarative mirror of init_db's effective ordering.
#
# Each tuple: (step_id, phase, destructive, description). ``order`` is the
# 1-based index in this list (== the init_db call sequence). ``idempotent`` is
# True for every step: init_db runs them on every boot, so each is guard-then-
# alter safe; ``create_all`` is checkfirst-idempotent.
# ---------------------------------------------------------------------------
_LEDGER: tuple[tuple[str, str, bool, str], ...] = (
    # --- pre_create_all: schema ALTERs applied BEFORE create_all ---
    ("_migrate_card_statuses", "pre_create_all", False,
     "Migrate the card status enum/values before create_all (avoids PG enum conflicts)."),
    ("_migrate_add_priority_column", "pre_create_all", False,
     "Add the card priority column."),
    ("_migrate_add_realm_id", "pre_create_all", False,
     "Add the realm_id column."),
    ("_migrate_add_comment_choice_columns", "pre_create_all", False,
     "Add comment choice columns."),
    ("_migrate_add_bug_card_columns", "pre_create_all", False,
     "Add bug-card columns."),
    ("_migrate_add_skip_rules_coverage", "pre_create_all", False,
     "Add skip-rules-coverage column."),
    ("_migrate_add_skip_trs_coverage", "pre_create_all", False,
     "Add skip-TRs-coverage column."),
    ("_migrate_add_decisions_columns", "pre_create_all", False,
     "Add decisions columns."),
    ("_migrate_decisions_default_false", "pre_create_all", False,
     "Backfill decisions default to false."),
    ("_migrate_add_archive_columns", "pre_create_all", False,
     "Add archive columns."),
    ("_migrate_add_spec_validation_columns", "pre_create_all", False,
     "Add spec-validation columns."),
    ("_migrate_add_ir_or_columns", "pre_create_all", False,
     "Add integration-requirement OR columns."),
    ("_migrate_add_spec_validation_gate_columns", "pre_create_all", False,
     "Add spec-validation-gate columns."),
    ("_migrate_add_ideation_skip_ambiguity_gate", "pre_create_all", False,
     "Add ideation skip-ambiguity-gate column."),
    ("_migrate_heal_task_validation_field_names", "pre_create_all", False,
     "Heal task-validation field names."),
    ("_migrate_status_renames", "pre_create_all", False,
     "Apply status renames (value transforms; preserves rows)."),
    ("_migrate_add_permission_columns", "pre_create_all", False,
     "Add permission columns."),
    ("_migrate_add_event_tables", "pre_create_all", False,
     "Add the event/outbox tables ahead of create_all."),
    # --- create_all_boundary ---
    (CREATE_ALL_BOUNDARY_STEP_ID, "create_all_boundary", False,
     "Base.metadata.create_all — the table-create boundary."),
    # --- post_create_all: schema ALTERs applied AFTER create_all ---
    ("_migrate_story_ideation_single_link", "post_create_all", False,
     "Enforce single story->ideation link."),
    ("_migrate_add_card_sprint_id", "post_create_all", False,
     "Add card.sprint_id."),
    ("_migrate_add_card_knowledge_bases", "post_create_all", False,
     "Add card knowledge-base columns."),
    ("_migrate_add_knowledge_source_columns", "post_create_all", False,
     "Add knowledge-source columns."),
    ("_migrate_add_kb_lineage_columns", "post_create_all", False,
     "Add knowledge-base lineage columns."),
    ("_migrate_add_sprint_scope_fields", "post_create_all", False,
     "Add sprint scope fields."),
    ("_migrate_add_sprint_lane_fields", "post_create_all", False,
     "Add sprint lane fields."),
    ("_migrate_agent_boards", "post_create_all", False,
     "Backfill the agent_boards junction table."),
    ("_migrate_add_task_validation_columns", "post_create_all", False,
     "Add task-validation columns."),
    ("_migrate_add_consolidation_resilience_columns", "post_create_all", False,
     "Add consolidation-resilience columns."),
    ("_migrate_add_kg_tick_boards_failed", "post_create_all", False,
     "Add kg_tick boards_failed column."),
    ("_migrate_drop_spec_skills", "post_create_all", True,
     "DROP TABLE spec_skills (Skills removal; no data preservation — destructive)."),
    ("_migrate_add_default_config_snapshot", "post_create_all", False,
     "Add default-config snapshot column on Board.settings."),
    ("_migrate_add_board_guideline_provenance", "post_create_all", False,
     "Add board-guideline provenance columns."),
    # _seed_builtin_presets runs here in init_db (DATA bootstrap -> EXCLUDED).
    ("_migrate_agent_permissions", "post_create_all", False,
     "Schema migration that runs in init_db AFTER _seed_builtin_presets; a real "
     "_migrate_* classified as post_create_all schema (runs late in the bootstrap region)."),
    # _reconcile_builtin_presets / _reconcile_agent_permission_flags /
    # _bootstrap_default_discovery_intents run here (DATA bootstrap -> EXCLUDED).
)

#: step_ids that are real ``_migrate_*`` functions (the gate compares these to
#: the AST scan of database.py). Excludes the create_all_boundary step.
_MIGRATE_STEP_IDS: tuple[str, ...] = tuple(
    sid for sid, phase, _d, _desc in _LEDGER if phase != "create_all_boundary"
)


def build_community_migration_ledger() -> tuple[MigrationStep, ...]:
    """Return the canonical, ordered ledger of :class:`MigrationStep`.

    Declarative only — carries no SQL and binds no callable. ``order`` is the
    1-based init_db call position; ``owner='community'``.
    """
    steps: list[MigrationStep] = []
    for order, (step_id, phase, destructive, description) in enumerate(_LEDGER, start=1):
        metadata: dict[str, object] = {"source": "okto_pulse.core.infra.database.init_db"}
        if step_id == "_migrate_agent_permissions":
            metadata["runs_in_bootstrap_region"] = True
            metadata["nuance"] = (
                "executes after _seed_builtin_presets in init_db but is a real "
                "schema migration (classified post_create_all)."
            )
        if step_id == CREATE_ALL_BOUNDARY_STEP_ID:
            metadata["is_create_all_boundary"] = True
        steps.append(
            MigrationStep(
                step_id=step_id,
                order=order,
                phase=phase,  # type: ignore[arg-type]
                description=description,
                idempotent=True,
                destructive=destructive,
                owner="community",
                metadata=metadata,
            )
        )
    return tuple(steps)


class CommunityRelationalSchemaMigrator:
    """Concrete :class:`RelationalSchemaMigrator` for the Community edition.

    Construct via :func:`make_community_relational_schema_migrator` for the real
    wiring, or directly with a custom ``steps``/``callables`` pair for
    deterministic testing (tr_d5941f41). Callables are zero-arg, sync OR async,
    raise on failure, and may return the sentinel ``"skipped"`` to report an
    explicit no-op.
    """

    def __init__(
        self,
        *,
        steps: tuple[MigrationStep, ...],
        callables: Mapping[str, StepCallable],
        target: str = "community-sqlite",
    ) -> None:
        self._steps = tuple(steps)
        self._callables = dict(callables)
        self._default_target = target
        # Adapter-level applied ledger: idempotent re-run -> skipped (no drift).
        self._applied: set[str] = set()

    # -- port: plan -------------------------------------------------------
    def plan(self, *, target: str) -> MigrationPlan:
        boundary_orders = [
            s.order for s in self._steps if s.phase == "create_all_boundary"
        ]
        return MigrationPlan(
            plan_id=f"community-relational-{target}",
            target=target,
            steps=self._steps,
            metadata={
                "owner": "community",
                "step_count": len(self._steps),
                "migration_step_count": sum(
                    1 for s in self._steps if s.phase != "create_all_boundary"
                ),
                "create_all_boundary_order": boundary_orders[0] if boundary_orders else None,
            },
        )

    # -- port: validate_plan (fail-closed) --------------------------------
    def validate_plan(self, plan: MigrationPlan) -> None:
        if not isinstance(plan, MigrationPlan):
            raise SchemaMigrationError(
                "invalid_plan_type",
                remediation="execute() requires a core.ports.MigrationPlan instance.",
            )
        seen_orders: set[int] = set()
        boundary_count = 0
        for step in plan.steps:
            if not isinstance(step.step_id, str) or not step.step_id.strip():
                raise SchemaMigrationError(
                    "invalid_step_id",
                    step_id=str(getattr(step, "step_id", None)),
                    phase=getattr(step, "phase", None),
                    remediation="every step needs a non-empty string step_id.",
                )
            if step.phase not in MIGRATION_PHASES:
                raise SchemaMigrationError(
                    "invalid_phase",
                    step_id=step.step_id,
                    remediation=f"phase must be one of {MIGRATION_PHASES}.",
                )
            if not isinstance(step.order, int) or step.order < 0:
                raise SchemaMigrationError(
                    "invalid_order",
                    step_id=step.step_id,
                    phase=step.phase,
                    remediation="order must be a non-negative int.",
                )
            if step.order in seen_orders:
                raise SchemaMigrationError(
                    "duplicate_order",
                    step_id=step.step_id,
                    phase=step.phase,
                    remediation="step orders must be unique.",
                )
            seen_orders.add(step.order)
            if step.phase == "create_all_boundary":
                boundary_count += 1

        # Phase rank must be non-decreasing by order (pre -> boundary -> post).
        last_rank = -1
        for step in sorted(plan.steps, key=lambda s: s.order):
            rank = _PHASE_RANK[step.phase]
            if rank < last_rank:
                raise SchemaMigrationError(
                    "phase_out_of_order",
                    step_id=step.step_id,
                    phase=step.phase,
                    remediation="phases must be non-decreasing in execution order.",
                )
            last_rank = rank

        if boundary_count != 1:
            raise SchemaMigrationError(
                "create_all_boundary_count",
                remediation=(
                    f"exactly one create_all_boundary step is required, found "
                    f"{boundary_count}."
                ),
            )

    # -- port: execute (sync facade) --------------------------------------
    def execute(self, plan: MigrationPlan) -> MigrationResult:
        """Synchronous port facade. Drives the async executor on a fresh event
        loop. In an already-running loop use :meth:`aexecute` instead."""
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.aexecute(plan))
        raise SchemaMigrationError(
            "execute_in_running_loop",
            remediation="call `await migrator.aexecute(plan)` inside an async context.",
        )

    async def aexecute(self, plan: MigrationPlan) -> MigrationResult:
        """Async executor. Runs steps in ``order``; sync or async callables are
        both supported. Fail-closed: a failing step returns a ``failed``/
        ``partial`` :class:`MigrationResult` (never ``success``)."""
        self.validate_plan(plan)
        applied: list[MigrationStepResult] = []
        skipped: list[MigrationStepResult] = []
        start = time.perf_counter()

        for step in sorted(plan.steps, key=lambda s: s.order):
            callable_ = self._callables.get(step.step_id)
            if callable_ is None:
                failed = MigrationStepResult(
                    step_id=step.step_id,
                    status="failed",
                    phase=step.phase,
                    failure_reason="no_callable_bound",
                    remediation=(
                        "bind a callable for this step (use "
                        "make_community_relational_schema_migrator)."
                    ),
                )
                return MigrationResult.failed_result(
                    failed,
                    applied_steps=tuple(applied),
                    skipped_steps=tuple(skipped),
                    duration_ms=(time.perf_counter() - start) * 1000,
                    partial=bool(applied),
                )

            # Adapter-level idempotency: an already-applied idempotent step is
            # skipped on re-run (no drift).
            if step.idempotent and step.step_id in self._applied:
                skipped.append(
                    MigrationStepResult(
                        step_id=step.step_id,
                        status="skipped",
                        phase=step.phase,
                        metadata={"reason": "already_applied"},
                    )
                )
                continue

            t0 = time.perf_counter()
            try:
                result = callable_()
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:  # noqa: BLE001 — surfaced as fail-closed result
                failed = MigrationStepResult(
                    step_id=step.step_id,
                    status="failed",
                    phase=step.phase,
                    failure_reason=f"{type(exc).__name__}: {exc}"[:300],
                    remediation=(
                        "inspect/repair the underlying schema migration, then "
                        "re-run; earlier steps already applied are not rolled back."
                    ),
                    duration_ms=(time.perf_counter() - t0) * 1000,
                )
                return MigrationResult.failed_result(
                    failed,
                    applied_steps=tuple(applied),
                    skipped_steps=tuple(skipped),
                    duration_ms=(time.perf_counter() - start) * 1000,
                    partial=bool(applied),
                )

            duration_ms = (time.perf_counter() - t0) * 1000
            if result == "skipped":
                self._applied.add(step.step_id)
                skipped.append(
                    MigrationStepResult(
                        step_id=step.step_id,
                        status="skipped",
                        phase=step.phase,
                        duration_ms=duration_ms,
                        metadata={"reason": "callable_noop"},
                    )
                )
            else:
                self._applied.add(step.step_id)
                applied.append(
                    MigrationStepResult(
                        step_id=step.step_id,
                        status="applied",
                        phase=step.phase,
                        duration_ms=duration_ms,
                    )
                )

        return MigrationResult(
            status="success",
            applied_steps=tuple(applied),
            skipped_steps=tuple(skipped),
            duration_ms=(time.perf_counter() - start) * 1000,
        )


def _make_create_all_callable(database_module) -> StepCallable:
    async def _create_all() -> None:
        async with database_module.get_engine().begin() as conn:
            await conn.run_sync(database_module.Base.metadata.create_all)

    return _create_all


def make_community_relational_schema_migrator(
    *,
    target: str = "community-sqlite",
) -> CommunityRelationalSchemaMigrator:
    """Composition factory — binds the canonical ledger to the REAL
    ``_migrate_*`` callables + ``create_all`` from ``core.infra.database``.

    ``core.infra.database`` is imported HERE (lazily), never at module top, so
    ``core`` never imports ``community`` and the adapter module stays
    import-light (only the pure ``core.ports`` contract).
    """
    from okto_pulse.core.infra import database as _database

    steps = build_community_migration_ledger()
    callables: dict[str, StepCallable] = {}
    for step in steps:
        if step.step_id == CREATE_ALL_BOUNDARY_STEP_ID:
            callables[step.step_id] = _make_create_all_callable(_database)
            continue
        fn = getattr(_database, step.step_id, None)
        if fn is None:  # pragma: no cover — guarded by the ledger gate test
            raise SchemaMigrationError(
                "missing_migration_callable",
                step_id=step.step_id,
                phase=step.phase,
                remediation=(
                    f"core.infra.database has no {step.step_id!r}; the ledger "
                    "drifted from init_db — reconcile R16-B with R16-A."
                ),
            )
        callables[step.step_id] = fn
    return CommunityRelationalSchemaMigrator(
        steps=steps, callables=callables, target=target
    )
