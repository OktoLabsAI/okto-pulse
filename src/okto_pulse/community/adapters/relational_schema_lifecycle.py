"""Community adapter: relational schema-lifecycle orchestrator (spec R01C REPLAN-IMP4).

Composes the R16-B ``CommunityRelationalSchemaMigrator`` (schema region) and the
R16-C ``CommunityDataBootstrapper`` (data-bootstrap region) into the single
``RelationalSchemaLifecycleOrchestrator`` the core schema-lifecycle seam
(``okto_pulse.core.infra.schema_lifecycle``) resolves. Registering it MOVES the
``init_db`` lifecycle ownership to the Community edition (FR3/FR5): once
registered, core ``init_db`` delegates the WHOLE migrate -> create_all -> seed
lifecycle here and its inline body never runs. Register-before-remove — the core
inline fallback stays until the final R01C physical removal (gated by
``relational_lifecycle_decomposition.r01c_lifecycle_removal_readiness``).

Ordering (decision IMP4-B, equivalence-PROVEN — see
``tests/test_r01c_imp4_schema_lifecycle_orchestrator.py``): the orchestrator runs
the schema plan FULLY (``pre_create_all`` -> ``create_all_boundary`` ->
``post_create_all``, including ``_migrate_agent_permissions`` at the tail of the
schema region) and THEN the data-bootstrap plan (presets -> permissions reconcile
-> discovery intents). This is the clean ports composition. It differs from
``init_db``'s effective order in exactly ONE position: ``_migrate_agent_permissions``
runs BEFORE ``_seed_builtin_presets`` (``init_db`` runs it AFTER). That reorder is
behavior-preserving and proven re-executable:

  * ``_migrate_agent_permissions`` writes ONLY ``agents``; ``_seed_builtin_presets``
    writes ONLY ``permission_presets`` (DISJOINT tables, no shared observable
    state) -> the two COMMUTE (commutativity oracle: ``[migrate, seed]`` and
    ``[seed, migrate]`` from an identical start yield identical final state of both
    tables).
  * ``_migrate_agent_permissions`` still runs BEFORE
    ``_reconcile_agent_permission_flags`` (the only real ordering dependency:
    populate null flags before backfilling missing keys).

Fail-closed: a failed/partial migration or bootstrap result is re-raised as the
port's structured error (``SchemaMigrationError`` / ``DataBootstrapError``) — the
lifecycle can NEVER report a silent partial success to ``init_db``.

Import-light (mirrors R16-B/R16-C): the module top pulls only the pure
``core.ports`` contract + the sibling adapter factories; ``core.infra.database`` is
imported lazily inside those factories, so ``core`` never imports ``community``.
"""

from __future__ import annotations

from okto_pulse.core.ports import (
    DataBootstrapError,
    SchemaMigrationError,
)

from .data_bootstrapper import (
    CommunityDataBootstrapper,
    make_community_data_bootstrapper,
)
from .relational_schema_migrator import (
    CommunityRelationalSchemaMigrator,
    make_community_relational_schema_migrator,
)

__all__ = [
    "CommunityRelationalSchemaLifecycleOrchestrator",
    "make_community_relational_schema_lifecycle_orchestrator",
    "register_community_relational_schema_lifecycle",
]


class CommunityRelationalSchemaLifecycleOrchestrator:
    """Concrete ``RelationalSchemaLifecycleOrchestrator`` for the Community edition.

    Implements the core seam Protocol (``async def initialize_schema``) by running
    the schema-migration plan and then the data-bootstrap plan against the live,
    R01B-owned engine/session factory. Construct via
    :func:`make_community_relational_schema_lifecycle_orchestrator` for the real
    wiring, or directly with a custom migrator/bootstrapper pair for deterministic
    tests.
    """

    def __init__(
        self,
        *,
        migrator: CommunityRelationalSchemaMigrator,
        bootstrapper: CommunityDataBootstrapper,
        target: str = "community-sqlite",
    ) -> None:
        self._migrator = migrator
        self._bootstrapper = bootstrapper
        self._target = target

    async def initialize_schema(self) -> None:
        """Run the full relational schema lifecycle (FR3): schema region first,
        then data bootstrap. Fail-closed — a non-success result is re-raised as the
        port's structured error so ``init_db`` never proceeds on a partial run."""
        # 1) Schema region: pre_create_all -> create_all_boundary -> post_create_all
        #    (incl _migrate_agent_permissions at the tail of the schema region).
        schema_plan = self._migrator.plan(target=self._target)
        schema_result = await self._migrator.aexecute(schema_plan)
        if not schema_result.is_success:
            step = schema_result.failed_step
            raise SchemaMigrationError(
                schema_result.failure_reason or "schema_migration_failed",
                step_id=step.step_id if step else None,
                phase=step.phase if step else None,
                remediation=schema_result.remediation,
            )
        # 2) Data-bootstrap region: presets -> permissions reconcile -> discovery.
        data_plan = self._bootstrapper.plan(target=self._target)
        data_result = await self._bootstrapper.aexecute(data_plan)
        if not data_result.is_success:
            step = data_result.failed_step
            raise DataBootstrapError(
                data_result.failure_reason or "data_bootstrap_failed",
                step_id=step.step_id if step else None,
                domain=step.domain if step else None,
                remediation=data_result.remediation,
            )


def make_community_relational_schema_lifecycle_orchestrator(
    *,
    target: str = "community-sqlite",
) -> CommunityRelationalSchemaLifecycleOrchestrator:
    """Composition factory — binds the R16-B migrator + R16-C bootstrapper (each
    wired to the REAL ``core.infra.database`` callables) into the lifecycle
    orchestrator. Import-light: the factories lazy-import ``core.infra.database``."""
    return CommunityRelationalSchemaLifecycleOrchestrator(
        migrator=make_community_relational_schema_migrator(target=target),
        bootstrapper=make_community_data_bootstrapper(target=target),
        target=target,
    )


def register_community_relational_schema_lifecycle(
    *,
    target: str = "community-sqlite",
) -> CommunityRelationalSchemaLifecycleOrchestrator:
    """Register the Community schema-lifecycle orchestrator on the core seam (FR3).

    Idempotent (last-writer-wins on the process-global seam). Call from every
    Community boot path BEFORE ``init_db`` so the core delegates the lifecycle to
    the edition instead of running its inline body. Returns the registered
    orchestrator (handy for tests / diagnostics)."""
    from okto_pulse.core.infra.schema_lifecycle import (
        register_relational_schema_lifecycle_orchestrator,
    )

    orchestrator = make_community_relational_schema_lifecycle_orchestrator(target=target)
    register_relational_schema_lifecycle_orchestrator(orchestrator)
    return orchestrator
