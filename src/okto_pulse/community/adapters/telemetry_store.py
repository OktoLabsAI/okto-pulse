"""Community telemetry EVENT-store adapter (spec R10-B + R10-E).

The Community edition OWNS the concrete append-only JSONL telemetry store behind
the core ``TelemetryEventStore`` port. R10-E ABSORBED the full implementation
here — this class is standalone (no ``super()``), never subclassing a core
concrete. It depends only on the pure, telemetry-internal helpers in the
Community ``_telemetry_helpers`` module (``parse_iso`` / ``ensure_inside`` /
``add_guided_help_counts``) and the closed schema serializer (a stable core
contract) — never on a core concrete store class.

(R10-E removed the core ``LocalTelemetryStore`` concrete and made the registry
fail-closed: this Community adapter is the SOLE concrete ``TelemetryEventStore``.)

Canonical layout (``metrics_dir/{events,sent,failures,exports,snapshots}``), the
confirmation-ledger, retention/prune semantics, ``export_local`` / ``purge_local``
and the ``ensure_inside`` path-guard (``PATH_OUTSIDE_METRICS_DIR``) are byte-for-
byte the golden baseline.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from okto_pulse.core.ports.telemetry import TelemetryEventStore
from okto_pulse.core.telemetry.schema import canonical_json
from okto_pulse.community.adapters._telemetry_helpers import (
    add_guided_help_counts,
    ensure_inside,
    parse_iso,
)


class CommunityLocalTelemetryStore:
    """TelemetryEventStore (Community) — the concrete local JSONL store under the
    user-owned metrics directory. Standalone (no core base class)."""

    def __init__(self, metrics_dir: Path, retention_days: int = 30):
        self.metrics_dir = metrics_dir.resolve()
        self.retention_days = retention_days

    @property
    def events_dir(self) -> Path:
        return self.metrics_dir / "events"

    @property
    def sent_dir(self) -> Path:
        return self.metrics_dir / "sent"

    @property
    def failures_dir(self) -> Path:
        return self.metrics_dir / "failures"

    @property
    def exports_dir(self) -> Path:
        return self.metrics_dir / "exports"

    @property
    def snapshots_dir(self) -> Path:
        return self.metrics_dir / "snapshots"

    def ensure_dirs(self) -> None:
        for path in (
            self.metrics_dir,
            self.events_dir,
            self.sent_dir,
            self.failures_dir,
            self.exports_dir,
            self.snapshots_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def append_snapshot(self, record: dict[str, Any]) -> Path:
        """Persist a product-telemetry SNAPSHOT locally, append-only (R3A-F)."""
        self.ensure_dirs()
        dt = str(record.get("snapshot_at", ""))[:10] or datetime.now(timezone.utc).date().isoformat()
        path = self.snapshots_dir / f"snapshot-{dt}.jsonl"
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(canonical_json(record))
            f.write("\n")
        return path

    def append_event(self, event: dict[str, Any]) -> Path:
        self.ensure_dirs()
        dt = str(event.get("occurred_at", ""))[:10] or datetime.now(timezone.utc).date().isoformat()
        path = self.events_dir / f"events-{dt}.jsonl"
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(canonical_json(event))
            f.write("\n")
        return path

    def append_sent(self, record: dict[str, Any], *, failed: bool = False) -> Path:
        self.ensure_dirs()
        root = self.failures_dir if failed else self.sent_dir
        dt = str(record.get("sent_at") or record.get("failed_at") or "")[:10]
        if not dt:
            dt = datetime.now(timezone.utc).date().isoformat()
        path = root / f"{'failures' if failed else 'sent'}-{dt}.jsonl"
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(canonical_json(record))
            f.write("\n")
        return path

    def confirmed_event_ids(self) -> set[str]:
        """Set of local ``event_id``s the backend has confirmed (R3A-B/C).

        The durable confirmation ledger is the append-only ``sent/`` store: each
        accepted batch is recorded with a ``confirmed_event_ids`` list, so the
        confirmed set is rebuilt here from disk and SURVIVES a restart — a
        confirmed event never re-enters a delta after reload (``fr_fe9b844d``).
        """
        confirmed: set[str] = set()
        if not self.sent_dir.exists():
            return confirmed
        for path in sorted(self.sent_dir.glob("sent-*.jsonl")):
            ensure_inside(self.metrics_dir, path)
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                for event_id in record.get("confirmed_event_ids") or []:
                    if isinstance(event_id, str) and event_id:
                        confirmed.add(event_id)
        return confirmed

    def iter_events(self, *, since: datetime | None = None) -> Iterable[dict[str, Any]]:
        if not self.events_dir.exists():
            return
        for path in sorted(self.events_dir.glob("events-*.jsonl")):
            ensure_inside(self.metrics_dir, path)
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                occurred = parse_iso(str(event.get("occurred_at", "")))
                if since and occurred and occurred < since:
                    continue
                if isinstance(event, dict):
                    yield event

    def summarize(self, *, window_days: int = 30) -> dict[str, Any]:
        since = datetime.now(timezone.utc) - timedelta(days=window_days)
        by_type: Counter[str] = Counter()
        by_day: Counter[str] = Counter()
        guided_help_counts: Counter[str] = Counter()
        files = 0
        for path in self.events_dir.glob("events-*.jsonl") if self.events_dir.exists() else []:
            ensure_inside(self.metrics_dir, path)
            files += 1
        for event in self.iter_events(since=since):
            event_type = str(event.get("event_type", "unknown"))
            by_type[event_type] += 1
            day = str(event.get("occurred_at", ""))[:10]
            if day:
                by_day[day] += 1
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event_type == "guided_help":
                add_guided_help_counts(guided_help_counts, payload)
        return {
            "event_count": sum(by_type.values()),
            "by_event_type": dict(sorted(by_type.items())),
            "by_day": dict(sorted(by_day.items())),
            "guided_help_counts": dict(sorted(guided_help_counts.items())),
            "files_count": files,
        }

    @staticmethod
    def _file_date(path: Path):
        try:
            return datetime.strptime("-".join(path.stem.split("-")[-3:]), "%Y-%m-%d").date()
        except ValueError:
            return None

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def _atomic_write_jsonl(self, path: Path, records: list[dict[str, Any]]) -> None:
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as out:
            for record in records:
                out.write(canonical_json(record))
                out.write("\n")
        tmp.replace(path)

    def prune_old(self, *, now: datetime | None = None) -> dict[str, int]:
        """Retention sweep that NEVER deletes an unconfirmed (pending) event."""
        reference = (now or datetime.now(timezone.utc)).date()
        cutoff = reference - timedelta(days=self.retention_days)
        confirmed = self.confirmed_event_ids()
        removed_confirmed = 0
        preserved_pending = 0

        if self.events_dir.exists():
            for path in sorted(self.events_dir.glob("events-*.jsonl")):
                ensure_inside(self.metrics_dir, path)
                file_date = self._file_date(path)
                if file_date is None or file_date >= cutoff:
                    continue
                events = self._read_jsonl(path)
                pending = [e for e in events if str(e.get("event_id") or "") not in confirmed]
                removed_confirmed += len(events) - len(pending)
                preserved_pending += len(pending)
                if pending:
                    self._atomic_write_jsonl(path, pending)
                else:
                    path.unlink(missing_ok=True)

        surviving = {str(e.get("event_id") or "") for e in self.iter_events()}
        pruned_ledger_ids = 0
        removed_sent_files = 0
        if self.sent_dir.exists():
            for path in sorted(self.sent_dir.glob("sent-*.jsonl")):
                ensure_inside(self.metrics_dir, path)
                file_date = self._file_date(path)
                records = self._read_jsonl(path)
                kept: list[dict[str, Any]] = []
                changed = False
                confirms_survivor = False
                for record in records:
                    ids = record.get("confirmed_event_ids")
                    if isinstance(ids, list):
                        filtered = [i for i in ids if i in surviving]
                        if filtered:
                            confirms_survivor = True
                        if len(filtered) != len(ids):
                            pruned_ledger_ids += len(ids) - len(filtered)
                            record = {**record, "confirmed_event_ids": filtered}
                            changed = True
                    kept.append(record)
                if file_date is not None and file_date < cutoff and not confirms_survivor:
                    path.unlink(missing_ok=True)
                    removed_sent_files += 1
                elif changed:
                    self._atomic_write_jsonl(path, kept)

        removed_failure_files = 0
        if self.failures_dir.exists():
            for path in self.failures_dir.glob("*.jsonl"):
                ensure_inside(self.metrics_dir, path)
                file_date = self._file_date(path)
                if file_date is not None and file_date < cutoff:
                    path.unlink(missing_ok=True)
                    removed_failure_files += 1

        return {
            "removed_confirmed_events": removed_confirmed,
            "preserved_pending_events": preserved_pending,
            "pruned_ledger_ids": pruned_ledger_ids,
            "removed_sent_files": removed_sent_files,
            "removed_failure_files": removed_failure_files,
        }

    def export_local(self, output_path: Path | None = None) -> Path:
        self.ensure_dirs()
        if output_path is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            output_path = self.exports_dir / f"metrics-export-{stamp}.jsonl"
        output_path = ensure_inside(self.metrics_dir, output_path)
        with output_path.open("w", encoding="utf-8", newline="\n") as out:
            for event in self.iter_events():
                out.write(canonical_json(event))
                out.write("\n")
        return output_path

    def purge_local(self) -> dict[str, int]:
        self.ensure_dirs()
        removed_files = 0
        for root in (self.events_dir, self.sent_dir, self.failures_dir, self.exports_dir):
            ensure_inside(self.metrics_dir, root)
            if root.exists():
                for path in root.glob("*"):
                    ensure_inside(self.metrics_dir, path)
                    if path.is_file():
                        path.unlink()
                        removed_files += 1
                    elif path.is_dir():
                        shutil.rmtree(path)
                        removed_files += 1
        return {"purged_files": removed_files}


def build_community_telemetry_event_store(
    metrics_dir: Path, retention_days: int = 30
) -> TelemetryEventStore:
    """Factory matching ``TelemetryEventStoreFactory`` — builds the Community
    event store for a given ``metrics_dir`` / ``retention_days``."""
    return CommunityLocalTelemetryStore(Path(metrics_dir), retention_days)


def register_community_telemetry_event_store() -> None:
    """Composition-root hook: register the Community event-store factory so the
    core telemetry runtime obtains the store through the port."""
    from okto_pulse.core.telemetry.event_store_registry import (
        register_telemetry_event_store_factory,
    )

    register_telemetry_event_store_factory(build_community_telemetry_event_store)


__all__ = [
    "CommunityLocalTelemetryStore",
    "build_community_telemetry_event_store",
    "register_community_telemetry_event_store",
]
