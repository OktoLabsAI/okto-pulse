"""Grafx implementation of the Core ``GraphSchemaManager`` port."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from okto_grafx import Timestamp
from okto_pulse.core.kg.interfaces.graph_errors import (
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
from okto_pulse.community.adapters.grafx_schema_manifest import (
    PULSE_GRAFX_SCHEMA_MANIFEST,
)

TimestampFactory = Callable[[], Timestamp]


def _require_board_id(board_id: object) -> str:
    if type(board_id) is not str or not board_id:
        raise ValueError("board_id must be non-empty text")
    return board_id




class CommunityGrafxGraphSchemaManager:
    """Create and validate the current Grafx schema."""

    def __init__(
        self,
        database_resolver: DatabaseResolver,
        revalidate_fence: FenceRevalidator,
        *,
        read_database_resolver: DatabaseResolver | None = None,
        read_database_scope: Callable[[str], AbstractContextManager[Any]] | None = None,
        admission: AdmissionValidator | None = None,
        timestamp_factory: TimestampFactory = current_grafx_timestamp,
    ) -> None:
        self._database_resolver = database_resolver
        self._read_database_resolver = read_database_resolver or database_resolver
        self._read_database_scope = read_database_scope
        self._revalidate_fence = revalidate_fence
        self._admission = admission
        self._timestamp_factory = timestamp_factory

    def _database(self, board_id: str):
        database = self._database_resolver(board_id)
        require_pulse_grafx_admission(board_id, database, self._admission)
        return database

    def _read_database(self, board_id: str):
        database = self._read_database_resolver(board_id)
        require_pulse_grafx_admission(board_id, database, self._admission)
        return database

    @contextmanager
    def _read_scope(self, board_id: str) -> Iterator[Any]:
        """Charge the lane through admission and the complete metadata read.

        This is scheduling only, not a retained route or a new snapshot proof.
        The resolver and admission checks apply to both read lanes.
        """
        if self._read_database_scope is None:
            yield self._read_database(board_id)
            return
        with self._read_database_scope(board_id) as database:
            require_pulse_grafx_admission(board_id, database, self._admission)
            yield database

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


    async def current_version(self, board_id: str) -> str:
        board_id = _require_board_id(board_id)
        try:
            with self._read_scope(board_id) as database:
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
            with self._read_scope(board_id) as database:
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
    "CommunityGrafxGraphSchemaManager",
]
