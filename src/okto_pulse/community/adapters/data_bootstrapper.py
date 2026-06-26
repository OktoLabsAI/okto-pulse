"""Community adapter for the ``DataBootstrapper`` port (spec R16-C).

Concrete Community-edition implementation of
``okto_pulse.core.ports.DataBootstrapper`` (R16-C). It models the DATA-bootstrap
fatia of ``okto_pulse.core.infra.database.init_db`` — the seed / reconcile /
discovery-intent steps that run AFTER schema migration — as an ordered,
declarative ledger of :class:`DataBootstrapStep`, WITHOUT moving, reordering,
removing or re-implementing any bootstrap function (register-before-remove).
``init_db`` remains the single source of truth; this adapter only *describes*
and *replays* the same steps.

Boundary (``br_e16ff5a1`` / R16-C ts_5a7b50e2): this ledger contains ONLY data
bootstrap. Schema migrations (incl. ``_migrate_agent_permissions``, which runs
late in init_db's bootstrap region but is SCHEMA and lives in the R16-B
``CommunityRelationalSchemaMigrator`` ledger) are NOT here. The two ledgers are
disjoint and cross-checked.

Layering (mirrors R16-B):
  * Module top-level imports ONLY the pure ``core.ports`` contract.
  * ``make_community_data_bootstrapper`` lazy-imports ``core.infra.database`` and
    binds the real callables — so ``core`` never imports ``community``.

Async seam (same as R16-B, documented not resolved): the bootstrap functions
are ``async``. The port's ``execute`` is sync; this adapter implements it as a
facade over the async :meth:`aexecute`, which is what an async host (the
community lifespan) should call directly.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable, Mapping

from okto_pulse.core.ports import (
    BOOTSTRAP_DOMAINS,
    DataBootstrapError,
    DataBootstrapPlan,
    DataBootstrapResult,
    DataBootstrapStep,
    DataBootstrapStepResult,
)

__all__ = [
    "CommunityDataBootstrapper",
    "build_community_data_bootstrap_ledger",
    "make_community_data_bootstrapper",
]

# A callable per step: zero-arg, sync OR async; raises on failure. May return
# the sentinel ``"skipped"`` to signal an explicit no-op (otherwise -> applied).
StepCallable = Callable[[], "Awaitable[object] | object"]


# ---------------------------------------------------------------------------
# Canonical ledger — the declarative mirror of init_db's DATA-bootstrap tail.
#
# Each tuple: (step_id, domain, description). ``order`` is the 1-based index in
# this list (== the init_db call sequence, after the schema region). Every step
# is idempotent: init_db runs them on every boot (ON CONFLICT / merge / refresh
# semantics), so re-running is safe.
# ---------------------------------------------------------------------------
_LEDGER: tuple[tuple[str, str, str], ...] = (
    ("_seed_builtin_presets", "presets",
     "Seed built-in permission presets if they don't exist."),
    ("_reconcile_builtin_presets", "presets",
     "Refresh built-in preset flags from the code definitions on every startup."),
    ("_reconcile_agent_permission_flags", "permissions",
     "Backfill missing registry keys into agents' permission_flags (deep-merge, "
     "non-destructive)."),
    ("_bootstrap_default_discovery_intents", "discovery_intents",
     "Upsert the v1 seed catalog of Discovery intents (idempotent ON CONFLICT)."),
)


def build_community_data_bootstrap_ledger() -> tuple[DataBootstrapStep, ...]:
    """Return the canonical, ordered ledger of :class:`DataBootstrapStep`.

    Declarative only — carries no SQL and binds no callable. ``order`` is the
    1-based init_db call position (within the data-bootstrap tail);
    ``owner='community'``.
    """
    steps: list[DataBootstrapStep] = []
    for order, (step_id, domain, description) in enumerate(_LEDGER, start=1):
        steps.append(
            DataBootstrapStep(
                step_id=step_id,
                order=order,
                owner="community",
                domain=domain,  # type: ignore[arg-type]
                idempotent=True,
                metadata={
                    "source": "okto_pulse.core.infra.database.init_db",
                    "description": description,
                },
            )
        )
    return tuple(steps)


class CommunityDataBootstrapper:
    """Concrete :class:`DataBootstrapper` for the Community edition.

    Construct via :func:`make_community_data_bootstrapper` for the real wiring,
    or directly with a custom ``steps``/``callables`` pair for deterministic
    testing. Callables are zero-arg, sync OR async, raise on failure, and may
    return the sentinel ``"skipped"`` to report an explicit no-op.
    """

    def __init__(
        self,
        *,
        steps: tuple[DataBootstrapStep, ...],
        callables: Mapping[str, StepCallable],
        target: str = "community-sqlite",
    ) -> None:
        self._steps = tuple(steps)
        self._callables = dict(callables)
        self._default_target = target
        # Adapter-level applied ledger: idempotent re-run -> skipped (no drift).
        self._applied: set[str] = set()

    # -- port: plan -------------------------------------------------------
    def plan(self, *, target: str) -> DataBootstrapPlan:
        domains = sorted({s.domain for s in self._steps})
        return DataBootstrapPlan(
            plan_id=f"community-data-bootstrap-{target}",
            target=target,
            steps=self._steps,
            metadata={
                "owner": "community",
                "step_count": len(self._steps),
                "domains": tuple(domains),
            },
        )

    # -- port: validate_plan (fail-closed) --------------------------------
    def validate_plan(self, plan: DataBootstrapPlan) -> None:
        if not isinstance(plan, DataBootstrapPlan):
            raise DataBootstrapError(
                "invalid_plan_type",
                remediation="execute() requires a core.ports.DataBootstrapPlan instance.",
            )
        seen_orders: set[int] = set()
        for step in plan.steps:
            if not isinstance(step.step_id, str) or not step.step_id.strip():
                raise DataBootstrapError(
                    "invalid_step_id",
                    step_id=str(getattr(step, "step_id", None)),
                    domain=getattr(step, "domain", None),
                    remediation="every step needs a non-empty string step_id.",
                )
            if step.domain not in BOOTSTRAP_DOMAINS:
                raise DataBootstrapError(
                    "invalid_domain",
                    step_id=step.step_id,
                    remediation=f"domain must be one of {BOOTSTRAP_DOMAINS}.",
                )
            if not isinstance(step.order, int) or step.order < 0:
                raise DataBootstrapError(
                    "invalid_order",
                    step_id=step.step_id,
                    domain=step.domain,
                    remediation="order must be a non-negative int.",
                )
            if step.order in seen_orders:
                raise DataBootstrapError(
                    "duplicate_order",
                    step_id=step.step_id,
                    domain=step.domain,
                    remediation="step orders must be unique.",
                )
            seen_orders.add(step.order)

    # -- port: execute (sync facade) --------------------------------------
    def execute(self, plan: DataBootstrapPlan) -> DataBootstrapResult:
        """Synchronous port facade. Drives the async executor on a fresh event
        loop. In an already-running loop use :meth:`aexecute` instead."""
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.aexecute(plan))
        raise DataBootstrapError(
            "execute_in_running_loop",
            remediation="call `await bootstrapper.aexecute(plan)` inside an async context.",
        )

    async def aexecute(self, plan: DataBootstrapPlan) -> DataBootstrapResult:
        """Async executor. Runs steps in ``order``; sync or async callables are
        both supported. Fail-closed: a failing step returns a ``failed``/
        ``partial`` :class:`DataBootstrapResult` (never ``success``)."""
        self.validate_plan(plan)
        applied: list[DataBootstrapStepResult] = []
        skipped: list[DataBootstrapStepResult] = []
        start = time.perf_counter()

        for step in sorted(plan.steps, key=lambda s: s.order):
            callable_ = self._callables.get(step.step_id)
            if callable_ is None:
                failed = DataBootstrapStepResult(
                    step_id=step.step_id,
                    status="failed",
                    owner=step.owner,
                    domain=step.domain,
                    failure_reason="no_callable_bound",
                    remediation=(
                        "bind a callable for this step (use "
                        "make_community_data_bootstrapper)."
                    ),
                )
                return DataBootstrapResult.failed_result(
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
                    DataBootstrapStepResult(
                        step_id=step.step_id,
                        status="skipped",
                        owner=step.owner,
                        domain=step.domain,
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
                failed = DataBootstrapStepResult(
                    step_id=step.step_id,
                    status="failed",
                    owner=step.owner,
                    domain=step.domain,
                    failure_reason=f"{type(exc).__name__}: {exc}"[:300],
                    remediation=(
                        "inspect/repair the underlying bootstrap step, then "
                        "re-run; earlier steps already applied are not rolled back."
                    ),
                    duration_ms=(time.perf_counter() - t0) * 1000,
                )
                return DataBootstrapResult.failed_result(
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
                    DataBootstrapStepResult(
                        step_id=step.step_id,
                        status="skipped",
                        owner=step.owner,
                        domain=step.domain,
                        duration_ms=duration_ms,
                        metadata={"reason": "callable_noop"},
                    )
                )
            else:
                self._applied.add(step.step_id)
                applied.append(
                    DataBootstrapStepResult(
                        step_id=step.step_id,
                        status="applied",
                        owner=step.owner,
                        domain=step.domain,
                        duration_ms=duration_ms,
                    )
                )

        return DataBootstrapResult(
            status="success",
            applied_steps=tuple(applied),
            skipped_steps=tuple(skipped),
            duration_ms=(time.perf_counter() - start) * 1000,
        )


def make_community_data_bootstrapper(
    *,
    target: str = "community-sqlite",
) -> CommunityDataBootstrapper:
    """Composition factory — binds the canonical ledger to the REAL bootstrap
    callables from ``core.infra.database``.

    ``core.infra.database`` is imported HERE (lazily), never at module top, so
    ``core`` never imports ``community`` and the adapter module stays
    import-light (only the pure ``core.ports`` contract).
    """
    from okto_pulse.core.infra import database as _database

    steps = build_community_data_bootstrap_ledger()
    callables: dict[str, StepCallable] = {}
    for step in steps:
        fn = getattr(_database, step.step_id, None)
        if fn is None:  # pragma: no cover — guarded by the ledger gate test
            raise DataBootstrapError(
                "missing_bootstrap_callable",
                step_id=step.step_id,
                domain=step.domain,
                remediation=(
                    f"core.infra.database has no {step.step_id!r}; the ledger "
                    "drifted from init_db — reconcile R16-C with init_db."
                ),
            )
        callables[step.step_id] = fn
    return CommunityDataBootstrapper(
        steps=steps, callables=callables, target=target
    )
