"""Grafx implementation of the Core ``GraphSchemaManager`` port."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from okto_grafx import Timestamp
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphError,
)
from okto_pulse.core.kg.interfaces.graph_schema_manager import SchemaValidationResult

from okto_pulse.community.adapters.grafx_board_operational import (
    AdmissionValidator,
    DatabaseResolver,
    FenceRevalidator,
    core_error_code,
    current_grafx_timestamp,
    require_pulse_grafx_admission,
)
from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error
from okto_pulse.community.adapters.grafx_schema_bootstrap import (
    ensure_current_grafx_board_schema,
    read_current_grafx_schema_version,
    validate_current_grafx_schema,
)
from okto_pulse.community.adapters.grafx_schema_evolution import (
    GrafxSchemaCandidateResult,
    rebuild_grafx_schema_candidate,
)
from okto_pulse.community.adapters.grafx_schema_manifest import (
    PULSE_GRAFX_SCHEMA_MANIFEST,
)

CandidatePathResolver = Callable[[str], Path]
CandidateActivator = Callable[[str, Path, GrafxSchemaCandidateResult], None]
TimestampFactory = Callable[[], Timestamp]


def _require_board_id(board_id: object) -> str:
    if type(board_id) is not str or not board_id:
        raise ValueError("board_id must be non-empty text")
    return board_id


def _migration_capability(reason: str, *, board_id: str) -> GraphError:
    return GraphCapabilityUnavailable(
        "Grafx schema migration is not fully composed for this board.",
        details={
            "backend": "okto_grafx",
            "operation": "schema_migrate",
            "reason": reason,
            "board_id": board_id,
        },
    )


class CommunityGrafxGraphSchemaManager:
    """Bootstrap, validate, and atomically activate Grafx schema generations."""

    def __init__(
        self,
        database_resolver: DatabaseResolver,
        revalidate_fence: FenceRevalidator,
        *,
        admission: AdmissionValidator | None = None,
        candidate_path_resolver: CandidatePathResolver | None = None,
        candidate_activator: CandidateActivator | None = None,
        timestamp_factory: TimestampFactory = current_grafx_timestamp,
        rebuild_batch_size: int = 256,
    ) -> None:
        self._database_resolver = database_resolver
        self._revalidate_fence = revalidate_fence
        self._admission = admission
        self._candidate_path_resolver = candidate_path_resolver
        self._candidate_activator = candidate_activator
        self._timestamp_factory = timestamp_factory
        self._rebuild_batch_size = rebuild_batch_size

    def _database(self, board_id: str):
        database = self._database_resolver(board_id)
        require_pulse_grafx_admission(board_id, database, self._admission)
        return database

    def _bootstrap(self, board_id: str, database):
        self._revalidate_fence(board_id, "bootstrap")
        return ensure_current_grafx_board_schema(
            database,
            board_id=board_id,
            bootstrapped_at=self._timestamp_factory(),
            revalidate_fence=lambda phase: self._revalidate_fence(board_id, phase),
        )

    async def ensure_bootstrapped(self, board_id: str) -> None:
        board_id = _require_board_id(board_id)
        try:
            self._revalidate_fence(board_id, "bootstrap")
            database = self._database(board_id)
            self._bootstrap(board_id, database)
        except GraphError:
            raise
        except Exception as exc:
            mapped = map_grafx_error(exc, operation="schema_bootstrap")
            raise mapped from exc

    async def migrate(self, board_id: str) -> dict[str, Any]:
        board_id = _require_board_id(board_id)
        try:
            self._revalidate_fence(board_id, "schema_migrate")
            source = self._database(board_id)
            observed = read_current_grafx_schema_version(source)
            target = PULSE_GRAFX_SCHEMA_MANIFEST.schema_version
            if observed is None or observed == target:
                result = self._bootstrap(board_id, source)
                return {
                    "board_id": board_id,
                    "from_version": observed,
                    "to_version": target,
                    "migrated": result.changed,
                    "activated": False,
                    "logical_fingerprint": result.logical_fingerprint,
                }

            if self._candidate_path_resolver is None:
                raise _migration_capability(
                    "candidate_path_resolver_not_configured",
                    board_id=board_id,
                )
            if self._candidate_activator is None:
                raise _migration_capability(
                    "candidate_activator_not_configured",
                    board_id=board_id,
                )

            candidate_path = Path(self._candidate_path_resolver(board_id))
            self._revalidate_fence(board_id, "schema_migrate_candidate")
            candidate = rebuild_grafx_schema_candidate(
                source,
                candidate_path,
                batch_size=self._rebuild_batch_size,
            )
            self._revalidate_fence(board_id, "schema_migrate_cutover")
            self._candidate_activator(board_id, candidate_path, candidate)
            return {
                "board_id": board_id,
                "from_version": candidate.source_schema_version,
                "to_version": candidate.target_schema_version,
                "migrated": True,
                "activated": True,
                "candidate_changed": candidate.changed,
                "logical_fingerprint": candidate.logical_data_fingerprint,
                "node_row_counts": dict(candidate.node_row_counts),
                "relationship_row_counts": dict(candidate.relationship_row_counts),
                "candidate_database_uuid": candidate.candidate_database_uuid.hex(),
            }
        except GraphError:
            raise
        except Exception as exc:
            mapped = map_grafx_error(exc, operation="schema_migrate")
            raise mapped from exc

    async def current_version(self, board_id: str) -> str:
        board_id = _require_board_id(board_id)
        try:
            database = self._database(board_id)
            return (
                read_current_grafx_schema_version(database)
                or PULSE_GRAFX_SCHEMA_MANIFEST.schema_version
            )
        except GraphError:
            raise
        except Exception as exc:
            mapped = map_grafx_error(exc, operation="schema_current_version")
            raise mapped from exc

    async def validate(self, board_id: str) -> SchemaValidationResult:
        board_id = _require_board_id(board_id)
        expected = PULSE_GRAFX_SCHEMA_MANIFEST.schema_version
        current: str | None = None
        try:
            database = self._database(board_id)
            current = read_current_grafx_schema_version(database)
            validate_current_grafx_schema(database)
            if current != expected:
                return SchemaValidationResult(
                    board_id=board_id,
                    valid=False,
                    current_version=current,
                    expected_version=expected,
                    issues=("schema_version_mismatch",),
                )
            return SchemaValidationResult(
                board_id=board_id,
                valid=True,
                current_version=current,
                expected_version=expected,
            )
        except Exception as exc:
            mapped = map_grafx_error(exc, operation="schema_validate")
            reason = mapped.details.get("reason")
            issue = (
                reason if type(reason) is str and reason else core_error_code(mapped)
            )
            return SchemaValidationResult(
                board_id=board_id,
                valid=False,
                current_version=current,
                expected_version=expected,
                issues=(issue,),
            )


__all__ = [
    "CandidateActivator",
    "CandidatePathResolver",
    "CommunityGrafxGraphSchemaManager",
]
