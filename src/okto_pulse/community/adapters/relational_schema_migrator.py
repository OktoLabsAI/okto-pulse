"""Community adapter for the ``RelationalSchemaMigrator`` port (spec R16-B).

This adapter is the Community-edition concrete implementation of the
``okto_pulse.core.ports.RelationalSchemaMigrator`` Protocol (R16-A). It owns the
ordered, declarative ledger of :class:`MigrationStep` and binds it to
Community-owned migration callables from ``relational_schema_steps``.

Layering (br/ac of R16-A + R16-B):
  * The module top-level imports ONLY the pure ``core.ports`` contract (DTOs +
    Protocol). It does NOT import SQLAlchemy, ``infra.database``, any
    ``_migrate_*`` function or the engine at import time.
  * ``make_community_relational_schema_migrator`` (the composition factory)
    imports concrete Community step functions lazily. ``core`` never imports
    ``community`` and the adapter module stays import-light.

Ledger scope (br_e16ff5a1):
  * EXACTLY ONE ``create_all_boundary`` step (``Base.metadata.create_all``).
  * Every Community-owned ``async def _migrate_*`` in the lifecycle ledger is a
    schema step.
  * ``_seed_builtin_presets`` / ``_reconcile_*`` / ``_bootstrap_default_discovery_intents``
    are DATA bootstrap (``data_bootstrap_boundary``) and are deliberately
    EXCLUDED — a schema plan must never silently absorb data seeding.
  * Nuance: ``_migrate_agent_permissions`` is an ``async def _migrate_*`` that
    runs at the tail of the schema region. It remains classified
    ``post_create_all`` so permission-flag schema migration precedes data
    reconciliation.

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
# Canonical ledger — the Community relational schema lifecycle ordering.
#
# Each tuple: (step_id, phase, destructive, description). ``order`` is the
# 1-based index in this list (== the init_db call sequence). ``idempotent`` is
# True for every step: init_db runs them on every boot, so each is guard-then-
# alter safe; ``create_all`` is checkfirst-idempotent.
# ---------------------------------------------------------------------------
_LEDGER: tuple[tuple[str, str, bool, str], ...] = (
    # --- pre_create_all: schema ALTERs applied BEFORE create_all ---
    (
        "_migrate_card_statuses",
        "pre_create_all",
        False,
        "Migrate the card status enum/values before create_all (avoids PG enum conflicts).",
    ),
    (
        "_migrate_add_priority_column",
        "pre_create_all",
        False,
        "Add the card priority column.",
    ),
    ("_migrate_add_realm_id", "pre_create_all", False, "Add the realm_id column."),
    (
        "_migrate_add_comment_choice_columns",
        "pre_create_all",
        False,
        "Add comment choice columns.",
    ),
    ("_migrate_add_bug_card_columns", "pre_create_all", False, "Add bug-card columns."),
    (
        "_migrate_add_task_requirement_gate_card_column",
        "pre_create_all",
        False,
        "Add task requirement-link gate skip column on cards.",
    ),
    (
        "_migrate_add_skip_rules_coverage",
        "pre_create_all",
        False,
        "Add skip-rules-coverage column.",
    ),
    (
        "_migrate_add_skip_trs_coverage",
        "pre_create_all",
        False,
        "Add skip-TRs-coverage column.",
    ),
    (
        "_migrate_add_decisions_columns",
        "pre_create_all",
        False,
        "Add decisions columns.",
    ),
    (
        "_migrate_decisions_default_false",
        "pre_create_all",
        False,
        "Backfill decisions default to false.",
    ),
    ("_migrate_add_archive_columns", "pre_create_all", False, "Add archive columns."),
    (
        "_migrate_add_spec_edition",
        "pre_create_all",
        False,
        "Add and backfill the human-facing Spec edition counter.",
    ),
    (
        "_migrate_add_spec_validation_columns",
        "pre_create_all",
        False,
        "Add spec-validation columns.",
    ),
    (
        "_migrate_add_ir_or_columns",
        "pre_create_all",
        False,
        "Add integration-requirement OR columns.",
    ),
    (
        "_migrate_add_spec_validation_gate_columns",
        "pre_create_all",
        False,
        "Add spec-validation-gate columns.",
    ),
    (
        "_migrate_add_ideation_skip_ambiguity_gate",
        "pre_create_all",
        False,
        "Add ideation skip-ambiguity-gate column.",
    ),
    (
        "_migrate_add_refinement_skip_ambiguity_gate",
        "pre_create_all",
        False,
        "Add refinement skip-ambiguity-gate column.",
    ),
    (
        "_migrate_heal_task_validation_field_names",
        "pre_create_all",
        False,
        "Heal task-validation field names.",
    ),
    (
        "_migrate_status_renames",
        "pre_create_all",
        False,
        "Apply status renames (value transforms; preserves rows).",
    ),
    (
        "_migrate_add_permission_columns",
        "pre_create_all",
        False,
        "Add permission columns.",
    ),
    (
        "_migrate_add_event_tables",
        "pre_create_all",
        False,
        "Add the event/outbox tables ahead of create_all.",
    ),
    # --- create_all_boundary ---
    (
        CREATE_ALL_BOUNDARY_STEP_ID,
        "create_all_boundary",
        False,
        "Base.metadata.create_all — the table-create boundary.",
    ),
    # --- post_create_all: schema ALTERs applied AFTER create_all ---
    (
        "_migrate_add_consolidation_work_kinds",
        "post_create_all",
        False,
        "Rebuild the legacy consolidation queue into the governed multi-kind, "
        "generation-aware contract.",
    ),
    (
        "_migrate_global_discovery_delivery_contract",
        "post_create_all",
        False,
        "Create and validate the durable GD delivery ledger, and widen the "
        "Global Update Outbox physical attempt key without losing rows.",
    ),
    (
        "_migrate_cognitive_source_revision_ledger",
        "post_create_all",
        False,
        "Audit the additive cognitive-source revision ledger and install "
        "immutable UPDATE/DELETE guards.",
    ),
    (
        "_migrate_global_discovery_recovery_control_plane",
        "post_create_all",
        False,
        "Converge the durable Global Discovery recovery attempt, singleton slot, "
        "and claimed-dispatch control-plane schema.",
    ),
    (
        "_migrate_story_ideation_single_link",
        "post_create_all",
        False,
        "Enforce single story->ideation link.",
    ),
    ("_migrate_add_card_sprint_id", "post_create_all", False, "Add card.sprint_id."),
    (
        "_migrate_add_card_knowledge_bases",
        "post_create_all",
        False,
        "Add card knowledge-base columns.",
    ),
    (
        "_migrate_add_knowledge_source_columns",
        "post_create_all",
        False,
        "Add knowledge-source columns.",
    ),
    (
        "_migrate_add_kb_lineage_columns",
        "post_create_all",
        False,
        "Add knowledge-base lineage columns.",
    ),
    (
        "_migrate_add_kb_governance_metadata",
        "post_create_all",
        False,
        "Add and validate nullable JSON governance metadata on entity knowledge bases.",
    ),
    (
        "_migrate_knowledge_propagation_v2_schema",
        "post_create_all",
        False,
        "Create and post-validate target scopes, temporal assignments, governed "
        "snapshots, DROP tombstones, and append-only mutation ledgers.",
    ),
    (
        "_migrate_add_sprint_scope_fields",
        "post_create_all",
        False,
        "Add sprint scope fields.",
    ),
    (
        "_migrate_add_sprint_lane_fields",
        "post_create_all",
        False,
        "Add sprint lane fields.",
    ),
    (
        "_migrate_agent_boards",
        "post_create_all",
        False,
        "Backfill the agent_boards junction table.",
    ),
    (
        "_migrate_add_task_validation_columns",
        "post_create_all",
        False,
        "Add task-validation columns.",
    ),
    (
        "_migrate_add_consolidation_resilience_columns",
        "post_create_all",
        False,
        "Add consolidation-resilience columns.",
    ),
    (
        "_migrate_add_kg_tick_boards_failed",
        "post_create_all",
        False,
        "Add kg_tick boards_failed column.",
    ),
    (
        "_migrate_drop_spec_skills",
        "post_create_all",
        True,
        "DROP TABLE spec_skills (Skills removal; no data preservation — destructive).",
    ),
    (
        "_migrate_add_default_config_snapshot",
        "post_create_all",
        False,
        "Add default-config snapshot column on Board.settings.",
    ),
    (
        "_migrate_add_default_config_spec_checklist_mode",
        "post_create_all",
        False,
        "Add curated Spec checklist mode to default-board templates.",
    ),
    (
        "_migrate_add_agent_seen_board_id",
        "post_create_all",
        False,
        "Add and backfill board scope on agent seen markers.",
    ),
    (
        "_migrate_add_board_guideline_provenance",
        "post_create_all",
        False,
        "Add board-guideline provenance columns.",
    ),
    (
        "_migrate_add_cancellation_columns",
        "post_create_all",
        False,
        "Add cancellation-justification columns (reason/at/by) to ideations, "
        "refinements, specs, sprints, and cards (ITEM 17).",
    ),
    (
        "_migrate_pagination_indices_and_positions",
        "post_create_all",
        False,
        "Pagination covering indices + dense card-position backfill "
        "(actives 0..n-1, archived n..m; idempotent — spec 8b33f9a8).",
    ),
    (
        "_migrate_ensure_guideline_binding_exact_authority_index",
        "post_create_all",
        False,
        "Backfill the unique 5-column authority index on guideline_board_bindings "
        "so the SK-B3 binding-configuration composite FK is structurally valid on "
        "migrated databases (fresh create_all already declares it; without it "
        "PRAGMA foreign_key_check fails with 'foreign key mismatch').",
    ),
    (
        "_migrate_rebuild_guideline_import_candidates_semantic_shape",
        "post_create_all",
        False,
        "Rebuild the legacy guideline_import_binding_candidates table into the "
        "SK-B3 semantic shape (source_enforcement rename + FK repoint to "
        "semantic_guideline_revisions) so the strict B03 substrate audit passes "
        "on migrated databases; deferred FK enforcement validates copied rows.",
    ),
    (
        "_migrate_rebuild_guideline_policy_v1_semantic_alignment",
        "post_create_all",
        False,
        "Rebuild the legacy guideline v1 family (bindings, impact receipts/"
        "unlinks, retirements, retirement impacts) into the SK-B3 semantic "
        "shape on migrated databases: enforcement/metric column renames, "
        "semantic proposal backfills, and impact FK repoint with "
        "legacy-context-only semantic revision seeding (same construction as "
        "the semantic governance step, so its fences pass).",
    ),
    (
        "_migrate_drop_retired_guideline_impact_v1_triggers",
        "post_create_all",
        False,
        "Drop the retired trg_guideline_impact_v1_* guard family on migrated "
        "databases: the v2 semantic manifest re-guards every v1 surface, and "
        "the stale v1 policy-constraint execution guard rejects every v2 "
        "semantic adoption/retirement event at runtime.",
    ),
    (
        "_migrate_repair_known_fixture_fk_orphans",
        "post_create_all",
        True,
        "Purge only allowlisted historical test-fixture pollution (including its "
        "synthetic board), then require a clean SQLite foreign_key_check.",
    ),
    (
        "_migrate_guideline_policy_lifecycle_substrate",
        "post_create_all",
        False,
        "Add and validate append-only guideline binding lifecycle state and "
        "the terminal retirement plus inert import-candidate substrates before "
        "the strict B03 authority audit.",
    ),
    (
        "_migrate_guideline_impact_substrate",
        "post_create_all",
        False,
        "Add guideline impact receipt/item/adoption tables and the nullable "
        "binding evidence pin before the strict B03 authority audit.",
    ),
    (
        "_migrate_guideline_policy_v1_schema",
        "post_create_all",
        False,
        "Backfill exact immutable guideline revisions, heads, and board bindings; "
        "pin default refs and install permit-aware append-only guards.",
    ),
    (
        "_migrate_guideline_impact_v1_schema",
        "post_create_all",
        False,
        "Seal immutable impact evidence and explicit-adoption integrity after "
        "legacy/global guideline bindings have been backfilled.",
    ),
    (
        "_migrate_policy_compliance_v1_schema",
        "post_create_all",
        False,
        "Add policy subject version fences; persist immutable compliance "
        "receipts, adopted revisions, and findings with keyset indexes.",
    ),
    (
        "_migrate_policy_waiver_v1_schema",
        "post_create_all",
        False,
        "Persist governed waiver heads and append-only lifecycle events; "
        "install CAS, immutability, and board-erasure guards.",
    ),
    (
        "_migrate_semantic_guideline_governance_schema",
        "post_create_all",
        False,
        "Install semantic metric/configuration authority, sealed cognitive "
        "assessment evidence, exact waivers/skips and explicit inert policy/v1 "
        "migration audit.",
    ),
    (
        "_migrate_seed_semantic_configurations_for_legacy_bindings",
        "post_create_all",
        False,
        "Seed the semantically inert default configuration (advisory, "
        "minimum confidence 70, no overrides, context-only revision pin) for "
        "migrated legacy bindings so the fail-closed SK-B3 binding hydration "
        "inventory is satisfiable on upgraded databases.",
    ),
    (
        "_migrate_recompute_cognitive_source_fingerprints_v2",
        "post_create_all",
        False,
        "Rewrite every durable cognitive-source revision fingerprint under "
        "the v2 identity contract (volatile usage statistics excluded) so "
        "replays of drifted-but-identical knowledge resolve idempotently "
        "instead of poisoning consolidation with replay conflicts.",
    ),
    (
        "_migrate_quality_assessment_c7_schema",
        "post_create_all",
        False,
        "Converge quality Q&A lifecycle columns and install permit-aware "
        "immutability guards for quality, RDL, checklist, and legacy-import rows.",
    ),
    (
        "_migrate_agent_permissions",
        "post_create_all",
        False,
        "Schema migration classified as post_create_all so legacy agent permissions "
        "are migrated before permission-flag data reconciliation.",
    ),
    # Data bootstrap (_seed_builtin_presets / _reconcile_* /
    # _bootstrap_default_discovery_intents) runs after the schema ledger.
)

#: step_ids that are real Community ``_migrate_*`` functions. Excludes the
#: create_all_boundary step.
_MIGRATE_STEP_IDS: tuple[str, ...] = tuple(
    sid for sid, phase, _d, _desc in _LEDGER if phase != "create_all_boundary"
)


def build_community_migration_ledger() -> tuple[MigrationStep, ...]:
    """Return the canonical, ordered ledger of :class:`MigrationStep`.

    Declarative only — carries no SQL and binds no callable. ``order`` is the
    1-based lifecycle call position; ``owner='community'``.
    """
    steps: list[MigrationStep] = []
    for order, (step_id, phase, destructive, description) in enumerate(
        _LEDGER, start=1
    ):
        metadata: dict[str, object] = {
            "source": "okto_pulse.community.adapters.relational_schema_steps"
        }
        if step_id == "_migrate_agent_permissions":
            metadata["runs_at_schema_tail"] = True
            metadata["nuance"] = (
                "executes at the tail of the schema ledger before data-bootstrap "
                "permission reconciliation."
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
                "create_all_boundary_order": boundary_orders[0]
                if boundary_orders
                else None,
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


def make_community_relational_schema_migrator(
    *,
    target: str = "community-sqlite",
) -> CommunityRelationalSchemaMigrator:
    """Bind the canonical ledger to concrete Community schema step callables.

    Concrete DDL and ``create_all`` execution live in
    ``relational_schema_steps``. The core keeps the ORM ``Base`` but no longer
    owns lifecycle execution.
    """
    from .relational_schema_steps import (
        SCHEMA_STEP_CALLABLES,
        create_all_boundary,
    )

    steps = build_community_migration_ledger()
    callables: dict[str, StepCallable] = dict(SCHEMA_STEP_CALLABLES)
    callables[CREATE_ALL_BOUNDARY_STEP_ID] = create_all_boundary
    for step in steps:
        if step.step_id not in callables:  # pragma: no cover — guarded by tests
            raise SchemaMigrationError(
                "missing_migration_callable",
                step_id=step.step_id,
                phase=step.phase,
                remediation=(
                    f"Community schema steps have no {step.step_id!r}; "
                    "the ledger drifted from the concrete adapter callables."
                ),
            )
    return CommunityRelationalSchemaMigrator(
        steps=steps, callables=callables, target=target
    )
