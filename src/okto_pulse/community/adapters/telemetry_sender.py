"""Community telemetry beacon SENDER adapter (spec R10-C + R10-E).

The Community edition OWNS the concrete telemetry beacon sender behind the core
``TelemetrySink`` port. R10-E ABSORBED the full implementation here
(helpers + class) — this module is SELF-CONTAINED and the class is standalone
(no ``super()``), never subclassing a core concrete. The patchable helpers
(``_utcnow`` / ``_backoff_jitter`` / ``sign_payload`` / …) are DEFINED here so
the behavioral tests patch THIS module's bindings.

(R10-E removed the core ``TelemetryBeaconSender`` concrete + its ``requests``
dependency and made the registry fail-closed: this Community adapter is the SOLE
concrete ``TelemetrySink`` beacon sender.)

The handshake/usage protocol, HMAC signing, payload/headers/redaction, reason
codes, watermark and failure_state transitions are byte-for-byte the golden
baseline. ``send_pending`` is the ``TelemetrySink`` port method (an alias of the
steady-state ``send_once``). Coordination R10-B/C/D (NO bypass): ``_store()``
reads via the TelemetryEventStore registry (R10-B) and ``build_product_snapshot``
via the ProductAggregationPort registry (R10-D) — never the concrete core
store/aggregator.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import random
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from okto_pulse.core.infra.config import CoreSettings
from okto_pulse.core.telemetry.product_aggregator_registry import get_product_aggregator
from okto_pulse.core.telemetry.schema import canonical_json, now_utc
from okto_pulse.core.telemetry.settings import (
    resolve_telemetry_config,
    save_state,
)
from okto_pulse.core.telemetry import failure_state as fs
from okto_pulse.core.telemetry import watermark as wm
from okto_pulse.core.telemetry.era import POST_FIX_DELTA_MARKER, POST_FIX_SNAPSHOT_MARKER
from okto_pulse.core.telemetry.event_store_registry import get_telemetry_event_store
from okto_pulse.core.ports.telemetry import TelemetryEventStore, TelemetrySink
from okto_pulse.community.adapters._telemetry_helpers import add_guided_help_counts, parse_iso

logger = logging.getLogger("okto_pulse.telemetry.sender")

# R1-B: preventive token refresh + jittered exponential backoff for transient
# failures. Time and jitter go through small indirections so tests can simulate
# the clock and make backoff deterministic.
DEFAULT_TOKEN_REFRESH_MARGIN_HOURS = 24
_BACKOFF_BASE_SECONDS = 30
_BACKOFF_CAP_SECONDS = 3600
_BACKOFF_JITTER_RATIO = 0.5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _backoff_jitter() -> float:
    """Jitter fraction in [0, _BACKOFF_JITTER_RATIO]; patched in tests."""
    return random.random() * _BACKOFF_JITTER_RATIO


def _backoff_delay_seconds(retry_count: int) -> float:
    """Exponential backoff base*2^(n-1), capped, with additive jitter."""
    steps = min(20, max(0, retry_count - 1))
    base = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2**steps))
    return min(_BACKOFF_CAP_SECONDS, base * (1.0 + _backoff_jitter()))


def install_id_path(settings: CoreSettings) -> Path:
    override = os.environ.get("OKTO_PULSE_INSTALL_ID_PATH")
    if override:
        return Path(override).expanduser().resolve()
    docker_path = Path("/data/install_id")
    if docker_path.parent.exists():
        return docker_path
    return resolve_telemetry_config(settings).metrics_dir / "install_id"


def get_or_create_install_id(settings: CoreSettings) -> str:
    path = install_id_path(settings)
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    value = str(uuid.uuid4())
    path.write_text(value, encoding="utf-8")
    return value


def sign_payload(secret: str, timestamp: str, nonce: str, batch_seq: int, payload: dict[str, Any]) -> str:
    message = f"{timestamp}.{nonce}.{batch_seq}.{canonical_json(payload)}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _log_runtime_skip(*, reason: str) -> None:
    logger.info(
        "metrics.runtime_skip",
        extra={
            "metric_name": "metrics_runtime_skip_total",
            "component": "beacon_sender",
            "outcome": "skipped",
            "reason": reason,
        },
    )


def _log_beacon_outcome(*, reason: str, outcome: str = "skipped") -> None:
    logger.info(
        "metrics.beacon_outcome",
        extra={
            "metric_name": "metrics_beacon_outcome_total",
            "outcome": outcome,
            "reason": reason,
        },
    )


def _log_failure_transition(failure_state: "fs.FailureState", *, action: str) -> None:
    """R5A-D: structured, secret-free log of a failure-state transition (send /
    retry / refresh / duplicate / permanent failure). The payload is the
    allowlisted public projection — no install_token / token_hash / signature /
    nonce / payload — so the transition log can never leak a secret."""
    logger.info(
        "metrics.failure_state_transition",
        extra={
            "metric_name": "metrics_failure_state_transition_total",
            "action": action,
            "failure_state": failure_state.to_public_dict(),
        },
    )


def _log_watermark_state(
    *,
    component: str,
    reason_code: str,
    action: str,
    state: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> None:
    """R3A-E: emit a secret-free audit signal of the local watermark/retention state.

    Built strictly from allowlisted projections (the watermark schema fields +
    the publish-status failure_state fields), so it can NEVER carry
    ``install_token``/``token_hash``/``signature`` or any derived secret
    (``or_8f51cac2``). This lets an agent explain why the cursor advanced, stayed
    put on an error, reconciled a duplicate, or pruned — each ``send_once`` /
    ``prune_old`` transition records its final state and ``reason_code``.
    """
    payload: dict[str, Any] = {
        "metric_name": "MetricsClientWatermarkState",
        "component": component,
        "action": action,
        "reason_code": reason_code,
        "watermark_state": wm.public_watermark_projection(state),
        "publish_status": fs.public_status_projection(state),
    }
    if extra:
        payload.update(extra)
    logger.info("metrics.watermark_state", extra=payload)


class CommunityTelemetryBeaconSender:
    def __init__(self, settings: CoreSettings, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or requests.Session()

    def _store(self) -> TelemetryEventStore:
        # R10-B: store-ACCESS only — obtain the EVENT store via the registered
        # factory. (R10-E ABSORBED the transport implementation into this
        # standalone Community sender; the core sender concrete was removed.)
        cfg = resolve_telemetry_config(self.settings)
        return get_telemetry_event_store(cfg.metrics_dir, cfg.retention_days)

    def handshake(self, *, open_circuit_on_failure: bool = True) -> dict[str, Any] | None:
        cfg = resolve_telemetry_config(self.settings)
        if cfg.mode != "anonymous_beacon":
            _log_runtime_skip(reason="disabled")
            _log_beacon_outcome(reason="disabled")
            return None
        state = dict(cfg.state)
        payload = {
            "install_id": get_or_create_install_id(self.settings),
            "runtime": {
                "deployment": "docker" if Path("/data").exists() else "pypi",
                "python_version": f"{os.sys.version_info.major}.{os.sys.version_info.minor}",
                "os_family": os.name,
            },
            "app_version": getattr(self.settings, "app_version", "0.0.0+local"),
            "platform_arch": os.uname().machine if hasattr(os, "uname") else "unknown",
            "schema_version": cfg.schema_version,
        }
        try:
            resp = self.session.post(
                f"{cfg.beacon_url}/v1/handshake",
                json=payload,
                timeout=5,
            )
        except requests.RequestException:
            if open_circuit_on_failure:
                self._open_circuit(state, cfg, "HANDSHAKE_NETWORK")
            _log_beacon_outcome(reason="transport_failed")
            return None
        if resp.status_code in {410, 426}:
            state["mode"] = "disabled"
            state["schema_status"] = "gone" if resp.status_code == 410 else "sunset"
            save_state(cfg.metrics_dir, state)
            _log_beacon_outcome(reason="consent_stale")
            return None
        if resp.status_code == 429 or resp.status_code >= 500:
            if open_circuit_on_failure:
                self._open_circuit(state, cfg, f"HANDSHAKE_{resp.status_code}", http_status=resp.status_code)
            _log_beacon_outcome(reason="transport_failed")
            return None
        resp.raise_for_status()
        data = resp.json()
        state.update(
            {
                "install_token": data["install_token"],
                "install_token_expires_at": (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=int(data.get("token_ttl_seconds", 2592000)))
                ).isoformat().replace("+00:00", "Z"),
                "accepted_schema_version": data.get("accepted_schema_version", cfg.schema_version),
                "last_handshake_at": now_utc(),
                "limits": data.get("limits") or {},
            }
        )
        save_state(cfg.metrics_dir, state)
        return data

    def hourly_batch(self) -> dict[str, Any] | None:
        cfg = resolve_telemetry_config(self.settings)
        if cfg.mode != "anonymous_beacon":
            _log_runtime_skip(reason="disabled")
            _log_beacon_outcome(reason="disabled")
            return None
        batch, _included = self._build_delta_batch(cfg)
        return batch

    def build_product_snapshot(self) -> dict[str, Any] | None:
        """R3A-F: build product telemetry as a SNAPSHOT payload, never a delta.

        product_metrics is cumulative/snapshot (current spec/card/sprint shapes,
        domain-event counts), so it is marked ``era=post_fix``/``semantics=snapshot``
        and kept OUT of the delta batch — a consumer must never sum it as a
        ``trusted_delta``. Returns None when there is no product telemetry.
        """
        cfg = resolve_telemetry_config(self.settings)
        try:
            # R10-D: obtain the product aggregator via the registered factory
            # (Community supplies the concrete sqlite3 adapter); the port returns a
            # ProductState, projected back to the bounded metrics dict.
            product_metrics = get_product_aggregator(
                self.settings, cfg.metrics_dir
            ).aggregate().to_dict()
        except Exception:
            product_metrics = {}
        if not product_metrics:
            return None
        return {
            "schema_version": cfg.schema_version,
            "install_id": get_or_create_install_id(self.settings),
            **POST_FIX_SNAPSHOT_MARKER,
            "snapshot_at": now_utc(),
            "metrics": product_metrics,
        }

    def publish_product_snapshot(self) -> dict[str, Any]:
        """R3A-F: persist the product snapshot locally; do NOT transmit it.

        There is no safe snapshot ingest contract today: the deployed backend
        ``validate_usage_batch`` rejects unknown top-level fields (so a snapshot,
        like the era/semantics markers, would be 422 UNKNOWN_FIELDS), and
        product_metrics is forbidden inside the delta batch (it would be summed as
        a trusted_delta). Rather than silently drop product telemetry or fake a
        send, the client records the snapshot auditably and reports an explicit
        ``no_snapshot_ingest_endpoint`` outcome. Real transmission stays blocked by
        a SEPARATE consumer-side bug in okto_labs_community_metrics.
        """
        cfg = resolve_telemetry_config(self.settings)
        if cfg.mode != "anonymous_beacon":
            _log_runtime_skip(reason="disabled")
            return {"sent": False, "reason": "not_enabled"}
        snapshot = self.build_product_snapshot()
        if snapshot is None:
            return {"sent": False, "reason": "empty"}
        path = self._store().append_snapshot(snapshot)
        logger.info(
            "metrics.product_snapshot",
            extra={
                "metric_name": "MetricsClientProductSnapshot",
                "outcome": "persisted_local",
                "reason_code": "no_snapshot_ingest_endpoint",
                "era": snapshot["era"],
                "semantics": snapshot["semantics"],
                "family_count": len(snapshot["metrics"]),
            },
        )
        return {
            "sent": False,
            "reason": "no_snapshot_ingest_endpoint",
            "persisted": str(path),
            "semantics": snapshot["semantics"],
        }

    def _build_delta_batch(
        self, cfg, *, restrict_to: set[str] | None = None
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Assemble the steady-state delta batch from UNCONFIRMED events only.

        R3A-B selection is by backend CONFIRMATION of ``event_id`` (the durable
        ledger in :meth:`LocalTelemetryStore.confirmed_event_ids`), NOT by the
        watermark keyset order: an event that lands with a clock-skewed old
        ``occurred_at`` is included as long as it is unconfirmed (``ts_07d9a8b2``),
        and a confirmed event never re-enters as a new delta (``fr_fe9b844d``).
        ``bucket_start`` is the earliest PENDING event, so it is never pinned to
        the oldest historical confirmed event (``tr_f6f84016`` / ``ts_2ec547b9``).

        Returns ``(wire_batch, included_events)`` where each included event is a
        minimal ``{event_id, occurred_at}`` dict used to confirm + advance the
        watermark once the backend accepts the batch.
        """
        store = self._store()
        confirmed = store.confirmed_event_ids()
        buckets: dict[str, Counter[str]] = defaultdict(Counter)
        bucket_starts: list[str] = []
        guided_help_counts: Counter[str] = Counter()
        duration_buckets: Counter[str] = Counter()
        error_class_counts: Counter[str] = Counter()
        included: list[dict[str, Any]] = []
        for event in store.iter_events():
            event_id = str(event.get("event_id") or "")
            if event_id and event_id in confirmed:
                continue  # already confirmed → not a new delta (fr_fe9b844d)
            if restrict_to is not None and event_id not in restrict_to:
                continue  # R3A-G: re-send ONLY the original in-flight intent's events
            occurred = parse_iso(str(event.get("occurred_at", "")))
            if not occurred:
                continue
            included.append(
                {"event_id": event_id, "occurred_at": str(event.get("occurred_at", ""))}
            )
            bucket = occurred.replace(minute=0, second=0, microsecond=0)
            key = bucket.isoformat().replace("+00:00", "Z")
            bucket_starts.append(key)
            event_type = str(event.get("event_type", "unknown"))
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event_type == "guided_help":
                add_guided_help_counts(guided_help_counts, payload)
                continue
            label = str(
                payload.get("command")
                or payload.get("route_template")
                or payload.get("tool_name")
                or payload.get("operation")
                or payload.get("action")
                or payload.get("phase")
                or "unknown"
            )
            buckets[f"{event_type}:{key}"][label] += 1
            if "duration_ms" in payload:
                try:
                    ms = int(payload["duration_ms"])
                    duration_buckets["lt_100ms" if ms < 100 else "lt_1s" if ms < 1000 else "gte_1s"] += 1
                except (TypeError, ValueError):
                    pass
            if payload.get("error_class"):
                error_class_counts[str(payload["error_class"])] += 1
        # product_metrics is DELIBERATELY excluded from this delta batch (codex
        # decision, R3A-B): it is a cumulative/snapshot re-aggregation of the live
        # DB, so carrying it inside a semantics=delta payload would make R4 sum a
        # cumulative as a delta and inflate reports (fr_cfa32c6b "apenas eventos";
        # fr_fe9b844d / br_660cdac7). Product telemetry gets its own snapshot path
        # (tracked follow-up). A delta batch carries ONLY unconfirmed event-stream
        # events.
        if not buckets and not guided_help_counts:
            return None, []
        if bucket_starts:
            bucket_start = sorted(bucket_starts)[0]
        else:
            bucket_start = (
                datetime.now(timezone.utc)
                .replace(minute=0, second=0, microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
        metrics: dict[str, Any] = {
            "cli_counts": {},
            "http_route_template_counts": {},
            "mcp_tool_counts": {},
            "kg_operation_counts": {},
            "duration_buckets": dict(duration_buckets),
            "error_class_counts": dict(error_class_counts),
        }
        if guided_help_counts:
            metrics["guided_help_counts"] = dict(sorted(guided_help_counts.items()))
        # R5A-B: lifecycle / pipeline_transition get DEDICATED aggregate maps (they
        # used to fall into the generic bucket and be dropped — a phantom schema).
        # An unrecognized event_type is NOT silently dropped either: it lands in a
        # bounded diagnostic bucket keyed by the TYPE (never the label/payload).
        lifecycle_counts: Counter[str] = Counter()
        pipeline_transition_counts: Counter[str] = Counter()
        unknown_event_type_counts: Counter[str] = Counter()
        for key, counts in buckets.items():
            event_type, _ = key.split(":", 1)
            if event_type == "cli":
                metrics["cli_counts"].update(counts)
            elif event_type == "http":
                metrics["http_route_template_counts"].update(counts)
            elif event_type == "mcp":
                metrics["mcp_tool_counts"].update(counts)
            elif event_type == "kg":
                metrics["kg_operation_counts"].update(counts)
            elif event_type == "lifecycle":
                lifecycle_counts.update(counts)
            elif event_type == "pipeline_transition":
                pipeline_transition_counts.update(counts)
            else:
                unknown_event_type_counts[event_type] += sum(counts.values())
        # Conditional families (like guided_help_counts): present only when events
        # exist, so the batch shape is unchanged when there is nothing to report.
        if lifecycle_counts:
            metrics["lifecycle_counts"] = dict(sorted(lifecycle_counts.items()))
        if pipeline_transition_counts:
            metrics["pipeline_transition_counts"] = dict(sorted(pipeline_transition_counts.items()))
        if unknown_event_type_counts:
            metrics["unknown_event_type_counts"] = dict(sorted(unknown_event_type_counts.items()))
        batch = {
            "schema_version": cfg.schema_version,
            "install_id": get_or_create_install_id(self.settings),
            # Explicit post-fix delta marker so reports/backfill never infer
            # semantics from a date/path (fr_169be135 / br_8d26d92e / ir_d7bcef31).
            **POST_FIX_DELTA_MARKER,
            "bucket_start": bucket_start,
            "bucket_duration_seconds": 3600,
            "metrics": metrics,
        }
        return batch, included

    def send_once(self) -> dict[str, Any]:
        cfg = resolve_telemetry_config(self.settings)
        if cfg.mode != "anonymous_beacon":
            _log_runtime_skip(reason="disabled")
            _log_beacon_outcome(reason="disabled")
            return {"sent": False, "reason": "not_enabled"}
        state = dict(cfg.state)
        circuit_until = parse_iso(str(state.get("circuit_open_until", "")))
        if circuit_until and circuit_until > datetime.now(timezone.utc):
            _log_beacon_outcome(reason="transport_failed")
            return {"sent": False, "reason": "circuit_open"}
        refresh_status: str | None = None
        refresh_next_retry_at: str | None = None
        if not state.get("install_token"):
            self.handshake()
            cfg = resolve_telemetry_config(self.settings)
            state = dict(cfg.state)
        else:
            # R1-B: preventive refresh when the current token is within the
            # configurable expiry margin (default 24h) BEFORE POST /v1/usage.
            expires_at = parse_iso(str(state.get("install_token_expires_at") or ""))
            margin = timedelta(
                hours=int(
                    getattr(
                        self.settings,
                        "metrics_token_refresh_margin_hours",
                        DEFAULT_TOKEN_REFRESH_MARGIN_HOURS,
                    )
                )
            )
            if expires_at and (expires_at - _utcnow()) <= margin:
                refreshed = self.handshake(open_circuit_on_failure=False)
                cfg = resolve_telemetry_config(self.settings)
                state = dict(cfg.state)
                if refreshed is None:
                    if expires_at <= _utcnow():
                        # token already expired and refresh failed -> cannot publish
                        self._open_circuit(state, cfg, "REFRESH_FAILED")
                        _log_beacon_outcome(reason="transport_failed")
                        return {"sent": False, "reason": "refresh_failed"}
                    # AC ac_7dc06c55: refresh failed by 5xx/transport but the
                    # current token is still valid -> degrade and publish with it,
                    # recording the refresh retry without blocking the publish path.
                    refresh_status = "degraded"
                    refresh_next_retry_at = _iso(_utcnow() + timedelta(seconds=_backoff_delay_seconds(1)))
                    logger.info(
                        "metrics.token_refresh",
                        extra={
                            "metric_name": "metrics_token_refresh_total",
                            "outcome": "degraded",
                            "reason": "refresh_failed_token_valid",
                        },
                    )
                else:
                    refresh_status = "refreshed"
        token = state.get("install_token")
        if not token:
            _log_beacon_outcome(reason="ack_missing")
            return {"sent": False, "reason": "missing_token"}
        batch_seq = int(state.get("next_batch_seq") or 1)
        # R3A-G: an unresolved durable intent for THIS batch_seq means a prior
        # attempt's batch may already be committed remotely (crash between the
        # backend accept and the local confirmation). Re-send EXACTLY that intent's
        # events (reusing its nonce) so a DUPLICATE confirms only what the backend
        # could hold — events added since the original attempt stay pending and
        # are never confirmed without receipt. Otherwise build a fresh batch and
        # record a new intent BEFORE posting.
        intent = state.get("in_flight_batch")
        if isinstance(intent, dict) and intent.get("batch_seq") == batch_seq:
            restrict = {str(i) for i in (intent.get("event_ids") or [])}
            nonce = str(intent.get("nonce") or uuid.uuid4())
            batch, included = self._build_delta_batch(cfg, restrict_to=restrict)
        else:
            nonce = str(uuid.uuid4())
            batch, included = self._build_delta_batch(cfg)
        if not batch:
            # Nothing left to send (e.g. the intent's events are all confirmed).
            if "in_flight_batch" in state:
                state.pop("in_flight_batch", None)
                save_state(cfg.metrics_dir, state)
            return {"sent": False, "reason": "empty"}
        # Durable intent persisted BEFORE the POST (crash-safe): a later DUPLICATE
        # confirms ONLY these event_ids, never a grown batch (R3A-G data-loss fix).
        state["in_flight_batch"] = {
            "batch_seq": batch_seq,
            "nonce": nonce,
            "event_ids": [str(e["event_id"]) for e in included if e.get("event_id")],
        }
        save_state(cfg.metrics_dir, state)
        try:
            resp = self._sign_and_post_usage(cfg, token, batch, batch_seq, nonce=nonce)
        except requests.RequestException:
            self._open_circuit(state, cfg, "USAGE_NETWORK")
            _log_beacon_outcome(reason="transport_failed")
            # R3A-E: a transport failure before accept preserves the cursor.
            _log_watermark_state(
                component="send_once", reason_code="USAGE_NETWORK", action="preserved", state=state
            )
            return {"sent": False, "reason": "network"}
        outcome = self._handle_usage_response(
            resp, state, cfg, batch=batch, batch_seq=batch_seq, allow_rehandshake=True, included=included
        )
        if outcome.get("sent") and refresh_status is not None:
            outcome["refresh"] = refresh_status
            if refresh_next_retry_at is not None:
                outcome["refresh_next_retry_at"] = refresh_next_retry_at
        # R3A-E: audit the cursor transition (advanced / duplicate-reconciled /
        # preserved-on-error) with a secret-free state signal.
        if outcome.get("sent"):
            action, reason_code = "advanced", "accepted"
        elif outcome.get("reason") == "duplicate":
            action, reason_code = "duplicate_reconciled", "duplicate"
        else:
            action, reason_code = "preserved", str(outcome.get("reason") or "unknown")
        _log_watermark_state(component="send_once", reason_code=reason_code, action=action, state=state)
        # R3A-D: run the retention sweep in the normal publish flow, preserving
        # pending events (fr_f3425329). Best-effort — a prune failure must never
        # block publishing. The injected clock keeps it testable. R3A-E: audit it.
        try:
            prune_result = self._store().prune_old(now=_utcnow())
            _log_watermark_state(
                component="prune_old",
                reason_code="retention_sweep",
                action="pruned",
                state=state,
                extra=prune_result,
            )
        except Exception:
            pass
        return outcome

    def _redacted_install_id(self) -> str | None:
        """R5A-D instrumentation: the non-reversible redacted install token for the
        failure-state. Never the raw install_id / a secret; never raises."""
        try:
            return fs.redact_install_id(get_or_create_install_id(self.settings))
        except Exception:
            return None

    def _open_circuit(
        self, state: dict[str, Any], cfg, code: str, *, http_status: int | None = None, status: str = fs.STATUS_DEGRADED
    ) -> None:
        # R1-B: jittered exponential backoff recorded in the R1-A failure-state
        # schema. circuit_open_until/last_failure_code stay in sync for the
        # existing send_once gate and backward compatibility. R1-C passes
        # status=FATAL for integrity failures (INVALID_SIGNATURE).
        current = fs.read_failure_state(state)
        retry_count = current.retry_count + 1
        now = _utcnow()
        next_retry_at = _iso(now + timedelta(seconds=_backoff_delay_seconds(retry_count)))
        updated = fs.merge(
            current,
            status=status,
            reason_code=code,
            http_status=http_status,
            last_failure_at=_iso(now),
            next_retry_at=next_retry_at,
            retry_count=retry_count,
            recovered_at=None,
            install_id_redacted=self._redacted_install_id(),
        )
        state[fs.FAILURE_STATE_KEY] = updated.to_public_dict()
        state["circuit_open_until"] = next_retry_at
        state["last_failure_code"] = code
        save_state(cfg.metrics_dir, state)
        self._store().append_sent({"failed_at": now_utc(), "code": code}, failed=True)
        _log_failure_transition(updated, action="failed")

    def _record_success(
        self,
        state: dict[str, Any],
        cfg,
        *,
        batch_seq: int,
        included: list[dict[str, Any]],
        now_iso: str,
    ) -> None:
        # R1-B: record a successful publish in the failure-state schema, marking
        # recovery when the previous state was failing, and clear the legacy
        # circuit gate.
        current = fs.read_failure_state(state)
        was_failing = current.status in (fs.STATUS_DEGRADED, fs.STATUS_FATAL) or current.retry_count > 0
        updated = fs.merge(
            current,
            status=fs.STATUS_OK,
            reason_code=None,
            http_status=None,
            last_success_at=now_iso,
            next_retry_at=None,
            retry_count=0,
            recovered_at=now_iso if was_failing else current.recovered_at,
            install_id_redacted=self._redacted_install_id(),
        )
        state[fs.FAILURE_STATE_KEY] = updated.to_public_dict()
        _log_failure_transition(updated, action="recovered" if was_failing else "succeeded")
        state["last_send_at"] = now_iso
        state["next_batch_seq"] = batch_seq + 1
        state.pop("circuit_open_until", None)
        state.pop("last_failure_code", None)
        state.pop("in_flight_batch", None)  # R3A-G: this batch's intent is resolved
        # R3A-B: advance the watermark audit cursor to the newest confirmed event
        # and refresh the pending counter from the durable ledger (which already
        # carries this batch — the sent record was appended before this call).
        # Selection itself uses the confirmed-id ledger, NOT this cursor.
        self._apply_watermark_advance(
            state, cfg, included=included, updated_at=now_iso, next_batch_seq=batch_seq + 1
        )
        save_state(cfg.metrics_dir, state)

    def _apply_watermark_advance(
        self,
        state: dict[str, Any],
        cfg,
        *,
        included: list[dict[str, Any]],
        updated_at: str,
        next_batch_seq: int,
    ) -> None:
        advanced = wm.read_watermark(state)
        for event in included or []:
            event_id = str(event.get("event_id") or "")
            occurred_at = str(event.get("occurred_at") or "")
            if event_id and occurred_at:
                advanced = wm.advance(
                    advanced, event_id=event_id, occurred_at=occurred_at, updated_at=updated_at
                )
        store = self._store()
        confirmed = store.confirmed_event_ids()
        pending = sum(
            1 for event in store.iter_events() if str(event.get("event_id") or "") not in confirmed
        )
        advanced = wm.set_counters(
            advanced,
            pending_event_count=pending,
            next_batch_seq=next_batch_seq,
            retention_days=int(getattr(cfg, "retention_days", wm.DEFAULT_RETENTION_DAYS)),
        )
        state.update(advanced.to_state_fields())

    def _sign_and_post_usage(self, cfg, token, batch: dict[str, Any], batch_seq: int, *, nonce: str | None = None):
        timestamp = str(int(_utcnow().timestamp()))
        nonce = nonce or str(uuid.uuid4())
        signature = sign_payload(str(token), timestamp, nonce, batch_seq, batch)
        body = canonical_json(batch).encode("utf-8")
        headers = {
            "content-type": "application/json",
            "x-okto-signature": signature,
            "x-okto-timestamp": timestamp,
            "x-okto-nonce": nonce,
            "x-okto-batch-seq": str(batch_seq),
        }
        return self.session.post(f"{cfg.beacon_url}/v1/usage", data=body, headers=headers, timeout=5)

    @staticmethod
    def _response_code(resp) -> str | None:
        try:
            body = resp.json()
        except Exception:
            return None
        return body.get("code") if isinstance(body, dict) else None

    @staticmethod
    def _rehandshake_allowed(cfg, state: dict[str, Any]) -> bool:
        # R1-C / FR fr_07d36948: a re-handshake re-registers the install, so it is
        # only allowed while consent is valid — beacon opted-in AND a recorded
        # policy acknowledgement (policy_ack) present in local state.
        return cfg.mode == "anonymous_beacon" and bool(state.get("policy_version"))

    def _handle_usage_response(
        self,
        resp,
        state: dict[str, Any],
        cfg,
        *,
        batch: dict[str, Any],
        batch_seq: int,
        allow_rehandshake: bool,
        included: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if resp.status_code in {410, 426}:
            state["mode"] = "disabled"
            state["schema_status"] = "gone" if resp.status_code == 410 else "sunset"
            save_state(cfg.metrics_dir, state)
            _log_beacon_outcome(reason="consent_stale")
            return {"sent": False, "reason": "schema_incompatible"}
        if resp.status_code in {403, 429} or resp.status_code >= 500:
            self._open_circuit(state, cfg, f"USAGE_{resp.status_code}", http_status=resp.status_code)
            _log_beacon_outcome(reason="transport_failed")
            return {"sent": False, "reason": "retryable"}
        if 200 <= resp.status_code < 300:
            now_iso = now_utc()
            confirmed_ids = [str(e["event_id"]) for e in (included or []) if e.get("event_id")]
            # Durable confirmation ledger FIRST: the sent record is the source of
            # truth for selection, so a crash after this point still excludes the
            # confirmed events on reload (fr_fe9b844d, crash-durable). The
            # watermark advance below is only the denormalized audit marker.
            self._store().append_sent(
                {
                    "sent_at": now_iso,
                    "batch_seq": batch_seq,
                    "payload": batch,
                    "response_status": resp.status_code,
                    "confirmed_event_ids": confirmed_ids,
                }
            )
            self._record_success(
                state, cfg, batch_seq=batch_seq, included=included or [], now_iso=now_iso
            )
            _log_beacon_outcome(reason="sent", outcome="sent")
            return {"sent": True, "batch_seq": batch_seq}
        # R1-C: classify the named /v1/usage reason codes (testable, not log parsing).
        code = self._response_code(resp)
        if resp.status_code == 401 and code == "UNKNOWN_INSTALL":
            return self._recover_unknown_install(
                state,
                cfg,
                batch=batch,
                batch_seq=batch_seq,
                allow_rehandshake=allow_rehandshake,
                included=included,
            )
        if resp.status_code == 401 and code == "INVALID_SIGNATURE":
            # Integrity/auth failure: actionable/fatal, never a blind re-handshake loop.
            self._open_circuit(state, cfg, "INVALID_SIGNATURE", http_status=401, status=fs.STATUS_FATAL)
            _log_beacon_outcome(reason="fatal")
            return {"sent": False, "reason": "invalid_signature"}
        if resp.status_code == 401 and code == "TOKEN_EXPIRED":
            # R5A-D: recoverable auth failure — the backend rejected an EXPIRED token
            # (e.g. a clock skew the preventive refresh missed). Drop the token so the
            # next cycle re-handshakes for a fresh one, and degrade with backoff
            # (next_retry_at). Not fatal, not a blind retry loop. Previously this fell
            # through to raise_for_status -> an unhandled exception in send_once.
            state.pop("install_token", None)
            state.pop("install_token_expires_at", None)
            self._open_circuit(state, cfg, "TOKEN_EXPIRED", http_status=401, status=fs.STATUS_DEGRADED)
            _log_beacon_outcome(reason="token_expired")
            return {"sent": False, "reason": "token_expired"}
        if resp.status_code == 409 and code == "DUPLICATE_NONCE_OR_BATCH_SEQ":
            # R3A-C (br_4659bfcc / tr_067c08e6): a DUPLICATE_NONCE_OR_BATCH_SEQ is
            # the backend reporting it ALREADY committed this batch. The nonce is
            # fresh per send, so a duplicate is a batch_seq collision = a prior
            # accept — treat it as IDEMPOTENT CONFIRMATION of this batch's events:
            # confirm them in the durable ledger and advance the watermark just
            # like a 2xx, so they never replay (no loop) and the cursor is not
            # lost / left pending. br_7bced648: the watermark only ever reflects
            # backend-confirmed events, so this is not an optimistic advance.
            now_iso = now_utc()
            confirmed_ids = [str(e["event_id"]) for e in (included or []) if e.get("event_id")]
            self._store().append_sent(
                {
                    "sent_at": now_iso,
                    "batch_seq": batch_seq,
                    "duplicate": True,
                    "response_status": resp.status_code,
                    "confirmed_event_ids": confirmed_ids,
                }
            )
            self._record_duplicate(
                state, cfg, batch_seq=batch_seq, included=included or [], now_iso=now_iso
            )
            _log_beacon_outcome(reason="duplicate")
            return {"sent": False, "reason": "duplicate", "batch_seq": batch_seq}
        resp.raise_for_status()
        return {"sent": False, "reason": "unhandled"}

    def _recover_unknown_install(
        self,
        state: dict[str, Any],
        cfg,
        *,
        batch: dict[str, Any],
        batch_seq: int,
        allow_rehandshake: bool,
        included: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not allow_rehandshake:
            # Already re-handshaked + retried once and STILL unknown: persistent
            # failure, back off without a second re-handshake.
            self._open_circuit(state, cfg, "UNKNOWN_INSTALL", http_status=401)
            _log_beacon_outcome(reason="transport_failed")
            return {"sent": False, "reason": "unknown_install_unresolved"}
        if not self._rehandshake_allowed(cfg, state):
            # No valid consent: do NOT re-register; persist an actionable block.
            self._record_blocked(state, cfg, reason_code="UNKNOWN_INSTALL")
            _log_beacon_outcome(reason="consent_blocked")
            return {"sent": False, "reason": "consent_blocked"}
        refreshed = self.handshake(open_circuit_on_failure=False)
        cfg = resolve_telemetry_config(self.settings)
        state = dict(cfg.state)
        token = state.get("install_token")
        if refreshed is None or not token:
            self._open_circuit(state, cfg, "UNKNOWN_INSTALL", http_status=401)
            _log_beacon_outcome(reason="transport_failed")
            return {"sent": False, "reason": "rehandshake_failed"}
        try:
            retry = self._sign_and_post_usage(cfg, token, batch, batch_seq)
        except requests.RequestException:
            self._open_circuit(state, cfg, "USAGE_NETWORK")
            _log_beacon_outcome(reason="transport_failed")
            return {"sent": False, "reason": "network"}
        outcome = self._handle_usage_response(
            retry, state, cfg, batch=batch, batch_seq=batch_seq, allow_rehandshake=False, included=included
        )
        if outcome.get("sent"):
            outcome["recovered"] = "rehandshake"
        return outcome

    def _record_duplicate(
        self,
        state: dict[str, Any],
        cfg,
        *,
        batch_seq: int,
        included: list[dict[str, Any]],
        now_iso: str,
    ) -> None:
        current = fs.read_failure_state(state)
        was_failing = current.status in (fs.STATUS_DEGRADED, fs.STATUS_FATAL) or current.retry_count > 0
        updated = fs.merge(
            current,
            status=fs.STATUS_OK,
            reason_code=None,
            http_status=None,
            next_retry_at=None,
            retry_count=0,
            recovered_at=now_iso if was_failing else current.recovered_at,
            install_id_redacted=self._redacted_install_id(),
        )
        state[fs.FAILURE_STATE_KEY] = updated.to_public_dict()
        _log_failure_transition(updated, action="duplicate")
        state["next_batch_seq"] = batch_seq + 1
        state.pop("circuit_open_until", None)
        state.pop("last_failure_code", None)
        state.pop("in_flight_batch", None)  # R3A-G: this batch's intent is resolved
        # R3A-C: a confirmed DUPLICATE is idempotent confirmation of this batch's
        # events for the watermark (br_4659bfcc) — advance the cursor and refresh
        # the pending count from the durable ledger (already appended), so the
        # window is NOT left pending (no replay) nor the cursor lost.
        self._apply_watermark_advance(
            state, cfg, included=included, updated_at=now_iso, next_batch_seq=batch_seq + 1
        )
        save_state(cfg.metrics_dir, state)

    def _record_blocked(self, state: dict[str, Any], cfg, *, reason_code: str) -> None:
        current = fs.read_failure_state(state)
        now = _utcnow()
        retry_count = current.retry_count + 1
        next_retry_at = _iso(now + timedelta(seconds=_backoff_delay_seconds(retry_count)))
        updated = fs.merge(
            current,
            status=fs.STATUS_BLOCKED,
            reason_code=reason_code,
            http_status=401,
            last_failure_at=_iso(now),
            next_retry_at=next_retry_at,
            retry_count=retry_count,
            recovered_at=None,
            publish_enabled=False,
            consent_state=fs.CONSENT_BLOCKED,
            install_id_redacted=self._redacted_install_id(),
        )
        state[fs.FAILURE_STATE_KEY] = updated.to_public_dict()
        _log_failure_transition(updated, action="blocked")
        state["circuit_open_until"] = next_retry_at
        state["last_failure_code"] = reason_code
        save_state(cfg.metrics_dir, state)
        self._store().append_sent({"failed_at": now_utc(), "code": reason_code}, failed=True)

    def send_pending(self) -> dict[str, Any]:
        # The TelemetrySink port method — an alias of the steady-state ``send_once``
        # (pending unconfirmed events, signed, with backoff/circuit).
        return self.send_once()


def build_community_telemetry_sender(settings: Any) -> TelemetrySink:
    """Factory: build the Community telemetry sender for a ``settings``
    (signature matches ``TelemetrySenderFactory``)."""
    return CommunityTelemetryBeaconSender(settings)


def register_community_telemetry_sender() -> None:
    """Register the Community telemetry-sender factory at the core registry
    (composition root). Idempotent."""
    from okto_pulse.core.telemetry.sender_registry import register_telemetry_sender_factory

    register_telemetry_sender_factory(build_community_telemetry_sender)


__all__ = [
    "CommunityTelemetryBeaconSender",
    "build_community_telemetry_sender",
    "register_community_telemetry_sender",
]
