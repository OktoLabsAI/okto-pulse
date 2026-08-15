"""Strict contract for one legacy, manually-restored rebuild reconciliation.

The legacy recovery-only lane is deliberately narrower than ordinary rebuild
compensation.  A historical operator restore already isolated the failed
candidate and restored a readable graph, so the only remaining mutation is an
exact compare-and-set of residual queue rows.  This module owns the portable,
path-relative evidence payload shared by the offline executor and Community
adapters.  Core receives only the nominal F06 receipt carrying this payload.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any
from uuid import RFC_4122, UUID


LEGACY_QUEUE_ONLY_SCHEMA = "legacy_manual_restore_queue_only.v2"
LEGACY_QUEUE_ONLY_KIND = "legacy_manual_restore_queue_only"
LEGACY_QUEUE_ONLY_PREFIX_SCHEMA = "pre_v4_legacy"
LEGACY_DEAD_LETTER_GUARD_SCHEMA = "dead_letter_guard/v1"
LEGACY_QUEUE_ONLY_INTENT_EFFECT = (
    "legacy_manually_restored_blocked_after_enqueue_intent"
)
LEGACY_QUEUE_ONLY_INTENT_CODE = "legacy_manual_restore_queue_only_authorized"
LEGACY_QUEUE_ONLY_PREAPPLIED_ACTIONS = (
    "restore_quarantine",
    "discard_candidate_generation",
)
LEGACY_QUEUE_ONLY_REMAINING_ACTIONS = ("cancel_enqueued_sources",)
MAX_LEGACY_QUEUE_ROWS = 4_096

LEGACY_QUEUE_COLUMNS = (
    "id",
    "board_id",
    "artifact_type",
    "artifact_id",
    "work_kind",
    "generation",
    "payload",
    "delete_event_id",
    "priority",
    "source",
    "status",
    "triggered_at",
    "triggered_by_event",
    "claimed_by_session_id",
    "claim_token",
    "claimed_at",
    "last_error",
    "worker_id",
    "claim_timeout_at",
    "attempts",
    "next_retry_at",
)
LEGACY_DEAD_LETTER_COLUMNS = (
    "id",
    "board_id",
    "artifact_type",
    "artifact_id",
    "original_queue_id",
    "attempts",
    "errors",
    "dead_lettered_at",
    "created_at",
)
_QUEUE_ROW_KEYS = frozenset(LEGACY_QUEUE_COLUMNS)
_MEMBERSHIP_KEYS = frozenset(
    {
        "row_id",
        "artifact_type",
        "artifact_id",
        "run_id",
        "source_ref",
        "source_version",
        "content_hash",
    }
)
_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "reconciliation_kind",
        "recovery_run_id",
        "board_id",
        "recovery_actor_id",
        "recovery_reason",
        "legacy_actor_id",
        "legacy_reason",
        "manifest_ref",
        "f06_run_id",
        "legacy_run_id",
        "intent_ref",
        "candidate_generation_id",
        "checkpoint",
        "f06_artifacts",
        "manifest",
        "terminal_run",
        "original_quarantine",
        "manual_restore",
        "board_storage",
        "prefix_schema",
        "dead_letter_guard",
        "queue",
        "preapplied_actions",
        "remaining_actions",
        "evidence_digest",
        "intent_digest",
    }
)

_DEAD_LETTER_GUARD_KEYS = frozenset(
    {
        "schema_version",
        "checkpoint_started_at",
        "columns",
        "board_row_count",
        "board_ids",
        "snapshot_fingerprint",
        "target_queue_ids",
        "target_identities",
        "peers",
        "target_identities_without_peer",
    }
)
_DEAD_LETTER_IDENTITY_KEYS = frozenset({"artifact_type", "artifact_id"})
_DEAD_LETTER_PEER_KEYS = frozenset(
    {
        "id",
        "artifact_type",
        "artifact_id",
        "original_queue_id",
        "created_at",
        "dead_lettered_at",
        "row_sha256",
        "errors_sha256",
        "original_queue_absent",
        "preexisting_before_checkpoint",
    }
)

_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_UUID_IDENTIFIER = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
_F06_ARTIFACT_LABELS = (
    "snapshot",
    "quarantine",
    "enqueue",
    "lease_lost_audit",
)


class LegacyQueueOnlyIntentError(ValueError):
    """The nominal legacy queue-only evidence is malformed or conflicting."""


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LegacyQueueOnlyIntentError(
            "legacy_queue_only_noncanonical_value"
        ) from exc


def canonical_evidence_hash(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def legacy_queue_terminal_row(
    row: Mapping[str, Any],
    membership: Mapping[str, str],
) -> dict[str, Any]:
    """Project one validated legacy row to its exact adoptable tombstone."""

    result = dict(row)
    result.update(
        {
            "status": "failed",
            "payload": json.dumps(
                {
                    "_rebuild_membership": {
                        key: membership[key]
                        for key in (
                            "run_id",
                            "source_ref",
                            "source_version",
                            "content_hash",
                        )
                    }
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            "claimed_by_session_id": None,
            "claim_token": None,
            "claimed_at": None,
            "last_error": "rebuild_compensated",
            "worker_id": None,
            "claim_timeout_at": None,
            "next_retry_at": None,
        }
    )
    return result


def build_legacy_dead_letter_guard(
    *,
    board_id: str,
    checkpoint_started_at: str,
    target_rows: Sequence[Mapping[str, Any]],
    dlq_columns: Sequence[str],
    dlq_rows: Sequence[Sequence[Any]],
    present_original_queue_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Build the exact pre-v4 DLQ cut authorized by the nominal lane.

    Pre-v4 enqueue receipts did not persist a DLQ baseline.  We may reconstruct
    it only from the complete bounded board partition, and only when every
    same-identity peer demonstrably predates the failed rebuild.  The result is
    self-contained evidence carried by the durable intent and re-created both
    immediately before and inside the queue CAS transaction.
    """

    _require(
        tuple(dlq_columns) == LEGACY_DEAD_LETTER_COLUMNS,
        "legacy_queue_only_dead_letter_schema_invalid",
    )
    checkpoint_time = _parse_timestamp(
        checkpoint_started_at,
        code="legacy_queue_only_dead_letter_checkpoint_time_invalid",
    )
    target_queue_ids = tuple(sorted(str(row.get("id") or "") for row in target_rows))
    target_identities = tuple(
        sorted(
            {
                (
                    str(row.get("artifact_type") or ""),
                    str(row.get("artifact_id") or ""),
                )
                for row in target_rows
            }
        )
    )
    _require(
        len(target_queue_ids) == len(target_rows)
        and len(set(target_queue_ids)) == len(target_rows)
        and all(target_queue_ids)
        and len(target_identities) == len(target_rows)
        and all(all(identity) for identity in target_identities),
        "legacy_queue_only_dead_letter_target_invalid",
    )
    normalized_rows: list[tuple[Any, ...]] = []
    board_ids: list[str] = []
    peers: list[dict[str, Any]] = []
    peer_identities: set[tuple[str, str]] = set()
    target_id_set = set(target_queue_ids)
    target_identity_set = set(target_identities)
    seen_dlq_ids: set[str] = set()
    for raw in dlq_rows:
        row = tuple(raw)
        _require(
            len(row) == len(LEGACY_DEAD_LETTER_COLUMNS),
            "legacy_queue_only_dead_letter_row_invalid",
        )
        payload = dict(zip(LEGACY_DEAD_LETTER_COLUMNS, row, strict=True))
        dlq_id = str(payload.get("id") or "")
        identity = (
            str(payload.get("artifact_type") or ""),
            str(payload.get("artifact_id") or ""),
        )
        _require(
            payload.get("board_id") == board_id
            and _UUID_IDENTIFIER.fullmatch(dlq_id) is not None
            and dlq_id not in seen_dlq_ids,
            "legacy_queue_only_dead_letter_row_invalid",
        )
        seen_dlq_ids.add(dlq_id)
        board_ids.append(dlq_id)
        normalized_rows.append(row)
        original_queue_id = str(payload.get("original_queue_id") or "")
        _require(
            original_queue_id not in target_id_set,
            "legacy_queue_only_dead_letter_targets_current_queue",
        )
        if identity not in target_identity_set:
            continue
        _require(
            _UUID_IDENTIFIER.fullmatch(original_queue_id) is not None
            and original_queue_id not in present_original_queue_ids
            and type(payload.get("attempts")) is int
            and int(payload["attempts"]) > 0
            and isinstance(payload.get("errors"), str),
            "legacy_queue_only_dead_letter_peer_invalid",
        )
        created_at = _parse_timestamp(
            payload.get("created_at"),
            code="legacy_queue_only_dead_letter_peer_time_invalid",
        )
        dead_lettered_at = _parse_timestamp(
            payload.get("dead_lettered_at"),
            code="legacy_queue_only_dead_letter_peer_time_invalid",
        )
        _require(
            created_at <= dead_lettered_at < checkpoint_time,
            "legacy_queue_only_dead_letter_peer_not_historical",
        )
        try:
            errors = json.loads(str(payload["errors"]))
        except (TypeError, ValueError) as exc:
            raise LegacyQueueOnlyIntentError(
                "legacy_queue_only_dead_letter_peer_errors_invalid"
            ) from exc
        _require(
            isinstance(errors, list)
            and len(errors) == int(payload["attempts"])
            and bool(errors),
            "legacy_queue_only_dead_letter_peer_errors_invalid",
        )
        expected_error_keys = {
            "attempt",
            "occurred_at",
            "error_type",
            "message",
            "traceback",
            "recovery_class",
            "reason_code",
            "replay_safe",
            "correlation_id",
        }
        seen_correlations: set[str] = set()
        for expected_attempt, error in enumerate(errors, start=1):
            _require(
                isinstance(error, Mapping)
                and set(error) == expected_error_keys
                and type(error.get("attempt")) is int
                and error.get("attempt") == expected_attempt
                and isinstance(error.get("error_type"), str)
                and bool(error.get("error_type"))
                and isinstance(error.get("message"), str)
                and bool(error.get("message"))
                and error.get("traceback") is None
                and error.get("recovery_class") == "invalid_payload"
                and error.get("reason_code") == "kg_recovery.invalid_payload"
                and error.get("replay_safe") is False,
                "legacy_queue_only_dead_letter_peer_errors_invalid",
            )
            occurred_at = _parse_timestamp(
                error.get("occurred_at"),
                code="legacy_queue_only_dead_letter_peer_errors_invalid",
            )
            correlation_id = str(error.get("correlation_id") or "")
            _require(
                occurred_at < checkpoint_time
                and _UUID_IDENTIFIER.fullmatch(correlation_id) is not None
                and correlation_id not in seen_correlations,
                "legacy_queue_only_dead_letter_peer_errors_invalid",
            )
            seen_correlations.add(correlation_id)
        peers.append(
            {
                "id": dlq_id,
                "artifact_type": identity[0],
                "artifact_id": identity[1],
                "original_queue_id": original_queue_id,
                "created_at": str(payload["created_at"]),
                "dead_lettered_at": str(payload["dead_lettered_at"]),
                "row_sha256": canonical_evidence_hash(payload),
                "errors_sha256": hashlib.sha256(
                    str(payload["errors"]).encode("utf-8")
                ).hexdigest(),
                "original_queue_absent": True,
                "preexisting_before_checkpoint": True,
            }
        )
        peer_identities.add(identity)
    _require(
        board_ids == sorted(board_ids),
        "legacy_queue_only_dead_letter_order_invalid",
    )
    peers.sort(key=lambda peer: str(peer["id"]))
    identity_payload = [
        {"artifact_type": artifact_type, "artifact_id": artifact_id}
        for artifact_type, artifact_id in target_identities
    ]
    without_peer = [
        {"artifact_type": artifact_type, "artifact_id": artifact_id}
        for artifact_type, artifact_id in target_identities
        if (artifact_type, artifact_id) not in peer_identities
    ]
    return {
        "schema_version": LEGACY_DEAD_LETTER_GUARD_SCHEMA,
        "checkpoint_started_at": checkpoint_started_at,
        "columns": list(LEGACY_DEAD_LETTER_COLUMNS),
        "board_row_count": len(normalized_rows),
        "board_ids": board_ids,
        "snapshot_fingerprint": canonical_evidence_hash(
            {
                "columns": list(LEGACY_DEAD_LETTER_COLUMNS),
                "rows": [list(row) for row in normalized_rows],
            }
        ),
        "target_queue_ids": list(target_queue_ids),
        "target_identities": identity_payload,
        "peers": peers,
        "target_identities_without_peer": without_peer,
    }


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise LegacyQueueOnlyIntentError(code)


def _is_sha256(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_legacy_uuid4_report_id(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("report_"):
        return False
    raw = value.removeprefix("report_")
    try:
        parsed = UUID(raw)
    except (TypeError, ValueError):
        return False
    return (
        parsed.version == 4
        and parsed.variant == RFC_4122
        and value == f"report_{parsed.hex}"
    )


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    _require(isinstance(value, str) and bool(value), f"legacy_queue_only_{key}_invalid")
    return value


def _required_identifier(
    payload: Mapping[str, Any],
    key: str,
    *,
    prefix: str | None = None,
) -> str:
    value = _required_string(payload, key)
    _require(
        _SAFE_IDENTIFIER.fullmatch(value) is not None
        and (prefix is None or value.startswith(prefix)),
        f"legacy_queue_only_{key}_invalid",
    )
    return value


def _parse_timestamp(value: object, *, code: str) -> datetime:
    _require(isinstance(value, str) and bool(value.strip()), code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LegacyQueueOnlyIntentError(code) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _effect_relative(effect_key: str) -> str:
    digest = hashlib.sha256(effect_key.encode("utf-8")).hexdigest()[:24]
    return f"audit/f06-effect-{digest}.json"


def _exact_mapping(
    value: object,
    *,
    keys: frozenset[str],
    code: str,
) -> dict[str, Any]:
    _require(isinstance(value, Mapping), code)
    result = dict(value)
    _require(set(result) == set(keys), code)
    return result


def _validate_relative_json(value: object, *, prefix: str, code: str) -> str:
    _require(isinstance(value, str), code)
    normalized = str(value).replace("\\", "/")
    _require(
        normalized == value
        and normalized.startswith(prefix)
        and normalized.endswith(".json")
        and not normalized.startswith("/")
        and ".." not in normalized.split("/"),
        code,
    )
    return normalized


def _validate_f06_artifacts(
    value: object,
    *,
    f06_run_id: str,
) -> dict[str, dict[str, str]]:
    payload = _exact_mapping(
        value,
        keys=frozenset(_F06_ARTIFACT_LABELS),
        code="legacy_queue_only_f06_artifacts_invalid",
    )
    expected_suffixes = {
        "snapshot": "snapshot",
        "quarantine": "quarantine",
        "enqueue": "enqueue",
        "lease_lost_audit": "audit:lease_lost",
    }
    result: dict[str, dict[str, str]] = {}
    for label in _F06_ARTIFACT_LABELS:
        artifact = _exact_mapping(
            payload[label],
            keys=frozenset({"effect_key", "relative", "sha256"}),
            code="legacy_queue_only_f06_artifact_invalid",
        )
        expected_key = f"{f06_run_id}:{expected_suffixes[label]}"
        _require(
            artifact["effect_key"] == expected_key
            and artifact["relative"] == _effect_relative(expected_key)
            and _is_sha256(artifact["sha256"]),
            "legacy_queue_only_f06_artifact_invalid",
        )
        result[label] = {
            "effect_key": str(artifact["effect_key"]),
            "relative": str(artifact["relative"]),
            "sha256": str(artifact["sha256"]),
        }
    return result


def _validate_storage_hashes(value: object, *, code: str) -> dict[str, str]:
    _require(isinstance(value, Mapping) and bool(value), code)
    result: dict[str, str] = {}
    for raw_relative, raw_digest in value.items():
        _require(isinstance(raw_relative, str) and bool(raw_relative), code)
        relative = raw_relative.replace("\\", "/")
        _require(
            relative == raw_relative
            and not relative.startswith("/")
            and ".." not in relative.split("/"),
            code,
        )
        _require(_is_sha256(raw_digest), code)
        result[relative] = str(raw_digest)
    return dict(sorted(result.items()))


@dataclass(frozen=True, slots=True)
class LegacyManualRestoreQueueOnlyIntent:
    """Validated immutable wrapper around the exact portable intent payload."""

    payload: Mapping[str, Any]

    @classmethod
    def build(cls, evidence: Mapping[str, Any]) -> "LegacyManualRestoreQueueOnlyIntent":
        body = dict(evidence)
        body.pop("evidence_digest", None)
        body.pop("intent_digest", None)
        body.pop("recovery_run_id", None)
        f06_run_id = str(body.get("f06_run_id") or "")
        body["legacy_run_id"] = f06_run_id
        body["intent_ref"] = _effect_relative(
            f"{f06_run_id}:{LEGACY_QUEUE_ONLY_INTENT_EFFECT}"
        )
        digest = canonical_evidence_hash(body)
        body["recovery_run_id"] = f"legacy_reconcile_{digest[:24]}"
        body["evidence_digest"] = digest
        body["intent_digest"] = digest
        return cls.from_payload(body)

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
    ) -> "LegacyManualRestoreQueueOnlyIntent":
        normalized = _validate_payload(payload)
        return cls(MappingProxyType(normalized))

    @property
    def evidence_digest(self) -> str:
        return str(self.payload["evidence_digest"])

    @property
    def recovery_run_id(self) -> str:
        return str(self.payload["recovery_run_id"])

    @property
    def board_id(self) -> str:
        return str(self.payload["board_id"])

    @property
    def manifest_ref(self) -> str:
        return str(self.payload["manifest_ref"])

    @property
    def f06_run_id(self) -> str:
        return str(self.payload["f06_run_id"])

    @property
    def queue_source(self) -> str:
        return str(dict(self.payload["queue"])["source"])

    @property
    def queue_rows(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(dict(row) for row in dict(self.payload["queue"])["rows"])

    @property
    def memberships(self) -> tuple[Mapping[str, str], ...]:
        return tuple(
            {str(key): str(value) for key, value in dict(row).items()}
            for row in dict(self.payload["queue"])["memberships"]
        )

    @property
    def terminal_queue_fingerprint(self) -> str:
        memberships = {str(row["row_id"]): row for row in self.memberships}
        terminal_rows = [
            legacy_queue_terminal_row(row, memberships[str(row["id"])])
            for row in self.queue_rows
        ]
        return canonical_evidence_hash(
            {
                "columns": list(LEGACY_QUEUE_COLUMNS),
                "rows": [
                    [row[column] for column in LEGACY_QUEUE_COLUMNS]
                    for row in terminal_rows
                ],
            }
        )

    def to_payload(self) -> dict[str, Any]:
        return json.loads(_canonical_bytes(dict(self.payload)))


def _validate_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    _require(set(result) == set(_TOP_LEVEL_KEYS), "legacy_queue_only_shape_invalid")
    _require(
        result.get("schema_version") == LEGACY_QUEUE_ONLY_SCHEMA,
        "legacy_queue_only_schema_invalid",
    )
    _require(
        result.get("reconciliation_kind") == LEGACY_QUEUE_ONLY_KIND,
        "legacy_queue_only_kind_invalid",
    )
    board_id = _required_string(result, "board_id")
    _require(
        _UUID_IDENTIFIER.fullmatch(board_id) is not None,
        "legacy_queue_only_board_id_invalid",
    )
    manifest_ref = _required_identifier(
        result,
        "manifest_ref",
        prefix="rebuild_manifest_",
    )
    f06_run_id = _required_string(result, "f06_run_id")
    _require(
        f06_run_id == f"f06:{manifest_ref}", "legacy_queue_only_run_binding_invalid"
    )
    intent_effect_key = f"{f06_run_id}:{LEGACY_QUEUE_ONLY_INTENT_EFFECT}"
    _require(
        result.get("legacy_run_id") == f06_run_id
        and result.get("intent_ref") == _effect_relative(intent_effect_key),
        "legacy_queue_only_intent_ref_invalid",
    )
    for key in (
        "recovery_actor_id",
        "recovery_reason",
        "legacy_actor_id",
        "legacy_reason",
    ):
        _required_string(result, key)
    _required_identifier(result, "candidate_generation_id")

    checkpoint = _exact_mapping(
        result.get("checkpoint"),
        keys=frozenset({"relative", "sha256", "source_rows_sha256", "state"}),
        code="legacy_queue_only_checkpoint_invalid",
    )
    checkpoint_relative = _validate_relative_json(
        checkpoint["relative"],
        prefix="audit/f06-checkpoint-",
        code="legacy_queue_only_checkpoint_invalid",
    )
    _require(
        re.fullmatch(
            r"audit/f06-checkpoint-[0-9a-f]{24}\.json",
            checkpoint_relative,
        )
        is not None
        and checkpoint["state"] == "blocked"
        and _is_sha256(checkpoint["sha256"])
        and _is_sha256(checkpoint["source_rows_sha256"]),
        "legacy_queue_only_checkpoint_invalid",
    )
    _validate_f06_artifacts(result.get("f06_artifacts"), f06_run_id=f06_run_id)

    manifest = _exact_mapping(
        result.get("manifest"),
        keys=frozenset({"relative", "sha256", "preflight_hash", "source_set_hash"}),
        code="legacy_queue_only_manifest_invalid",
    )
    _require(
        manifest["relative"] == f"manifests/{manifest_ref}.json"
        and _is_sha256(manifest["sha256"])
        and _is_sha256(manifest["preflight_hash"])
        and _is_sha256(manifest["source_set_hash"]),
        "legacy_queue_only_manifest_invalid",
    )

    terminal = _exact_mapping(
        result.get("terminal_run"),
        keys=frozenset(
            {
                "run_id",
                "audit_relative",
                "audit_sha256",
                "report_id",
                "report_relative",
                "report_sha256",
                "confirmation_ref",
                "confirmation_audit_relative",
                "confirmation_audit_sha256",
                "outcome",
                "reason",
                "event_emitted",
                "previous_generation_id",
                "current_generation_id",
                "promotion_outcome",
                "current_generation_pointer_present",
                "candidate_decision_present",
            }
        ),
        code="legacy_queue_only_terminal_run_invalid",
    )
    terminal_run_id = _required_identifier(terminal, "run_id", prefix="run_")
    report_id = _required_identifier(terminal, "report_id", prefix="report_")
    confirmation_ref = _required_string(terminal, "confirmation_ref")
    confirmation_relative = _validate_relative_json(
        terminal["confirmation_audit_relative"],
        prefix=f"audit/confirmation/{board_id}/",
        code="legacy_queue_only_terminal_run_invalid",
    )
    _require(
        _is_legacy_uuid4_report_id(report_id)
        and terminal["audit_relative"] == f"audit/{terminal_run_id}.json"
        and terminal["report_relative"] == f"reports/{report_id}.json"
        and re.fullmatch(
            rf"audit/confirmation/{re.escape(board_id)}/"
            r"audit_[0-9a-f]{32}\.json",
            confirmation_relative,
        )
        is not None
        and confirmation_ref.startswith("conf_fp_")
        and len(confirmation_ref) == len("conf_fp_") + 64
        and _is_sha256(confirmation_ref.removeprefix("conf_fp_"))
        and _is_sha256(terminal["audit_sha256"])
        and _is_sha256(terminal["report_sha256"])
        and _is_sha256(terminal["confirmation_audit_sha256"])
        and terminal["outcome"] == "rebuild_failed"
        and terminal["reason"] == "lease_lost"
        and terminal["event_emitted"] is True
        and terminal["previous_generation_id"] is None
        and terminal["current_generation_id"] is None
        and terminal["promotion_outcome"] is None
        and terminal["current_generation_pointer_present"] is False
        and terminal["candidate_decision_present"] is False,
        "legacy_queue_only_terminal_run_invalid",
    )

    original = _exact_mapping(
        result.get("original_quarantine"),
        keys=frozenset(
            {"quarantine_id", "manifest_relative", "manifest_sha256", "storage_hashes"}
        ),
        code="legacy_queue_only_original_quarantine_invalid",
    )
    original_id = _required_identifier(
        original,
        "quarantine_id",
        prefix="q_",
    )
    _require(
        re.fullmatch(r"q_[A-Za-z0-9_-]{22}", original_id) is not None
        and original["manifest_relative"] == f"{original_id}/manifest.json"
        and _is_sha256(original["manifest_sha256"]),
        "legacy_queue_only_original_quarantine_invalid",
    )
    original_storage = _validate_storage_hashes(
        original["storage_hashes"],
        code="legacy_queue_only_original_quarantine_invalid",
    )
    _require(
        set(original_storage) == {f"{original_id}/graph.lbug"},
        "legacy_queue_only_original_quarantine_invalid",
    )

    manual = _exact_mapping(
        result.get("manual_restore"),
        keys=frozenset(
            {
                "quarantine_id",
                "manifest_relative",
                "manifest_sha256",
                "journal_relative",
                "journal_sha256",
                "source_quarantine_id",
                "storage_hashes",
            }
        ),
        code="legacy_queue_only_manual_restore_invalid",
    )
    manual_id = _required_identifier(
        manual,
        "quarantine_id",
        prefix="q_",
    )
    _require(
        re.fullmatch(r"q_[A-Za-z0-9_-]{22}", manual_id) is not None
        and manual["source_quarantine_id"] == original_id
        and manual["manifest_relative"] == f"{manual_id}/manifest.json"
        and manual["journal_relative"] == f"{manual_id}/restore_operation.json"
        and _is_sha256(manual["manifest_sha256"])
        and _is_sha256(manual["journal_sha256"]),
        "legacy_queue_only_manual_restore_invalid",
    )
    manual_storage = _validate_storage_hashes(
        manual["storage_hashes"],
        code="legacy_queue_only_manual_restore_invalid",
    )
    _require(
        set(manual_storage)
        == {f"{manual_id}/graph.lbug", f"{manual_id}/graph.lbug.wal"},
        "legacy_queue_only_manual_restore_invalid",
    )

    board_storage = _exact_mapping(
        result.get("board_storage"),
        keys=frozenset({"hashes", "sha256"}),
        code="legacy_queue_only_board_storage_invalid",
    )
    board_hashes = _validate_storage_hashes(
        board_storage["hashes"],
        code="legacy_queue_only_board_storage_invalid",
    )
    _require(
        "graph.lbug" in board_hashes
        and set(board_hashes) <= {"graph.lbug", "graph.lbug.wal"}
        and board_storage["sha256"] == canonical_evidence_hash(board_hashes),
        "legacy_queue_only_board_storage_invalid",
    )

    _require(
        result.get("prefix_schema") == LEGACY_QUEUE_ONLY_PREFIX_SCHEMA,
        "legacy_queue_only_prefix_schema_invalid",
    )
    dead_letter_guard = _exact_mapping(
        result.get("dead_letter_guard"),
        keys=_DEAD_LETTER_GUARD_KEYS,
        code="legacy_queue_only_dead_letter_guard_invalid",
    )
    checkpoint_started_at = _parse_timestamp(
        dead_letter_guard.get("checkpoint_started_at"),
        code="legacy_queue_only_dead_letter_guard_invalid",
    )
    _require(
        dead_letter_guard.get("schema_version") == LEGACY_DEAD_LETTER_GUARD_SCHEMA
        and tuple(dead_letter_guard.get("columns") or ()) == LEGACY_DEAD_LETTER_COLUMNS
        and type(dead_letter_guard.get("board_row_count")) is int
        and 0 <= int(dead_letter_guard["board_row_count"]) <= MAX_LEGACY_QUEUE_ROWS * 4
        and isinstance(dead_letter_guard.get("board_ids"), list)
        and len(dead_letter_guard["board_ids"])
        == int(dead_letter_guard["board_row_count"])
        and dead_letter_guard["board_ids"] == sorted(dead_letter_guard["board_ids"])
        and len(set(dead_letter_guard["board_ids"]))
        == len(dead_letter_guard["board_ids"])
        and all(
            isinstance(value, str) and _UUID_IDENTIFIER.fullmatch(value) is not None
            for value in dead_letter_guard["board_ids"]
        )
        and _is_sha256(dead_letter_guard.get("snapshot_fingerprint"))
        and isinstance(dead_letter_guard.get("target_queue_ids"), list)
        and isinstance(dead_letter_guard.get("target_identities"), list)
        and isinstance(dead_letter_guard.get("peers"), list)
        and isinstance(dead_letter_guard.get("target_identities_without_peer"), list),
        "legacy_queue_only_dead_letter_guard_invalid",
    )

    def _guard_identity(raw: object) -> tuple[str, str]:
        identity = _exact_mapping(
            raw,
            keys=_DEAD_LETTER_IDENTITY_KEYS,
            code="legacy_queue_only_dead_letter_guard_invalid",
        )
        artifact_type = str(identity.get("artifact_type") or "")
        artifact_id = str(identity.get("artifact_id") or "")
        _require(
            _SAFE_IDENTIFIER.fullmatch(artifact_type) is not None
            and _SAFE_IDENTIFIER.fullmatch(artifact_id) is not None,
            "legacy_queue_only_dead_letter_guard_invalid",
        )
        return artifact_type, artifact_id

    guard_identities = tuple(
        _guard_identity(raw) for raw in dead_letter_guard["target_identities"]
    )
    guard_without_peer = tuple(
        _guard_identity(raw)
        for raw in dead_letter_guard["target_identities_without_peer"]
    )
    _require(
        guard_identities == tuple(sorted(set(guard_identities)))
        and guard_without_peer == tuple(sorted(set(guard_without_peer))),
        "legacy_queue_only_dead_letter_guard_invalid",
    )
    guard_peers: list[tuple[str, str]] = []
    seen_peer_ids: set[str] = set()
    for raw_peer in dead_letter_guard["peers"]:
        peer = _exact_mapping(
            raw_peer,
            keys=_DEAD_LETTER_PEER_KEYS,
            code="legacy_queue_only_dead_letter_guard_invalid",
        )
        peer_id = str(peer.get("id") or "")
        identity = (
            str(peer.get("artifact_type") or ""),
            str(peer.get("artifact_id") or ""),
        )
        original_queue_id = str(peer.get("original_queue_id") or "")
        _require(
            _UUID_IDENTIFIER.fullmatch(peer_id) is not None
            and peer_id in dead_letter_guard["board_ids"]
            and peer_id not in seen_peer_ids
            and identity in guard_identities
            and _UUID_IDENTIFIER.fullmatch(original_queue_id) is not None
            and original_queue_id not in dead_letter_guard["target_queue_ids"]
            and _parse_timestamp(
                peer.get("created_at"),
                code="legacy_queue_only_dead_letter_guard_invalid",
            )
            <= _parse_timestamp(
                peer.get("dead_lettered_at"),
                code="legacy_queue_only_dead_letter_guard_invalid",
            )
            < checkpoint_started_at
            and _is_sha256(peer.get("row_sha256"))
            and _is_sha256(peer.get("errors_sha256"))
            and peer.get("original_queue_absent") is True
            and peer.get("preexisting_before_checkpoint") is True,
            "legacy_queue_only_dead_letter_guard_invalid",
        )
        seen_peer_ids.add(peer_id)
        guard_peers.append(identity)
    _require(
        [str(peer["id"]) for peer in dead_letter_guard["peers"]]
        == sorted(str(peer["id"]) for peer in dead_letter_guard["peers"])
        and set(guard_without_peer) == set(guard_identities) - set(guard_peers),
        "legacy_queue_only_dead_letter_guard_invalid",
    )

    queue = _exact_mapping(
        result.get("queue"),
        keys=frozenset(
            {
                "source",
                "snapshot_fingerprint",
                "rows",
                "memberships",
                "board_non_target_fingerprint",
                "dlq_fingerprint",
            }
        ),
        code="legacy_queue_only_queue_invalid",
    )
    source = _required_string(queue, "source")
    _require(
        source == f"rebuild:{manifest_ref}"
        and all(
            _is_sha256(queue[key])
            for key in (
                "snapshot_fingerprint",
                "board_non_target_fingerprint",
                "dlq_fingerprint",
            )
        ),
        "legacy_queue_only_queue_invalid",
    )
    rows_value = queue.get("rows")
    memberships_value = queue.get("memberships")
    _require(
        isinstance(rows_value, Sequence)
        and not isinstance(rows_value, (str, bytes))
        and isinstance(memberships_value, Sequence)
        and not isinstance(memberships_value, (str, bytes))
        and 0 < len(rows_value) <= MAX_LEGACY_QUEUE_ROWS
        and len(rows_value) == len(memberships_value),
        "legacy_queue_only_queue_invalid",
    )
    rows: list[dict[str, Any]] = []
    memberships: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_identities: set[tuple[str, str]] = set()
    for raw_row, raw_membership in zip(rows_value, memberships_value, strict=True):
        row = _exact_mapping(
            raw_row,
            keys=_QUEUE_ROW_KEYS,
            code="legacy_queue_only_queue_row_invalid",
        )
        membership = _exact_mapping(
            raw_membership,
            keys=_MEMBERSHIP_KEYS,
            code="legacy_queue_only_membership_invalid",
        )
        row_id = _required_identifier(row, "id")
        artifact_type = _required_identifier(row, "artifact_type")
        artifact_id = _required_identifier(row, "artifact_id")
        identity = (artifact_type, artifact_id)
        triggered_at = _parse_timestamp(
            row["triggered_at"],
            code="legacy_queue_only_queue_triggered_at_invalid",
        )
        _require(
            row_id not in seen_ids
            and identity not in seen_identities
            and row["board_id"] == board_id
            and row["work_kind"] == "consolidate"
            and type(row["generation"]) is int
            and row["generation"] == 0
            and row["delete_event_id"] is None
            and row["priority"] == "high"
            and row["source"] == source
            and row["status"] in {"pending", "claimed"}
            and row["payload"] is None
            and type(row["attempts"]) is int
            and row["attempts"] == 0
            and row["last_error"] is None
            and row["triggered_by_event"] is None
            and row["next_retry_at"] is None,
            "legacy_queue_only_queue_row_invalid",
        )
        claim_values = tuple(
            row[key]
            for key in (
                "claimed_by_session_id",
                "claim_token",
                "claimed_at",
                "worker_id",
                "claim_timeout_at",
            )
        )
        _require(
            all(value is None for value in claim_values)
            if row["status"] == "pending"
            else (
                all(isinstance(value, str) and bool(value) for value in claim_values)
                and row["worker_id"] == row["claimed_by_session_id"]
                and re.fullmatch(r"[0-9a-f]{32}", str(row["claim_token"])) is not None
                and _parse_timestamp(
                    row["claim_timeout_at"],
                    code="legacy_queue_only_queue_claim_invalid",
                )
                > _parse_timestamp(
                    row["claimed_at"],
                    code="legacy_queue_only_queue_claim_invalid",
                )
                >= triggered_at
            ),
            "legacy_queue_only_queue_claim_invalid",
        )
        _require(
            all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in membership.items()
            ),
            "legacy_queue_only_membership_invalid",
        )
        normalized_membership = {
            str(key): str(value) for key, value in membership.items()
        }
        _require(
            normalized_membership["row_id"] == row_id
            and normalized_membership["artifact_type"] == artifact_type
            and normalized_membership["artifact_id"] == artifact_id
            and normalized_membership["run_id"] == manifest_ref
            and all(
                normalized_membership[key]
                for key in ("source_ref", "source_version", "content_hash")
            )
            and _is_sha256(normalized_membership["content_hash"]),
            "legacy_queue_only_membership_invalid",
        )
        seen_ids.add(row_id)
        seen_identities.add(identity)
        rows.append(row)
        memberships.append(normalized_membership)

    _require(
        [str(row["id"]) for row in rows] == sorted(str(row["id"]) for row in rows),
        "legacy_queue_only_queue_order_invalid",
    )

    _require(
        dead_letter_guard["target_queue_ids"] == sorted(str(row["id"]) for row in rows)
        and guard_identities
        == tuple(
            sorted((str(row["artifact_type"]), str(row["artifact_id"])) for row in rows)
        )
        and queue["dlq_fingerprint"] == dead_letter_guard["snapshot_fingerprint"],
        "legacy_queue_only_dead_letter_queue_binding_invalid",
    )

    _require(
        queue["snapshot_fingerprint"]
        == canonical_evidence_hash({"rows": rows, "memberships": memberships}),
        "legacy_queue_only_queue_fingerprint_invalid",
    )
    _require(
        tuple(result.get("preapplied_actions") or ())
        == LEGACY_QUEUE_ONLY_PREAPPLIED_ACTIONS
        and tuple(result.get("remaining_actions") or ())
        == LEGACY_QUEUE_ONLY_REMAINING_ACTIONS,
        "legacy_queue_only_actions_invalid",
    )

    digest = result.get("evidence_digest")
    intent_digest = result.get("intent_digest")
    recovery_run_id = result.get("recovery_run_id")
    _require(
        _is_sha256(digest)
        and intent_digest == digest
        and recovery_run_id == f"legacy_reconcile_{str(digest)[:24]}",
        "legacy_queue_only_digest_invalid",
    )
    digest_body = dict(result)
    digest_body.pop("evidence_digest")
    digest_body.pop("intent_digest")
    digest_body.pop("recovery_run_id")
    _require(
        digest == canonical_evidence_hash(digest_body),
        "legacy_queue_only_digest_invalid",
    )
    # Round-trip canonical JSON here so callers never retain mutable nested
    # aliases supplied by an external fixture or artifact reader.
    return json.loads(_canonical_bytes(result))


__all__ = [
    "LEGACY_DEAD_LETTER_COLUMNS",
    "LEGACY_DEAD_LETTER_GUARD_SCHEMA",
    "LEGACY_QUEUE_ONLY_INTENT_CODE",
    "LEGACY_QUEUE_ONLY_INTENT_EFFECT",
    "LEGACY_QUEUE_ONLY_KIND",
    "LEGACY_QUEUE_ONLY_PREAPPLIED_ACTIONS",
    "LEGACY_QUEUE_ONLY_PREFIX_SCHEMA",
    "LEGACY_QUEUE_ONLY_REMAINING_ACTIONS",
    "LEGACY_QUEUE_ONLY_SCHEMA",
    "LEGACY_QUEUE_COLUMNS",
    "LegacyManualRestoreQueueOnlyIntent",
    "LegacyQueueOnlyIntentError",
    "build_legacy_dead_letter_guard",
    "canonical_evidence_hash",
    "legacy_queue_terminal_row",
]
