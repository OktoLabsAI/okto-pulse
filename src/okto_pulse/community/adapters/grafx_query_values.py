"""Project public Grafx observations into Pulse's plain result envelope.

The Core never receives native driver objects. Qualified IDs and provenance are
observations, not write handles; wide identifiers remain decimal strings in JSON.
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from math import isfinite, isnan
from typing import Any

from okto_grafx import (
    DateTimeValue,
    DateValue,
    DecimalValue,
    DurationValue,
    EntityIdentity,
    LocalDateTimeValue,
    LocalTimeValue,
    NodeValue,
    PathValue,
    RelationshipValue,
    Timestamp,
    TimeValue,
    VectorValue,
)
from okto_grafx.domain.model import Uuid

_TEMPORALS = (DateValue, LocalTimeValue, TimeValue, LocalDateTimeValue, DateTimeValue, DurationValue)


def _identity(value: EntityIdentity) -> dict[str, Any]:
    return {
        "database": value.database_uuid.hex(),
        "table": str(value.table_id),
        "kind": value.kind,
        "offset": None if value.record_id is None else str(value.record_id),
        "provisional": None if value.provisional_id is None else value.provisional_id.hex(),
    }


def normalize_query_value(value: Any) -> Any:
    if isinstance(value, (NodeValue, RelationshipValue)):
        properties = {key: normalize_query_value(item) for key, item in value.properties.items()}
        # Flattened properties remain convenient to existing Pulse callers, but
        # reserved metadata names cannot destroy the original property payload.
        result = {
            **properties,
            "_PROPERTIES": properties,
            "_ID": _identity(value.identity),
            "_LABEL": value.label,
            "_PROVENANCE": value.provenance.to_dict(),
        }
        if isinstance(value, NodeValue):
            result["_LABELS"] = list(value.labels)
        else:
            result["_SRC"] = _identity(value.source)
            result["_DST"] = _identity(value.target)
        return result
    if isinstance(value, PathValue):
        return {"_NODES": [normalize_query_value(node) for node in value.nodes],
                "_RELS": [normalize_query_value(rel) for rel in value.relationships]}
    if isinstance(value, _TEMPORALS):
        # Native formatting retains nanoseconds, extended years and zone/offset;
        # converting through Python datetime would narrow that representation.
        return value.isoformat()
    if isinstance(value, Timestamp):
        try:
            rendered = (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value.micros)).isoformat(
                timespec="microseconds"
            )
        except OverflowError:
            # TIMESTAMP spans int64 microseconds, wider than Python datetime.
            return {"type": "timestamp", "micros": str(value.micros)}
        return rendered.replace("+00:00", "Z")
    if isinstance(value, DecimalValue):
        return {"type": "decimal", "coefficient": str(value.coefficient),
                "precision": value.precision, "scale": value.scale}
    if isinstance(value, Uuid):
        return value.canonical()
    if isinstance(value, bytes):
        return {"type": "bytes", "hex": value.hex()}
    if isinstance(value, float) and not isfinite(value):
        # Observation only: this does not admit nonfinite stored properties.
        return {"type": "float", "value": "NaN" if isnan(value) else
                ("Infinity" if value > 0 else "-Infinity")}
    if isinstance(value, VectorValue):
        return [normalize_query_value(item) for item in value.values]
    if isinstance(value, tuple):
        return tuple(normalize_query_value(item) for item in value)
    if isinstance(value, list):
        return [normalize_query_value(item) for item in value]
    if isinstance(value, Mapping):
        if all(isinstance(key, str) for key in value):
            return {key: normalize_query_value(item) for key, item in value.items()}
        # JSON object keys cannot distinguish 1 from "1". Preserve each pair;
        # ordinary string-keyed objects keep the established Pulse envelope.
        return {"type": "map", "entries": [
            [normalize_query_value(key), normalize_query_value(item)]
            for key, item in value.items()
        ]}
    return value
