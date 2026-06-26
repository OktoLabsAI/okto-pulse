"""Community product-telemetry aggregation adapter (spec R10-D + R10-E).

The Community edition OWNS the concrete ``sqlite3`` product aggregation (over
domain_events/specs/cards/sprints/architecture_designs) + the local
``product_state.json`` snapshot, behind the core ``ProductAggregationPort``.
R10-E ABSORBED the full implementation here — this class is standalone (no
``super()``), never subclassing a core concrete. It depends only on the pure,
telemetry-internal SQL/parse helpers in the Community ``_telemetry_helpers``
module, never on a core concrete class.

(R10-E removed the core ``ProductTelemetryAggregator`` concrete and made the
registry fail-closed: this Community adapter is the SOLE concrete
``ProductAggregationPort``.)

The aggregation, the family ordering, and the ``product_state.json`` write
(``families`` + ``last_aggregate_total``) are byte-for-byte the golden baseline;
the snapshot stays LOCAL-ONLY.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from okto_pulse.core.ports.telemetry import (
    PRODUCT_METRIC_KEYS,
    ProductAggregationPort,
    ProductState,
)
from okto_pulse.community.adapters._telemetry_helpers import (
    _json_array_len,
    _load_json,
    _origin_from_spec_source,
    _safe_count_key,
    _sqlite_path,
    _table_exists,
)


class CommunityProductTelemetryAggregator:
    """ProductAggregationPort (Community) — count-only product metrics from the
    local domain state, without exporting artifact identifiers. Standalone (no
    core base class); ``aggregate()`` returns a :class:`ProductState`."""

    def __init__(self, settings: Any, metrics_dir: Path):
        self.settings = settings
        self.metrics_dir = metrics_dir

    @property
    def state_path(self) -> Path:
        return self.metrics_dir / "product_state.json"

    def aggregate(self) -> ProductState:
        db_path = _sqlite_path(str(getattr(self.settings, "database_url", "")))
        if db_path is None or not db_path.exists():
            return ProductState.from_dict({})
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            metrics = self._aggregate_conn(conn)
            self._save_state(metrics)
            return ProductState.from_dict(metrics)
        finally:
            conn.close()

    def _aggregate_conn(self, conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
        metrics: dict[str, Counter[str]] = {key: Counter() for key in PRODUCT_METRIC_KEYS}
        if _table_exists(conn, "domain_events"):
            self._aggregate_domain_events(conn, metrics)
        self._aggregate_current_shapes(conn, metrics)
        return {
            key: dict(sorted(counter.items()))
            for key, counter in sorted(metrics.items())
            if counter
        }

    def _aggregate_domain_events(self, conn: sqlite3.Connection, metrics: dict[str, Counter[str]]) -> None:
        rows = conn.execute("SELECT event_type, payload_json FROM domain_events").fetchall()
        for row in rows:
            event_type = _safe_count_key(row["event_type"])
            payload = _load_json(row["payload_json"])
            metrics["product_feature_usage_counts"][event_type] += 1

            if event_type == "spec.created":
                metrics["product_flow_origin_counts"][_origin_from_spec_source(payload.get("source"))] += 1
            elif event_type == "spec.moved":
                to_status = _safe_count_key(payload.get("to_status"))
                metrics["product_workflow_stage_counts"][f"spec.{to_status}"] += 1
                if to_status == "done":
                    metrics["product_flow_completion_counts"]["completed"] += 1
            elif event_type == "card.created":
                card_type = _safe_count_key(payload.get("card_type"), fallback="normal")
                metrics["product_work_item_type_counts"][card_type] += 1
                if card_type == "bug":
                    metrics["product_quality_signal_counts"]["bugs_created"] += 1
                elif card_type in {"test", "test_scenario"}:
                    metrics["product_quality_signal_counts"]["tests_created"] += 1
                else:
                    metrics["product_quality_signal_counts"]["tasks_created"] += 1
            elif event_type == "card.moved":
                to_status = _safe_count_key(payload.get("to_status"))
                metrics["product_workflow_stage_counts"][f"card.{to_status}"] += 1
                if to_status in {"validation", "done"}:
                    metrics["product_quality_signal_counts"][f"cards_{to_status}"] += 1
            elif event_type.startswith("kg."):
                metrics["product_advanced_capability_counts"][event_type] += 1
            elif event_type in {"ideation.derived_to_spec", "refinement.derived_to_spec"}:
                metrics["product_flow_origin_counts"][event_type.split(".", 1)[0]] += 1

    def _aggregate_current_shapes(self, conn: sqlite3.Connection, metrics: dict[str, Counter[str]]) -> None:
        if _table_exists(conn, "specs"):
            spec_rows = conn.execute(
                "SELECT id, status, ideation_id, refinement_id, test_scenarios, decisions "
                "FROM specs"
            ).fetchall()
            for row in spec_rows:
                status = _safe_count_key(row["status"])
                metrics["product_workflow_stage_counts"][f"spec.current.{status}"] += 1
                origin = self._origin_from_spec_row(conn, row)
                metrics["product_flow_origin_counts"][f"current.{origin}"] += 1
                if status == "done":
                    metrics["product_flow_completion_counts"][origin] += 1
                metrics["product_quality_signal_counts"]["test_scenarios_total"] += _json_array_len(row["test_scenarios"])
                metrics["product_advanced_capability_counts"]["decisions_total"] += _json_array_len(row["decisions"])

        if _table_exists(conn, "cards"):
            for row in conn.execute("SELECT status, card_type FROM cards").fetchall():
                status = _safe_count_key(row["status"])
                card_type = _safe_count_key(row["card_type"], fallback="normal")
                metrics["product_workflow_stage_counts"][f"card.current.{status}"] += 1
                metrics["product_work_item_type_counts"][f"current.{card_type}"] += 1

        if _table_exists(conn, "sprints"):
            for row in conn.execute("SELECT status FROM sprints").fetchall():
                metrics["product_workflow_stage_counts"][f"sprint.current.{_safe_count_key(row['status'])}"] += 1

        if _table_exists(conn, "architecture_designs"):
            count = conn.execute("SELECT COUNT(*) FROM architecture_designs").fetchone()[0]
            if count:
                metrics["product_advanced_capability_counts"]["architecture_designs_total"] += int(count)

    def _origin_from_spec_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> str:
        if row["refinement_id"]:
            return "refinement"
        if row["ideation_id"]:
            if _table_exists(conn, "story_ideation_links"):
                linked_story = conn.execute(
                    "SELECT 1 FROM story_ideation_links WHERE ideation_id = ? LIMIT 1",
                    (row["ideation_id"],),
                ).fetchone()
                if linked_story:
                    return "story"
            return "ideation"
        return "spec"

    def _save_state(self, metrics: dict[str, dict[str, int]]) -> None:
        self.metrics_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "families": sorted(metrics),
            "last_aggregate_total": sum(sum(group.values()) for group in metrics.values()),
        }
        self.state_path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")


def build_community_product_aggregator(settings: Any, metrics_dir: Any) -> ProductAggregationPort:
    """Factory: build the Community product aggregator for a ``settings`` /
    ``metrics_dir`` (signature matches ``ProductAggregatorFactory``)."""
    return CommunityProductTelemetryAggregator(settings, Path(metrics_dir))


def register_community_product_aggregator() -> None:
    """Register the Community product-aggregator factory at the core registry
    (composition root). Idempotent."""
    from okto_pulse.core.telemetry.product_aggregator_registry import (
        register_product_aggregator_factory,
    )

    register_product_aggregator_factory(build_community_product_aggregator)


__all__ = [
    "CommunityProductTelemetryAggregator",
    "build_community_product_aggregator",
    "register_community_product_aggregator",
]
