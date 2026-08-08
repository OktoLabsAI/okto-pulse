"""Mechanism-attested isolated-copy runner and post-correction proof."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from okto_pulse.community.adapters.terminal_debt_source import (
    SqliteTerminalDebtSnapshotIsolation,
    TerminalDebtSourceIdentityError,
    sqlite_file_digest,
    sqlite_storage_fingerprint,
)
from okto_pulse.core.application.terminal_debt_recovery import (
    build_recovery_plan,
    verify_recovery_proof,
)
from okto_pulse.core.domain.terminal_debt import (
    TerminalDebtContractError,
    TerminalDebtDomain,
    TerminalDebtExecutionResult,
    TerminalDebtIdentity,
    TerminalDebtManifest,
    TerminalDebtPlanDecision,
    TerminalDebtProof,
    normalize_sha256,
)
from okto_pulse.core.ports.terminal_debt import TerminalDebtCopyExecutor


class SnapshotExecutionDenied(RuntimeError):
    """Copy execution cannot begin without every isolation boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TerminalDebtSnapshotReport:
    decision: TerminalDebtPlanDecision
    results: tuple[TerminalDebtExecutionResult, ...] = ()
    proof: TerminalDebtProof | None = None
    isolation_provenance_digest: str | None = None
    origin_before_file_digest: str | None = None
    origin_after_file_digest: str | None = None
    copy_after_file_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision, TerminalDebtPlanDecision):
            raise TerminalDebtContractError("terminal_debt_snapshot_decision_invalid")
        if self.decision.allowed != (self.proof is not None):
            raise TerminalDebtContractError("terminal_debt_snapshot_report_invalid")
        if self.proof is None:
            if self.results or any(
                value is not None
                for value in (
                    self.isolation_provenance_digest,
                    self.origin_before_file_digest,
                    self.origin_after_file_digest,
                    self.copy_after_file_digest,
                )
            ):
                raise TerminalDebtContractError("terminal_debt_snapshot_report_invalid")
            return
        for field_name in (
            "isolation_provenance_digest",
            "origin_before_file_digest",
            "origin_after_file_digest",
            "copy_after_file_digest",
        ):
            value = getattr(self, field_name)
            if value is None:
                raise TerminalDebtContractError("terminal_debt_snapshot_report_invalid")
            object.__setattr__(
                self,
                field_name,
                normalize_sha256(
                    value,
                    f"terminal_debt_snapshot_{field_name}_invalid",
                ),
            )

    @property
    def executed(self) -> bool:
        return self.proof is not None

    @property
    def origin_bytes_unchanged(self) -> bool:
        return (
            self.proof is not None
            and self.origin_before_file_digest == self.origin_after_file_digest
        )

    @property
    def verified(self) -> bool:
        return (
            self.proof is not None
            and self.proof.verified
            and self.origin_bytes_unchanged
        )


def _verify_isolation(isolation: SqliteTerminalDebtSnapshotIsolation) -> None:
    try:
        isolation.verify_pre_execution()
    except TerminalDebtSourceIdentityError as exc:
        raise SnapshotExecutionDenied(exc.code) from exc


class IsolatedCopyTerminalDebtRunner:
    """Run only against a byte-identical, mechanism-attested SQLite copy."""

    def __init__(self, executors: Sequence[TerminalDebtCopyExecutor]) -> None:
        if not isinstance(executors, Sequence) or isinstance(
            executors, str | bytes | bytearray
        ):
            raise SnapshotExecutionDenied("copy_executors_invalid")
        by_domain: dict[TerminalDebtDomain, TerminalDebtCopyExecutor] = {}
        for executor in executors:
            domain = getattr(executor, "domain", None)
            if not isinstance(domain, TerminalDebtDomain):
                raise SnapshotExecutionDenied("copy_executor_domain_invalid")
            if domain in by_domain:
                raise SnapshotExecutionDenied("copy_executor_domain_duplicate")
            by_domain[domain] = executor
        self._executors = by_domain

    async def run(
        self,
        *,
        isolation: SqliteTerminalDebtSnapshotIsolation,
        origin_before: TerminalDebtManifest,
        copy_before: TerminalDebtManifest,
        selection: Sequence[TerminalDebtIdentity],
        read_origin_after: Callable[[], Awaitable[TerminalDebtManifest]],
        read_copy_after: Callable[[], Awaitable[TerminalDebtManifest]],
    ) -> TerminalDebtSnapshotReport:
        if not isinstance(isolation, SqliteTerminalDebtSnapshotIsolation):
            raise SnapshotExecutionDenied("source_identity_unproven")
        _verify_isolation(isolation)
        if (
            origin_before.source_fingerprint != isolation.origin_fingerprint
            or copy_before.source_fingerprint != isolation.copy_fingerprint
        ):
            raise SnapshotExecutionDenied("source_identity_unproven")

        decision = build_recovery_plan(
            manifest=origin_before,
            selection=selection,
            origin_fingerprint=isolation.origin_fingerprint,
            copy_fingerprint=isolation.copy_fingerprint,
        )
        if not decision.allowed:
            return TerminalDebtSnapshotReport(decision=decision)

        plan = decision.plan
        assert plan is not None
        if (
            copy_before.domain is not origin_before.domain
            or copy_before.scope_id != origin_before.scope_id
            or copy_before.semantic_digest != origin_before.semantic_digest
        ):
            raise SnapshotExecutionDenied("copy_baseline_mismatch")

        executor = self._executors.get(plan.domain)
        if executor is None:
            raise SnapshotExecutionDenied("copy_executor_not_configured")
        try:
            # Community does not trust the executor's declared label.  The
            # concrete target path must resolve to the attested copy's file
            # identity before any command capability is invoked.
            mechanism_target_fingerprint = sqlite_storage_fingerprint(
                getattr(executor, "target_path", None)
            )
            target_fingerprint = normalize_sha256(
                getattr(executor, "target_fingerprint", None),
                "terminal_debt_executor_target_fingerprint_invalid",
            )
        except (TerminalDebtContractError, TerminalDebtSourceIdentityError) as exc:
            raise SnapshotExecutionDenied("copy_target_mismatch") from exc
        if (
            mechanism_target_fingerprint != isolation.copy_fingerprint
            or target_fingerprint != mechanism_target_fingerprint
        ):
            raise SnapshotExecutionDenied("copy_target_mismatch")

        # Repeat the mechanism-backed seal check immediately before the first
        # command call.  Any origin/copy replacement or byte change during
        # planning therefore denies with executor call count still at zero.
        _verify_isolation(isolation)
        results = tuple(await executor.execute(plan))
        if any(not isinstance(item, TerminalDebtExecutionResult) for item in results):
            raise SnapshotExecutionDenied("copy_executor_result_invalid")

        origin_after_file_digest = sqlite_file_digest(isolation.origin_path)
        copy_after_file_digest = sqlite_file_digest(isolation.copy_path)
        origin_after = await read_origin_after()
        copy_after = await read_copy_after()
        proof = verify_recovery_proof(
            plan=plan,
            origin_before=origin_before,
            origin_after=origin_after,
            copy_before=copy_before,
            copy_after=copy_after,
            results=results,
        )
        return TerminalDebtSnapshotReport(
            decision=decision,
            results=results,
            proof=proof,
            isolation_provenance_digest=isolation.provenance_digest,
            origin_before_file_digest=isolation.baseline_content_digest,
            origin_after_file_digest=origin_after_file_digest,
            copy_after_file_digest=copy_after_file_digest,
        )


__all__ = [
    "IsolatedCopyTerminalDebtRunner",
    "SnapshotExecutionDenied",
    "TerminalDebtSnapshotReport",
]
