"""Durable Board-only logical reconciliation journal for graph rollout.

The journal is deliberately independent from the graph router and from either
backend.  A caller prepares a logical mutation before touching the active
source, terminalizes that entry after the source outcome is known, and later
replays only ``source_committed`` entries into the opposite generation.

Every public operation opens and closes its own stdlib SQLite connection.  The
database uses WAL with ``synchronous=FULL`` and all snapshots/changes run under
``BEGIN IMMEDIATE`` so state CAS, sequence allocation and acknowledgements are
serialized across processes.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal

from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphCorruption,
    GraphError,
    GraphLockContention,
    GraphUnavailable,
)

from okto_pulse.community.adapters.filesystem_erasure import (
    fsync_directory,
    is_filesystem_alias,
    reject_filesystem_alias_ancestry,
    remove_contained_tree,
    validate_scope_id,
)
from okto_pulse.community.config import validate_grafx_page_size

ROLLOUT_JOURNAL_FORMAT: Final = "okto-pulse-community-graph-rollout-journal/1"
ROLLOUT_SCHEMA_VERSION: Final = 1
ROLLOUT_APPLICATION_ID: Final = 0x4F505247  # ASCII "OPRG"
ROLLOUT_DATABASE_FILENAME: Final = "journal.sqlite3"
MAX_MUTATION_PAGE_SIZE: Final = 1_000
MAX_DIVERGENCE_PAGE_SIZE: Final = 1_000
MAX_COMPARISON_PAGE_SIZE: Final = 1_000

RolloutState = Literal[
    "shadowing",
    "canary_ready",
    "grafx_active_rollback_open",
    "grafx_active_rollback_closed",
    "rolled_back",
    "completed",
    "erased",
]
MutationStatus = Literal[
    "prepared",
    "source_committed",
    "source_abandoned",
    "source_reconciled",
]
CheckpointDirection = Literal["shadow", "reverse"]
GraphBackend = Literal["ladybug", "grafx"]

_ROLLOUT_STATES: Final = frozenset(
    {
        "shadowing",
        "canary_ready",
        "grafx_active_rollback_open",
        "grafx_active_rollback_closed",
        "rolled_back",
        "completed",
        "erased",
    }
)
_MUTATION_STATUSES: Final = frozenset(
    {"prepared", "source_committed", "source_abandoned", "source_reconciled"}
)
_CHECKPOINT_DIRECTIONS: Final = frozenset({"shadow", "reverse"})
_LEGAL_STATE_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "shadowing": frozenset({"canary_ready"}),
    "canary_ready": frozenset({"grafx_active_rollback_open"}),
    "grafx_active_rollback_open": frozenset(
        {"grafx_active_rollback_closed", "rolled_back"}
    ),
    "grafx_active_rollback_closed": frozenset({"completed"}),
    "rolled_back": frozenset({"completed"}),
    "completed": frozenset(),
    "erased": frozenset(),
}
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_FAMILY_RE: Final = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_PORTABLE_SEGMENT_FORBIDDEN: Final = frozenset('<>:"|?*')
_WINDOWS_RESERVED_SEGMENTS: Final = frozenset(
    {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)

_TABLE_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "journal_meta": ("key", "value"),
    "rollout_state": (
        "singleton",
        "board_id",
        "state",
        "state_version",
        "next_seq",
        "source_backend",
        "source_binding_sha256",
        "source_generation",
        "source_physical_path",
        "source_page_size",
        "candidate_backend",
        "candidate_binding_sha256",
        "candidate_generation",
        "candidate_physical_path",
        "candidate_page_size",
        "created_at_utc",
        "updated_at_utc",
        "row_sha256",
    ),
    "logical_mutations": (
        "seq",
        "family",
        "payload_json",
        "payload_sha256",
        "expected_binding_sha256",
        "status",
        "prepared_at_utc",
        "terminal_at_utc",
        "row_sha256",
    ),
    "replay_checkpoints": (
        "direction",
        "ack_version",
        "through_seq",
        "source_fingerprint",
        "target_fingerprint",
        "generation",
        "binding_sha256",
        "physical_path",
        "page_size",
        "acked_at_utc",
        "row_sha256",
    ),
    "rollout_divergences": (
        "divergence_id",
        "direction",
        "through_seq",
        "expected_fingerprint",
        "actual_fingerprint",
        "generation",
        "details_json",
        "details_sha256",
        "detected_at_utc",
        "row_sha256",
    ),
    "comparison_receipts": (
        "receipt_id",
        "direction",
        "through_seq",
        "generation",
        "binding_sha256",
        "physical_path",
        "page_size",
        "corpus_sha256",
        "source_result_sha256",
        "target_result_sha256",
        "query_count",
        "completed_at_utc",
        "row_sha256",
    ),
}

_SCHEMA_STATEMENTS: Final = (
    """
    CREATE TABLE journal_meta (
        key TEXT PRIMARY KEY NOT NULL,
        value TEXT NOT NULL
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE rollout_state (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        board_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN (
            'shadowing', 'canary_ready', 'grafx_active_rollback_open',
            'grafx_active_rollback_closed', 'rolled_back', 'completed', 'erased'
        )),
        state_version INTEGER NOT NULL CHECK (state_version >= 1),
        next_seq INTEGER NOT NULL CHECK (next_seq >= 1),
        source_backend TEXT NOT NULL CHECK (source_backend = 'ladybug'),
        source_binding_sha256 TEXT NOT NULL,
        source_generation TEXT NOT NULL,
        source_physical_path TEXT NOT NULL,
        source_page_size INTEGER,
        candidate_backend TEXT NOT NULL CHECK (candidate_backend = 'grafx'),
        candidate_binding_sha256 TEXT,
        candidate_generation TEXT NOT NULL,
        candidate_physical_path TEXT NOT NULL,
        candidate_page_size INTEGER NOT NULL,
        created_at_utc TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL,
        row_sha256 TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE logical_mutations (
        seq INTEGER PRIMARY KEY CHECK (seq >= 1),
        family TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        expected_binding_sha256 TEXT NOT NULL,
        status TEXT NOT NULL CHECK (
            status IN (
                'prepared', 'source_committed', 'source_abandoned',
                'source_reconciled'
            )
        ),
        prepared_at_utc TEXT NOT NULL,
        terminal_at_utc TEXT,
        row_sha256 TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE replay_checkpoints (
        direction TEXT PRIMARY KEY NOT NULL CHECK (direction IN ('shadow', 'reverse')),
        ack_version INTEGER NOT NULL CHECK (ack_version >= 1),
        through_seq INTEGER NOT NULL CHECK (through_seq >= 0),
        source_fingerprint TEXT NOT NULL,
        target_fingerprint TEXT NOT NULL,
        generation TEXT NOT NULL,
        binding_sha256 TEXT,
        physical_path TEXT NOT NULL,
        page_size INTEGER,
        acked_at_utc TEXT NOT NULL,
        row_sha256 TEXT NOT NULL
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE rollout_divergences (
        divergence_id INTEGER PRIMARY KEY CHECK (divergence_id >= 1),
        direction TEXT NOT NULL CHECK (direction IN ('shadow', 'reverse')),
        through_seq INTEGER NOT NULL CHECK (through_seq >= 0),
        expected_fingerprint TEXT NOT NULL,
        actual_fingerprint TEXT NOT NULL,
        generation TEXT NOT NULL,
        details_json TEXT NOT NULL,
        details_sha256 TEXT NOT NULL,
        detected_at_utc TEXT NOT NULL,
        row_sha256 TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE comparison_receipts (
        receipt_id INTEGER PRIMARY KEY CHECK (receipt_id >= 1),
        direction TEXT NOT NULL CHECK (direction IN ('shadow', 'reverse')),
        through_seq INTEGER NOT NULL CHECK (through_seq >= 0),
        generation TEXT NOT NULL,
        binding_sha256 TEXT NOT NULL,
        physical_path TEXT NOT NULL,
        page_size INTEGER,
        corpus_sha256 TEXT NOT NULL,
        source_result_sha256 TEXT NOT NULL,
        target_result_sha256 TEXT NOT NULL,
        query_count INTEGER NOT NULL CHECK (query_count >= 1),
        completed_at_utc TEXT NOT NULL,
        row_sha256 TEXT NOT NULL,
        UNIQUE (
            direction, through_seq, generation, binding_sha256,
            corpus_sha256, source_result_sha256
        )
    )
    """,
    """
    CREATE INDEX logical_mutations_status_seq_idx
    ON logical_mutations(status, seq)
    """,
    """
    CREATE INDEX rollout_divergences_direction_id_idx
    ON rollout_divergences(direction, divergence_id)
    """,
    """
    CREATE INDEX comparison_receipts_direction_id_idx
    ON comparison_receipts(direction, receipt_id)
    """,
)


class GraphRolloutJournalConflict(GraphError):
    """A lifecycle CAS, terminal outcome or monotonic ACK was stale."""

    code = "graph_rollout_journal_conflict"


@dataclass(frozen=True, slots=True)
class RolloutEndpointIdentity:
    """Authenticated identity of one rollout endpoint."""

    backend: GraphBackend
    binding_sha256: str | None
    generation: str
    physical_path: Path
    page_size: int | None = None


@dataclass(frozen=True, slots=True)
class GraphRolloutRecord:
    board_id: str
    state: RolloutState
    state_version: int
    next_seq: int
    source: RolloutEndpointIdentity
    candidate: RolloutEndpointIdentity
    created_at_utc: str
    updated_at_utc: str


@dataclass(frozen=True, slots=True)
class LogicalMutationRecord:
    seq: int
    family: str
    payload: object
    payload_json: str
    payload_sha256: str
    expected_binding_sha256: str
    status: MutationStatus
    prepared_at_utc: str
    terminal_at_utc: str | None


@dataclass(frozen=True, slots=True)
class CommittedMutationPage:
    items: tuple[LogicalMutationRecord, ...]
    cursor: int
    next_cursor: int
    high_water: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class ReplayCheckpoint:
    direction: CheckpointDirection
    ack_version: int
    through_seq: int
    source_fingerprint: str
    target_fingerprint: str
    generation: str
    binding_sha256: str | None
    physical_path: Path
    page_size: int | None
    acked_at_utc: str


@dataclass(frozen=True, slots=True)
class RolloutMutationToken:
    """Opaque durable token returned to the transaction capture wrapper."""

    board_id: str
    seq: int
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class RolloutDivergence:
    divergence_id: int
    direction: CheckpointDirection
    through_seq: int
    expected_fingerprint: str
    actual_fingerprint: str
    generation: str
    details: object
    details_sha256: str
    detected_at_utc: str


@dataclass(frozen=True, slots=True)
class ComparisonReceipt:
    receipt_id: int
    direction: CheckpointDirection
    through_seq: int
    generation: str
    binding_sha256: str
    physical_path: Path
    page_size: int | None
    corpus_sha256: str
    source_result_sha256: str
    target_result_sha256: str
    query_count: int
    completed_at_utc: str


@dataclass(frozen=True, slots=True)
class PrivacyEraseProof:
    board_id: str
    invalidated_state_version: int | None
    files_removed: int
    directories_removed: int
    checked_paths: tuple[Path, ...]
    storage_absent: bool


def _capability(reason: str, *, operation: str, **details: object) -> Exception:
    return GraphCapabilityUnavailable(
        "The Board graph rollout journal operation was refused.",
        details={"operation": operation, "reason": reason, **details},
    )


def _corruption(reason: str, *, operation: str, board_id: str) -> Exception:
    return GraphCorruption(
        "The persisted Board graph rollout journal is invalid.",
        details={
            "operation": operation,
            "reason": reason,
            "scope": "board",
            "scope_id": board_id,
        },
    )


def _unavailable(
    reason: str,
    *,
    operation: str,
    board_id: str,
    error_type: str | None = None,
) -> Exception:
    details: dict[str, object] = {
        "operation": operation,
        "reason": reason,
        "scope": "board",
        "scope_id": board_id,
    }
    if error_type is not None:
        details["error_type"] = error_type
    return GraphUnavailable(
        "The Board graph rollout journal is unavailable.", details=details
    )


def _conflict(reason: str, *, operation: str, **details: object) -> Exception:
    return GraphRolloutJournalConflict(
        "The Board graph rollout journal rejected a stale or conflicting operation.",
        details={"operation": operation, "reason": reason, **details},
    )


def _canonical_json_text(value: object) -> str:
    _validate_json_value(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("value_not_canonical_json") from exc


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_text(value).encode("utf-8")).hexdigest()


def _validate_json_value(value: object, *, path: str = "$") -> None:
    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"non_finite_number:{path}")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"non_string_key:{path}")
            _validate_json_value(item, path=f"{path}.{key}")
        return
    raise ValueError(f"unsupported_json_value:{path}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non_standard_json_constant:{value}")


def _decode_canonical_json(encoded: object) -> object:
    if type(encoded) is not str:
        raise ValueError("json_not_text")
    value = json.loads(
        encoded,
        object_pairs_hook=_strict_object,
        parse_constant=_reject_json_constant,
    )
    _validate_json_value(value)
    if _canonical_json_text(value) != encoded:
        raise ValueError("json_not_canonical")
    return value


def _require_sha256(value: object, *, field_name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name}_invalid")
    return value


def _portable_segment(value: object, *, field_name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name}_not_string")
    normalized = validate_scope_id(value, field_name=field_name)
    if (
        len(normalized) > 128
        or normalized[-1:] in {".", " "}
        or normalized.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_SEGMENTS
        or any(character in _PORTABLE_SEGMENT_FORBIDDEN for character in normalized)
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError(f"{field_name}_not_portable")
    return normalized


def _require_non_negative_int(value: object, *, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name}_invalid")
    return value


def _require_positive_int(value: object, *, field_name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{field_name}_invalid")
    return value


def _require_timestamp(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError(f"{field_name}_invalid")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as exc:
        raise ValueError(f"{field_name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{field_name}_invalid")
    return value


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class CommunityGraphRolloutJournal:
    """One durable logical outbox and rollout state machine per Board."""

    def __init__(
        self,
        kg_base_dir: str | os.PathLike[str],
        board_id: str,
        *,
        busy_timeout_seconds: float = 30.0,
    ) -> None:
        operation = "configure_graph_rollout_journal"
        try:
            self._root = self._canonical_root(kg_base_dir)
            self._board_id = _portable_segment(board_id, field_name="board_id")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise _capability("journal_identity_invalid", operation=operation) from exc
        if (
            isinstance(busy_timeout_seconds, bool)
            or not isinstance(busy_timeout_seconds, (int, float))
            or not math.isfinite(float(busy_timeout_seconds))
            or busy_timeout_seconds <= 0
        ):
            raise _capability("busy_timeout_invalid", operation=operation)
        self._busy_timeout_seconds = float(busy_timeout_seconds)
        self._board_root = self._root / "boards" / self._board_id
        self._rollout_root = self._board_root / "rollout"
        self._database_path = self._rollout_root / ROLLOUT_DATABASE_FILENAME

    @property
    def board_id(self) -> str:
        return self._board_id

    @property
    def rollout_root(self) -> Path:
        return self._rollout_root

    @property
    def database_path(self) -> Path:
        return self._database_path

    def start(
        self,
        *,
        source: RolloutEndpointIdentity,
        candidate: RolloutEndpointIdentity,
    ) -> GraphRolloutRecord:
        """Create the rollout once, or return the identical persisted rollout."""

        operation = "start_graph_rollout"
        try:
            source_body = self._normalize_endpoint(source, expected_backend="ladybug")
            candidate_body = self._normalize_endpoint(
                candidate, expected_backend="grafx"
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise _capability(
                "rollout_endpoint_identity_invalid", operation=operation
            ) from exc

        with self._transaction(
            operation=operation, create=True, verify_integrity=True
        ) as connection:
            current = self._load_rollout(
                connection, operation=operation, required=False
            )
            if current is not None:
                requested_source = self._endpoint_from_body(source_body)
                requested_candidate = self._endpoint_from_body(candidate_body)
                candidate_matches = (
                    current.candidate.generation == requested_candidate.generation
                    and current.candidate.physical_path
                    == requested_candidate.physical_path
                    and current.candidate.page_size == requested_candidate.page_size
                    and (
                        requested_candidate.binding_sha256 is None
                        or current.candidate.binding_sha256
                        == requested_candidate.binding_sha256
                    )
                )
                if current.source != requested_source or not candidate_matches:
                    raise _conflict(
                        "rollout_identity_conflict",
                        operation=operation,
                        board_id=self._board_id,
                    )
                return current

            now = _utc_now()
            body: dict[str, object] = {
                "singleton": 1,
                "board_id": self._board_id,
                "state": "shadowing",
                "state_version": 1,
                "next_seq": 1,
                "source_backend": source_body["backend"],
                "source_binding_sha256": source_body["binding_sha256"],
                "source_generation": source_body["generation"],
                "source_physical_path": source_body["physical_path"],
                "source_page_size": source_body["page_size"],
                "candidate_backend": candidate_body["backend"],
                "candidate_binding_sha256": candidate_body["binding_sha256"],
                "candidate_generation": candidate_body["generation"],
                "candidate_physical_path": candidate_body["physical_path"],
                "candidate_page_size": candidate_body["page_size"],
                "created_at_utc": now,
                "updated_at_utc": now,
            }
            connection.execute(
                """
                INSERT INTO rollout_state (
                    singleton, board_id, state, state_version, next_seq,
                    source_backend, source_binding_sha256, source_generation,
                    source_physical_path, source_page_size, candidate_backend,
                    candidate_binding_sha256, candidate_generation,
                    candidate_physical_path, candidate_page_size, created_at_utc,
                    updated_at_utc, row_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*body.values(), _canonical_sha256(body)),
            )
            created = self._load_rollout(connection, operation=operation, required=True)
            assert created is not None
            return created

    def read(self) -> GraphRolloutRecord:
        operation = "read_graph_rollout"
        with self._transaction(operation=operation) as connection:
            record = self._load_rollout(connection, operation=operation, required=True)
            assert record is not None
            return record

    def read_if_exists(self) -> GraphRolloutRecord | None:
        """Read an active Board rollout without creating any directory."""

        operation = "read_graph_rollout_if_exists"
        if not self._rollout_storage_exists(operation=operation):
            return None
        with self._transaction(operation=operation) as connection:
            record = self._load_rollout(connection, operation=operation, required=True)
            assert record is not None
            return record

    def verify(self) -> GraphRolloutRecord:
        """Run the expensive SQLite/schema integrity audit explicitly."""

        operation = "verify_graph_rollout_journal"
        with self._transaction(
            operation=operation, verify_integrity=True
        ) as connection:
            record = self._load_rollout(connection, operation=operation, required=True)
            assert record is not None
            return record

    def certify_candidate(
        self,
        *,
        expected_version: int,
        candidate_binding_sha256: str,
    ) -> GraphRolloutRecord:
        """Seal the prospective Grafx binding after cold certification."""

        operation = "certify_graph_rollout_candidate"
        try:
            version = _require_positive_int(
                expected_version, field_name="expected_version"
            )
            binding_sha256 = _require_sha256(
                candidate_binding_sha256, field_name="candidate_binding_sha256"
            )
        except (TypeError, ValueError) as exc:
            raise _capability(
                "candidate_certification_invalid", operation=operation
            ) from exc
        with self._transaction(operation=operation) as connection:
            current = self._load_rollout(connection, operation=operation, required=True)
            assert current is not None
            if current.state != "shadowing":
                raise _conflict(
                    "candidate_certification_state_invalid",
                    operation=operation,
                    board_id=self._board_id,
                    observed_state=current.state,
                )
            if current.candidate.binding_sha256 is not None:
                if (
                    current.candidate.binding_sha256 == binding_sha256
                    and current.state_version in {version, version + 1}
                ):
                    return current
                raise _conflict(
                    "candidate_already_certified",
                    operation=operation,
                    board_id=self._board_id,
                )
            if current.state_version != version:
                raise _conflict(
                    "stale_candidate_certification_cas",
                    operation=operation,
                    board_id=self._board_id,
                    expected_version=version,
                    observed_version=current.state_version,
                )
            candidate = RolloutEndpointIdentity(
                backend="grafx",
                binding_sha256=binding_sha256,
                generation=current.candidate.generation,
                physical_path=current.candidate.physical_path,
                page_size=current.candidate.page_size,
            )
            return self._write_candidate(
                connection,
                current=current,
                candidate=candidate,
                operation=operation,
            )

    def replace_candidate(
        self,
        *,
        expected_version: int,
        expected_candidate: RolloutEndpointIdentity,
        replacement: RolloutEndpointIdentity,
    ) -> GraphRolloutRecord:
        """CAS one certified shadow generation for a fresh certified one."""

        operation = "replace_graph_rollout_candidate"
        try:
            version = _require_positive_int(
                expected_version, field_name="expected_version"
            )
            expected_body = self._normalize_endpoint(
                expected_candidate, expected_backend="grafx"
            )
            replacement_body = self._normalize_endpoint(
                replacement, expected_backend="grafx"
            )
            normalized_expected = self._endpoint_from_body(expected_body)
            normalized_replacement = self._endpoint_from_body(replacement_body)
            if normalized_replacement.binding_sha256 is None:
                raise ValueError("replacement_not_certified")
            if (
                normalized_replacement.generation == normalized_expected.generation
                or normalized_replacement.physical_path
                == normalized_expected.physical_path
            ):
                raise ValueError("replacement_not_fresh")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise _capability(
                "candidate_replacement_invalid", operation=operation
            ) from exc
        with self._transaction(operation=operation) as connection:
            current = self._load_rollout(connection, operation=operation, required=True)
            assert current is not None
            if current.state != "shadowing":
                raise _conflict(
                    "candidate_replacement_state_invalid",
                    operation=operation,
                    board_id=self._board_id,
                    observed_state=current.state,
                )
            if (
                current.state_version != version
                or current.candidate != normalized_expected
            ):
                raise _conflict(
                    "stale_candidate_replacement_cas",
                    operation=operation,
                    board_id=self._board_id,
                    expected_version=version,
                    observed_version=current.state_version,
                )
            return self._write_candidate(
                connection,
                current=current,
                candidate=normalized_replacement,
                operation=operation,
            )

    def compare_and_set_state(
        self,
        *,
        expected_state: RolloutState,
        expected_version: int,
        new_state: RolloutState,
    ) -> GraphRolloutRecord:
        """Advance lifecycle state iff both state and state version still match."""

        operation = "compare_and_set_graph_rollout_state"
        try:
            expected = self._validate_state(expected_state)
            replacement = self._validate_state(new_state)
            version = _require_positive_int(
                expected_version, field_name="expected_version"
            )
        except (TypeError, ValueError) as exc:
            raise _capability("state_cas_invalid", operation=operation) from exc

        with self._transaction(operation=operation) as connection:
            current = self._load_rollout(connection, operation=operation, required=True)
            assert current is not None
            if current.state != expected or current.state_version != version:
                raise _conflict(
                    "stale_state_cas",
                    operation=operation,
                    board_id=self._board_id,
                    expected_state=expected,
                    observed_state=current.state,
                    expected_version=version,
                    observed_version=current.state_version,
                )
            if current.state in {"completed", "erased"}:
                raise _conflict(
                    "terminal_state",
                    operation=operation,
                    board_id=self._board_id,
                    observed_state=current.state,
                )
            if replacement not in _LEGAL_STATE_TRANSITIONS[current.state]:
                raise _conflict(
                    "illegal_state_transition",
                    operation=operation,
                    board_id=self._board_id,
                    observed_state=current.state,
                    requested_state=replacement,
                )
            if (
                replacement
                in {
                    "canary_ready",
                    "grafx_active_rollback_open",
                    "grafx_active_rollback_closed",
                }
                and current.candidate.binding_sha256 is None
            ):
                raise _conflict(
                    "candidate_binding_not_certified",
                    operation=operation,
                    board_id=self._board_id,
                )
            if (current.state == "shadowing" and replacement == "canary_ready") or (
                current.state == "canary_ready"
                and replacement == "grafx_active_rollback_open"
            ):
                self._require_canary_gate_locked(
                    connection, rollout=current, operation=operation
                )
            return self._write_state(
                connection,
                current=current,
                state=replacement,
                operation=operation,
            )

    def require_current_canary_gate(
        self, *, expected_version: int
    ) -> GraphRolloutRecord:
        """Revalidate the complete canary proof for the current state version."""

        operation = "require_current_graph_rollout_canary_gate"
        try:
            version = _require_positive_int(
                expected_version, field_name="expected_version"
            )
        except (TypeError, ValueError) as exc:
            raise _capability(
                "canary_gate_request_invalid", operation=operation
            ) from exc

        with self._transaction(operation=operation) as connection:
            current = self._load_rollout(connection, operation=operation, required=True)
            assert current is not None
            if current.state != "canary_ready" or current.state_version != version:
                raise _conflict(
                    "stale_canary_gate_cas",
                    operation=operation,
                    board_id=self._board_id,
                    expected_state="canary_ready",
                    observed_state=current.state,
                    expected_version=version,
                    observed_version=current.state_version,
                )
            self._require_canary_gate_locked(
                connection, rollout=current, operation=operation
            )
            return current

    def prepare_if_active(
        self,
        *,
        family: str,
        payload: Mapping[str, object],
        expected_binding_sha256: str,
        backend: GraphBackend,
    ) -> LogicalMutationRecord | None:
        """Prepare when rollout state exists; absence is a side-effect-free no-op."""

        if not self._rollout_storage_exists(
            operation="prepare_graph_rollout_mutation_if_active"
        ):
            return None
        return self._prepare_mutation(
            family=family,
            payload=payload,
            expected_binding_sha256=expected_binding_sha256,
            backend=backend,
            completed_is_noop=True,
        )

    def close_rollback_before_write_if_active(
        self,
        *,
        expected_binding_sha256: str,
        backend: GraphBackend,
    ) -> GraphRolloutRecord | None:
        """Fence administrative writes that bypass logical mutation capture."""

        operation = "close_graph_rollout_rollback_before_write"
        try:
            binding_sha256 = _require_sha256(
                expected_binding_sha256, field_name="expected_binding_sha256"
            )
            if backend not in {"ladybug", "grafx"}:
                raise ValueError("backend_invalid")
        except (TypeError, ValueError) as exc:
            raise _capability("write_fence_invalid", operation=operation) from exc
        if not self._rollout_storage_exists(operation=operation):
            return None
        with self._transaction(operation=operation) as connection:
            rollout = self._load_rollout(connection, operation=operation, required=True)
            assert rollout is not None
            if rollout.state == "erased":
                raise _conflict(
                    "rollout_not_writable",
                    operation=operation,
                    board_id=self._board_id,
                    observed_state=rollout.state,
                )
            if rollout.state == "completed":
                self._require_completed_route(
                    rollout,
                    expected_binding_sha256=binding_sha256,
                    backend=backend,
                    operation=operation,
                )
                return rollout
            if rollout.state == "canary_ready":
                raise _conflict(
                    "canary_recovery_required",
                    operation=operation,
                    board_id=self._board_id,
                    observed_state=rollout.state,
                )
            required_backend: GraphBackend = (
                "grafx"
                if rollout.state
                in {
                    "grafx_active_rollback_open",
                    "grafx_active_rollback_closed",
                }
                else "ladybug"
            )
            if backend != required_backend:
                raise _conflict(
                    "backend_fence_mismatch",
                    operation=operation,
                    board_id=self._board_id,
                    required_backend=required_backend,
                    supplied_backend=backend,
                )
            required_binding = self._active_binding_sha256(rollout, operation=operation)
            if binding_sha256 != required_binding:
                raise _conflict(
                    "binding_fence_mismatch",
                    operation=operation,
                    board_id=self._board_id,
                    expected_binding_sha256=binding_sha256,
                    required_binding_sha256=required_binding,
                )
            if rollout.state == "grafx_active_rollback_open":
                return self._write_state(
                    connection,
                    current=rollout,
                    state="grafx_active_rollback_closed",
                    operation=operation,
                )
            return rollout

    def prepare_mutation(
        self,
        *,
        family: str,
        payload: Mapping[str, object],
        expected_binding_sha256: str,
        backend: GraphBackend | None = None,
    ) -> LogicalMutationRecord:
        """Durably allocate the next logical sequence before the source write."""

        mutation = self._prepare_mutation(
            family=family,
            payload=payload,
            expected_binding_sha256=expected_binding_sha256,
            backend=backend,
            completed_is_noop=False,
        )
        assert mutation is not None
        return mutation

    def _prepare_mutation(
        self,
        *,
        family: str,
        payload: Mapping[str, object],
        expected_binding_sha256: str,
        backend: GraphBackend | None,
        completed_is_noop: bool,
    ) -> LogicalMutationRecord | None:
        """Allocate under one lock, or release capture for a completed rollout."""

        operation = "prepare_graph_rollout_mutation"
        try:
            if type(family) is not str or _FAMILY_RE.fullmatch(family) is None:
                raise ValueError("family_invalid")
            if type(payload) is not dict:
                raise ValueError("payload_not_object")
            payload_json = _canonical_json_text(payload)
            payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            binding_sha256 = _require_sha256(
                expected_binding_sha256, field_name="expected_binding_sha256"
            )
            if backend is not None and backend not in {"ladybug", "grafx"}:
                raise ValueError("backend_invalid")
        except (TypeError, ValueError) as exc:
            raise _capability("logical_mutation_invalid", operation=operation) from exc

        with self._transaction(operation=operation) as connection:
            rollout = self._load_rollout(connection, operation=operation, required=True)
            assert rollout is not None
            if rollout.state == "erased":
                raise _conflict(
                    "rollout_not_writable",
                    operation=operation,
                    board_id=self._board_id,
                    observed_state=rollout.state,
                )
            if rollout.state == "completed" and completed_is_noop:
                assert backend is not None
                self._require_completed_route(
                    rollout,
                    expected_binding_sha256=binding_sha256,
                    backend=backend,
                    operation=operation,
                )
                return None
            if rollout.state == "completed":
                raise _conflict(
                    "rollout_not_writable",
                    operation=operation,
                    board_id=self._board_id,
                    observed_state=rollout.state,
                )
            if rollout.state == "canary_ready":
                raise _conflict(
                    "canary_recovery_required",
                    operation=operation,
                    board_id=self._board_id,
                    observed_state=rollout.state,
                )
            required_backend: GraphBackend = (
                "grafx"
                if rollout.state
                in {
                    "grafx_active_rollback_open",
                    "grafx_active_rollback_closed",
                }
                else "ladybug"
            )
            if backend is not None and backend != required_backend:
                raise _conflict(
                    "backend_fence_mismatch",
                    operation=operation,
                    board_id=self._board_id,
                    observed_state=rollout.state,
                    required_backend=required_backend,
                    supplied_backend=backend,
                )
            required_binding = self._active_binding_sha256(rollout, operation=operation)
            if binding_sha256 != required_binding:
                raise _conflict(
                    "binding_fence_mismatch",
                    operation=operation,
                    board_id=self._board_id,
                    expected_binding_sha256=binding_sha256,
                    required_binding_sha256=required_binding,
                )

            if rollout.state == "grafx_active_rollback_open":
                # This transition and the prepared record share one SQLite
                # BEGIN IMMEDIATE transaction. The graph call happens only
                # after this method returns, so a failed close never writes.
                rollout = self._write_state(
                    connection,
                    current=rollout,
                    state="grafx_active_rollback_closed",
                    operation=operation,
                )

            seq = rollout.next_seq
            now = _utc_now()
            body: dict[str, object] = {
                "seq": seq,
                "family": family,
                "payload_json": payload_json,
                "payload_sha256": payload_sha256,
                "expected_binding_sha256": binding_sha256,
                "status": "prepared",
                "prepared_at_utc": now,
                "terminal_at_utc": None,
            }
            connection.execute(
                """
                INSERT INTO logical_mutations (
                    seq, family, payload_json, payload_sha256,
                    expected_binding_sha256, status, prepared_at_utc,
                    terminal_at_utc, row_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*body.values(), _canonical_sha256(body)),
            )
            self._write_next_seq(
                connection,
                rollout=rollout,
                next_seq=seq + 1,
                operation=operation,
            )
            return self._load_mutation(connection, seq=seq, operation=operation)

    def mark_source_committed(
        self, *, seq: int, payload_sha256: str
    ) -> LogicalMutationRecord:
        return self._terminalize_mutation(
            seq=seq,
            payload_sha256=payload_sha256,
            status="source_committed",
        )

    def mark_source_abandoned(
        self, *, seq: int, payload_sha256: str
    ) -> LogicalMutationRecord:
        return self._terminalize_mutation(
            seq=seq,
            payload_sha256=payload_sha256,
            status="source_abandoned",
        )

    def list_committed(
        self,
        *,
        cursor: int = 0,
        high_water: int | None = None,
        limit: int = 100,
    ) -> CommittedMutationPage:
        """Return a bounded stable page from the contiguous terminal prefix."""

        operation = "list_graph_rollout_committed_mutations"
        try:
            safe_cursor = _require_non_negative_int(cursor, field_name="cursor")
            safe_limit = _require_positive_int(limit, field_name="limit")
            if safe_limit > MAX_MUTATION_PAGE_SIZE:
                raise ValueError("limit_too_large")
            supplied_high_water = (
                None
                if high_water is None
                else _require_non_negative_int(high_water, field_name="high_water")
            )
        except (TypeError, ValueError) as exc:
            raise _capability("committed_page_invalid", operation=operation) from exc

        with self._transaction(operation=operation) as connection:
            self._load_rollout(connection, operation=operation, required=True)
            terminal_high_water = self._terminal_high_water(connection)
            stable_high_water = (
                terminal_high_water
                if supplied_high_water is None
                else supplied_high_water
            )
            if stable_high_water > terminal_high_water:
                raise _conflict(
                    "high_water_not_terminal",
                    operation=operation,
                    board_id=self._board_id,
                    requested_high_water=stable_high_water,
                    terminal_high_water=terminal_high_water,
                )
            if safe_cursor > stable_high_water:
                raise _capability("cursor_after_high_water", operation=operation)

            rows = connection.execute(
                """
                SELECT * FROM logical_mutations
                WHERE status = 'source_committed' AND seq > ? AND seq <= ?
                ORDER BY seq
                LIMIT ?
                """,
                (safe_cursor, stable_high_water, safe_limit + 1),
            ).fetchall()
            has_more = len(rows) > safe_limit
            selected = rows[:safe_limit]
            items = tuple(
                self._mutation_from_row(row, operation=operation) for row in selected
            )
            if has_more:
                next_cursor = items[-1].seq
            else:
                # Advancing over abandoned terminal entries is safe. Prepared
                # entries can never be at or below this captured high-water.
                next_cursor = stable_high_water
            return CommittedMutationPage(
                items=items,
                cursor=safe_cursor,
                next_cursor=next_cursor,
                high_water=stable_high_water,
                has_more=has_more,
            )

    def capture_high_water(self) -> int:
        """Capture the last allocated sequence, including ambiguous prepared rows."""

        operation = "capture_graph_rollout_high_water"
        with self._transaction(operation=operation) as connection:
            rollout = self._load_rollout(connection, operation=operation, required=True)
            assert rollout is not None
            return rollout.next_seq - 1

    def reconcile_snapshot(
        self,
        *,
        direction: CheckpointDirection,
        through_seq: int,
        expected_binding_sha256: str,
        source_fingerprint: str,
        target_fingerprint: str,
        generation: str,
    ) -> ReplayCheckpoint:
        """Resolve prepared ambiguity through a fenced full-state snapshot.

        A successful full-state comparison subsumes every prepared operation
        through the captured high-water without claiming whether that one
        operation committed. Those rows become ``source_reconciled`` and are
        intentionally excluded from event replay.
        """

        operation = "reconcile_graph_rollout_snapshot"
        try:
            safe_direction = self._validate_direction(direction)
            safe_through_seq = _require_non_negative_int(
                through_seq, field_name="through_seq"
            )
            binding_sha256 = _require_sha256(
                expected_binding_sha256, field_name="expected_binding_sha256"
            )
            safe_source_fingerprint = _require_sha256(
                source_fingerprint, field_name="source_fingerprint"
            )
            safe_target_fingerprint = _require_sha256(
                target_fingerprint, field_name="target_fingerprint"
            )
            safe_generation = _portable_segment(generation, field_name="generation")
            if safe_source_fingerprint != safe_target_fingerprint:
                raise ValueError("reconciliation_fingerprint_mismatch")
        except (TypeError, ValueError) as exc:
            raise _capability(
                "snapshot_reconciliation_invalid", operation=operation
            ) from exc

        with self._transaction(operation=operation) as connection:
            rollout = self._load_rollout(connection, operation=operation, required=True)
            assert rollout is not None
            required_binding = self._active_binding_sha256(rollout, operation=operation)
            if binding_sha256 != required_binding:
                raise _conflict(
                    "binding_fence_mismatch",
                    operation=operation,
                    board_id=self._board_id,
                    expected_binding_sha256=binding_sha256,
                    required_binding_sha256=required_binding,
                )
            allocated_high_water = rollout.next_seq - 1
            if safe_through_seq > allocated_high_water:
                raise _conflict(
                    "reconciliation_beyond_allocated_high_water",
                    operation=operation,
                    board_id=self._board_id,
                    through_seq=safe_through_seq,
                    allocated_high_water=allocated_high_water,
                )
            rows = connection.execute(
                """
                SELECT * FROM logical_mutations
                WHERE status = 'prepared' AND seq <= ? ORDER BY seq
                """,
                (safe_through_seq,),
            ).fetchall()
            for row in rows:
                current = self._mutation_from_row(row, operation=operation)
                body: dict[str, object] = {
                    "seq": current.seq,
                    "family": current.family,
                    "payload_json": current.payload_json,
                    "payload_sha256": current.payload_sha256,
                    "expected_binding_sha256": current.expected_binding_sha256,
                    "status": "source_reconciled",
                    "prepared_at_utc": current.prepared_at_utc,
                    "terminal_at_utc": _utc_now(),
                }
                result = connection.execute(
                    """
                    UPDATE logical_mutations
                    SET status = 'source_reconciled', terminal_at_utc = ?,
                        row_sha256 = ?
                    WHERE seq = ? AND status = 'prepared'
                    """,
                    (
                        body["terminal_at_utc"],
                        _canonical_sha256(body),
                        current.seq,
                    ),
                )
                if result.rowcount != 1:
                    raise _conflict(
                        "stale_reconciliation_mutation",
                        operation=operation,
                        board_id=self._board_id,
                        seq=current.seq,
                    )
            return self._record_checkpoint_locked(
                connection,
                rollout=rollout,
                direction=safe_direction,
                through_seq=safe_through_seq,
                source_fingerprint=safe_source_fingerprint,
                target_fingerprint=safe_target_fingerprint,
                generation=safe_generation,
                require_terminal=True,
                operation=operation,
            )

    def record_checkpoint(
        self,
        *,
        direction: CheckpointDirection,
        through_seq: int,
        source_fingerprint: str,
        target_fingerprint: str,
        generation: str,
    ) -> ReplayCheckpoint:
        """Persist a monotonic replay ACK and both fingerprints atomically."""

        operation = "record_graph_rollout_checkpoint"
        try:
            safe_direction = self._validate_direction(direction)
            safe_through_seq = _require_non_negative_int(
                through_seq, field_name="through_seq"
            )
            safe_source_fingerprint = _require_sha256(
                source_fingerprint, field_name="source_fingerprint"
            )
            safe_target_fingerprint = _require_sha256(
                target_fingerprint, field_name="target_fingerprint"
            )
            safe_generation = _portable_segment(generation, field_name="generation")
        except (TypeError, ValueError) as exc:
            raise _capability("checkpoint_invalid", operation=operation) from exc

        with self._transaction(operation=operation) as connection:
            rollout = self._load_rollout(connection, operation=operation, required=True)
            assert rollout is not None
            return self._record_checkpoint_locked(
                connection,
                rollout=rollout,
                direction=safe_direction,
                through_seq=safe_through_seq,
                source_fingerprint=safe_source_fingerprint,
                target_fingerprint=safe_target_fingerprint,
                generation=safe_generation,
                require_terminal=True,
                operation=operation,
            )

    def read_checkpoint(
        self, direction: CheckpointDirection
    ) -> ReplayCheckpoint | None:
        operation = "read_graph_rollout_checkpoint"
        try:
            safe_direction = self._validate_direction(direction)
        except (TypeError, ValueError) as exc:
            raise _capability(
                "checkpoint_direction_invalid", operation=operation
            ) from exc
        with self._transaction(operation=operation) as connection:
            self._load_rollout(connection, operation=operation, required=True)
            row = connection.execute(
                "SELECT * FROM replay_checkpoints WHERE direction = ?",
                (safe_direction,),
            ).fetchone()
            return (
                None
                if row is None
                else self._checkpoint_from_row(row, operation=operation)
            )

    def record_divergence(
        self,
        *,
        direction: CheckpointDirection,
        through_seq: int,
        expected_fingerprint: str,
        actual_fingerprint: str,
        generation: str,
        details: Mapping[str, object],
    ) -> RolloutDivergence:
        """Append authenticated evidence that source and target diverged."""

        operation = "record_graph_rollout_divergence"
        try:
            safe_direction = self._validate_direction(direction)
            safe_through_seq = _require_non_negative_int(
                through_seq, field_name="through_seq"
            )
            safe_expected = _require_sha256(
                expected_fingerprint, field_name="expected_fingerprint"
            )
            safe_actual = _require_sha256(
                actual_fingerprint, field_name="actual_fingerprint"
            )
            safe_generation = _portable_segment(generation, field_name="generation")
            if type(details) is not dict:
                raise ValueError("details_not_object")
            details_json = _canonical_json_text(details)
            details_sha256 = hashlib.sha256(details_json.encode("utf-8")).hexdigest()
        except (TypeError, ValueError) as exc:
            raise _capability("divergence_invalid", operation=operation) from exc

        with self._transaction(operation=operation) as connection:
            rollout = self._load_rollout(connection, operation=operation, required=True)
            assert rollout is not None
            expected_generation = (
                rollout.candidate.generation
                if safe_direction == "shadow"
                else rollout.source.generation
            )
            if safe_generation != expected_generation:
                raise _conflict(
                    "divergence_generation_mismatch",
                    operation=operation,
                    board_id=self._board_id,
                )
            allocated_high_water = rollout.next_seq - 1
            if safe_through_seq > allocated_high_water:
                raise _conflict(
                    "divergence_beyond_allocated_high_water",
                    operation=operation,
                    board_id=self._board_id,
                    through_seq=safe_through_seq,
                    allocated_high_water=allocated_high_water,
                )
            next_id = int(
                connection.execute(
                    "SELECT COALESCE(MAX(divergence_id), 0) + 1 FROM rollout_divergences"
                ).fetchone()[0]
            )
            now = _utc_now()
            body: dict[str, object] = {
                "divergence_id": next_id,
                "direction": safe_direction,
                "through_seq": safe_through_seq,
                "expected_fingerprint": safe_expected,
                "actual_fingerprint": safe_actual,
                "generation": safe_generation,
                "details_json": details_json,
                "details_sha256": details_sha256,
                "detected_at_utc": now,
            }
            connection.execute(
                """
                INSERT INTO rollout_divergences (
                    divergence_id, direction, through_seq, expected_fingerprint,
                    actual_fingerprint, generation, details_json, details_sha256,
                    detected_at_utc, row_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*body.values(), _canonical_sha256(body)),
            )
            row = connection.execute(
                "SELECT * FROM rollout_divergences WHERE divergence_id = ?", (next_id,)
            ).fetchone()
            assert row is not None
            return self._divergence_from_row(row, operation=operation)

    def list_divergences(
        self, *, cursor: int = 0, limit: int = 100
    ) -> tuple[RolloutDivergence, ...]:
        operation = "list_graph_rollout_divergences"
        try:
            safe_cursor = _require_non_negative_int(cursor, field_name="cursor")
            safe_limit = _require_positive_int(limit, field_name="limit")
            if safe_limit > MAX_DIVERGENCE_PAGE_SIZE:
                raise ValueError("limit_too_large")
        except (TypeError, ValueError) as exc:
            raise _capability("divergence_page_invalid", operation=operation) from exc
        with self._transaction(operation=operation) as connection:
            self._load_rollout(connection, operation=operation, required=True)
            rows = connection.execute(
                """
                SELECT * FROM rollout_divergences
                WHERE divergence_id > ? ORDER BY divergence_id LIMIT ?
                """,
                (safe_cursor, safe_limit),
            ).fetchall()
            return tuple(
                self._divergence_from_row(row, operation=operation) for row in rows
            )

    def record_comparison_receipt(
        self,
        *,
        direction: CheckpointDirection,
        through_seq: int,
        generation: str,
        corpus_sha256: str,
        source_result_sha256: str,
        target_result_sha256: str,
        query_count: int,
    ) -> ComparisonReceipt:
        """Persist proof that one bounded result corpus matched exactly."""

        operation = "record_graph_rollout_comparison_receipt"
        try:
            safe_direction = self._validate_direction(direction)
            safe_through_seq = _require_non_negative_int(
                through_seq, field_name="through_seq"
            )
            safe_generation = _portable_segment(generation, field_name="generation")
            safe_corpus = _require_sha256(corpus_sha256, field_name="corpus_sha256")
            safe_source = _require_sha256(
                source_result_sha256, field_name="source_result_sha256"
            )
            safe_target = _require_sha256(
                target_result_sha256, field_name="target_result_sha256"
            )
            safe_query_count = _require_positive_int(
                query_count, field_name="query_count"
            )
            if safe_source != safe_target:
                raise ValueError("comparison_results_diverged")
        except (TypeError, ValueError) as exc:
            raise _capability(
                "comparison_receipt_invalid", operation=operation
            ) from exc

        with self._transaction(operation=operation) as connection:
            rollout = self._load_rollout(connection, operation=operation, required=True)
            assert rollout is not None
            target_identity = (
                rollout.candidate if safe_direction == "shadow" else rollout.source
            )
            if safe_generation != target_identity.generation:
                raise _conflict(
                    "comparison_generation_mismatch",
                    operation=operation,
                    board_id=self._board_id,
                    expected_generation=target_identity.generation,
                    observed_generation=safe_generation,
                )
            if target_identity.binding_sha256 is None:
                raise _conflict(
                    "comparison_target_not_certified",
                    operation=operation,
                    board_id=self._board_id,
                )
            terminal_high_water = self._terminal_high_water(connection)
            if safe_through_seq > terminal_high_water:
                raise _conflict(
                    "comparison_beyond_terminal_high_water",
                    operation=operation,
                    board_id=self._board_id,
                    through_seq=safe_through_seq,
                    terminal_high_water=terminal_high_water,
                )
            persisted = connection.execute(
                """
                SELECT * FROM comparison_receipts
                WHERE direction = ? AND through_seq = ? AND generation = ?
                  AND binding_sha256 = ? AND corpus_sha256 = ?
                  AND source_result_sha256 = ?
                """,
                (
                    safe_direction,
                    safe_through_seq,
                    safe_generation,
                    target_identity.binding_sha256,
                    safe_corpus,
                    safe_source,
                ),
            ).fetchone()
            if persisted is not None:
                current = self._comparison_from_row(persisted, operation=operation)
                if current.query_count != safe_query_count:
                    raise _conflict(
                        "comparison_same_identity_mismatch",
                        operation=operation,
                        board_id=self._board_id,
                    )
                return current

            receipt_id = int(
                connection.execute(
                    "SELECT COALESCE(MAX(receipt_id), 0) + 1 FROM comparison_receipts"
                ).fetchone()[0]
            )
            body: dict[str, object] = {
                "receipt_id": receipt_id,
                "direction": safe_direction,
                "through_seq": safe_through_seq,
                "generation": safe_generation,
                "binding_sha256": target_identity.binding_sha256,
                "physical_path": self._relative_path(target_identity.physical_path),
                "page_size": target_identity.page_size,
                "corpus_sha256": safe_corpus,
                "source_result_sha256": safe_source,
                "target_result_sha256": safe_target,
                "query_count": safe_query_count,
                "completed_at_utc": _utc_now(),
            }
            connection.execute(
                """
                INSERT INTO comparison_receipts (
                    receipt_id, direction, through_seq, generation,
                    binding_sha256, physical_path, page_size, corpus_sha256,
                    source_result_sha256, target_result_sha256, query_count,
                    completed_at_utc, row_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*body.values(), _canonical_sha256(body)),
            )
            row = connection.execute(
                "SELECT * FROM comparison_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
            assert row is not None
            return self._comparison_from_row(row, operation=operation)

    def list_comparison_receipts(
        self,
        *,
        cursor: int = 0,
        limit: int = 100,
        direction: CheckpointDirection | None = None,
    ) -> tuple[ComparisonReceipt, ...]:
        operation = "list_graph_rollout_comparison_receipts"
        try:
            safe_cursor = _require_non_negative_int(cursor, field_name="cursor")
            safe_limit = _require_positive_int(limit, field_name="limit")
            if safe_limit > MAX_COMPARISON_PAGE_SIZE:
                raise ValueError("limit_too_large")
            safe_direction = (
                None if direction is None else self._validate_direction(direction)
            )
        except (TypeError, ValueError) as exc:
            raise _capability("comparison_page_invalid", operation=operation) from exc
        with self._transaction(operation=operation) as connection:
            self._load_rollout(connection, operation=operation, required=True)
            if safe_direction is None:
                rows = connection.execute(
                    """
                    SELECT * FROM comparison_receipts
                    WHERE receipt_id > ? ORDER BY receipt_id LIMIT ?
                    """,
                    (safe_cursor, safe_limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM comparison_receipts
                    WHERE direction = ? AND receipt_id > ?
                    ORDER BY receipt_id LIMIT ?
                    """,
                    (safe_direction, safe_cursor, safe_limit),
                ).fetchall()
            return tuple(
                self._comparison_from_row(row, operation=operation) for row in rows
            )

    def latest_comparison_receipt(
        self, direction: CheckpointDirection | None = None
    ) -> ComparisonReceipt | None:
        operation = "read_latest_graph_rollout_comparison_receipt"
        try:
            safe_direction = (
                None if direction is None else self._validate_direction(direction)
            )
        except (TypeError, ValueError) as exc:
            raise _capability(
                "comparison_direction_invalid", operation=operation
            ) from exc
        with self._transaction(operation=operation) as connection:
            self._load_rollout(connection, operation=operation, required=True)
            if safe_direction is None:
                row = connection.execute(
                    "SELECT * FROM comparison_receipts ORDER BY receipt_id DESC LIMIT 1"
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT * FROM comparison_receipts
                    WHERE direction = ? ORDER BY receipt_id DESC LIMIT 1
                    """,
                    (safe_direction,),
                ).fetchone()
            return (
                None
                if row is None
                else self._comparison_from_row(row, operation=operation)
            )

    def close_for_privacy(self, *, expected_version: int) -> GraphRolloutRecord:
        """Durably invalidate the journal before irreversible byte erasure."""

        operation = "close_graph_rollout_for_privacy"
        try:
            version = _require_positive_int(
                expected_version, field_name="expected_version"
            )
        except (TypeError, ValueError) as exc:
            raise _capability("privacy_version_invalid", operation=operation) from exc
        with self._transaction(operation=operation) as connection:
            current = self._load_rollout(connection, operation=operation, required=True)
            assert current is not None
            if current.state == "erased":
                if current.state_version in {version, version + 1}:
                    return current
                raise _conflict(
                    "stale_privacy_cas",
                    operation=operation,
                    board_id=self._board_id,
                    expected_version=version,
                    observed_version=current.state_version,
                )
            if current.state_version != version:
                raise _conflict(
                    "stale_privacy_cas",
                    operation=operation,
                    board_id=self._board_id,
                    expected_version=version,
                    observed_version=current.state_version,
                )
            return self._write_state(
                connection,
                current=current,
                state="erased",
                operation=operation,
            )

    def erase_privacy_storage(
        self,
        *,
        invalidated_state_version: int | None = None,
        before_mutation: Callable[[], None] | None = None,
    ) -> PrivacyEraseProof:
        """Erase an already-invalidated rollout, supporting cleanup retry."""

        operation = "erase_graph_rollout_privacy_storage"
        callback = before_mutation or (lambda: None)
        if invalidated_state_version is not None:
            try:
                invalidated_state_version = _require_positive_int(
                    invalidated_state_version,
                    field_name="invalidated_state_version",
                )
            except (TypeError, ValueError) as exc:
                raise _capability(
                    "privacy_state_version_invalid", operation=operation
                ) from exc

        if self._database_path.exists():
            with self._transaction(operation=operation) as connection:
                current = self._load_rollout(
                    connection, operation=operation, required=True
                )
                assert current is not None
                if current.state != "erased":
                    raise _conflict(
                        "privacy_storage_not_invalidated",
                        operation=operation,
                        board_id=self._board_id,
                        observed_state=current.state,
                    )
                if (
                    invalidated_state_version is not None
                    and current.state_version != invalidated_state_version
                ):
                    raise _conflict(
                        "privacy_state_version_mismatch",
                        operation=operation,
                        board_id=self._board_id,
                    )
                invalidated_state_version = current.state_version

        checked_paths = self._privacy_checked_paths()
        files_removed = 0
        directories_removed = 0
        try:
            self._validate_layout(allow_missing_rollout=True)
            files_removed, directories_removed = remove_contained_tree(
                self._rollout_root,
                base_dir=self._board_root,
                before_mutation=callback,
            )
            if self._board_root.exists():
                fsync_directory(self._board_root)
            self._validate_layout(allow_missing_rollout=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise _unavailable(
                "privacy_erase_failed",
                operation=operation,
                board_id=self._board_id,
                error_type=type(exc).__name__,
            ) from exc

        absent = not self._rollout_root.exists() and all(
            not path.exists() for path in checked_paths
        )
        if not absent:
            raise _corruption(
                "privacy_absence_proof_failed",
                operation=operation,
                board_id=self._board_id,
            )
        return PrivacyEraseProof(
            board_id=self._board_id,
            invalidated_state_version=invalidated_state_version,
            files_removed=files_removed,
            directories_removed=directories_removed,
            checked_paths=checked_paths,
            storage_absent=True,
        )

    def privacy_storage_present(self) -> bool:
        operation = "inspect_graph_rollout_privacy_storage"
        try:
            self._validate_layout(allow_missing_rollout=True)
            return self._rollout_root.exists() or any(
                path.exists() for path in self._privacy_checked_paths()
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise _corruption(
                "privacy_storage_identity_invalid",
                operation=operation,
                board_id=self._board_id,
            ) from exc

    def _terminalize_mutation(
        self,
        *,
        seq: int,
        payload_sha256: str,
        status: Literal["source_committed", "source_abandoned"],
    ) -> LogicalMutationRecord:
        operation = "terminalize_graph_rollout_mutation"
        try:
            safe_seq = _require_positive_int(seq, field_name="seq")
            safe_payload_sha256 = _require_sha256(
                payload_sha256, field_name="payload_sha256"
            )
        except (TypeError, ValueError) as exc:
            raise _capability("mutation_terminal_invalid", operation=operation) from exc
        with self._transaction(operation=operation) as connection:
            self._load_rollout(connection, operation=operation, required=True)
            current = self._load_mutation(connection, seq=safe_seq, operation=operation)
            if current.payload_sha256 != safe_payload_sha256:
                raise _conflict(
                    "mutation_payload_fence_mismatch",
                    operation=operation,
                    board_id=self._board_id,
                    seq=safe_seq,
                )
            if current.status == status:
                return current
            if current.status != "prepared":
                raise _conflict(
                    "mutation_terminal_outcome_conflict",
                    operation=operation,
                    board_id=self._board_id,
                    seq=safe_seq,
                    observed_status=current.status,
                    requested_status=status,
                )
            body: dict[str, object] = {
                "seq": current.seq,
                "family": current.family,
                "payload_json": current.payload_json,
                "payload_sha256": current.payload_sha256,
                "expected_binding_sha256": current.expected_binding_sha256,
                "status": status,
                "prepared_at_utc": current.prepared_at_utc,
                "terminal_at_utc": _utc_now(),
            }
            connection.execute(
                """
                UPDATE logical_mutations
                SET status = ?, terminal_at_utc = ?, row_sha256 = ?
                WHERE seq = ? AND status = 'prepared'
                """,
                (
                    status,
                    body["terminal_at_utc"],
                    _canonical_sha256(body),
                    safe_seq,
                ),
            )
            return self._load_mutation(connection, seq=safe_seq, operation=operation)

    def _require_canary_gate_locked(
        self,
        connection: sqlite3.Connection,
        *,
        rollout: GraphRolloutRecord,
        operation: str,
    ) -> None:
        candidate = rollout.candidate
        binding_sha256 = candidate.binding_sha256
        if binding_sha256 is None:
            raise _conflict(
                "candidate_binding_not_certified",
                operation=operation,
                board_id=self._board_id,
            )
        allocated_high_water = rollout.next_seq - 1
        checkpoint_row = connection.execute(
            "SELECT * FROM replay_checkpoints WHERE direction = 'shadow'"
        ).fetchone()
        if checkpoint_row is None:
            raise _conflict(
                "canary_checkpoint_missing",
                operation=operation,
                board_id=self._board_id,
            )
        checkpoint = self._checkpoint_from_row(checkpoint_row, operation=operation)
        checkpoint_current = (
            checkpoint.through_seq == allocated_high_water
            and checkpoint.generation == candidate.generation
            and checkpoint.binding_sha256 == binding_sha256
            and checkpoint.physical_path == candidate.physical_path
            and checkpoint.page_size == candidate.page_size
        )
        if not checkpoint_current:
            raise _conflict(
                "canary_checkpoint_stale",
                operation=operation,
                board_id=self._board_id,
                allocated_high_water=allocated_high_water,
                checkpoint_through_seq=checkpoint.through_seq,
            )
        if checkpoint.source_fingerprint != checkpoint.target_fingerprint:
            raise _conflict(
                "canary_fingerprint_diverged",
                operation=operation,
                board_id=self._board_id,
            )

        receipt_rows = connection.execute(
            """
            SELECT * FROM comparison_receipts
            WHERE direction = 'shadow' AND through_seq = ?
              AND generation = ? AND binding_sha256 = ?
            ORDER BY receipt_id DESC
            """,
            (allocated_high_water, candidate.generation, binding_sha256),
        ).fetchall()
        matching_receipt = False
        for row in receipt_rows:
            receipt = self._comparison_from_row(row, operation=operation)
            if (
                receipt.physical_path == candidate.physical_path
                and receipt.page_size == candidate.page_size
                and receipt.source_result_sha256 == receipt.target_result_sha256
            ):
                matching_receipt = True
                break
        if not matching_receipt:
            raise _conflict(
                "canary_comparison_receipt_missing",
                operation=operation,
                board_id=self._board_id,
                through_seq=allocated_high_water,
            )

        divergence = connection.execute(
            """
            SELECT 1 FROM rollout_divergences
            WHERE direction = 'shadow' AND through_seq = ? AND generation = ?
            LIMIT 1
            """,
            (allocated_high_water, candidate.generation),
        ).fetchone()
        if divergence is not None:
            raise _conflict(
                "canary_divergence_present",
                operation=operation,
                board_id=self._board_id,
                through_seq=allocated_high_water,
            )

    def _record_checkpoint_locked(
        self,
        connection: sqlite3.Connection,
        *,
        rollout: GraphRolloutRecord,
        direction: CheckpointDirection,
        through_seq: int,
        source_fingerprint: str,
        target_fingerprint: str,
        generation: str,
        require_terminal: bool,
        operation: str,
    ) -> ReplayCheckpoint:
        target_identity = rollout.candidate if direction == "shadow" else rollout.source
        if generation != target_identity.generation:
            raise _conflict(
                "checkpoint_generation_mismatch",
                operation=operation,
                board_id=self._board_id,
                direction=direction,
                expected_generation=target_identity.generation,
                observed_generation=generation,
            )
        if target_identity.binding_sha256 is None:
            raise _conflict(
                "checkpoint_target_not_certified",
                operation=operation,
                board_id=self._board_id,
                direction=direction,
            )
        if require_terminal:
            terminal_high_water = self._terminal_high_water(connection)
            if through_seq > terminal_high_water:
                raise _conflict(
                    "checkpoint_beyond_terminal_high_water",
                    operation=operation,
                    board_id=self._board_id,
                    through_seq=through_seq,
                    terminal_high_water=terminal_high_water,
                )

        row = connection.execute(
            "SELECT * FROM replay_checkpoints WHERE direction = ?", (direction,)
        ).fetchone()
        current = (
            None if row is None else self._checkpoint_from_row(row, operation=operation)
        )
        target_changed = False
        if current is not None:
            if through_seq < current.through_seq:
                raise _conflict(
                    "checkpoint_regression",
                    operation=operation,
                    board_id=self._board_id,
                    direction=direction,
                    current_through_seq=current.through_seq,
                    requested_through_seq=through_seq,
                )
            target_changed = (
                target_identity.binding_sha256 != current.binding_sha256
                or target_identity.physical_path != current.physical_path
                or target_identity.page_size != current.page_size
                or target_identity.generation != current.generation
            )
            same = (
                through_seq == current.through_seq
                and source_fingerprint == current.source_fingerprint
                and target_fingerprint == current.target_fingerprint
                and not target_changed
            )
            if same:
                return current
            if through_seq == current.through_seq and not target_changed:
                raise _conflict(
                    "checkpoint_same_seq_mismatch",
                    operation=operation,
                    board_id=self._board_id,
                    direction=direction,
                )
            ack_version = current.ack_version + 1
        else:
            ack_version = 1

        now = _utc_now()
        body: dict[str, object] = {
            "direction": direction,
            "ack_version": ack_version,
            "through_seq": through_seq,
            "source_fingerprint": source_fingerprint,
            "target_fingerprint": target_fingerprint,
            "generation": generation,
            "binding_sha256": target_identity.binding_sha256,
            "physical_path": self._relative_path(target_identity.physical_path),
            "page_size": target_identity.page_size,
            "acked_at_utc": now,
        }
        connection.execute(
            """
            INSERT INTO replay_checkpoints (
                direction, ack_version, through_seq, source_fingerprint,
                target_fingerprint, generation, binding_sha256,
                physical_path, page_size, acked_at_utc, row_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(direction) DO UPDATE SET
                ack_version = excluded.ack_version,
                through_seq = excluded.through_seq,
                source_fingerprint = excluded.source_fingerprint,
                target_fingerprint = excluded.target_fingerprint,
                generation = excluded.generation,
                binding_sha256 = excluded.binding_sha256,
                physical_path = excluded.physical_path,
                page_size = excluded.page_size,
                acked_at_utc = excluded.acked_at_utc,
                row_sha256 = excluded.row_sha256
            """,
            (*body.values(), _canonical_sha256(body)),
        )
        persisted = connection.execute(
            "SELECT * FROM replay_checkpoints WHERE direction = ?", (direction,)
        ).fetchone()
        assert persisted is not None
        return self._checkpoint_from_row(persisted, operation=operation)

    def _write_candidate(
        self,
        connection: sqlite3.Connection,
        *,
        current: GraphRolloutRecord,
        candidate: RolloutEndpointIdentity,
        operation: str,
    ) -> GraphRolloutRecord:
        body = self._rollout_body(current)
        body.update(
            {
                "state_version": current.state_version + 1,
                "candidate_binding_sha256": candidate.binding_sha256,
                "candidate_generation": candidate.generation,
                "candidate_physical_path": self._relative_path(candidate.physical_path),
                "candidate_page_size": candidate.page_size,
                "updated_at_utc": _utc_now(),
            }
        )
        result = connection.execute(
            """
            UPDATE rollout_state
            SET state_version = ?, candidate_binding_sha256 = ?,
                candidate_generation = ?, candidate_physical_path = ?,
                candidate_page_size = ?, updated_at_utc = ?, row_sha256 = ?
            WHERE singleton = 1 AND state_version = ?
              AND candidate_binding_sha256 IS ?
              AND candidate_generation = ? AND candidate_physical_path = ?
              AND candidate_page_size = ?
            """,
            (
                body["state_version"],
                body["candidate_binding_sha256"],
                body["candidate_generation"],
                body["candidate_physical_path"],
                body["candidate_page_size"],
                body["updated_at_utc"],
                _canonical_sha256(body),
                current.state_version,
                current.candidate.binding_sha256,
                current.candidate.generation,
                self._relative_path(current.candidate.physical_path),
                current.candidate.page_size,
            ),
        )
        if result.rowcount != 1:
            raise _conflict(
                "stale_candidate_cas",
                operation=operation,
                board_id=self._board_id,
            )
        updated = self._load_rollout(connection, operation=operation, required=True)
        assert updated is not None
        return updated

    def _write_state(
        self,
        connection: sqlite3.Connection,
        *,
        current: GraphRolloutRecord,
        state: RolloutState,
        operation: str,
    ) -> GraphRolloutRecord:
        body = self._rollout_body(current)
        body["state"] = state
        body["state_version"] = current.state_version + 1
        body["updated_at_utc"] = _utc_now()
        result = connection.execute(
            """
            UPDATE rollout_state
            SET state = ?, state_version = ?, updated_at_utc = ?, row_sha256 = ?
            WHERE singleton = 1 AND state = ? AND state_version = ?
            """,
            (
                body["state"],
                body["state_version"],
                body["updated_at_utc"],
                _canonical_sha256(body),
                current.state,
                current.state_version,
            ),
        )
        if result.rowcount != 1:
            raise _conflict(
                "stale_state_cas",
                operation=operation,
                board_id=self._board_id,
            )
        updated = self._load_rollout(connection, operation=operation, required=True)
        assert updated is not None
        return updated

    def _write_next_seq(
        self,
        connection: sqlite3.Connection,
        *,
        rollout: GraphRolloutRecord,
        next_seq: int,
        operation: str,
    ) -> None:
        body = self._rollout_body(rollout)
        body["next_seq"] = next_seq
        body["updated_at_utc"] = _utc_now()
        result = connection.execute(
            """
            UPDATE rollout_state
            SET next_seq = ?, updated_at_utc = ?, row_sha256 = ?
            WHERE singleton = 1 AND next_seq = ?
            """,
            (
                next_seq,
                body["updated_at_utc"],
                _canonical_sha256(body),
                rollout.next_seq,
            ),
        )
        if result.rowcount != 1:
            raise _conflict(
                "stale_mutation_sequence",
                operation=operation,
                board_id=self._board_id,
            )

    def _load_rollout(
        self,
        connection: sqlite3.Connection,
        *,
        operation: str,
        required: bool,
    ) -> GraphRolloutRecord | None:
        rows = connection.execute("SELECT * FROM rollout_state").fetchall()
        if not rows:
            if required:
                raise _corruption(
                    "rollout_state_missing",
                    operation=operation,
                    board_id=self._board_id,
                )
            return None
        if len(rows) != 1:
            raise _corruption(
                "rollout_state_not_singleton",
                operation=operation,
                board_id=self._board_id,
            )
        row = rows[0]
        try:
            body = {key: row[key] for key in _TABLE_COLUMNS["rollout_state"][:-1]}
            if _canonical_sha256(body) != row["row_sha256"]:
                raise ValueError("rollout_checksum_mismatch")
            if row["singleton"] != 1 or row["board_id"] != self._board_id:
                raise ValueError("rollout_identity_mismatch")
            state = self._validate_state(row["state"])
            state_version = _require_positive_int(
                row["state_version"], field_name="state_version"
            )
            next_seq = _require_positive_int(row["next_seq"], field_name="next_seq")
            source = self._endpoint_from_persisted(
                backend=row["source_backend"],
                binding_sha256=row["source_binding_sha256"],
                generation=row["source_generation"],
                relative_path=row["source_physical_path"],
                page_size=row["source_page_size"],
                expected_backend="ladybug",
            )
            candidate = self._endpoint_from_persisted(
                backend=row["candidate_backend"],
                binding_sha256=row["candidate_binding_sha256"],
                generation=row["candidate_generation"],
                relative_path=row["candidate_physical_path"],
                page_size=row["candidate_page_size"],
                expected_backend="grafx",
            )
            created = _require_timestamp(
                row["created_at_utc"], field_name="created_at_utc"
            )
            updated = _require_timestamp(
                row["updated_at_utc"], field_name="updated_at_utc"
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise _corruption(
                "rollout_state_invalid",
                operation=operation,
                board_id=self._board_id,
            ) from exc
        return GraphRolloutRecord(
            board_id=self._board_id,
            state=state,
            state_version=state_version,
            next_seq=next_seq,
            source=source,
            candidate=candidate,
            created_at_utc=created,
            updated_at_utc=updated,
        )

    def _load_mutation(
        self, connection: sqlite3.Connection, *, seq: int, operation: str
    ) -> LogicalMutationRecord:
        row = connection.execute(
            "SELECT * FROM logical_mutations WHERE seq = ?", (seq,)
        ).fetchone()
        if row is None:
            raise _conflict(
                "mutation_missing",
                operation=operation,
                board_id=self._board_id,
                seq=seq,
            )
        return self._mutation_from_row(row, operation=operation)

    def _mutation_from_row(
        self, row: sqlite3.Row, *, operation: str
    ) -> LogicalMutationRecord:
        try:
            body = {key: row[key] for key in _TABLE_COLUMNS["logical_mutations"][:-1]}
            if _canonical_sha256(body) != row["row_sha256"]:
                raise ValueError("mutation_checksum_mismatch")
            seq = _require_positive_int(row["seq"], field_name="seq")
            family = row["family"]
            if type(family) is not str or _FAMILY_RE.fullmatch(family) is None:
                raise ValueError("family_invalid")
            payload = _decode_canonical_json(row["payload_json"])
            if type(payload) is not dict:
                raise ValueError("payload_not_object")
            payload_sha256 = _require_sha256(
                row["payload_sha256"], field_name="payload_sha256"
            )
            if hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest() != (
                payload_sha256
            ):
                raise ValueError("payload_checksum_mismatch")
            expected_binding_sha256 = _require_sha256(
                row["expected_binding_sha256"],
                field_name="expected_binding_sha256",
            )
            status = row["status"]
            if type(status) is not str or status not in _MUTATION_STATUSES:
                raise ValueError("mutation_status_invalid")
            prepared_at = _require_timestamp(
                row["prepared_at_utc"], field_name="prepared_at_utc"
            )
            terminal_at = row["terminal_at_utc"]
            if status == "prepared":
                if terminal_at is not None:
                    raise ValueError("prepared_has_terminal_time")
            else:
                terminal_at = _require_timestamp(
                    terminal_at, field_name="terminal_at_utc"
                )
        except (TypeError, ValueError) as exc:
            raise _corruption(
                "logical_mutation_invalid",
                operation=operation,
                board_id=self._board_id,
            ) from exc
        return LogicalMutationRecord(
            seq=seq,
            family=family,
            payload=payload,
            payload_json=row["payload_json"],
            payload_sha256=payload_sha256,
            expected_binding_sha256=expected_binding_sha256,
            status=status,
            prepared_at_utc=prepared_at,
            terminal_at_utc=terminal_at,
        )

    def _checkpoint_from_row(
        self, row: sqlite3.Row, *, operation: str
    ) -> ReplayCheckpoint:
        try:
            body = {key: row[key] for key in _TABLE_COLUMNS["replay_checkpoints"][:-1]}
            if _canonical_sha256(body) != row["row_sha256"]:
                raise ValueError("checkpoint_checksum_mismatch")
            direction = self._validate_direction(row["direction"])
            ack_version = _require_positive_int(
                row["ack_version"], field_name="ack_version"
            )
            through_seq = _require_non_negative_int(
                row["through_seq"], field_name="through_seq"
            )
            source = _require_sha256(
                row["source_fingerprint"], field_name="source_fingerprint"
            )
            target = _require_sha256(
                row["target_fingerprint"], field_name="target_fingerprint"
            )
            generation = _portable_segment(row["generation"], field_name="generation")
            binding_sha256 = row["binding_sha256"]
            if binding_sha256 is not None:
                binding_sha256 = _require_sha256(
                    binding_sha256, field_name="binding_sha256"
                )
            relative_path = row["physical_path"]
            if type(relative_path) is not str or "\\" in relative_path:
                raise ValueError("checkpoint_physical_path_invalid")
            pure_path = PurePosixPath(relative_path)
            if pure_path.is_absolute() or ".." in pure_path.parts:
                raise ValueError("checkpoint_physical_path_invalid")
            physical_path = self._root.joinpath(*pure_path.parts)
            page_size = row["page_size"]
            if direction == "shadow":
                page_size = validate_grafx_page_size(page_size)
                endpoint = self._normalize_endpoint(
                    RolloutEndpointIdentity(
                        backend="grafx",
                        binding_sha256=binding_sha256,
                        generation=generation,
                        physical_path=physical_path,
                        page_size=page_size,
                    ),
                    expected_backend="grafx",
                )
            else:
                if page_size is not None:
                    raise ValueError("ladybug_checkpoint_page_size_not_null")
                endpoint = self._normalize_endpoint(
                    RolloutEndpointIdentity(
                        backend="ladybug",
                        binding_sha256=binding_sha256,
                        generation=generation,
                        physical_path=physical_path,
                        page_size=None,
                    ),
                    expected_backend="ladybug",
                )
            if endpoint["physical_path"] != relative_path:
                raise ValueError("checkpoint_physical_path_not_canonical")
            acked_at = _require_timestamp(
                row["acked_at_utc"], field_name="acked_at_utc"
            )
        except (TypeError, ValueError) as exc:
            raise _corruption(
                "checkpoint_row_invalid",
                operation=operation,
                board_id=self._board_id,
            ) from exc
        return ReplayCheckpoint(
            direction=direction,
            ack_version=ack_version,
            through_seq=through_seq,
            source_fingerprint=source,
            target_fingerprint=target,
            generation=generation,
            binding_sha256=binding_sha256,
            physical_path=physical_path,
            page_size=page_size,
            acked_at_utc=acked_at,
        )

    def _divergence_from_row(
        self, row: sqlite3.Row, *, operation: str
    ) -> RolloutDivergence:
        try:
            body = {key: row[key] for key in _TABLE_COLUMNS["rollout_divergences"][:-1]}
            if _canonical_sha256(body) != row["row_sha256"]:
                raise ValueError("divergence_checksum_mismatch")
            divergence_id = _require_positive_int(
                row["divergence_id"], field_name="divergence_id"
            )
            direction = self._validate_direction(row["direction"])
            through_seq = _require_non_negative_int(
                row["through_seq"], field_name="through_seq"
            )
            expected = _require_sha256(
                row["expected_fingerprint"], field_name="expected_fingerprint"
            )
            actual = _require_sha256(
                row["actual_fingerprint"], field_name="actual_fingerprint"
            )
            generation = _portable_segment(row["generation"], field_name="generation")
            details = _decode_canonical_json(row["details_json"])
            if type(details) is not dict:
                raise ValueError("details_not_object")
            details_sha256 = _require_sha256(
                row["details_sha256"], field_name="details_sha256"
            )
            if hashlib.sha256(row["details_json"].encode("utf-8")).hexdigest() != (
                details_sha256
            ):
                raise ValueError("details_checksum_mismatch")
            detected_at = _require_timestamp(
                row["detected_at_utc"], field_name="detected_at_utc"
            )
        except (TypeError, ValueError) as exc:
            raise _corruption(
                "divergence_row_invalid",
                operation=operation,
                board_id=self._board_id,
            ) from exc
        return RolloutDivergence(
            divergence_id=divergence_id,
            direction=direction,
            through_seq=through_seq,
            expected_fingerprint=expected,
            actual_fingerprint=actual,
            generation=generation,
            details=details,
            details_sha256=details_sha256,
            detected_at_utc=detected_at,
        )

    def _comparison_from_row(
        self, row: sqlite3.Row, *, operation: str
    ) -> ComparisonReceipt:
        try:
            body = {key: row[key] for key in _TABLE_COLUMNS["comparison_receipts"][:-1]}
            if _canonical_sha256(body) != row["row_sha256"]:
                raise ValueError("comparison_checksum_mismatch")
            receipt_id = _require_positive_int(
                row["receipt_id"], field_name="receipt_id"
            )
            direction = self._validate_direction(row["direction"])
            through_seq = _require_non_negative_int(
                row["through_seq"], field_name="through_seq"
            )
            generation = _portable_segment(row["generation"], field_name="generation")
            binding_sha256 = _require_sha256(
                row["binding_sha256"], field_name="binding_sha256"
            )
            relative_path = row["physical_path"]
            if type(relative_path) is not str or "\\" in relative_path:
                raise ValueError("comparison_physical_path_invalid")
            pure_path = PurePosixPath(relative_path)
            if pure_path.is_absolute() or ".." in pure_path.parts:
                raise ValueError("comparison_physical_path_invalid")
            physical_path = self._root.joinpath(*pure_path.parts)
            raw_page_size = row["page_size"]
            backend: GraphBackend = "grafx" if direction == "shadow" else "ladybug"
            page_size = (
                validate_grafx_page_size(raw_page_size) if backend == "grafx" else None
            )
            if backend == "ladybug" and raw_page_size is not None:
                raise ValueError("comparison_ladybug_page_size_not_null")
            endpoint = self._normalize_endpoint(
                RolloutEndpointIdentity(
                    backend=backend,
                    binding_sha256=binding_sha256,
                    generation=generation,
                    physical_path=physical_path,
                    page_size=page_size,
                ),
                expected_backend=backend,
            )
            if endpoint["physical_path"] != relative_path:
                raise ValueError("comparison_physical_path_not_canonical")
            corpus_sha256 = _require_sha256(
                row["corpus_sha256"], field_name="corpus_sha256"
            )
            source_result_sha256 = _require_sha256(
                row["source_result_sha256"], field_name="source_result_sha256"
            )
            target_result_sha256 = _require_sha256(
                row["target_result_sha256"], field_name="target_result_sha256"
            )
            if source_result_sha256 != target_result_sha256:
                raise ValueError("comparison_results_diverged")
            query_count = _require_positive_int(
                row["query_count"], field_name="query_count"
            )
            completed_at = _require_timestamp(
                row["completed_at_utc"], field_name="completed_at_utc"
            )
        except (TypeError, ValueError) as exc:
            raise _corruption(
                "comparison_receipt_invalid",
                operation=operation,
                board_id=self._board_id,
            ) from exc
        return ComparisonReceipt(
            receipt_id=receipt_id,
            direction=direction,
            through_seq=through_seq,
            generation=generation,
            binding_sha256=binding_sha256,
            physical_path=physical_path,
            page_size=page_size,
            corpus_sha256=corpus_sha256,
            source_result_sha256=source_result_sha256,
            target_result_sha256=target_result_sha256,
            query_count=query_count,
            completed_at_utc=completed_at,
        )

    def _terminal_high_water(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            """
            SELECT
                COALESCE(MIN(CASE WHEN status = 'prepared' THEN seq END),
                         (SELECT next_seq FROM rollout_state WHERE singleton = 1)) - 1,
                (SELECT next_seq FROM rollout_state WHERE singleton = 1) - 1
            FROM logical_mutations
            """
        ).fetchone()
        terminal_high_water = int(row[0])
        last_allocated = int(row[1])
        if terminal_high_water < 0 or terminal_high_water > last_allocated:
            raise ValueError("terminal_high_water_invalid")
        return terminal_high_water

    def _active_binding_sha256(
        self, rollout: GraphRolloutRecord, *, operation: str
    ) -> str:
        if rollout.state in {
            "grafx_active_rollback_open",
            "grafx_active_rollback_closed",
            "completed",
        }:
            if rollout.candidate.binding_sha256 is None:
                raise _corruption(
                    "candidate_binding_not_certified",
                    operation=operation,
                    board_id=self._board_id,
                )
            return rollout.candidate.binding_sha256
        if rollout.source.binding_sha256 is None:
            raise _corruption(
                "source_binding_missing",
                operation=operation,
                board_id=self._board_id,
            )
        return rollout.source.binding_sha256

    def _require_completed_route(
        self,
        rollout: GraphRolloutRecord,
        *,
        expected_binding_sha256: str,
        backend: GraphBackend,
        operation: str,
    ) -> None:
        """Authenticate the still-bound source without reopening capture."""

        if any(
            endpoint.backend == backend
            and endpoint.binding_sha256 == expected_binding_sha256
            for endpoint in (rollout.source, rollout.candidate)
        ):
            return
        raise _conflict(
            "completed_route_fence_mismatch",
            operation=operation,
            board_id=self._board_id,
            supplied_backend=backend,
            supplied_binding_sha256=expected_binding_sha256,
        )

    @staticmethod
    def _validate_state(value: object) -> RolloutState:
        if type(value) is not str or value not in _ROLLOUT_STATES:
            raise ValueError("rollout_state_invalid")
        return value

    @staticmethod
    def _validate_direction(value: object) -> CheckpointDirection:
        if type(value) is not str or value not in _CHECKPOINT_DIRECTIONS:
            raise ValueError("checkpoint_direction_invalid")
        return value

    def _rollout_body(self, record: GraphRolloutRecord) -> dict[str, object]:
        return {
            "singleton": 1,
            "board_id": record.board_id,
            "state": record.state,
            "state_version": record.state_version,
            "next_seq": record.next_seq,
            "source_backend": record.source.backend,
            "source_binding_sha256": record.source.binding_sha256,
            "source_generation": record.source.generation,
            "source_physical_path": self._relative_path(record.source.physical_path),
            "source_page_size": record.source.page_size,
            "candidate_backend": record.candidate.backend,
            "candidate_binding_sha256": record.candidate.binding_sha256,
            "candidate_generation": record.candidate.generation,
            "candidate_physical_path": self._relative_path(
                record.candidate.physical_path
            ),
            "candidate_page_size": record.candidate.page_size,
            "created_at_utc": record.created_at_utc,
            "updated_at_utc": record.updated_at_utc,
        }

    def _normalize_endpoint(
        self,
        endpoint: RolloutEndpointIdentity,
        *,
        expected_backend: GraphBackend,
    ) -> dict[str, object]:
        if not isinstance(endpoint, RolloutEndpointIdentity):
            raise TypeError("endpoint_identity_invalid")
        if endpoint.backend != expected_backend:
            raise ValueError("endpoint_backend_invalid")
        binding_sha256 = endpoint.binding_sha256
        if binding_sha256 is not None:
            binding_sha256 = _require_sha256(
                binding_sha256, field_name="binding_sha256"
            )
        if expected_backend == "ladybug":
            if binding_sha256 is None:
                raise ValueError("ladybug_binding_sha256_missing")
            if endpoint.page_size is not None:
                raise ValueError("ladybug_page_size_not_null")
            page_size = None
        else:
            page_size = validate_grafx_page_size(endpoint.page_size)
        generation = _portable_segment(endpoint.generation, field_name="generation")
        supplied_path = Path(endpoint.physical_path)
        if not supplied_path.is_absolute():
            raise ValueError("endpoint_path_not_absolute")
        lexical = Path(os.path.abspath(supplied_path))
        try:
            resolved = supplied_path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValueError("endpoint_path_unresolvable") from exc
        if os.path.normcase(str(lexical)) != os.path.normcase(str(resolved)):
            raise ValueError("endpoint_path_alias_refused")
        relative = lexical.relative_to(self._root)
        expected = (
            self._root / "boards" / self._board_id / "graph.lbug"
            if expected_backend == "ladybug"
            else self._root / "boards" / self._board_id / "grafx" / generation
        )
        if os.path.normcase(str(lexical)) != os.path.normcase(str(expected)):
            raise ValueError("endpoint_path_not_canonical")
        reject_filesystem_alias_ancestry(lexical.parent)
        if is_filesystem_alias(lexical):
            raise ValueError("endpoint_path_alias_refused")
        return {
            "backend": expected_backend,
            "binding_sha256": binding_sha256,
            "generation": generation,
            "physical_path": PurePosixPath(*relative.parts).as_posix(),
            "page_size": page_size,
        }

    def _endpoint_from_persisted(
        self,
        *,
        backend: object,
        binding_sha256: object,
        generation: object,
        relative_path: object,
        page_size: object,
        expected_backend: GraphBackend,
    ) -> RolloutEndpointIdentity:
        if type(relative_path) is not str or "\\" in relative_path:
            raise ValueError("endpoint_relative_path_invalid")
        pure = PurePosixPath(relative_path)
        if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
            raise ValueError("endpoint_relative_path_invalid")
        absolute = self._root.joinpath(*pure.parts)
        body = self._normalize_endpoint(
            RolloutEndpointIdentity(
                backend=backend,
                binding_sha256=binding_sha256,
                generation=generation,
                physical_path=absolute,
                page_size=page_size,
            ),
            expected_backend=expected_backend,
        )
        if body["physical_path"] != relative_path:
            raise ValueError("endpoint_relative_path_not_canonical")
        return self._endpoint_from_body(body)

    def _endpoint_from_body(
        self, body: Mapping[str, object]
    ) -> RolloutEndpointIdentity:
        return RolloutEndpointIdentity(
            backend=body["backend"],  # type: ignore[arg-type]
            binding_sha256=body["binding_sha256"],  # type: ignore[arg-type]
            generation=body["generation"],  # type: ignore[arg-type]
            physical_path=self._root.joinpath(
                *PurePosixPath(str(body["physical_path"])).parts
            ),
            page_size=body["page_size"],  # type: ignore[arg-type]
        )

    def _relative_path(self, path: Path) -> str:
        relative = Path(path).relative_to(self._root)
        return PurePosixPath(*relative.parts).as_posix()

    @staticmethod
    def _canonical_root(value: str | os.PathLike[str]) -> Path:
        raw = os.fspath(value)
        if not raw.strip() or "://" in raw:
            raise ValueError("kg_base_dir_not_local")
        expanded = Path(raw).expanduser()
        if not expanded.is_absolute():
            raise ValueError("kg_base_dir_not_absolute")
        lexical = Path(os.path.abspath(expanded))
        resolved = expanded.resolve(strict=False)
        if os.path.normcase(str(lexical)) != os.path.normcase(str(resolved)):
            raise ValueError("kg_base_dir_alias_refused")
        reject_filesystem_alias_ancestry(lexical)
        return lexical

    def _privacy_checked_paths(self) -> tuple[Path, ...]:
        database = self._database_path
        return (
            database,
            Path(f"{database}-wal"),
            Path(f"{database}-shm"),
            Path(f"{database}-journal"),
        )

    def _rollout_storage_exists(self, *, operation: str) -> bool:
        try:
            self._rollout_root.lstat()
        except FileNotFoundError:
            try:
                self._validate_layout(allow_missing_rollout=True)
            except (OSError, RuntimeError, ValueError) as exc:
                raise _corruption(
                    "rollout_path_invalid",
                    operation=operation,
                    board_id=self._board_id,
                ) from exc
            return False
        except OSError as exc:
            raise _unavailable(
                "rollout_path_unreadable",
                operation=operation,
                board_id=self._board_id,
                error_type=type(exc).__name__,
            ) from exc
        try:
            self._validate_layout(allow_missing_rollout=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise _corruption(
                "rollout_path_invalid",
                operation=operation,
                board_id=self._board_id,
            ) from exc
        if not self._database_path.exists():
            raise _corruption(
                "journal_database_missing",
                operation=operation,
                board_id=self._board_id,
            )
        return True

    def _validate_layout(self, *, allow_missing_rollout: bool) -> None:
        reject_filesystem_alias_ancestry(self._board_root)
        if self._rollout_root.exists():
            metadata = self._rollout_root.lstat()
            if is_filesystem_alias(self._rollout_root) or not stat.S_ISDIR(
                metadata.st_mode
            ):
                raise ValueError("rollout_root_alias_or_type_invalid")
            lexical = Path(os.path.abspath(self._rollout_root))
            if os.path.normcase(str(lexical)) != os.path.normcase(
                str(self._rollout_root.resolve(strict=False))
            ):
                raise ValueError("rollout_root_alias_refused")
        elif not allow_missing_rollout:
            raise FileNotFoundError(self._rollout_root)
        if self._database_path.exists():
            metadata = self._database_path.lstat()
            if is_filesystem_alias(self._database_path) or not stat.S_ISREG(
                metadata.st_mode
            ):
                raise ValueError("journal_database_alias_or_type_invalid")

    def _ensure_layout(self) -> None:
        existed = self._rollout_root.exists()
        self._rollout_root.mkdir(parents=True, exist_ok=True)
        self._validate_layout(allow_missing_rollout=False)
        if not existed:
            fsync_directory(self._board_root)

    @contextmanager
    def _transaction(
        self,
        *,
        operation: str,
        create: bool = False,
        verify_integrity: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        began = False
        try:
            if create:
                self._ensure_layout()
            else:
                self._validate_layout(allow_missing_rollout=False)
                if not self._database_path.exists():
                    raise _unavailable(
                        "journal_missing",
                        operation=operation,
                        board_id=self._board_id,
                    )
            connection = sqlite3.connect(
                str(self._database_path),
                timeout=self._busy_timeout_seconds,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            self._configure_connection(connection)
            self._validate_layout(allow_missing_rollout=False)
            connection.execute("BEGIN IMMEDIATE")
            began = True
            self._initialize_or_validate_schema(
                connection,
                allow_initialize=create,
                verify_integrity=verify_integrity,
            )
            yield connection
            connection.execute("COMMIT")
            began = False
        except GraphError:
            if connection is not None and began:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        except sqlite3.OperationalError as exc:
            if connection is not None and began:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            lowered = str(exc).lower()
            if "locked" in lowered or "busy" in lowered:
                raise GraphLockContention(
                    "The Board graph rollout journal writer is contended.",
                    details={
                        "operation": operation,
                        "reason": "journal_lock_contention",
                        "scope": "board",
                        "scope_id": self._board_id,
                    },
                ) from exc
            raise _corruption(
                "sqlite_operational_failure",
                operation=operation,
                board_id=self._board_id,
            ) from exc
        except sqlite3.DatabaseError as exc:
            if connection is not None and began:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise _corruption(
                "sqlite_database_failure",
                operation=operation,
                board_id=self._board_id,
            ) from exc
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            if connection is not None and began:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise _corruption(
                "journal_operation_invalid",
                operation=operation,
                board_id=self._board_id,
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    def _configure_connection(self, connection: sqlite3.Connection) -> None:
        busy_timeout_ms = max(1, round(self._busy_timeout_seconds * 1000))
        connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        if mode.casefold() != "wal":
            raise sqlite3.DatabaseError("journal_mode_not_wal")
        connection.execute("PRAGMA synchronous=FULL")
        if int(connection.execute("PRAGMA synchronous").fetchone()[0]) != 2:
            raise sqlite3.DatabaseError("synchronous_not_full")
        connection.execute("PRAGMA foreign_keys=ON")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise sqlite3.DatabaseError("foreign_keys_not_enabled")
        connection.execute("PRAGMA trusted_schema=OFF")

    def _initialize_or_validate_schema(
        self,
        connection: sqlite3.Connection,
        *,
        allow_initialize: bool,
        verify_integrity: bool,
    ) -> None:
        created = False
        if allow_initialize:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type = 'table'"
                ).fetchall()
            }
            application_id = int(
                connection.execute("PRAGMA application_id").fetchone()[0]
            )
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            created = not tables and application_id == 0 and user_version == 0
        if created:
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.execute(f"PRAGMA application_id={ROLLOUT_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={ROLLOUT_SCHEMA_VERSION}")
            connection.executemany(
                "INSERT INTO journal_meta (key, value) VALUES (?, ?)",
                (
                    ("format", ROLLOUT_JOURNAL_FORMAT),
                    ("schema_version", str(ROLLOUT_SCHEMA_VERSION)),
                ),
            )
            fsync_directory(self._rollout_root)
        self._validate_header(connection)
        if created or verify_integrity:
            self._verify_integrity(connection)

    def _validate_header(self, connection: sqlite3.Connection) -> None:
        if int(connection.execute("PRAGMA application_id").fetchone()[0]) != (
            ROLLOUT_APPLICATION_ID
        ):
            raise sqlite3.DatabaseError("journal_application_id_invalid")
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != (
            ROLLOUT_SCHEMA_VERSION
        ):
            raise sqlite3.DatabaseError("journal_schema_version_unsupported")
        meta = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT key, value FROM journal_meta ORDER BY key"
            ).fetchall()
        }
        if meta != {
            "format": ROLLOUT_JOURNAL_FORMAT,
            "schema_version": str(ROLLOUT_SCHEMA_VERSION),
        }:
            raise sqlite3.DatabaseError("journal_meta_invalid")

    def _verify_integrity(self, connection: sqlite3.Connection) -> None:
        quick_check = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
        if quick_check != ["ok"]:
            raise sqlite3.DatabaseError("journal_quick_check_failed")
        observed_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        if observed_tables != set(_TABLE_COLUMNS):
            raise sqlite3.DatabaseError("journal_table_set_invalid")
        for table, expected_columns in _TABLE_COLUMNS.items():
            observed_columns = tuple(
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            )
            if observed_columns != expected_columns:
                raise sqlite3.DatabaseError(f"journal_table_shape_invalid:{table}")
        observed_indexes = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_schema
                WHERE type = 'index' AND sql IS NOT NULL
                """
            ).fetchall()
        }
        if observed_indexes != {
            "comparison_receipts_direction_id_idx",
            "logical_mutations_status_seq_idx",
            "rollout_divergences_direction_id_idx",
        }:
            raise sqlite3.DatabaseError("journal_index_set_invalid")


class CommunityGraphRolloutMutationRecorder:
    """Duck-typed adapter consumed by ``CapturedGraphTransactionScope``."""

    def __init__(
        self,
        kg_base_dir: str | os.PathLike[str],
        *,
        busy_timeout_seconds: float = 30.0,
    ) -> None:
        try:
            self._root = CommunityGraphRolloutJournal._canonical_root(kg_base_dir)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise _capability(
                "journal_identity_invalid",
                operation="configure_graph_rollout_mutation_recorder",
            ) from exc
        if (
            isinstance(busy_timeout_seconds, bool)
            or not isinstance(busy_timeout_seconds, (int, float))
            or not math.isfinite(float(busy_timeout_seconds))
            or busy_timeout_seconds <= 0
        ):
            raise _capability(
                "busy_timeout_invalid",
                operation="configure_graph_rollout_mutation_recorder",
            )
        self._busy_timeout_seconds = float(busy_timeout_seconds)

    def prepare_mutation(
        self,
        *,
        board_id: str,
        binding_sha256: str,
        backend: str,
        transaction_id: str,
        family: str,
        payload: Mapping[str, object],
    ) -> RolloutMutationToken | None:
        operation = "capture_graph_rollout_mutation"
        try:
            safe_transaction_id = _portable_segment(
                transaction_id, field_name="transaction_id"
            )
            if backend not in {"ladybug", "grafx"}:
                raise ValueError("backend_invalid")
            if type(payload) is not dict:
                raise ValueError("payload_not_object")
        except (TypeError, ValueError) as exc:
            raise _capability("capture_identity_invalid", operation=operation) from exc
        journal = self._journal(board_id)
        mutation = journal.prepare_if_active(
            family=family,
            payload={
                "capture_format": "okto-pulse-board-rollout-capture/1",
                "transaction_id": safe_transaction_id,
                "payload": dict(payload),
            },
            expected_binding_sha256=binding_sha256,
            backend=backend,
        )
        if mutation is None:
            return None
        return RolloutMutationToken(
            board_id=journal.board_id,
            seq=mutation.seq,
            payload_sha256=mutation.payload_sha256,
        )

    def mark_source_committed(self, token: object) -> None:
        safe = self._token(token, operation="capture_graph_rollout_source_committed")
        self._journal(safe.board_id).mark_source_committed(
            seq=safe.seq, payload_sha256=safe.payload_sha256
        )

    def mark_source_abandoned(self, token: object) -> None:
        safe = self._token(token, operation="capture_graph_rollout_source_abandoned")
        self._journal(safe.board_id).mark_source_abandoned(
            seq=safe.seq, payload_sha256=safe.payload_sha256
        )

    def mark_source_ambiguous(self, token: object, *, error_type: str) -> None:
        # Deliberately retain ``prepared``. A later fenced full-state snapshot
        # resolves the ambiguous outcome as ``source_reconciled``.
        self._token(token, operation="capture_graph_rollout_source_ambiguous")
        if type(error_type) is not str or not error_type or len(error_type) > 256:
            raise _capability(
                "ambiguous_error_type_invalid",
                operation="capture_graph_rollout_source_ambiguous",
            )

    def close_rollback_before_write_if_active(
        self,
        board_id: str,
        binding_sha256: str,
        backend: GraphBackend,
    ) -> GraphRolloutRecord | None:
        """Apply the rollout write fence through the recorder's journal factory."""

        return self._journal(board_id).close_rollback_before_write_if_active(
            expected_binding_sha256=binding_sha256,
            backend=backend,
        )

    def _journal(self, board_id: str) -> CommunityGraphRolloutJournal:
        return CommunityGraphRolloutJournal(
            self._root,
            board_id,
            busy_timeout_seconds=self._busy_timeout_seconds,
        )

    @staticmethod
    def _token(token: object, *, operation: str) -> RolloutMutationToken:
        if not isinstance(token, RolloutMutationToken):
            raise _capability("capture_token_invalid", operation=operation)
        return token


__all__ = [
    "MAX_COMPARISON_PAGE_SIZE",
    "MAX_DIVERGENCE_PAGE_SIZE",
    "MAX_MUTATION_PAGE_SIZE",
    "ROLLOUT_APPLICATION_ID",
    "ROLLOUT_DATABASE_FILENAME",
    "ROLLOUT_JOURNAL_FORMAT",
    "ROLLOUT_SCHEMA_VERSION",
    "CheckpointDirection",
    "CommittedMutationPage",
    "CommunityGraphRolloutJournal",
    "CommunityGraphRolloutMutationRecorder",
    "ComparisonReceipt",
    "GraphRolloutJournalConflict",
    "GraphRolloutRecord",
    "LogicalMutationRecord",
    "MutationStatus",
    "PrivacyEraseProof",
    "ReplayCheckpoint",
    "RolloutDivergence",
    "RolloutEndpointIdentity",
    "RolloutMutationToken",
    "RolloutState",
]
