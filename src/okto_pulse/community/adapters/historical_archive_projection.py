"""Closed projections of archived formats, independent of retired live ORM types."""

import json

from okto_pulse.core.ports.historical_archive import ArchiveSection
from okto_pulse.core.ports.historical_archive_read import ArchiveReadLimitExceeded, ArchiveReadRequest, ArchiveSectionPage


_CONTENT = (
    "id", "title", "description", "objective", "expected_outcome", "status", "version",
    "start_date", "end_date", "labels", "archived", "pre_archive_status",
    "cancellation_reason", "cancelled_at", "cancelled_by", "created_by", "created_at", "updated_at",
    "skip_test_coverage", "skip_rules_coverage", "skip_qualitative_validation", "validation_threshold",
    "require_task_validation", "validation_min_confidence", "validation_min_completeness", "validation_max_drift",
)
_CONTENT_ALIASES = {"spec_id": "related_spec_id", "spec_version": "related_spec_version",
    "origin_sprint_id": "previous_origin_id", "origin_bug_id": "related_bug_id", "lane_type": "origin_category"}
_QA = ("id", "question", "question_type", "choices", "allow_free_text", "answer", "selected",
    "asked_by", "answered_by", "created_at", "answered_at")
_HISTORY = ("id", "action", "actor_type", "actor_id", "actor_name", "changes", "summary", "version", "created_at")
_JSON_FIELDS = frozenset({"labels", "choices", "selected", "changes", "evaluations"})
_BOOL_FIELDS = frozenset({"archived", "skip_test_coverage", "skip_rules_coverage",
    "skip_qualitative_validation", "require_task_validation", "allow_free_text"})


def _invalid_constant(value):
    raise ValueError("historical_archive_json_constant_invalid")


def _value(cell, field):
    if not isinstance(cell, list) or len(cell) != 2:
        raise ValueError("historical_archive_cell_invalid")
    kind, value = cell
    if kind == "null" and value is None:
        return None
    if kind == "text" and type(value) is str:
        return json.loads(value, parse_constant=_invalid_constant) if field in _JSON_FIELDS else value
    if kind == "integer" and type(value) is str:
        number = int(value)
        if field in _BOOL_FIELDS:
            if number not in (0, 1):
                raise ValueError("historical_archive_boolean_invalid")
            return bool(number)
        return number
    # Projected fields have no binary/native float data. Unknown physical
    # columns stay in privileged evidence instead of becoming public by default.
    raise ValueError("historical_archive_cell_type_unsupported")


def _rows(document, table, owner_column, origin, fields):
    descriptor = document["tables"][table]
    columns = [column["name"] for column in descriptor["columns"]]
    if len(set(columns)) != len(columns) or owner_column not in columns or not set(fields) <= set(columns):
        raise ValueError("historical_archive_projection_schema_invalid")
    owner_index = columns.index(owner_column)
    indexes = {field: columns.index(field) for field in fields}
    records = []
    for row in descriptor["rows"]:
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError("historical_archive_projection_row_invalid")
        if _value(row[owner_index], owner_column) == origin:
            records.append({field: _value(row[index], field) for field, index in indexes.items()})
    return records


def _section_records(document, request: ArchiveReadRequest):
    """Interpret a verified historical format using explicit field allowlists.

    Format v4 stores Sprint history as opaque migration evidence. No live Sprint
    model/service is loaded. Cards, scenario/rule IDs, jobs, other origins and
    access manifests cannot be disclosed by any of these four permissions.
    """
    if (document["format"] != "historical-relational-archive/v4"
            or document["board_id"] != request.scope.board_id
            or document["origin_kind"] != request.scope.origin_kind
            or request.scope.origin_kind != "sprint"):
        raise ValueError("historical_archive_projection_format_unsupported")
    root = _rows(document, "sprints", "id", request.scope.origin_id, ("id",))
    if len(root) != 1:
        raise ValueError("historical_archive_projection_origin_invalid")
    if request.section is ArchiveSection.CONTENT:
        records = _rows(document, "sprints", "id", request.scope.origin_id, (*_CONTENT, *_CONTENT_ALIASES))
        for record in records:
            for before, after in _CONTENT_ALIASES.items():
                record[after] = record.pop(before)
    elif request.section is ArchiveSection.QA:
        records = _rows(document, "sprint_qa_items", "sprint_id", request.scope.origin_id, _QA)
    elif request.section is ArchiveSection.HISTORY:
        records = _rows(document, "sprint_history", "sprint_id", request.scope.origin_id, _HISTORY)
    else:
        records = _rows(document, "sprints", "id", request.scope.origin_id, ("evaluations",))[0]["evaluations"]
        if records is None:
            records = []
        if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
            raise ValueError("historical_archive_evaluations_invalid")
    if len(records) > 100_000:
        # Embedded arrays are not physical SQL rows. Bound the entire section
        # before page one, rather than emitting a cursor the contract cannot read.
        raise ArchiveReadLimitExceeded("historical_archive_section_record_limit")
    return records


def project_historical_archive_section(document, request: ArchiveReadRequest, archive_id: str) -> ArchiveSectionPage:
    records = _section_records(document, request)
    end = request.offset + request.limit
    page = tuple(json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        for record in records[request.offset:end])
    return ArchiveSectionPage(request, archive_id, page, end if end < len(records) else None)


def project_historical_context_record(document, request: ArchiveReadRequest, archive_id: str, selection) -> ArchiveSectionPage:
    """Select only a verified binding's record through the same closed allowlists."""
    records = _section_records(document, request)
    index = selection["record_index"]
    if index is not None:
        if type(index) is not int or not 0 <= index < len(records):
            raise ValueError("historical_context_record_missing")
        selected = [records[index]]
    else:
        selected = [record for record in records if record.get("id") == selection["record_identity"]]
    if len(selected) != 1:
        raise ValueError("historical_context_record_missing")
    record = selected[0]
    field = selection["field"]
    if field is not None:
        # Preserve original attribution, not unrelated source prose or live policy.
        record = {key: record[key] for key in ("id", "title", "created_by", "created_at", "updated_at", field)}
    encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return ArchiveSectionPage(request, archive_id, (encoded,), None)
