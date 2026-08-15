"""Community-owned production rebuild ingestion adapter for KG-02.

Wires the KG-02 rebuild service to the existing consolidation pipeline
without reimplementing materialization. Strategy is **enqueue-then-wake**:

1. The rebuild service holds the KG-01 admin single-writer lock.
2. This adapter receives the source set already enumerated by
   the BoardSourceReader port (KG-02.2 ``RebuildSourceEnumerator`` passes
   it forward through ``sources_payload``).
3. For each source row we UPSERT into ``ConsolidationQueue`` with an explicit
   rebuild fence (insert if new; reset/resequence pending and terminal rows;
   revoke any stale claimed token before the admin lease is delegated). Rows use
   high priority because explicit recovery must not sit behind unrelated
   corrupt-board backlog.
4. We signal the consolidation worker so it picks up the new rows
   without waiting for its heartbeat.
5. The structural_hash / source_hash come from KG-02.5
   ``DeterministicStructuralRebuilder`` over the same source set so the
   rebuild report carries a deterministic receipt.

Trade-off documented:
* The adapter returns ``ok=True`` as soon as the rows are enqueued.
  Actual KG mutation happens asynchronously inside the consolidation
  worker (which has its own per-board commit lock that nests safely
  inside the KG-01 admin lock — the existing worker already serialises
  board-by-board). For E2E we expose a ``drain_until_idle`` helper that
  can be wired by callers that want synchronous wait-until-done
  semantics.
* The adapter uses stdlib ``sqlite3`` for the UPSERT because the KG-01
  ``rebuild_step_adapter`` callable is synchronous. The same SQLite
  file is shared with the async SQLAlchemy engine; readers and writers
  are serialised by SQLite's own file-level locks.
"""

from __future__ import annotations

import heapq
import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from okto_pulse.core.kg.async_bridge import run_async_blocking
from okto_pulse.core.kg.board_rebuild_adapter import (
    DETERMINISTIC_SOURCE_ARTIFACT_TYPES,
    expected_layers_from_sources,
    queue_artifact_type,
    rebuild_source_order_key,
)
from okto_pulse.core.kg.interfaces.graph_lifecycle import PurgeReport

from okto_pulse.community.adapters.board_source_reader import (
    CommunityBoardSourceReader,
    resolve_pulse_db_path,
)
from okto_pulse.community.adapters.legacy_rebuild_reconciliation import (
    LEGACY_DEAD_LETTER_COLUMNS,
    LEGACY_QUEUE_COLUMNS,
    LegacyManualRestoreQueueOnlyIntent,
    build_legacy_dead_letter_guard,
    canonical_evidence_hash,
    legacy_queue_terminal_row,
)

logger = logging.getLogger("okto_pulse.community.board_rebuild_ingestion")
REBUILD_QUEUE_ORDER_VERSION = 4
_EVIDENCE_CLOSURE_CANDIDATE = "code_evidence_supersedence"
_MAX_EVIDENCE_CLOSURE_DEPTH = 256
_MAX_LEGACY_PROTECTED_ROWS = 16_384
_MAX_LEGACY_PROTECTED_BYTES = 32 * 1024 * 1024
_REBUILD_SOURCE_OPERATIONAL_MARKERS = frozenset(
    {
        "_rebuild_manifest_created_at",
        "_rebuild_dependency_closure",
    }
)
_REBUILD_CHECKPOINT_UPGRADE_STATES = frozenset(
    {
        "planned",
        "snapshotted",
        "quarantined",
        "enqueued",
        "draining",
    }
)


def _bounded_legacy_sql_rows(
    cursor: sqlite3.Cursor,
    columns: Sequence[str],
    *,
    code: str,
    max_rows: int | None = None,
) -> tuple[tuple[object, ...], ...]:
    """Materialize one SQL evidence cut under explicit row/byte limits."""

    rows: list[tuple[object, ...]] = []
    total_bytes = 0
    row_limit = _MAX_LEGACY_PROTECTED_ROWS if max_rows is None else max_rows
    for raw in cursor:
        if len(rows) >= row_limit:
            raise RuntimeError(f"{code}_row_limit_exceeded")
        values = tuple(raw[column] for column in columns)
        try:
            encoded = json.dumps(
                values,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{code}_noncanonical") from exc
        total_bytes += len(encoded)
        if total_bytes > _MAX_LEGACY_PROTECTED_BYTES:
            raise RuntimeError(f"{code}_byte_limit_exceeded")
        rows.append(values)
    return tuple(rows)


def _manifest_cut(rows: Sequence[Mapping[str, Any]]) -> str | None:
    """Return one non-empty manifest cut shared by every supplied row."""

    cuts = {str(row.get("_rebuild_manifest_created_at") or "").strip() for row in rows}
    if len(cuts) != 1 or "" in cuts:
        return None
    return next(iter(cuts))


def _source_upgrade_identity(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(row.get(field) or "")
        for field in (
            "artifact_type",
            "id",
            "source_ref",
            "source_version",
            "content_hash",
        )
    )


def _normalized_upgrade_source(row: Mapping[str, Any]) -> str:
    return json.dumps(
        {
            key: value
            for key, value in dict(row).items()
            if key not in _REBUILD_SOURCE_OPERATIONAL_MARKERS
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _checkpoint_source_upgrade_allowed(
    checkpoint: Any,
    sources: Sequence[Mapping[str, Any]],
) -> bool:
    """Prove a bounded pre-v4 command upgrade without changing its cut.

    The persisted denominator must be byte-equivalent after removing only
    operational markers.  Current extras may only be resolver-validated
    historical Evidence closure rows.  Snapshot/quarantine receipts stay
    authoritative and the old enqueue receipt is replayed once at v4.
    """

    if str(getattr(checkpoint.state, "value", checkpoint.state)) not in (
        _REBUILD_CHECKPOINT_UPGRADE_STATES
    ):
        return False
    receipts = tuple(checkpoint.receipts.values())
    if any(
        receipt.effect in {"restore", "promote", "compensate"} for receipt in receipts
    ):
        return False
    enqueue = next(
        (receipt for receipt in receipts if receipt.effect == "enqueue"),
        None,
    )
    if (
        enqueue is not None
        and int(dict(enqueue.details).get("queue_order_version", 0))
        >= REBUILD_QUEUE_ORDER_VERSION
    ):
        return False
    if enqueue is not None and "baseline_dead_letter_ids" not in dict(enqueue.details):
        return False

    old_rows = tuple(dict(row) for row in checkpoint.command.source_rows)
    new_denominator = tuple(
        dict(row) for row in sources if not row.get("_rebuild_dependency_closure")
    )
    closure = tuple(
        dict(row) for row in sources if row.get("_rebuild_dependency_closure")
    )
    if any(
        row.get("_rebuild_dependency_closure") != _EVIDENCE_CLOSURE_CANDIDATE
        or row.get("artifact_type") != "code_evidence"
        or row.get("source_artifact_status") != "superseded"
        or row.get("disposition") != "skipped_expired_working"
        for row in closure
    ):
        return False
    denominator_cut = _manifest_cut(new_denominator)
    if denominator_cut is None or (
        closure and _manifest_cut(closure) != denominator_cut
    ):
        return False

    denominator_evidence = {
        str(row.get("id") or ""): row
        for row in new_denominator
        if row.get("artifact_type") == "code_evidence" and row.get("id")
    }
    closure_by_id = {str(row.get("id") or ""): row for row in closure if row.get("id")}
    reachable_closure: set[str] = set()
    frontier = [
        str(row.get("supersedes_evidence_id") or "")
        for row in denominator_evidence.values()
        if row.get("supersedes_evidence_id")
    ]
    traversed: set[str] = set()
    while frontier:
        evidence_id = frontier.pop()
        if evidence_id in traversed:
            continue
        traversed.add(evidence_id)
        historical = closure_by_id.get(evidence_id)
        if historical is None:
            continue
        reachable_closure.add(evidence_id)
        predecessor = str(historical.get("supersedes_evidence_id") or "")
        if predecessor:
            frontier.append(predecessor)
    if reachable_closure != set(closure_by_id):
        return False

    def indexed(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[str, ...], str] | None:
        result: dict[tuple[str, ...], str] = {}
        for row in rows:
            identity = _source_upgrade_identity(row)
            if not all(identity) or identity in result:
                return None
            result[identity] = _normalized_upgrade_source(row)
        return result

    old_index = indexed(old_rows)
    current_index = indexed(new_denominator)
    return old_index is not None and old_index == current_index


def _source_identity(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("source_ref") or ""),
        str(row.get("source_version") or ""),
        str(row.get("content_hash") or ""),
        str(row.get("source_artifact_status") or row.get("status") or ""),
    )


def _resolve_evidence_dependency_closure(
    *,
    db_path: Path,
    board_id: str,
    sources: Sequence[Mapping[str, Any]],
) -> tuple[tuple[dict[str, Any], ...], int]:
    """Select the manifest-bound historical evidence closure.

    Candidates are never trusted as payload authority.  The current Community
    source projection must match their immutable manifest identity exactly;
    only recursively referenced ``superseded`` predecessors are admitted.
    They remain working historical roots and are marked separately so report
    denominators exclude them while structural/source hashes still bind them.
    """

    base_rows: list[dict[str, Any]] = []
    candidates: dict[str, dict[str, Any]] = {}
    for raw in sources:
        row = dict(raw)
        marker = row.pop("_rebuild_dependency_closure_candidate", None)
        if marker is None:
            base_rows.append(row)
            continue
        if marker != _EVIDENCE_CLOSURE_CANDIDATE:
            raise RuntimeError("rebuild_dependency_closure_candidate_invalid")
        evidence_id = str(row.get("id") or "")
        if (
            not evidence_id
            or row.get("artifact_type") != "code_evidence"
            or row.get("source_artifact_status") != "superseded"
            or row.get("disposition") != "skipped_expired_working"
            or evidence_id in candidates
        ):
            raise RuntimeError("rebuild_evidence_closure_candidate_invalid")
        candidates[evidence_id] = row

    if candidates:
        denominator_cut = _manifest_cut(base_rows)
        if (
            denominator_cut is None
            or _manifest_cut(tuple(candidates.values())) != denominator_cut
        ):
            raise RuntimeError("rebuild_evidence_closure_manifest_drift")

    evidence_bases = {
        str(row.get("id")): row
        for row in base_rows
        if row.get("artifact_type") == "code_evidence" and row.get("id")
    }
    if not evidence_bases:
        return tuple(base_rows), 0

    snapshot = CommunityBoardSourceReader(db_path=db_path).fetch(board_id)
    if not snapshot.complete:
        raise RuntimeError(
            f"rebuild_evidence_closure_source_unavailable:{snapshot.cause or 'unknown'}"
        )
    current = {
        str(row.get("id")): dict(row)
        for row in snapshot.rows
        if row.get("artifact_type") == "code_evidence" and row.get("id")
    }

    # Materializable manifest identities are revalidated by the outer service,
    # but proving them here keeps this closure fail-closed when the adapter is
    # invoked directly or a relational mutation races command construction.
    for evidence_id, manifest_row in evidence_bases.items():
        live = current.get(evidence_id)
        if live is None or _source_identity(live) != _source_identity(manifest_row):
            raise RuntimeError("rebuild_evidence_source_identity_drift")

    selected: dict[str, dict[str, Any]] = {}
    for starting_id in sorted(evidence_bases):
        cursor = starting_id
        path: set[str] = {starting_id}
        for _depth in range(_MAX_EVIDENCE_CLOSURE_DEPTH):
            live = current.get(cursor)
            if live is None:
                raise RuntimeError("rebuild_evidence_source_missing")
            predecessor = str(live.get("supersedes_evidence_id") or "")
            if not predecessor:
                break
            if predecessor in path:
                raise RuntimeError("rebuild_evidence_supersedence_cycle")
            path.add(predecessor)
            if predecessor in evidence_bases:
                cursor = predecessor
                continue
            candidate = candidates.get(predecessor)
            predecessor_live = current.get(predecessor)
            if candidate is None or predecessor_live is None:
                with sqlite3.connect(str(db_path), timeout=5.0) as conn:
                    other_board = conn.execute(
                        "SELECT board_id FROM code_evidence WHERE id=?",
                        (predecessor,),
                    ).fetchone()
                if other_board is not None and str(other_board[0]) != board_id:
                    raise RuntimeError("rebuild_evidence_predecessor_cross_board")
                raise RuntimeError("rebuild_evidence_predecessor_missing")
            if (
                predecessor_live.get("artifact_type") != "code_evidence"
                or str(predecessor_live.get("source_artifact_status") or "")
                != "superseded"
                or _source_identity(predecessor_live) != _source_identity(candidate)
            ):
                raise RuntimeError("rebuild_evidence_predecessor_identity_drift")
            closure = dict(candidate)
            closure["_rebuild_dependency_closure"] = _EVIDENCE_CLOSURE_CANDIDATE
            selected[predecessor] = closure
            cursor = predecessor
        else:
            raise RuntimeError("rebuild_evidence_closure_depth_exceeded")

    return tuple(base_rows + [selected[key] for key in sorted(selected)]), len(selected)


def _spec_topological_positions(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    spec_ids: set[str],
) -> dict[str, int]:
    if not spec_ids:
        return {}
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='spec_dependencies'"
    ).fetchone()
    if table_exists is None:
        return {spec_id: index for index, spec_id in enumerate(sorted(spec_ids))}

    dependencies: dict[str, set[str]] = {spec_id: set() for spec_id in spec_ids}
    dependents: dict[str, set[str]] = {spec_id: set() for spec_id in spec_ids}
    rows = conn.execute(
        "SELECT dependent_spec_id, "
        "COALESCE(prerequisite_spec_id, prerequisite_spec_ref) "
        "FROM spec_dependencies WHERE board_id=? AND active=1",
        (board_id,),
    ).fetchall()
    for dependent_raw, prerequisite_raw in rows:
        dependent = str(dependent_raw or "")
        prerequisite = str(prerequisite_raw or "")
        if dependent not in spec_ids:
            continue
        if prerequisite not in spec_ids:
            raise RuntimeError("rebuild_spec_dependency_prerequisite_missing")
        if prerequisite in dependencies[dependent]:
            continue
        dependencies[dependent].add(prerequisite)
        dependents[prerequisite].add(dependent)

    ready = [spec_id for spec_id, parents in dependencies.items() if not parents]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        current = heapq.heappop(ready)
        ordered.append(current)
        for dependent in sorted(dependents[current]):
            dependencies[dependent].discard(current)
            if not dependencies[dependent]:
                heapq.heappush(ready, dependent)
    if len(ordered) != len(spec_ids):
        raise RuntimeError("rebuild_spec_dependency_cycle")
    return {spec_id: index for index, spec_id in enumerate(ordered)}


def _evidence_topological_positions(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    evidence_ids: set[str],
) -> dict[str, int]:
    """Order immutable evidence predecessors before their successors."""

    if not evidence_ids:
        return {}
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='code_evidence'"
    ).fetchone()
    if table_exists is None:
        return {
            evidence_id: index for index, evidence_id in enumerate(sorted(evidence_ids))
        }

    dependencies: dict[str, set[str]] = {
        evidence_id: set() for evidence_id in evidence_ids
    }
    dependents: dict[str, set[str]] = {
        evidence_id: set() for evidence_id in evidence_ids
    }
    rows = conn.execute(
        "SELECT id, supersedes_evidence_id FROM code_evidence "
        "WHERE board_id=? AND id IN (" + ",".join("?" for _ in evidence_ids) + ")",
        (board_id, *sorted(evidence_ids)),
    ).fetchall()
    if {str(row[0]) for row in rows} != evidence_ids:
        raise RuntimeError("rebuild_evidence_source_missing")
    for successor_raw, predecessor_raw in rows:
        successor = str(successor_raw or "")
        predecessor = str(predecessor_raw or "")
        if not predecessor:
            continue
        if predecessor not in evidence_ids:
            raise RuntimeError("rebuild_evidence_predecessor_missing")
        dependencies[successor].add(predecessor)
        dependents[predecessor].add(successor)

    ready = [
        evidence_id
        for evidence_id, predecessors in dependencies.items()
        if not predecessors
    ]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        current = heapq.heappop(ready)
        ordered.append(current)
        for successor in sorted(dependents[current]):
            dependencies[successor].discard(current)
            if not dependencies[successor]:
                heapq.heappush(ready, successor)
    if len(ordered) != len(evidence_ids):
        raise RuntimeError("rebuild_evidence_supersedence_cycle")
    return {evidence_id: index for index, evidence_id in enumerate(ordered)}


def _ordered_rebuild_sources(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    sources: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    spec_ids = {
        str(row.get("id", ""))
        for row in sources
        if str(row.get("artifact_type", "")) == "spec" and row.get("id")
    }
    spec_positions = _spec_topological_positions(
        conn,
        board_id=board_id,
        spec_ids=spec_ids,
    )
    evidence_ids = {
        str(row.get("id", ""))
        for row in sources
        if str(row.get("artifact_type", "")) == "code_evidence" and row.get("id")
    }
    evidence_positions = _evidence_topological_positions(
        conn,
        board_id=board_id,
        evidence_ids=evidence_ids,
    )

    def _key(row: Mapping[str, Any]) -> tuple[int, int, str, str]:
        dependency_rank, artifact_type, artifact_id = rebuild_source_order_key(row)
        source_type = str(row.get("artifact_type", ""))
        if source_type == "spec":
            within_type = spec_positions.get(artifact_id, 0)
        elif source_type == "code_evidence":
            within_type = evidence_positions.get(artifact_id, 0)
        else:
            within_type = 0
        return dependency_rank, within_type, artifact_type, artifact_id

    return sorted(sources, key=_key)


@dataclass(frozen=True, slots=True)
class CommunityBoardRebuildIngestionAdapter:
    """Sync rebuild step adapter that enqueues sources for the existing
    consolidation worker to drain. Produces a deterministic structural
    hash + source hash via KG-02.5 primitives so the rebuild report and
    KG-02.4 promotion path receive a real receipt — not a stub one."""

    db_path: Path | None = None
    db_path_provider: Callable[[], Path] | None = None
    drain_timeout_seconds: float = 900.0
    drain_poll_interval_seconds: float = 0.5
    drain_final_grace_seconds: float = 180.0
    drain_low_depth_threshold: int = 10
    # Teto ABSOLUTO do drain (campo 2026-06-10): o timeout acima é a janela
    # de ESTAGNAÇÃO (sem progresso), não o teto total. Um board grande
    # (520+ sources a ~6 entries/min) leva >1h para drenar — com teto fixo
    # de 15min o rebuild SEMPRE falhava (queue_drain_timeout), a generation
    # nunca promovia e o cognitive pending nunca materializava (sem badges),
    # apesar de o worker completar o grafo minutos depois. Enquanto a fila
    # PROGRIDE o drain continua; este teto só protege contra um worker
    # zumbi que progride para sempre sem terminar.
    drain_hard_timeout_seconds: float = 14400.0
    artifact_store: Any | None = None
    quarantine_restore: Any | None = None
    salvage_pending_provider: Callable[[str], bool] | None = None
    # B14 is a distinct deterministic derivative of relational policy
    # authority.  Production composition always injects this callback; the
    # optional default exists only for legacy/unit construction of this
    # adapter and never routes policy rows through the cognitive queue.
    policy_constraint_rebuild: Callable[[str], Any] | None = None
    _rebuild_effect_cache: dict[str, Any] = field(
        default_factory=dict, compare=False, repr=False
    )
    _rebuild_checkpoint_cache: dict[str, Any] = field(
        default_factory=dict, compare=False, repr=False
    )
    _rebuild_run_boards: dict[str, str] = field(
        default_factory=dict, compare=False, repr=False
    )

    def _path(self) -> Path:
        if self.db_path is not None:
            return Path(self.db_path)
        if self.db_path_provider is not None:
            return Path(self.db_path_provider())
        return resolve_pulse_db_path()

    def prepare_board_graph_storage(
        self,
        *,
        board_id: str,
        reason: str,
    ) -> tuple[str, ...]:
        report = self.prepare_board_graph_storage_report(
            board_id=board_id,
            reason=reason,
        )
        return tuple(ref.token for ref in report.affected_storage_refs)

    def prepare_board_graph_storage_report(
        self,
        *,
        board_id: str,
        reason: str,
    ) -> PurgeReport:
        """Quarantine existing board graph files for an explicit rebuild.

        The bootstrap path is fail-closed and must never purge an existing
        graph just because opening it failed. A confirmed rebuild is different:
        the operator already requested replacement, so we move the current
        graph files to quarantine before the deterministic worker bootstraps a
        fresh graph. If quarantine fails, the rebuild step fails and preserves
        the original files.
        """

        from okto_pulse.community.adapters.kg_runtime import board_kuzu_path

        path = board_kuzu_path(board_id)
        targets: list[Path] = []
        if path.exists():
            targets.append(path)
        if path.parent.exists():
            targets.extend(sorted(path.parent.glob(path.name + ".*")))
        if not targets:
            return PurgeReport(
                board_id=board_id,
                status="noop",
                reason=reason,
            )

        from okto_pulse.core.services.application_kg import (
            get_current_provider_registry,
        )

        registry = get_current_provider_registry()
        report = run_async_blocking(
            registry.graph_lifecycle.purge(board_id, reason=reason)
        )
        still_present = [p for p in targets if p.exists()]
        if still_present:
            raise RuntimeError(
                "explicit rebuild could not quarantine existing graph files: "
                + ", ".join(str(p) for p in still_present)
            )
        return report

    def enqueue_sources(
        self,
        *,
        board_id: str,
        run_id: str,
        sources: Sequence[Mapping[str, Any]],
    ) -> dict[str, int]:
        """UPSERT one ConsolidationQueue row per source. Returns counts
        bucketed by (inserted | reset_to_pending | left_alone). Pending and
        Pending rows are safely adopted and re-enqueued with the current
        deterministic order and fresh retry budget. Claimed rows have their
        exact claim token revoked while the rebuild still owns the exclusive
        admin writer lease, so a stale worker fails its pre-commit CAS instead
        of writing after quarantine. Terminal/retryable rows are reset to
        pending. Uses
        ``priority='high'`` because an explicit rebuild is an operator recovery
        action; it must preempt unrelated backlog from other boards that may
        themselves be corrupt."""

        counts = {
            "inserted": 0,
            "reset_to_pending": 0,
            "reordered_pending": 0,
            "fenced_claimed": 0,
            "deferred_unrelated": 0,
            "preserved_live_intent": 0,
            "left_alone": 0,
        }
        with sqlite3.connect(str(self._path()), timeout=10.0) as conn:
            # Reserve the writer before the first queue read so the complete
            # SELECT-to-UPSERT adoption is one atomic cut with live events.
            conn.execute("BEGIN IMMEDIATE")
            conn.row_factory = sqlite3.Row
            queue_columns = {
                str(column["name"])
                for column in conn.execute(
                    "PRAGMA table_info(consolidation_queue)"
                ).fetchall()
            }
            claim_token_reset = (
                "claim_token=NULL, " if "claim_token" in queue_columns else ""
            )
            triggered_event_projection = (
                "triggered_by_event"
                if "triggered_by_event" in queue_columns
                else "NULL AS triggered_by_event"
            )
            ordered_sources = _ordered_rebuild_sources(
                conn,
                board_id=board_id,
                sources=sources,
            )
            target_keys = {
                (
                    queue_artifact_type(str(row.get("artifact_type", ""))),
                    str(row.get("id", "")),
                )
                for row in ordered_sources
                if row.get("id")
                and str(row.get("artifact_type", ""))
                in DETERMINISTIC_SOURCE_ARTIFACT_TYPES
            }
            # A worker may have claimed unrelated live/delete work just before
            # the admin reservation was acquired.  Revoke every such claim
            # while writer A is still held.  Rows outside this manifest remain
            # durable and are ineligible under the exact reservation source;
            # they resume normally after the reservation is released.
            active_board_rows = conn.execute(
                "SELECT id, artifact_type, artifact_id, work_kind, source "
                "FROM consolidation_queue WHERE board_id=? "
                "AND status IN ('pending', 'claimed')",
                (board_id,),
            ).fetchall()
            for active in active_board_rows:
                key = (str(active["artifact_type"]), str(active["artifact_id"]))
                if str(active["work_kind"]) == "consolidate" and key in target_keys:
                    continue
                source = str(active["source"] or "state_transition")
                if source.startswith("rebuild:"):
                    source = f"deferred_admin:{run_id}"
                conn.execute(
                    "UPDATE consolidation_queue SET status='pending', "
                    "claimed_by_session_id=NULL, "
                    f"{claim_token_reset}claimed_at=NULL, worker_id=NULL, "
                    "claim_timeout_at=NULL, next_retry_at=NULL, source=? "
                    "WHERE id=?",
                    (source, str(active["id"])),
                )
                counts["deferred_unrelated"] += 1
            triggered_at_base = datetime.now(timezone.utc).replace(tzinfo=None)
            for ordinal, row in enumerate(ordered_sources):
                artifact_type = str(row.get("artifact_type", ""))
                artifact_id = str(row.get("id", ""))
                if artifact_type not in DETERMINISTIC_SOURCE_ARTIFACT_TYPES:
                    continue
                queued_artifact_type = queue_artifact_type(artifact_type)
                if not artifact_id:
                    continue
                queue_id = str(uuid.uuid4())
                triggered_at = (
                    triggered_at_base + timedelta(microseconds=ordinal)
                ).strftime("%Y-%m-%d %H:%M:%S.%f")
                existing = conn.execute(
                    "SELECT id, status, source, payload, "
                    f"{triggered_event_projection} FROM consolidation_queue "
                    "WHERE board_id=? AND artifact_type=? AND artifact_id=? "
                    "AND work_kind='consolidate'",
                    (board_id, queued_artifact_type, artifact_id),
                ).fetchone()
                membership_payload: dict[str, Any] = {
                    "_rebuild_membership": {
                        "run_id": run_id,
                        "source_ref": str(row.get("source_ref") or ""),
                        "source_version": str(row.get("source_version") or ""),
                        "content_hash": str(row.get("content_hash") or ""),
                    }
                }
                if existing is not None:
                    existing_payload = existing["payload"]
                    if isinstance(existing_payload, str) and existing_payload:
                        try:
                            existing_payload = json.loads(existing_payload)
                        except (TypeError, ValueError):
                            existing_payload = None
                    existing_source = str(existing["source"] or "state_transition")
                    if existing_source.startswith("rebuild:"):
                        if isinstance(existing_payload, Mapping) and isinstance(
                            existing_payload.get("_rebuild_deferred_live"),
                            Mapping,
                        ):
                            membership_payload["_rebuild_deferred_live"] = dict(
                                existing_payload["_rebuild_deferred_live"]
                            )
                            counts["preserved_live_intent"] += 1
                    elif str(existing["status"]) in {"pending", "claimed"}:
                        membership_payload["_rebuild_deferred_live"] = {
                            "source": existing_source,
                            "triggered_by_event": existing["triggered_by_event"],
                            "payload": (
                                dict(existing_payload)
                                if isinstance(existing_payload, Mapping)
                                else None
                            ),
                        }
                        counts["preserved_live_intent"] += 1
                written = conn.execute(
                    "INSERT INTO consolidation_queue "
                    "(id, board_id, artifact_type, artifact_id, priority, "
                    "source, status, triggered_at, attempts, work_kind, generation, "
                    "payload) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, 0, "
                    "'consolidate', 0, ?) "
                    "ON CONFLICT(board_id, artifact_type, artifact_id) "
                    "WHERE work_kind='consolidate' "
                    "DO UPDATE SET "
                    "status='pending', attempts=0, last_error=NULL, "
                    "claimed_by_session_id=NULL, "
                    f"{claim_token_reset}claimed_at=NULL, "
                    "worker_id=NULL, claim_timeout_at=NULL, "
                    "next_retry_at=NULL, priority=excluded.priority, "
                    "source=excluded.source, triggered_at=excluded.triggered_at, "
                    "payload=excluded.payload "
                    "RETURNING id",
                    (
                        queue_id,
                        board_id,
                        queued_artifact_type,
                        artifact_id,
                        "high",
                        f"rebuild:{run_id}",
                        triggered_at,
                        json.dumps(
                            membership_payload,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                ).fetchone()
                if written is None:
                    counts["left_alone"] += 1
                elif existing is None:
                    counts["inserted"] += 1
                elif str(existing["status"]) == "pending":
                    counts["reordered_pending"] += 1
                elif str(existing["status"]) == "claimed":
                    counts["fenced_claimed"] += 1
                else:
                    counts["reset_to_pending"] += 1
            conn.commit()
        return counts

    def queue_observation(
        self,
        board_id: str,
        *,
        run_id: str | None = None,
        baseline_dead_letter_ids: Sequence[str] = (),
    ) -> tuple[int, str | None]:
        """Return active depth for one rebuild fence (or the legacy board view)."""

        with sqlite3.connect(str(self._path()), timeout=5.0) as conn:
            scope_sql = ""
            params: tuple[object, ...] = (board_id,)
            if run_id is not None:
                scope_sql = " AND work_kind='consolidate' AND source=?"
                params = (board_id, f"rebuild:{run_id}")
            row = conn.execute(
                "SELECT COUNT(*), "
                "MAX(CASE "
                "WHEN status = 'pending' AND ("
                "LOWER(TRIM(COALESCE(last_error, ''))) = "
                "'graph_memory_pressure' "
                "OR LOWER(TRIM(COALESCE(last_error, ''))) LIKE "
                "'graph_memory_pressure:%') "
                "THEN 1 ELSE 0 END) "
                "FROM consolidation_queue "
                "WHERE board_id=? AND status IN ('pending', 'claimed')"
                f"{scope_sql}",
                params,
            ).fetchone()
            new_dead_letter = False
            if run_id is not None:
                dlq_exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='consolidation_dead_letter'"
                ).fetchone()
                if dlq_exists is not None:
                    current_dlq_ids = {
                        str(item[0])
                        for item in conn.execute(
                            "SELECT id FROM consolidation_dead_letter WHERE board_id=?",
                            (board_id,),
                        ).fetchall()
                    }
                    new_dead_letter = bool(
                        current_dlq_ids - set(baseline_dead_letter_ids)
                    )
        depth = int(row[0]) if row else 0
        blocking_reason = (
            "rebuild_new_dead_letter"
            if new_dead_letter
            else ("graph_memory_pressure" if row is not None and bool(row[1]) else None)
        )
        return depth, blocking_reason

    def dead_letter_ids(self, board_id: str) -> tuple[str, ...]:
        with sqlite3.connect(str(self._path()), timeout=5.0) as conn:
            exists_row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='consolidation_dead_letter'"
            ).fetchone()
            if exists_row is None:
                return ()
            return tuple(
                str(row[0])
                for row in conn.execute(
                    "SELECT id FROM consolidation_dead_letter "
                    "WHERE board_id=? ORDER BY id",
                    (board_id,),
                ).fetchall()
            )

    def queue_depth(self, board_id: str) -> int:
        """Return the queue depth while preserving the original adapter contract."""

        return self.queue_observation(board_id)[0]

    def compensate_pending_sources(
        self, *, board_id: str, run_id: str
    ) -> dict[str, int]:
        """Fence every active row from this rebuild before graph compensation."""

        with sqlite3.connect(str(self._path()), timeout=10.0) as conn:
            # Deferred-live marker reads and their restore/fail writes share
            # one writer reservation; a live retag cannot interleave here.
            conn.execute("BEGIN IMMEDIATE")
            conn.row_factory = sqlite3.Row
            queue_columns = {
                str(column[1])
                for column in conn.execute(
                    "PRAGMA table_info(consolidation_queue)"
                ).fetchall()
            }
            claim_token_reset = (
                "claim_token=NULL, " if "claim_token" in queue_columns else ""
            )
            before = conn.execute(
                "SELECT "
                "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status='claimed' THEN 1 ELSE 0 END) "
                "FROM consolidation_queue "
                "WHERE board_id=? AND source=? "
                "AND status IN ('pending', 'claimed')",
                (board_id, f"rebuild:{run_id}"),
            ).fetchone()
            active_rows = conn.execute(
                "SELECT id, payload FROM consolidation_queue "
                "WHERE board_id=? AND source=? "
                "AND status IN ('pending', 'claimed')",
                (board_id, f"rebuild:{run_id}"),
            ).fetchall()
            live_restored = 0
            for active in active_rows:
                payload: Any = active["payload"]
                if isinstance(payload, str) and payload:
                    try:
                        payload = json.loads(payload)
                    except (TypeError, ValueError):
                        payload = None
                marker = (
                    payload.get("_rebuild_deferred_live")
                    if isinstance(payload, Mapping)
                    else None
                )
                if not isinstance(marker, Mapping):
                    continue
                live_source = str(marker.get("source") or "").strip()
                live_payload = marker.get("payload")
                if (
                    not live_source
                    or live_source.startswith("rebuild:")
                    or (
                        live_payload is not None
                        and not isinstance(live_payload, Mapping)
                    )
                ):
                    raise RuntimeError("rebuild_deferred_live_marker_invalid")
                triggered_update = (
                    "triggered_by_event=?, "
                    if "triggered_by_event" in queue_columns
                    else ""
                )
                params: list[Any] = []
                if triggered_update:
                    params.append(marker.get("triggered_by_event"))
                params.extend(
                    [
                        live_source,
                        (
                            json.dumps(
                                dict(live_payload),
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            if live_payload is not None
                            else None
                        ),
                        str(active["id"]),
                    ]
                )
                conn.execute(
                    "UPDATE consolidation_queue SET status='pending', "
                    "attempts=0, last_error=NULL, next_retry_at=NULL, "
                    "claimed_by_session_id=NULL, "
                    f"{claim_token_reset}claimed_at=NULL, worker_id=NULL, "
                    f"claim_timeout_at=NULL, {triggered_update}source=?, payload=?, "
                    "triggered_at=datetime('now') WHERE id=?",
                    tuple(params),
                )
                live_restored += 1
            cursor = conn.execute(
                "UPDATE consolidation_queue SET status='failed', "
                "last_error='rebuild_compensated', next_retry_at=NULL, "
                "claimed_by_session_id=NULL, "
                f"{claim_token_reset}claimed_at=NULL, "
                "worker_id=NULL, claim_timeout_at=NULL "
                "WHERE board_id=? AND source=? "
                "AND status IN ('pending', 'claimed')",
                (board_id, f"rebuild:{run_id}"),
            )
            remaining = int(
                conn.execute(
                    "SELECT COUNT(*) FROM consolidation_queue "
                    "WHERE board_id=? AND source=? "
                    "AND status IN ('pending', 'claimed')",
                    (board_id, f"rebuild:{run_id}"),
                ).fetchone()[0]
            )
            conn.commit()
        return {
            "pending_compensated": int((before or (0, 0))[0] or 0),
            "claimed_compensated": int((before or (0, 0))[1] or 0),
            "active_remaining": remaining,
            "live_intents_restored": live_restored,
            "total_compensated": max(0, int(cursor.rowcount or 0)),
        }

    def compensate_legacy_manual_restore_queue_only(
        self,
        *,
        intent_payload: Mapping[str, Any],
        mutation_guard: Callable[[], bool],
    ) -> dict[str, object]:
        """CAS exactly the residual rows authorized by one legacy intent.

        The historical graph restore is already durable and must not be
        repeated.  This transaction therefore changes queue rows only.  It
        compares every current column against the intent snapshot, rejects
        extras/peers/DLQ aliases, and writes the v4 membership required for a
        later fresh admission to adopt the resulting tombstones safely.
        """

        intent = LegacyManualRestoreQueueOnlyIntent.from_payload(intent_payload)
        board_id = intent.board_id
        source = intent.queue_source
        expected_rows = {str(row["id"]): dict(row) for row in intent.queue_rows}
        memberships = {str(row["row_id"]): dict(row) for row in intent.memberships}
        expected_columns = LEGACY_QUEUE_COLUMNS
        queue_evidence = dict(intent.payload["queue"])
        dead_letter_evidence = dict(intent.payload["dead_letter_guard"])

        def _guard(phase: str) -> None:
            try:
                live = bool(mutation_guard())
            except BaseException as exc:
                raise RuntimeError(
                    f"legacy_queue_only_mutation_guard_error:{phase}"
                ) from exc
            if not live:
                raise RuntimeError(f"legacy_queue_only_mutation_guard_lost:{phase}")

        _guard("before_transaction")
        with sqlite3.connect(str(self._path()), timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            queue_table_entries = tuple(
                row
                for row in conn.execute("PRAGMA table_list")
                if str(row[0]) == "main" and str(row[1]) == "consolidation_queue"
            )
            if (
                len(queue_table_entries) != 1
                or str(queue_table_entries[0][2]) != "table"
            ):
                # A virtual table can expose the exact declared columns while
                # mutating SQLite-owned shadow tables on UPDATE.  The nominal
                # lane is authorized to change only ordinary queue rows.
                raise RuntimeError("legacy_queue_only_queue_storage_invalid")
            queue_xinfo = tuple(
                tuple(column)
                for column in conn.execute("PRAGMA table_xinfo(consolidation_queue)")
            )
            queue_columns = tuple(str(column[1]) for column in queue_xinfo)
            if queue_columns != expected_columns or any(
                len(column) < 7 or int(column[6]) != 0 for column in queue_xinfo
            ):
                raise RuntimeError("legacy_queue_only_schema_mismatch")
            queue_trigger = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='consolidation_queue' ORDER BY name LIMIT 1"
            ).fetchone()
            if queue_trigger is not None:
                # A target-row UPDATE trigger can mutate any table (including
                # another board) inside this transaction.  The nominal lane is
                # authorized to change only the exact queue rows in the
                # intent, so no trigger on the target table is admissible.
                # BEGIN IMMEDIATE prevents a schema writer from installing one
                # after this check and before commit.
                raise RuntimeError("legacy_queue_only_queue_trigger_present")
            projection = ", ".join(
                '"' + column.replace('"', '""') + '"' for column in expected_columns
            )
            current_values = _bounded_legacy_sql_rows(
                conn.execute(
                    f"SELECT {projection} FROM consolidation_queue "
                    "WHERE board_id=? AND source=? ORDER BY id",
                    (board_id, source),
                ),
                expected_columns,
                code="legacy_queue_only_target_queue",
                max_rows=len(expected_rows),
            )
            current_rows = {
                str(row[0]): {
                    column: row[index] for index, column in enumerate(expected_columns)
                }
                for row in current_values
            }
            if set(current_rows) != set(expected_rows):
                raise RuntimeError("legacy_queue_only_row_set_conflict")

            target_ids = tuple(sorted(expected_rows))
            target_placeholders = ",".join("?" for _ in target_ids)
            non_target_rows = _bounded_legacy_sql_rows(
                conn.execute(
                    f"SELECT {projection} FROM consolidation_queue WHERE board_id=? "
                    f"AND id NOT IN ({target_placeholders}) ORDER BY id",
                    (board_id, *target_ids),
                ),
                expected_columns,
                code="legacy_queue_only_non_target_queue",
            )
            non_target_fingerprint = canonical_evidence_hash(
                {
                    "columns": list(expected_columns),
                    "rows": [list(row) for row in non_target_rows],
                }
            )
            if non_target_fingerprint != queue_evidence["board_non_target_fingerprint"]:
                raise RuntimeError("legacy_queue_only_non_target_queue_changed")

            expected_identities = {
                (str(row["artifact_type"]), str(row["artifact_id"])): row_id
                for row_id, row in expected_rows.items()
            }

            def _dead_letter_guard_current(*, phase: str) -> dict[str, object]:
                table_entries = tuple(
                    row
                    for row in conn.execute("PRAGMA table_list")
                    if str(row[0]) == "main"
                    and str(row[1]) == "consolidation_dead_letter"
                )
                if len(table_entries) != 1 or str(table_entries[0][2]) != "table":
                    raise RuntimeError(f"legacy_queue_only_dlq_storage_invalid:{phase}")
                xinfo = tuple(
                    tuple(column)
                    for column in conn.execute(
                        "PRAGMA table_xinfo(consolidation_dead_letter)"
                    )
                )
                dlq_columns = tuple(str(column[1]) for column in xinfo)
                if dlq_columns != LEGACY_DEAD_LETTER_COLUMNS or any(
                    len(column) < 7 or int(column[6]) != 0 for column in xinfo
                ):
                    raise RuntimeError(f"legacy_queue_only_dlq_schema_invalid:{phase}")
                dlq_projection = ", ".join(
                    '"' + column.replace('"', '""') + '"' for column in dlq_columns
                )
                dlq_rows = _bounded_legacy_sql_rows(
                    conn.execute(
                        f"SELECT {dlq_projection} FROM consolidation_dead_letter "
                        "WHERE board_id=? ORDER BY id",
                        (board_id,),
                    ),
                    dlq_columns,
                    code=f"legacy_queue_only_{phase}_dlq",
                )
                identity_indices = (
                    dlq_columns.index("artifact_type"),
                    dlq_columns.index("artifact_id"),
                )
                original_index = dlq_columns.index("original_queue_id")
                original_ids = {
                    str(row[original_index] or "")
                    for row in dlq_rows
                    if (
                        str(row[identity_indices[0]] or ""),
                        str(row[identity_indices[1]] or ""),
                    )
                    in expected_identities
                }
                present_original_ids = frozenset(
                    original_id
                    for original_id in original_ids
                    if original_id
                    and conn.execute(
                        "SELECT 1 FROM consolidation_queue WHERE id=? LIMIT 1",
                        (original_id,),
                    ).fetchone()
                    is not None
                )
                try:
                    current = build_legacy_dead_letter_guard(
                        board_id=board_id,
                        checkpoint_started_at=str(
                            dead_letter_evidence["checkpoint_started_at"]
                        ),
                        target_rows=tuple(expected_rows.values()),
                        dlq_columns=dlq_columns,
                        dlq_rows=dlq_rows,
                        present_original_queue_ids=present_original_ids,
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"legacy_queue_only_dlq_guard_invalid:{phase}"
                    ) from exc
                if current != dead_letter_evidence:
                    raise RuntimeError(f"legacy_queue_only_dlq_changed:{phase}")
                if current["snapshot_fingerprint"] != queue_evidence["dlq_fingerprint"]:
                    raise RuntimeError(f"legacy_queue_only_dlq_changed:{phase}")
                return current

            placeholders = ",".join("(?, ?)" for _ in expected_identities)
            identity_params: list[object] = [board_id]
            for artifact_type, artifact_id in expected_identities:
                identity_params.extend((artifact_type, artifact_id))
            peer_rows = conn.execute(
                "SELECT id, artifact_type, artifact_id FROM consolidation_queue "
                "WHERE board_id=? AND work_kind='consolidate' AND "
                f"(artifact_type, artifact_id) IN ({placeholders}) ORDER BY id "
                "LIMIT ?",
                (*identity_params, len(expected_rows) + 1),
            ).fetchall()
            if len(peer_rows) != len(expected_rows) or any(
                expected_identities.get(
                    (str(row["artifact_type"]), str(row["artifact_id"]))
                )
                != str(row["id"])
                for row in peer_rows
            ):
                raise RuntimeError("legacy_queue_only_peer_identity_conflict")

            _dead_letter_guard_current(phase="before_updates")

            pending_compensated = 0
            claimed_compensated = 0
            already_compensated = 0
            _guard("before_updates")
            for row_id in sorted(expected_rows):
                expected = expected_rows[row_id]
                membership = memberships[row_id]
                terminal = legacy_queue_terminal_row(expected, membership)
                current = current_rows[row_id]
                if current == terminal:
                    already_compensated += 1
                    continue
                if current != expected:
                    raise RuntimeError(f"legacy_queue_only_row_cas_conflict:{row_id}")
                if expected["status"] == "claimed":
                    raw_timeout = expected["claim_timeout_at"]
                    try:
                        timeout_at = datetime.fromisoformat(str(raw_timeout))
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError(
                            f"legacy_queue_only_claim_timeout_invalid:{row_id}"
                        ) from exc
                    if timeout_at.tzinfo is None:
                        timeout_at = timeout_at.replace(tzinfo=timezone.utc)
                    if timeout_at.astimezone(timezone.utc) >= datetime.now(
                        timezone.utc
                    ):
                        raise RuntimeError(
                            f"legacy_queue_only_claim_not_expired:{row_id}"
                        )
                    claimed_compensated += 1
                else:
                    pending_compensated += 1
                _guard(f"before_update:{row_id}")
                cursor = conn.execute(
                    "UPDATE consolidation_queue SET status='failed', "
                    "payload=?, claimed_by_session_id=NULL, claim_token=NULL, "
                    "claimed_at=NULL, last_error='rebuild_compensated', "
                    "worker_id=NULL, claim_timeout_at=NULL, next_retry_at=NULL "
                    "WHERE id=? AND board_id=? AND source=? AND status=?",
                    (
                        terminal["payload"],
                        row_id,
                        board_id,
                        source,
                        expected["status"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"legacy_queue_only_row_cas_lost:{row_id}")

            after_values = _bounded_legacy_sql_rows(
                conn.execute(
                    f"SELECT {projection} FROM consolidation_queue "
                    "WHERE board_id=? AND source=? ORDER BY id",
                    (board_id, source),
                ),
                expected_columns,
                code="legacy_queue_only_terminal_target_queue",
                max_rows=len(expected_rows),
            )
            after_rows = {
                str(row[0]): {
                    column: row[index] for index, column in enumerate(expected_columns)
                }
                for row in after_values
            }
            if after_rows != {
                row_id: legacy_queue_terminal_row(row, memberships[row_id])
                for row_id, row in expected_rows.items()
            }:
                raise RuntimeError("legacy_queue_only_terminal_rows_unproved")

            # A persistent UPDATE trigger was refused before any mutation and
            # BEGIN IMMEDIATE excludes concurrent writers. Re-prove the
            # bounded board cut and identity exclusivity immediately before
            # the final fence/commit.
            terminal_non_target_rows = _bounded_legacy_sql_rows(
                conn.execute(
                    f"SELECT {projection} FROM consolidation_queue WHERE board_id=? "
                    f"AND id NOT IN ({target_placeholders}) ORDER BY id",
                    (board_id, *target_ids),
                ),
                expected_columns,
                code="legacy_queue_only_terminal_non_target_queue",
            )
            terminal_non_target_fingerprint = canonical_evidence_hash(
                {
                    "columns": list(expected_columns),
                    "rows": [list(row) for row in terminal_non_target_rows],
                }
            )
            if (
                terminal_non_target_fingerprint
                != queue_evidence["board_non_target_fingerprint"]
            ):
                raise RuntimeError("legacy_queue_only_non_target_queue_changed")
            terminal_peer_rows = conn.execute(
                "SELECT id, artifact_type, artifact_id FROM consolidation_queue "
                "WHERE board_id=? AND work_kind='consolidate' AND "
                f"(artifact_type, artifact_id) IN ({placeholders}) ORDER BY id "
                "LIMIT ?",
                (*identity_params, len(expected_rows) + 1),
            ).fetchall()
            if len(terminal_peer_rows) != len(expected_rows) or any(
                expected_identities.get(
                    (str(row["artifact_type"]), str(row["artifact_id"]))
                )
                != str(row["id"])
                for row in terminal_peer_rows
            ):
                raise RuntimeError("legacy_queue_only_peer_identity_conflict")
            _dead_letter_guard_current(phase="before_commit")
            _guard("before_commit")
            conn.commit()
        return {
            "reconciliation_kind": "legacy_manual_restore_queue_only",
            "evidence_digest": intent.evidence_digest,
            "queue_source": source,
            "pending_compensated": pending_compensated,
            "claimed_compensated": claimed_compensated,
            "already_compensated": already_compensated,
            "active_remaining": 0,
            "live_intents_restored": 0,
            "total_compensated": pending_compensated + claimed_compensated,
        }

    def build_legacy_manual_restore_queue_only_adapter(
        self,
        *,
        evidence_probe: Callable[[LegacyManualRestoreQueueOnlyIntent], bool],
    ) -> Callable[[Any], Any]:
        """Compose the nominal Core lane over the exact Community CAS.

        The caller owns the physical evidence oracle because only the offline
        executor has the closed-graph/quarantine snapshots.  This adapter owns
        the durable F06 intent receipt and queue transaction.  No ordinary
        rebuild step, graph lifecycle, quarantine or report/event primitive is
        reachable from this closure.
        """

        if not callable(evidence_probe):
            raise TypeError("legacy_queue_only_evidence_probe_required")

        from okto_pulse.community.adapters.rebuild_effects import (
            CommunityRebuildEffects,
        )
        from okto_pulse.core.application.rebuild_processor import (
            RebuildProcessor,
        )

        def _adapter(request: Any) -> Any:
            intent = LegacyManualRestoreQueueOnlyIntent.from_payload(
                request.intent_receipt.details
            )
            expected_key = (
                f"{intent.f06_run_id}:"
                "legacy_manually_restored_blocked_after_enqueue_intent"
            )
            if (
                str(request.board_id) != intent.board_id
                or str(request.intent_id) != intent.evidence_digest
                or str(request.actor_id) != str(intent.payload["recovery_actor_id"])
                or str(request.reason) != str(intent.payload["recovery_reason"])
                or request.command.board_id != intent.board_id
                or request.command.manifest_ref != intent.manifest_ref
                or request.command.run_id != intent.f06_run_id
                or request.intent_receipt.effect_key != expected_key
                or request.intent_receipt.effect
                != "legacy_manually_restored_blocked_after_enqueue_intent"
                or not request.intent_receipt.ok
                or request.intent_receipt.code
                != "legacy_manual_restore_queue_only_authorized"
                or not isinstance(request.owner_token, str)
                or not request.owner_token
                or not callable(request.lease_renew)
                or not callable(request.orchestration_renew)
                or not callable(request.mutation_guard)
            ):
                raise RuntimeError("legacy_queue_only_adapter_binding_invalid")

            command = replace(request.command, owner_token=request.owner_token)
            self._rebuild_run_boards[command.run_id] = command.board_id
            effects = CommunityRebuildEffects(
                self,
                artifact_store=self.artifact_store,
            )
            persisted_intent = effects.persist_legacy_manual_restore_queue_only_intent(
                command,
                intent_payload=intent.to_payload(),
                mutation_guard=request.mutation_guard,
            )
            if persisted_intent != request.intent_receipt:
                raise RuntimeError("legacy_queue_only_intent_receipt_mismatch")

            def _probe(probe_command: Any, probe_receipt: Any) -> bool:
                try:
                    if (
                        probe_command.board_id != intent.board_id
                        or probe_command.manifest_ref != intent.manifest_ref
                        or probe_command.run_id != intent.f06_run_id
                        or probe_receipt != persisted_intent
                        or not bool(request.mutation_guard())
                    ):
                        return False
                    current = LegacyManualRestoreQueueOnlyIntent.from_payload(
                        probe_receipt.details
                    )
                    return bool(
                        current.evidence_digest == intent.evidence_digest
                        and evidence_probe(current)
                    )
                except BaseException:
                    return False

            processor = RebuildProcessor(
                effects,
                lease_renew=request.lease_renew,
                orchestration_renew=request.orchestration_renew,
                legacy_blocked_intent_probe=_probe,
            )
            return processor.reconcile_manually_restored_blocked_after_enqueue(
                command,
                intent_receipt=persisted_intent,
                recovery_actor_id=str(request.actor_id),
                recovery_reason=str(request.reason),
            )

        return _adapter

    def build_step_adapter(self, source_resolver):
        """Compose Local First effects behind the Core rebuild state machine."""

        from okto_pulse.community.adapters.rebuild_effects import (
            CommunityRebuildEffects,
        )
        from okto_pulse.core.application.rebuild_processor import (
            RebuildCommand,
            RebuildOutcomeCode,
            RebuildPlan,
            RebuildProcessor,
        )
        from okto_pulse.core.kg.rebuild_deterministic import (
            DeterministicStructuralRebuilder,
        )
        from okto_pulse.core.kg.rebuild_service import RebuildStepResult

        def _adapter(req):
            run_id = f"f06:{req.manifest_ref or req.candidate_kg_generation_id or req.board_id}"
            self._rebuild_run_boards[run_id] = req.board_id
            effects = CommunityRebuildEffects(self, artifact_store=self.artifact_store)
            checkpoint = effects.load_checkpoint(run_id)

            def _processor() -> RebuildProcessor:
                return RebuildProcessor(
                    effects,
                    plan=RebuildPlan(
                        stall_timeout_seconds=self.drain_timeout_seconds,
                        hard_timeout_seconds=self.drain_hard_timeout_seconds,
                        observation_wait_seconds=self.drain_poll_interval_seconds,
                        final_grace_seconds=self.drain_final_grace_seconds,
                        low_depth_threshold=self.drain_low_depth_threshold,
                    ),
                    cancel_requested=getattr(req, "cancel_requested", None),
                    lease_renew=getattr(req, "lease_renew", None),
                    orchestration_renew=getattr(req, "orchestration_renew", None),
                    release_writer_for_drain=getattr(
                        req,
                        "release_writer_for_drain",
                        None,
                    ),
                    reacquire_writer_after_drain=getattr(
                        req,
                        "reacquire_writer_after_drain",
                        None,
                    ),
                    source_revalidate=getattr(req, "source_revalidate", None),
                    receipt_replay_required=(
                        lambda effect_name, receipt: (
                            effect_name == "enqueue"
                            and (
                                int(
                                    dict(receipt.details).get(
                                        "queue_order_version",
                                        0,
                                    )
                                )
                                < REBUILD_QUEUE_ORDER_VERSION
                                or not bool(
                                    dict(receipt.details).get(
                                        "enqueue_admission_complete",
                                        False,
                                    )
                                )
                                or "baseline_dead_letter_ids"
                                not in dict(receipt.details)
                            )
                        )
                    ),
                )

            recovery_failure_code = getattr(req, "recovery_failure_code", None)
            if recovery_failure_code is not None:
                if checkpoint is None:
                    return RebuildStepResult(
                        ok=False,
                        detail=(
                            "manifest_drift:recovery_checkpoint_missing_before_mutation"
                        ),
                    )
                persisted = checkpoint.command
                if (
                    persisted.board_id != req.board_id
                    or persisted.manifest_ref != req.manifest_ref
                    or persisted.operation != req.operation
                ):
                    return RebuildStepResult(
                        ok=False,
                        detail="lease_lost:recovery_checkpoint_binding_mismatch",
                    )
                command = replace(
                    persisted,
                    owner_token=req.owner_token,
                    salvage_pending=False,
                )
                try:
                    failure_code = RebuildOutcomeCode(str(recovery_failure_code))
                except ValueError:
                    return RebuildStepResult(
                        ok=False,
                        detail="lease_lost:recovery_failure_code_invalid",
                    )
                outcome = _processor().fail_existing(
                    command,
                    code=failure_code,
                    detail=(
                        getattr(req, "recovery_failure_detail", None)
                        or failure_code.value
                    ),
                )
                by_effect = {receipt.effect: receipt for receipt in outcome.receipts}
                quarantine = by_effect.get("quarantine")
                compensate = by_effect.get("compensate")
                affected_files = tuple(
                    dict(quarantine.details).get("affected_files", ())
                    if quarantine is not None
                    else ()
                )
                cleanup_complete = bool(
                    outcome.state.value == "failed"
                    and (compensate is None or compensate.ok)
                )
                return RebuildStepResult(
                    ok=False,
                    detail=(
                        f"manifest_drift:{outcome.detail or failure_code.value}"
                        if cleanup_complete
                        else f"lease_lost:{outcome.detail or outcome.code.value}"
                    ),
                    affected_files=affected_files,
                    previous_kg_generation_id=command.previous_generation_id,
                    current_kg_generation_id=command.previous_generation_id,
                    drilldown={
                        "rebuild_processor": {
                            "state": outcome.state.value,
                            "code": outcome.code.value,
                            "promotion_allowed": outcome.promotion_allowed,
                            "compensation_actions": [
                                action.value for action in outcome.compensation_actions
                            ],
                        }
                    },
                )

            sources, dependency_closure_count = _resolve_evidence_dependency_closure(
                db_path=self._path(),
                board_id=req.board_id,
                sources=tuple(dict(row) for row in source_resolver(req)),
            )
            denominator_sources = tuple(
                row for row in sources if not row.get("_rebuild_dependency_closure")
            )
            deterministic = DeterministicStructuralRebuilder().as_rebuild_step_adapter(
                source_resolver=lambda _request: sources,
            )
            salvage_pending = (
                bool(self.salvage_pending_provider(req.board_id))
                if self.salvage_pending_provider is not None
                else False
            )
            if checkpoint is not None:
                persisted = checkpoint.command
                if (
                    persisted.board_id != req.board_id
                    or persisted.manifest_ref != req.manifest_ref
                    or persisted.operation != req.operation
                ):
                    raise RuntimeError("rebuild_resume_command_drift")
                if persisted.source_rows != sources:
                    if not _checkpoint_source_upgrade_allowed(
                        checkpoint,
                        sources,
                    ):
                        # Do not advertise a fresh-run boundary while the old
                        # checkpoint may still own quarantine/candidate state.
                        # An operator must reconcile or compensate this drift.
                        return RebuildStepResult(
                            ok=False,
                            detail=(
                                "rebuild_resume_command_drift_requires_"
                                "operator_recovery"
                            ),
                            previous_kg_generation_id=(req.previous_kg_generation_id),
                            current_kg_generation_id=(req.previous_kg_generation_id),
                            drilldown={
                                "rebuild_processor": {
                                    "state": "blocked",
                                    "code": "rebuild_resume_command_drift",
                                    "promotion_allowed": False,
                                    "compensation_actions": [],
                                }
                            },
                        )
                    # Strict pre-v4 compatibility upgrade: the denominator is
                    # identical and only resolver-proven historical closure was
                    # added. Persist the command before replaying enqueue so a
                    # crash cannot leave a v4 receipt bound to v3 source rows.
                    persisted = replace(persisted, source_rows=sources)
                    checkpoint = replace(checkpoint, command=persisted)
                    effects.save_checkpoint(checkpoint)
                # The manifest-scoped F06 run is resumable. Its original
                # candidate generation remains the durable identity even when
                # the outer service invocation generated a fresh provisional
                # candidate; only the live writer token and salvage fence are
                # rebound.
                command = replace(
                    persisted,
                    owner_token=req.owner_token,
                    salvage_pending=salvage_pending,
                )
                effective_req = replace(
                    req,
                    previous_kg_generation_id=command.previous_generation_id,
                    candidate_kg_generation_id=command.candidate_generation_id,
                )
            else:
                command = RebuildCommand(
                    run_id=run_id,
                    board_id=req.board_id,
                    manifest_ref=req.manifest_ref,
                    operation=req.operation,
                    actor_id=req.actor_id,
                    reason=f"explicit_rebuild:{req.manifest_ref or req.operation}",
                    source_rows=sources,
                    previous_generation_id=req.previous_kg_generation_id,
                    candidate_generation_id=req.candidate_kg_generation_id,
                    owner_token=req.owner_token,
                    salvage_pending=salvage_pending,
                )
                effective_req = req

            base_result = deterministic(effective_req)
            if not base_result.ok:
                return base_result

            if checkpoint is not None:
                enqueue_effect_key = f"{command.run_id}:enqueue"
                prior_enqueue_receipt = checkpoint.receipts.get(enqueue_effect_key)
                quarantine_completed = any(
                    receipt.effect == "quarantine" and receipt.ok
                    for receipt in checkpoint.receipts.values()
                )
                effects.prepare_enqueue_resume_baseline(
                    command,
                    effect_key=enqueue_effect_key,
                    prior_receipt=prior_enqueue_receipt,
                    prior_admission_possible=quarantine_completed,
                )

            outcome = _processor().execute(command)

            by_effect = {receipt.effect: receipt for receipt in outcome.receipts}
            quarantine = by_effect.get("quarantine")
            enqueue = by_effect.get("enqueue")
            restore = by_effect.get("restore")
            promotion = by_effect.get("promote")
            affected_files = tuple(
                dict(quarantine.details).get("affected_files", ())
                if quarantine is not None
                else ()
            )
            enqueue_counts = dict(enqueue.details) if enqueue is not None else {}
            merged_counts = {
                **base_result.counts,
                "sources": len(denominator_sources),
                "dependency_closure_sources": dependency_closure_count,
                "enqueue_inserted": int(enqueue_counts.get("inserted", 0)),
                "enqueue_reset_to_pending": int(
                    enqueue_counts.get("reset_to_pending", 0)
                ),
                "enqueue_reordered_pending": int(
                    enqueue_counts.get("reordered_pending", 0)
                ),
                "enqueue_fenced_claimed": int(enqueue_counts.get("fenced_claimed", 0)),
                "enqueue_left_alone": int(enqueue_counts.get("left_alone", 0)),
                "expected_by_layer": expected_layers_from_sources(denominator_sources),
            }
            policy_projection = dict(
                dict(promotion.details).get(
                    "policy_constraint_projection",
                    {
                        "configured": (self.policy_constraint_rebuild is not None),
                        "status": "not_executed",
                    },
                )
                if promotion is not None
                else {
                    "configured": self.policy_constraint_rebuild is not None,
                    "status": "not_executed",
                }
            )
            if policy_projection.get("status") == "completed":
                merged_counts.update(
                    {
                        "policy_constraints_activated": int(
                            policy_projection.get("activated_count", 0)
                        ),
                        "policy_constraints_ended": int(
                            policy_projection.get("ended_count", 0)
                        ),
                        "policy_constraints_active": int(
                            policy_projection.get("active_count", 0)
                        ),
                        "policy_constraints_unadopted_active": int(
                            policy_projection.get(
                                "unadopted_active_count",
                                0,
                            )
                        ),
                    }
                )
            checkpoint = self._rebuild_checkpoint_cache.get(run_id)
            queue_drain = {
                "idle": outcome.code is RebuildOutcomeCode.COMPLETED,
                "final_depth": (
                    checkpoint.best_queue_depth if checkpoint is not None else None
                ),
                "progress_events": (
                    checkpoint.queue_progress_events if checkpoint is not None else 0
                ),
                "stall_window_seconds": self.drain_timeout_seconds,
                "hard_timeout_seconds": self.drain_hard_timeout_seconds,
                "grace_applied": (
                    checkpoint.queue_grace_applied if checkpoint is not None else False
                ),
                "grace_reason": (
                    checkpoint.queue_grace_reason if checkpoint is not None else None
                ),
                "final_grace_seconds": self.drain_final_grace_seconds,
                "low_depth_threshold": self.drain_low_depth_threshold,
                "writer_handoff_count": (
                    checkpoint.writer_handoff_count if checkpoint is not None else 0
                ),
                "writer_reacquire_count": (
                    checkpoint.writer_reacquire_count if checkpoint is not None else 0
                ),
            }
            drilldown = {
                **base_result.drilldown,
                "graph_prepare": {"quarantined_files": len(affected_files)},
                "enqueue": enqueue_counts,
                "queue_drain": queue_drain,
                "ingestion_mode": "community_rebuild_effects",
                "cognitive_preservation": (
                    dict(restore.details) if restore is not None else {}
                ),
                "layer_materialization": {
                    "expected_by_layer": merged_counts["expected_by_layer"]
                },
                "rebuild_processor": {
                    "state": outcome.state.value,
                    "code": outcome.code.value,
                    "promotion_allowed": outcome.promotion_allowed,
                    "compensation_actions": [
                        action.value for action in outcome.compensation_actions
                    ],
                },
                "policy_constraint_projection": policy_projection,
            }
            if outcome.code is not RebuildOutcomeCode.COMPLETED:
                if outcome.code in {
                    RebuildOutcomeCode.DRAIN_STALLED,
                    RebuildOutcomeCode.HARD_TIMEOUT,
                }:
                    detail = (
                        "queue_drain_timeout:"
                        f"final_depth={queue_drain['final_depth']} "
                        f"cause={outcome.code.value}"
                    )
                elif (
                    outcome.code is RebuildOutcomeCode.RESTORE_FAILED
                    and outcome.detail == "integrity_error"
                ):
                    detail = "cognitive_preservation_integrity_error"
                else:
                    detail = f"{outcome.code.value}:{outcome.detail or ''}".rstrip(":")
                return RebuildStepResult(
                    ok=False,
                    detail=detail,
                    # The deterministic inner rebuild describes its candidate
                    # as current, but the outer generation repository promotes
                    # only after this adapter returns ``ok=True``.  Every
                    # failure therefore leaves the previous generation current.
                    current_kg_generation_id=(base_result.previous_kg_generation_id),
                    previous_kg_generation_id=base_result.previous_kg_generation_id,
                    affected_files=tuple(base_result.affected_files) + affected_files,
                    structural_hash=base_result.structural_hash,
                    source_hash=base_result.source_hash,
                    counts=merged_counts,
                    reconciliation_decisions=base_result.reconciliation_decisions,
                    drilldown=drilldown,
                )

            return RebuildStepResult(
                ok=True,
                current_kg_generation_id=base_result.current_kg_generation_id,
                previous_kg_generation_id=base_result.previous_kg_generation_id,
                affected_files=tuple(base_result.affected_files) + affected_files,
                structural_hash=base_result.structural_hash,
                source_hash=base_result.source_hash,
                counts=merged_counts,
                reconciliation_decisions=base_result.reconciliation_decisions,
                drilldown=drilldown,
            )

        return _adapter

    def drain_until_idle(
        self,
        *,
        board_id: str,
        timeout_seconds: float = 60.0,
        poll_interval_seconds: float = 0.5,
        final_grace_seconds: float | None = None,
        low_depth_threshold: int | None = None,
        hard_timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Compatibility runner; all timeout decisions come from Core policy."""

        from datetime import datetime, timedelta, timezone

        from okto_pulse.core.application.rebuild_processor import (
            QueueDrainDecision,
            QueueDrainPolicy,
            evaluate_queue_depth,
            start_queue_drain,
        )

        stall_window = max(0.5, float(timeout_seconds))
        hard_ceiling = max(
            stall_window,
            float(
                self.drain_hard_timeout_seconds
                if hard_timeout_seconds is None
                else hard_timeout_seconds
            ),
        )
        grace_seconds = max(
            0.0,
            float(
                self.drain_final_grace_seconds
                if final_grace_seconds is None
                else final_grace_seconds
            ),
        )
        low_depth = max(
            0,
            int(
                self.drain_low_depth_threshold
                if low_depth_threshold is None
                else low_depth_threshold
            ),
        )
        policy = QueueDrainPolicy(
            stall_timeout_seconds=stall_window,
            hard_timeout_seconds=hard_ceiling,
            final_grace_seconds=grace_seconds,
            low_depth_threshold=low_depth,
        )
        monotonic_start = time.monotonic()
        wall_start = datetime.now(timezone.utc)
        tracker = start_queue_drain(policy, now=wall_start)
        final_depth = -1
        decision = QueueDrainDecision.CONTINUE

        while decision is QueueDrainDecision.CONTINUE:
            final_depth = self.queue_depth(board_id)
            monotonic_now = time.monotonic()
            wall_now = wall_start + timedelta(seconds=monotonic_now - monotonic_start)
            evaluation = evaluate_queue_depth(
                policy,
                tracker,
                depth=final_depth,
                now=wall_now,
            )
            tracker = evaluation.tracker
            decision = evaluation.decision
            if decision is QueueDrainDecision.CONTINUE:
                remaining = min(
                    max(0.0, (tracker.stall_deadline - wall_now).total_seconds()),
                    max(0.0, (tracker.hard_deadline - wall_now).total_seconds()),
                )
                time.sleep(min(float(poll_interval_seconds), remaining))

        waited = round(time.monotonic() - monotonic_start, 2)
        return {
            "final_depth": final_depth,
            "waited_seconds": waited,
            "idle": decision is QueueDrainDecision.IDLE,
            "base_timeout_seconds": stall_window,
            "stall_window_seconds": stall_window,
            "hard_timeout_seconds": hard_ceiling,
            "hard_timed_out": decision is QueueDrainDecision.HARD_TIMEOUT,
            "progress_events": tracker.progress_events,
            "best_depth": tracker.best_depth,
            "final_grace_seconds": grace_seconds,
            "low_depth_threshold": low_depth,
            "grace_applied": tracker.grace_applied,
            "grace_reason": tracker.grace_reason,
        }


BoardRebuildIngestionAdapter = CommunityBoardRebuildIngestionAdapter


__all__ = [
    "BoardRebuildIngestionAdapter",
    "CommunityBoardRebuildIngestionAdapter",
    "REBUILD_QUEUE_ORDER_VERSION",
]
