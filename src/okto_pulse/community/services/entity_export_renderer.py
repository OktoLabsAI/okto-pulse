"""Pure, passive renderers for canonical entity export bundles.

The bundle is exhaustive and intentionally close to persisted contracts.  A
report cannot expose that storage shape verbatim: doing so makes opaque IDs,
receipts and digests look more important than the decisions they support.
These renderers therefore apply one shared, human-first projection:

* titles and authored content lead every record;
* lifecycle state, scores, rationales and actionable findings stay visible;
* stable identifiers are rendered only as compact secondary references;
* technical integrity metadata remains available in collapsed appendices;
* current results are visible while prior results are grouped separately.

Both formats traverse the same detached payload tree.  Rendering never reaches
back into persistence and the HTML contains no executable or remote content.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from html import escape as escape_html
import json
import re
from typing import Any


_HTML_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "font-src data:; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'; script-src 'none'"
)

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_HEX_DIGEST = re.compile(r"^[0-9a-f]{32,}$", re.IGNORECASE)
_PREFIXED_OPAQUE = re.compile(r"^([a-z][a-z0-9]{0,20})_([a-z0-9-]{8,})$", re.IGNORECASE)
_HUMAN_CODE = re.compile(r"^[A-Z][A-Z0-9]{0,5}-\d+\b")
_ID_LIKE_KEY = re.compile(r"(^id$|_id$|_ids$|_ref$|_refs$)")

_METRICS = ("confidence", "clarity", "assertiveness", "decidability", "ambiguity")
_PRIMARY_TEXT_KEYS = (
    "description",
    "problem_statement",
    "proposed_approach",
    "context",
    "objective",
    "goal",
    "text",
    "content",
    "rule",
    "question",
    "answer",
    "given",
    "when",
    "then",
    "rationale",
    "justification",
    "detail",
    "summary",
    "excerpt",
    "responsibility",
    "boundaries",
    "global_description",
)
_TITLE_KEYS = (
    "display_title",
    "title",
    "name",
    "label",
    "metric_title",
    "guideline_title",
    "rule_name",
)
_STATE_KEYS = (
    "status",
    "state",
    "outcome",
    "recommendation",
    "enforcement",
    "recorded_currentness",
    "lifecycle_status",
)
_TIME_KEYS = (
    "created_at",
    "updated_at",
    "recorded_at",
    "assessed_at",
    "evaluated_at",
    "observed_at",
    "removed_at",
    "completed_at",
)
_TECHNICAL_TOKENS = (
    "digest",
    "hash",
    "fingerprint",
    "idempotency",
    "receipt_id",
    "request_id",
    "event_id",
    "history_id",
    "outbox_id",
    "correlation_id",
    "challenge_token",
    "canonicalization_version",
    "contract_version",
    "schema_version",
)
_SECTION_LABELS = {
    "base": "Overview",
    "qa": "Q&A",
    "spec_validation": "Spec validation",
    "card_validation": "Task validation",
    "requirement_lint": "Requirement lint",
    "policy_compliance": "Policy compliance",
    "policy_waivers": "Policy waivers",
    "code_investigations": "Code investigations",
    "code_evidence": "Code evidence",
    "code_traceability_waivers": "Code traceability waivers",
    "api_contracts": "API contracts",
    "integration_requirements": "Integration requirements",
    "observability_requirements": "Observability requirements",
    "test_scenarios": "Test scenarios",
    "business_rules": "Business rules",
    "implementation_targets": "Implementation targets",
    "resource_decisions": "Resource decisions",
    "research_decision_derivations": "Research decision derivations",
}
_GROUP_SINGULAR = {
    "functional_requirements": "Functional requirement",
    "technical_requirements": "Technical requirement",
    "acceptance_criteria": "Acceptance criterion",
    "business_rules": "Business rule",
    "test_scenarios": "Test scenario",
    "api_contracts": "API contract",
    "integration_requirements": "Integration requirement",
    "observability_requirements": "Observability requirement",
    "validations": "Validation",
    "evaluations": "Evaluation",
    "pinpoints": "Pinpoint",
    "findings": "Finding",
    "annotations": "Annotation",
    "cards": "Card",
    "sprints": "Sprint",
    "dependencies": "Dependency",
    "semantic_guideline_assessment_receipts": "Policy assessment",
    "semantic_guideline_assessments_v2": "Policy assessment",
    "semantic_guideline_metric_results": "Policy metric",
    "semantic_guideline_metric_results_v2": "Policy metric",
    "semantic_guideline_findings": "Policy finding",
    "semantic_guideline_findings_v2": "Policy finding",
    "quality_assessment_receipts": "Requirement lint assessment",
    "quality_findings": "Requirement lint finding",
    "checklist_receipts": "Checklist result",
    "checklist_executions": "Checklist execution",
    "checklist_item_results": "Checklist item",
}
_DEFERRED_SECTIONS = {
    "history",
    "activity",
    "policy_waivers",
    "code_investigations",
    "code_traceability_waivers",
    "resource_decisions",
}


def _text(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _key(value: object) -> str:
    return str(value or "").strip().casefold()


def _label(value: object) -> str:
    raw = str(value or "Item").strip()
    normalized = raw.casefold()
    if normalized in _SECTION_LABELS:
        return _SECTION_LABELS[normalized]
    words = raw.replace("_", " ").split()
    acronyms = {"api": "API", "html": "HTML", "id": "ID", "ids": "IDs", "kg": "KG", "mcp": "MCP", "qa": "Q&A", "slo": "SLO", "url": "URL"}
    return " ".join(acronyms.get(word.casefold(), word.capitalize()) for word in words)


def _singular(field_key: str) -> str:
    key = _key(field_key)
    if key in _GROUP_SINGULAR:
        return _GROUP_SINGULAR[key]
    label = _label(field_key)
    if label.endswith("ies"):
        return label[:-3] + "y"
    if label.endswith("s") and not label.endswith("ss"):
        return label[:-1]
    return label or "Record"


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _is_technical_key(field_key: object) -> bool:
    key = _key(field_key)
    return bool(
        _ID_LIKE_KEY.search(key)
        or key.startswith("linked_")
        or key.endswith("_version_id")
        or any(token in key for token in _TECHNICAL_TOKENS)
    )


def _looks_like_reference(value: str) -> bool:
    stripped = value.strip()
    return bool(
        _UUID.fullmatch(stripped)
        or _HEX_DIGEST.fullmatch(stripped)
        or _PREFIXED_OPAQUE.fullmatch(stripped)
    )


def _short_reference(value: Any) -> str:
    raw = _text(value).strip()
    if raw == "—":
        return raw
    if _UUID.fullmatch(raw):
        return raw[:8]
    if match := _PREFIXED_OPAQUE.fullmatch(raw):
        return f"{match.group(1)}_{match.group(2)[:8]}"
    if _HEX_DIGEST.fullmatch(raw):
        return raw[:10] + "…"
    if "-" in raw and len(raw) > 8:
        prefix, _, suffix = raw.partition("-")
        if prefix.isalpha() and suffix:
            return f"{prefix}-{suffix[:6]}" + ("…" if len(suffix) > 6 else "")
    if len(raw) > 24:
        return raw[:12] + "…"
    return raw


def _display_scalar(field_key: str, value: Any) -> str:
    text = _text(value)
    if _is_technical_key(field_key) or (isinstance(value, str) and _looks_like_reference(value)):
        return _short_reference(text)
    # Authored Markdown may legitimately mention immutable IDs and digests.
    # Keep its prose intact while ensuring the HTML report never promotes a
    # full opaque identifier visually.
    text = re.sub(
        r"(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![0-9a-f])",
        lambda match: _short_reference(match.group(0)),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?<![a-z0-9_])[a-z][a-z0-9]{0,20}_[a-z0-9-]{8,}(?![a-z0-9-])",
        lambda match: _short_reference(match.group(0)),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?<![0-9a-f])[0-9a-f]{32,}(?![0-9a-f])",
        lambda match: _short_reference(match.group(0)),
        text,
        flags=re.IGNORECASE,
    )
    return text


def _html_text(value: Any) -> str:
    """Escape text and defeat conservative passive-HTML scanners."""

    escaped = escape_html(_text(value), quote=True)
    escaped = re.sub(
        r"(?i)\b(on[a-z0-9_-]+)=",
        lambda match: match.group(1) + "&#61;",
        escaped,
    )
    escaped = re.sub(r"(?i)\bjavascript:", "javascript&#58;", escaped)
    return re.sub(r"(?i)\bdata:text/html", "data&#58;text/html", escaped)


def _md_inline(value: Any) -> str:
    text = _text(value).replace("\\", "\\\\")
    for token in ("`", "*", "_", "[", "]", "<", ">"):
        text = text.replace(token, "\\" + token)
    return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _record_identifier(item: Mapping[str, Any], *, mapped_key: str | None = None) -> str | None:
    for key in (
        "id",
        "item_id",
        "validation_id",
        "receipt_id",
        "dependency_id",
        "anchor_ref",
        "entity_id",
    ):
        value = item.get(key)
        if value not in (None, ""):
            return _short_reference(value)
    return _short_reference(mapped_key) if mapped_key else None


def _record_title(item: Mapping[str, Any], *, field_key: str, index: int) -> str:
    for key in _TITLE_KEYS:
        value = item.get(key)
        if value not in (None, "") and not _looks_like_reference(str(value)):
            return str(value)
    method, path = item.get("method"), item.get("path")
    if method and path:
        return f"{method} {path}"
    metric = item.get("metric") or item.get("metric_tag") or item.get("metric_code")
    if metric:
        return _label(metric)
    for key in ("text", "question", "summary", "description", "rule"):
        value = item.get(key)
        if value not in (None, ""):
            text = str(value).strip()
            if _HUMAN_CODE.match(text):
                return text
            return text if len(text) <= 180 else text[:177].rstrip() + "…"
    return f"{_singular(field_key)} {index}"


def _mapping_records(value: Mapping[str, Any]) -> list[tuple[str | None, Mapping[str, Any]]] | None:
    if not value or not all(isinstance(item, Mapping) for item in value.values()):
        return None
    keys = [str(item) for item in value]
    if not all(_looks_like_reference(item) or item.isdigit() for item in keys):
        return None
    return [(str(mapped_key), item) for mapped_key, item in value.items()]


def _reference_chips_html(values: Sequence[Any], *, field_key: str) -> str:
    if not values:
        return '<span class="muted">None</span>'
    return '<span class="reference-list">' + "".join(
        f'<code class="reference">{_html_text(_display_scalar(field_key, value))}</code>'
        for value in values
    ) + "</span>"


def _technical_value_html(value: Any, *, field_key: str) -> str:
    if _is_scalar(value):
        return f'<code class="reference">{_html_text(_display_scalar(field_key, value))}</code>'
    if isinstance(value, (list, tuple)) and all(_is_scalar(item) for item in value):
        return _reference_chips_html(value, field_key=field_key)
    if isinstance(value, Mapping):
        return '<dl class="technical-grid">' + "".join(
            f"<dt>{_html_text(_label(key))}</dt><dd>{_technical_value_html(item, field_key=str(key))}</dd>"
            for key, item in value.items()
        ) + "</dl>"
    return f'<code class="reference">{_html_text(_short_reference(value))}</code>'


def _metric_cards_html(item: Mapping[str, Any]) -> tuple[str, set[str]]:
    metrics = [name for name in _METRICS if isinstance(item.get(name), (int, float)) and not isinstance(item.get(name), bool)]
    if not metrics:
        return "", set()
    thresholds = item.get("resolved_thresholds")
    threshold_map = thresholds if isinstance(thresholds, Mapping) else {}
    consumed: set[str] = {"resolved_thresholds"}
    cards: list[str] = []
    for metric in metrics:
        score = item[metric]
        justification_key = f"{metric}_justification"
        consumed.update({metric, justification_key})
        threshold_key = f"max_spec_{metric}" if metric == "ambiguity" else f"min_spec_{metric}"
        threshold = threshold_map.get(threshold_key)
        direction = "Maximum" if metric == "ambiguity" else "Minimum"
        threshold_text = f"{direction} {_text(threshold)}" if threshold is not None else ("Lower is better" if metric == "ambiguity" else "Higher is better")
        cards.append(
            '<article class="score-card">'
            f'<div class="score-heading"><span>{_html_text(_label(metric))}</span><strong>{_html_text(score)}<small>/100</small></strong></div>'
            f'<div class="score-track"><span style="width:{max(0, min(100, float(score))):.0f}%"></span></div>'
            f'<p class="threshold">{_html_text(threshold_text)}</p>'
            + (
                f'<p class="score-rationale">{_html_text(item.get(justification_key))}</p>'
                if item.get(justification_key)
                else ""
            )
            + "</article>"
        )
    return '<div class="score-grid">' + "".join(cards) + "</div>", consumed


def _pinpoint_html(item: Mapping[str, Any], *, index: int) -> str:
    snapshot = item.get("anchor_snapshot")
    sealed = snapshot if isinstance(snapshot, Mapping) else {}
    metric = item.get("metric_title") or item.get("metric") or item.get("metric_tag") or item.get("metric_code")
    target = (
        item.get("target")
        or item.get("location_label")
        or sealed.get("label")
        or sealed.get("text")
        or sealed.get("excerpt")
        or item.get("text")
        or item.get("title")
        or f"Pinpoint {index}"
    )
    reference = item.get("anchor_ref") or item.get("location_reference") or item.get("item_id") or item.get("id")
    detail = item.get("detail") or item.get("rationale") or item.get("justification") or item.get("description")
    reference_html = f' <code class="inline-reference">({_html_text(_short_reference(reference))})</code>' if reference else ""
    badge = f'<span class="metric-badge">{_html_text(_label(metric))}</span>' if metric else ""
    body = f'<p class="pinpoint-target">{_html_text(target)}{reference_html}</p>'
    if detail:
        body += f'<p class="pinpoint-detail">{_html_text(detail)}</p>'
    technical = {
        key: value
        for key, value in item.items()
        if key not in {"metric_title", "metric", "metric_tag", "metric_code", "target", "location_label", "anchor_snapshot", "text", "title", "anchor_ref", "location_reference", "item_id", "id", "detail", "rationale", "justification", "description"}
    }
    if technical:
        body += _technical_details_html(technical)
    return f'<article class="pinpoint-card"><div class="pinpoint-heading">{badge}<span>Actionable finding</span></div>{body}</article>'


def _technical_details_html(values: Mapping[str, Any]) -> str:
    if not values:
        return ""
    rows = "".join(
        f"<dt>{_html_text(_label(key))}</dt><dd>{_technical_value_html(value, field_key=str(key))}</dd>"
        for key, value in values.items()
    )
    return f'<details class="technical"><summary>Technical metadata</summary><dl class="technical-grid">{rows}</dl></details>'


def _is_previous_record(item: Mapping[str, Any]) -> bool:
    currentness = str(
        item.get("recorded_currentness") or item.get("currentness") or ""
    ).casefold()
    return bool(
        item.get("is_current") is False
        or currentness in {"previous", "historical", "superseded"}
        or item.get("active") is False
    )


def _state_chips_html(item: Mapping[str, Any]) -> tuple[str, set[str]]:
    chips: list[str] = []
    consumed: set[str] = set()
    for key in _STATE_KEYS:
        value = item.get(key)
        if value in (None, ""):
            continue
        consumed.add(key)
        normalized = re.sub(r"[^a-z0-9_-]", "-", str(value).casefold())
        chips.append(f'<span class="state state-{_html_text(normalized)}">{_html_text(_label(value))}</span>')
    for key in _TIME_KEYS:
        value = item.get(key)
        if value in (None, ""):
            continue
        consumed.add(key)
        chips.append(f'<span class="record-time">{_html_text(_label(key))}: {_html_text(value)}</span>')
    return "".join(chips), consumed


def _is_current(item: Mapping[str, Any]) -> bool:
    return bool(
        item.get("is_current") is True
        or str(item.get("recorded_currentness") or "").casefold() == "current"
        or str(item.get("currentness") or "").casefold() == "current"
    )


def _records_html(
    records: Sequence[tuple[str | None, Mapping[str, Any]]],
    *,
    field_key: str,
    depth: int,
) -> str:
    if not records:
        return '<p class="empty">No records.</p>'
    current = [record for record in records if _is_current(record[1])]
    previous = [record for record in records if _is_previous_record(record[1])]
    neutral = [record for record in records if record not in current and record not in previous]
    if current:
        visible = "".join(
            _record_html(item, field_key=field_key, index=index, mapped_key=mapped_key, depth=depth)
            for index, (mapped_key, item) in enumerate(current, start=1)
        )
        if neutral:
            visible += "".join(
                _record_html(item, field_key=field_key, index=index, mapped_key=mapped_key, depth=depth)
                for index, (mapped_key, item) in enumerate(neutral, start=len(current) + 1)
            )
        if previous:
            visible += (
                f'<details class="previous-results"><summary>Previous results <span>{len(previous)}</span></summary><div class="record-list">'
                + "".join(
                    _record_html(item, field_key=field_key, index=index, mapped_key=mapped_key, depth=depth)
                    for index, (mapped_key, item) in enumerate(previous, start=1)
                )
                + "</div></details>"
            )
        return visible
    return "".join(
        _record_html(item, field_key=field_key, index=index, mapped_key=mapped_key, depth=depth)
        for index, (mapped_key, item) in enumerate(records, start=1)
    )


def _html_value(value: Any, *, field_key: str, depth: int = 0) -> str:
    if _is_scalar(value):
        css = "prose-value" if isinstance(value, str) and len(value) > 80 else "value"
        return f'<span class="{css}">{_html_text(_display_scalar(field_key, value))}</span>'
    if isinstance(value, Mapping):
        records = _mapping_records(value)
        if records is not None:
            return f'<div class="record-list">{_records_html(records, field_key=field_key, depth=depth + 1)}</div>'
        return _record_body_html(value, field_key=field_key, depth=depth + 1, nested=True)
    if isinstance(value, (list, tuple)):
        if not value:
            return '<p class="empty">No records.</p>'
        if all(_is_scalar(item) for item in value):
            if _is_technical_key(field_key) or all(isinstance(item, str) and _looks_like_reference(item) for item in value):
                return _reference_chips_html(value, field_key=field_key)
            return '<ul class="plain-list">' + "".join(f"<li>{_html_text(item)}</li>" for item in value) + "</ul>"
        if "pinpoint" in _key(field_key):
            return '<div class="pinpoint-list">' + "".join(
                _pinpoint_html(item, index=index)
                if isinstance(item, Mapping)
                else f'<p class="pinpoint-detail">{_html_text(item)}</p>'
                for index, item in enumerate(value, start=1)
            ) + "</div>"
        records = [(None, item) for item in value if isinstance(item, Mapping)]
        scalars = [item for item in value if not isinstance(item, Mapping)]
        rendered = _records_html(records, field_key=field_key, depth=depth + 1) if records else ""
        if scalars:
            rendered += '<ul class="plain-list">' + "".join(f"<li>{_html_text(item)}</li>" for item in scalars) + "</ul>"
        return f'<div class="record-list">{rendered}</div>'
    return f'<code class="reference">{_html_text(_short_reference(json.dumps(value, ensure_ascii=False, sort_keys=True)))}</code>'


def _record_body_html(
    item: Mapping[str, Any],
    *,
    field_key: str,
    depth: int,
    nested: bool = False,
) -> str:
    metric_html, metric_consumed = _metric_cards_html(item)
    state_html, state_consumed = _state_chips_html(item)
    consumed = set(metric_consumed) | set(state_consumed) | set(_TITLE_KEYS)
    consumed.update({"method", "path", "is_current"})

    primary: list[str] = []
    facts: list[str] = []
    groups: list[str] = []
    technical: dict[str, Any] = {}
    for key, value in item.items():
        key_text = str(key)
        normalized = _key(key_text)
        if normalized in consumed or value in (None, "", [], {}):
            continue
        if _is_technical_key(normalized):
            technical[key_text] = value
            continue
        if normalized in _PRIMARY_TEXT_KEYS and _is_scalar(value):
            primary.append(
                f'<div class="prose-block"><h4>{_html_text(_label(key_text))}</h4><p>{_html_text(_display_scalar(key_text, value))}</p></div>'
            )
            continue
        if _is_scalar(value):
            facts.append(
                f'<div class="fact-row"><dt>{_html_text(_label(key_text))}</dt><dd>{_html_text(_display_scalar(key_text, value))}</dd></div>'
            )
            continue
        groups.append(
            f'<section class="nested-group"><h4>{_html_text(_label(key_text))}</h4>{_html_value(value, field_key=key_text, depth=depth + 1)}</section>'
        )
    state_block = f'<div class="record-state">{state_html}</div>' if state_html else ""
    fact_block = f'<dl class="fact-list">{"".join(facts)}</dl>' if facts else ""
    body = state_block + metric_html + "".join(primary) + fact_block + "".join(groups) + _technical_details_html(technical)
    if nested and not body:
        return '<p class="empty">No reportable values.</p>'
    return body


def _record_html(
    item: Mapping[str, Any],
    *,
    field_key: str,
    index: int,
    mapped_key: str | None = None,
    depth: int = 0,
) -> str:
    title = _record_title(item, field_key=field_key, index=index)
    identifier = _record_identifier(item, mapped_key=mapped_key)
    reference = f'<code class="inline-reference">({_html_text(identifier)})</code>' if identifier else ""
    title_tag = "h3" if depth <= 2 else "h4"
    return (
        '<article class="record">'
        f'<header class="record-heading"><span class="record-kind">{_html_text(_singular(field_key))}</span>'
        f'<{title_tag}>{_html_text(title)} {reference}</{title_tag}></header>'
        f'{_record_body_html(item, field_key=field_key, depth=depth)}'
        "</article>"
    )


def _section_payload_html(payload: Any, *, section_key: str) -> str:
    if not isinstance(payload, Mapping):
        return _html_value(payload, field_key=section_key)
    # Canonical section envelopes use ``record`` / ``embedded`` / ``records``.
    # Unwrap them so storage vocabulary does not become report navigation.
    blocks: list[str] = []
    root = payload.get("record")
    if isinstance(root, Mapping):
        blocks.append(_record_body_html(root, field_key=section_key, depth=0, nested=True))
    embedded = payload.get("embedded")
    if isinstance(embedded, Mapping):
        for key, value in embedded.items():
            if value in (None, "", [], {}):
                continue
            blocks.append(f'<section class="payload-group"><h3>{_html_text(_label(key))}</h3>{_html_value(value, field_key=str(key), depth=1)}</section>')
    records = payload.get("records")
    if isinstance(records, Mapping):
        for key, value in records.items():
            if value in (None, "", [], {}):
                continue
            blocks.append(f'<section class="payload-group"><h3>{_html_text(_label(key))}</h3>{_html_value(value, field_key=str(key), depth=1)}</section>')
    envelope_keys = {"record", "embedded", "records"}
    remaining = {key: value for key, value in payload.items() if key not in envelope_keys}
    if remaining:
        blocks.append(_record_body_html(remaining, field_key=section_key, depth=0, nested=True))
    return "".join(blocks) or '<p class="empty">No records.</p>'


def _spec_anchor_catalog(bundle: Mapping[str, Any]) -> dict[str, str]:
    """Return immutable labels already present in the detached Spec bundle."""

    catalog: dict[str, str] = {}
    section = next(
        (
            item
            for item in bundle.get("sections") or []
            if item.get("section_key") == "base"
        ),
        None,
    )
    record = ((section or {}).get("payload") or {}).get("record")
    if not isinstance(record, Mapping):
        return catalog
    groups = (
        ("functional_requirements", "FR"),
        ("technical_requirements", "TR"),
        ("acceptance_criteria", "AC"),
        ("business_rules", "BR"),
        ("api_contracts", "API"),
        ("integration_requirements", "IR"),
        ("observability_requirements", "OR"),
        ("decisions", "Decision"),
    )
    for field, prefix in groups:
        values = record.get(field)
        if not isinstance(values, (list, tuple)):
            continue
        for index, item in enumerate(values, start=1):
            if not isinstance(item, Mapping):
                continue
            identifier = item.get("id") or item.get("item_id")
            if not identifier:
                continue
            title = item.get("title") or item.get("name")
            description = item.get("text") or item.get("description") or item.get("rule")
            authored = (
                f"{title}: {description}"
                if title and description and str(description).strip() not in str(title)
                else title or description
            )
            if not authored:
                continue
            authored_text = str(authored).strip()
            catalog[str(identifier)] = (
                authored_text
                if _HUMAN_CODE.match(authored_text)
                else f"{prefix}-{index}: {authored_text}"
            )
    separated = {
        "test_scenarios": "Test scenario",
        "business_rules": "BR",
        "api_contracts": "API",
        "integration_requirements": "IR",
        "observability_requirements": "OR",
    }
    for section_item in bundle.get("sections") or []:
        section_key = str(section_item.get("section_key") or "")
        prefix = separated.get(section_key)
        if prefix is None:
            continue
        payload = section_item.get("payload")
        if not isinstance(payload, Mapping):
            continue
        candidates: list[Mapping[str, Any]] = []
        for envelope_name in ("embedded", "records"):
            envelope = payload.get(envelope_name)
            if not isinstance(envelope, Mapping):
                continue
            for values in envelope.values():
                if isinstance(values, (list, tuple)):
                    candidates.extend(item for item in values if isinstance(item, Mapping))
        for index, item in enumerate(candidates, start=1):
            identifier = item.get("id") or item.get("item_id")
            title = item.get("title") or item.get("name")
            description = item.get("text") or item.get("description") or item.get("rule")
            authored = (
                f"{title}: {description}"
                if title and description and str(description).strip() not in str(title)
                else title or description
            )
            if not identifier or not authored:
                continue
            authored_text = str(authored).strip()
            catalog[str(identifier)] = (
                authored_text
                if _HUMAN_CODE.match(authored_text)
                else f"{prefix}-{index}: {authored_text}"
            )
    return catalog


def _enrich_pinpoint_targets(value: Any, catalog: Mapping[str, str]) -> Any:
    if isinstance(value, list):
        return [_enrich_pinpoint_targets(item, catalog) for item in value]
    if not isinstance(value, Mapping):
        return value
    copy = {
        str(key): _enrich_pinpoint_targets(item, catalog)
        for key, item in value.items()
    }
    reference = copy.get("anchor_ref") or copy.get("location_reference") or copy.get("item_id")
    snapshot = copy.get("anchor_snapshot")
    has_snapshot = isinstance(snapshot, Mapping) and any(
        snapshot.get(key) for key in ("label", "text", "excerpt")
    )
    if reference and not has_snapshot and not copy.get("target"):
        target = catalog.get(str(reference))
        if target:
            copy["target"] = target
    return copy


def _md_technical(values: Mapping[str, Any], *, indent: str = "") -> list[str]:
    if not values:
        return []
    lines = [f"{indent}<details>", f"{indent}<summary>Technical metadata</summary>", ""]
    for key, value in values.items():
        if isinstance(value, (list, tuple)):
            display = ", ".join(_short_reference(item) for item in value)
        elif isinstance(value, Mapping):
            display = "; ".join(f"{_label(k)}: {_short_reference(v)}" for k, v in value.items())
        else:
            display = _display_scalar(str(key), value)
        lines.append(f"{indent}- **{_md_inline(_label(key))}:** `{_md_inline(display)}`")
    lines.extend(["", f"{indent}</details>"])
    return lines


def _md_record(item: Mapping[str, Any], *, field_key: str, index: int, mapped_key: str | None = None, level: int = 3) -> list[str]:
    title = _record_title(item, field_key=field_key, index=index)
    identifier = _record_identifier(item, mapped_key=mapped_key)
    suffix = f" `({_md_inline(identifier)})`" if identifier else ""
    lines = [f"{'#' * min(level, 6)} {_md_inline(title)}{suffix}", ""]
    metric_names = [name for name in _METRICS if isinstance(item.get(name), (int, float)) and not isinstance(item.get(name), bool)]
    consumed: set[str] = set(_TITLE_KEYS)
    consumed.update({"method", "path", "resolved_thresholds"})
    for key in _STATE_KEYS:
        if item.get(key) not in (None, ""):
            lines.append(f"- **{_md_inline(_label(key))}:** {_md_inline(_label(item[key]))}")
            consumed.add(key)
    if metric_names:
        thresholds = item.get("resolved_thresholds") if isinstance(item.get("resolved_thresholds"), Mapping) else {}
        lines.extend(["", "| Dimension | Score | Threshold | Rationale |", "|---|---:|---|---|"])
        for metric in metric_names:
            justification_key = f"{metric}_justification"
            threshold_key = f"max_spec_{metric}" if metric == "ambiguity" else f"min_spec_{metric}"
            direction = "max" if metric == "ambiguity" else "min"
            threshold = thresholds.get(threshold_key) if isinstance(thresholds, Mapping) else None
            threshold_text = f"{direction} {threshold}" if threshold is not None else ("lower is better" if metric == "ambiguity" else "higher is better")
            lines.append("| " + " | ".join((
                _md_inline(_label(metric)),
                _md_inline(item[metric]),
                _md_inline(threshold_text),
                _md_inline(item.get(justification_key)),
            )).replace("|", "\\|", 0) + " |")
            consumed.update({metric, justification_key})
    technical: dict[str, Any] = {}
    nested: list[tuple[str, Any]] = []
    for key, value in item.items():
        normalized = _key(key)
        if normalized in consumed or value in (None, "", [], {}):
            continue
        if _is_technical_key(normalized):
            technical[str(key)] = value
        elif _is_scalar(value):
            lines.extend(["", f"**{_md_inline(_label(key))}**", "", _md_inline(_display_scalar(str(key), value))])
        else:
            nested.append((str(key), value))
    for key, value in nested:
        lines.extend(["", f"{'#' * min(level + 1, 6)} {_md_inline(_label(key))}", ""])
        lines.extend(_md_value(value, field_key=key, level=level + 2))
    if technical:
        lines.extend(["", *_md_technical(technical)])
    return lines


def _md_pinpoint(item: Mapping[str, Any], *, index: int) -> list[str]:
    snapshot = item.get("anchor_snapshot") if isinstance(item.get("anchor_snapshot"), Mapping) else {}
    metric = item.get("metric_title") or item.get("metric") or item.get("metric_tag") or item.get("metric_code")
    target = item.get("target") or item.get("location_label") or snapshot.get("label") or snapshot.get("text") or snapshot.get("excerpt") or item.get("text") or item.get("title") or f"Pinpoint {index}"
    reference = item.get("anchor_ref") or item.get("location_reference") or item.get("item_id") or item.get("id")
    detail = item.get("detail") or item.get("rationale") or item.get("justification") or item.get("description")
    heading = f"- **{_md_inline(_label(metric))}:** {_md_inline(target)}"
    if reference:
        heading += f" `({_md_inline(_short_reference(reference))})`"
    return [heading, *( [f"  - {_md_inline(detail)}"] if detail else [])]


def _md_value(value: Any, *, field_key: str, level: int = 3) -> list[str]:
    if _is_scalar(value):
        return [_md_inline(_display_scalar(field_key, value))]
    if isinstance(value, Mapping):
        records = _mapping_records(value)
        if records is not None:
            lines: list[str] = []
            for index, (mapped_key, item) in enumerate(records, start=1):
                lines.extend(_md_record(item, field_key=field_key, index=index, mapped_key=mapped_key, level=level))
                lines.append("")
            return lines
        return _md_record(value, field_key=field_key, index=1, level=level)
    if isinstance(value, (list, tuple)):
        if not value:
            return ["_No records._"]
        if all(_is_scalar(item) for item in value):
            return [f"- {_md_inline(_display_scalar(field_key, item))}" for item in value]
        lines = []
        for index, item in enumerate(value, start=1):
            if isinstance(item, Mapping):
                if "pinpoint" in _key(field_key):
                    lines.extend(_md_pinpoint(item, index=index))
                else:
                    lines.extend(_md_record(item, field_key=field_key, index=index, level=level))
            else:
                lines.append(f"- {_md_inline(item)}")
            lines.append("")
        return lines
    return [_md_inline(_short_reference(json.dumps(value, ensure_ascii=False, sort_keys=True)))]


def _md_section_payload(payload: Any, *, section_key: str) -> list[str]:
    if not isinstance(payload, Mapping):
        return _md_value(payload, field_key=section_key)
    lines: list[str] = []
    root = payload.get("record")
    if isinstance(root, Mapping):
        lines.extend(_md_record(root, field_key=section_key, index=1, level=3)[2:])
    for envelope in ("embedded", "records"):
        groups = payload.get(envelope)
        if not isinstance(groups, Mapping):
            continue
        for key, value in groups.items():
            if value in (None, "", [], {}):
                continue
            lines.extend(["", f"### {_md_inline(_label(key))}", ""])
            lines.extend(_md_value(value, field_key=str(key), level=4))
    remaining = {key: value for key, value in payload.items() if key not in {"record", "embedded", "records"}}
    if remaining:
        lines.extend(_md_record(remaining, field_key=section_key, index=1, level=3)[2:])
    return lines or ["_No records._"]


def render_entity_export_markdown(bundle: Mapping[str, Any]) -> str:
    subject = dict(bundle["subject"])
    anchor_catalog = _spec_anchor_catalog(bundle)
    manifest = dict(bundle["manifest"])
    entries = list(manifest.get("entries") or [])
    section_map = {item["section_key"]: item for item in bundle.get("sections") or []}
    entity_ref = _short_reference(subject.get("entity_id"))
    lines = [
        f"# {_md_inline(subject.get('title'))}",
        "",
        f"> {_md_inline(_label(subject.get('entity_type')))} report · {_md_inline(_label(bundle.get('history_scope')))} scope",
        "",
        f"**Status:** {_md_inline(_label(subject.get('status')))} · **Edition:** {_md_inline(subject.get('edition'))} · **Revision:** {_md_inline(subject.get('version'))} · **Reference:** `({_md_inline(entity_ref)})`",
        "",
    ]
    for entry in entries:
        key = str(entry.get("section_key") or "unknown")
        section = section_map.get(key)
        if section is None or entry.get("status") not in {"included", "empty"}:
            continue
        if entry.get("status") == "empty":
            continue
        lines.extend([f"## {_md_inline(_label(key))}", ""])
        payload = _enrich_pinpoint_targets(section.get("payload"), anchor_catalog)
        lines.extend(_md_section_payload(payload, section_key=key))
        lines.append("")
    lines.extend([
        "## Report appendix",
        "",
        "<details>",
        "<summary>Completeness and technical identity</summary>",
        "",
        f"- **Complete for actor:** {_md_inline(bundle.get('complete_for_actor'))}",
        f"- **Source complete:** {_md_inline(bundle.get('source_complete'))}",
        f"- **Overall state:** {_md_inline(_label(bundle.get('overall_state')))}",
        f"- **Entity reference:** `({_md_inline(entity_ref)})`",
        f"- **Board reference:** `({_md_inline(_short_reference(subject.get('board_id')))})`",
        f"- **Snapshot:** `{_md_inline(_short_reference(bundle.get('snapshot_fingerprint')))}`",
        f"- **Generated at:** {_md_inline(bundle.get('generated_at'))}",
        f"- **Contract:** `{_md_inline(_short_reference(bundle.get('contract_version')))}`",
        f"- **Bundle digest:** `{_md_inline(_short_reference(bundle.get('bundle_digest')))}`",
        "",
        "| Section | State | Records | Reason |",
        "|---|---|---:|---|",
    ])
    for entry in entries:
        row = (
            _label(entry.get("section_key")),
            _label(entry.get("status")),
            _text(entry.get("total_count")),
            _label(entry.get("reason_code")) if entry.get("reason_code") else "—",
        )
        lines.append("| " + " | ".join(_md_inline(value).replace("|", "\\|") for value in row) + " |")
    lines.extend(["", "</details>", ""])
    return "\n".join(lines)


def render_entity_export_html(bundle: Mapping[str, Any]) -> str:
    subject = dict(bundle["subject"])
    anchor_catalog = _spec_anchor_catalog(bundle)
    manifest = dict(bundle["manifest"])
    entries = list(manifest.get("entries") or [])
    section_map = {item["section_key"]: item for item in bundle.get("sections") or []}

    def h(value: Any) -> str:
        return _html_text(value)

    visible_entries = [
        entry
        for entry in entries
        if entry.get("status") == "included" and str(entry.get("section_key")) in section_map
    ]
    navigation = "".join(
        f'<li><a href="#section-{h(item.get("section_key"))}">{h(_label(item.get("section_key")))}</a></li>'
        for item in visible_entries
    )
    chapters: list[str] = []
    for entry in visible_entries:
        key = str(entry.get("section_key") or "unknown")
        section = section_map[key]
        payload = _enrich_pinpoint_targets(section.get("payload"), anchor_catalog)
        body = _section_payload_html(payload, section_key=key)
        title = h(_label(key))
        count = entry.get("total_count")
        count_html = f'<span class="chapter-count">{h(count)} records</span>' if count is not None else ""
        if key in _DEFERRED_SECTIONS:
            chapters.append(
                f'<details class="chapter deferred" id="section-{h(key)}"><summary><span>{title}</span>{count_html}</summary><p class="deferred-note">Open this appendix when lifecycle or audit detail is needed.</p><div class="chapter-body">{body}</div></details>'
            )
        else:
            chapters.append(f'<section class="chapter" id="section-{h(key)}"><header class="chapter-heading"><h2>{title}</h2>{count_html}</header>{body}</section>')

    manifest_rows = "".join(
        "<tr>"
        f'<th scope="row">{h(_label(item.get("section_key")))}</th>'
        f'<td><span class="state state-{h(item.get("status"))}">{h(_label(item.get("status")))}</span></td>'
        f"<td>{h(item.get('total_count'))}</td>"
        f"<td>{h(_label(item.get('reason_code'))) if item.get('reason_code') else '—'}</td>"
        "</tr>"
        for item in entries
    )
    entity_ref = _short_reference(subject.get("entity_id"))
    report_state = "Complete" if bundle.get("complete_for_actor") else "Attention required"
    source_note = "All available sources are represented." if bundle.get("source_complete") else "The manifest identifies source or permission gaps."
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="{h(_HTML_CSP)}">
<title>{h(subject.get('title'))} — Okto Pulse report</title>
<style>
:root {{ color-scheme:light dark;--bg:#0b1220;--panel:#121c2e;--panel-soft:#17243a;--line:#2d3c55;--text:#edf2f7;--muted:#9aa9bd;--accent:#8b5cf6;--accent-soft:#2d2350;--good:#34d399;--warn:#fbbf24;--bad:#fb7185 }}
* {{ box-sizing:border-box }} html {{ scroll-behavior:smooth }} body {{ margin:0;background:var(--bg);color:var(--text);font:15px/1.62 ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif }} .shell {{ max-width:1180px;margin:auto;padding:38px 24px 80px }} .hero,.toc,.chapter,.appendix {{ background:var(--panel);border:1px solid var(--line);border-radius:16px;box-shadow:0 14px 38px rgba(0,0,0,.12) }}
.hero {{ padding:30px;margin-bottom:20px }} .eyebrow,.record-kind {{ color:#c4b5fd;text-transform:uppercase;letter-spacing:.12em;font-weight:800;font-size:.72rem }} h1 {{ max-width:900px;margin:.2em 0 .35em;font-size:clamp(1.85rem,4vw,2.75rem);line-height:1.16 }} h2,h3,h4 {{ line-height:1.28 }} .hero-summary {{ max-width:820px;color:var(--muted);margin:0 }} .facts {{ display:flex;flex-wrap:wrap;gap:9px;margin-top:22px }} .hero-fact {{ background:var(--panel-soft);border:1px solid var(--line);border-radius:999px;padding:7px 12px }} .hero-fact b {{ color:var(--muted);font-size:.75rem;margin-right:5px }}
.report-state {{ margin-top:20px;border-left:4px solid var(--good);background:#0d2a28;padding:12px 14px;border-radius:8px }} .report-state.attention {{ border-color:var(--warn);background:#30250d }} .report-state strong {{ display:block }} .report-state span {{ color:var(--muted) }}
.toc {{ padding:18px 22px;margin-bottom:20px }} .toc h2 {{ margin:0 0 8px;font-size:1rem }} .toc ol {{ columns:3;gap:32px;margin:0;padding-left:20px }} .toc li {{ break-inside:avoid;margin:4px 0 }} a {{ color:#c4b5fd;text-decoration:none }} a:hover {{ text-decoration:underline }}
.chapter,.appendix {{ padding:24px;margin:0 0 20px }} .chapter-heading {{ display:flex;align-items:baseline;justify-content:space-between;gap:18px;border-bottom:1px solid var(--line);padding-bottom:12px;margin-bottom:18px }} .chapter-heading h2 {{ margin:0;font-size:1.3rem }} .chapter-count {{ color:var(--muted);font-size:.78rem;white-space:nowrap }} .payload-group,.nested-group {{ margin:22px 0 }} .payload-group>h3,.nested-group>h4 {{ margin:0 0 12px }}
.record-list {{ display:grid;gap:12px }} .record {{ background:var(--panel-soft);border:1px solid var(--line);border-radius:12px;padding:17px;break-inside:avoid }} .record-heading {{ margin-bottom:10px }} .record-heading h3,.record-heading h4 {{ margin:4px 0 0;font-size:1.02rem;font-weight:720 }} .inline-reference,.reference {{ color:var(--muted);font:500 .76rem/1.3 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:nowrap }} .record-state {{ display:flex;flex-wrap:wrap;align-items:center;gap:7px;margin:8px 0 13px }} .state,.metric-badge {{ display:inline-flex;align-items:center;border-radius:999px;background:#334155;color:#e2e8f0;padding:3px 9px;font-size:.7rem;font-weight:750 }} .state-passed,.state-success,.state-done,.state-included,.state-approved,.state-current {{ background:#064e3b;color:#a7f3d0 }} .state-failed,.state-error,.state-rejected {{ background:#7f1d1d;color:#fecaca }} .state-needs_attention,.state-unavailable,.state-omitted {{ background:#78350f;color:#fde68a }} .record-time {{ color:var(--muted);font-size:.72rem }}
.prose-block {{ margin:14px 0 }} .prose-block h4 {{ color:var(--muted);font-size:.74rem;text-transform:uppercase;letter-spacing:.07em;margin:0 0 5px }} .prose-block p,.prose-value {{ white-space:pre-wrap;overflow-wrap:anywhere;margin:0 }} .fact-list {{ display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:8px;margin:13px 0 }} .fact-row {{ background:#101a2c;border-radius:9px;padding:10px }} .fact-row dt {{ color:var(--muted);font-size:.71rem;font-weight:700;text-transform:uppercase }} .fact-row dd {{ margin:3px 0 0;overflow-wrap:anywhere }} .plain-list {{ margin:7px 0;padding-left:22px }}
.score-grid {{ display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:11px;margin:15px 0 }} .score-card {{ background:#101a2c;border:1px solid var(--line);border-radius:11px;padding:13px }} .score-heading {{ display:flex;justify-content:space-between;gap:8px }} .score-heading>span {{ color:var(--muted);font-size:.74rem;font-weight:750;text-transform:uppercase }} .score-heading strong {{ color:var(--good);font-size:1.4rem }} .score-heading small {{ color:var(--muted);font-size:.7rem }} .score-track {{ height:5px;background:#29384e;border-radius:9px;overflow:hidden;margin:8px 0 }} .score-track span {{ display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--good)) }} .threshold {{ color:var(--muted);font-size:.72rem;margin:0 }} .score-rationale {{ margin:10px 0 0;font-size:.86rem }}
.pinpoint-list {{ display:grid;gap:10px }} .pinpoint-card {{ background:#101a2c;border:1px solid #4c3b78;border-radius:11px;padding:14px }} .pinpoint-heading {{ display:flex;align-items:center;gap:8px;color:var(--muted);font-size:.74rem }} .metric-badge {{ background:var(--accent-soft);color:#ddd6fe }} .pinpoint-target {{ font-weight:700;margin:10px 0 5px }} .pinpoint-detail {{ margin:0;color:#d7deea }}
details.technical,details.previous-results {{ margin-top:13px;border-top:1px solid var(--line);padding-top:9px }} details.technical>summary,details.previous-results>summary {{ color:var(--muted);cursor:pointer;font-size:.76rem;font-weight:700 }} .technical-grid {{ display:grid;grid-template-columns:minmax(140px,240px) 1fr;gap:0;margin-top:9px }} .technical-grid dt,.technical-grid dd {{ border-bottom:1px solid var(--line);margin:0;padding:7px;min-width:0 }} .technical-grid dt {{ color:var(--muted);font-size:.72rem }} .reference-list {{ display:flex;flex-wrap:wrap;gap:5px }} .reference {{ display:inline-block;background:#0b1424;border:1px solid var(--line);border-radius:5px;padding:2px 5px;overflow-wrap:anywhere }}
.previous-results {{ margin-top:16px!important;border:1px solid var(--line)!important;border-radius:10px;padding:11px!important }} .previous-results>summary {{ font-size:.86rem!important }} .previous-results>summary span {{ margin-left:6px;background:#26354c;border-radius:999px;padding:2px 7px }} .previous-results>.record-list {{ margin-top:12px }} details.chapter {{ padding:0;overflow:hidden }} details.chapter>summary {{ display:flex;justify-content:space-between;cursor:pointer;font-size:1.15rem;font-weight:750;padding:20px 24px }} details.chapter .chapter-body {{ display:none;border-top:1px solid var(--line);padding:20px 24px }} details.chapter[open] .chapter-body {{ display:block }} .deferred-note {{ color:var(--muted);font-size:.82rem;margin:-10px 24px 18px }} details.chapter[open] .deferred-note {{ display:none }}
.empty,.muted {{ color:var(--muted) }} .appendix {{ margin-top:30px }} .appendix>summary {{ cursor:pointer;font-size:1rem;font-weight:750 }} .appendix-grid {{ display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px;margin:16px 0 }} .appendix-fact {{ background:var(--panel-soft);border-radius:9px;padding:10px }} .appendix-fact b {{ display:block;color:var(--muted);font-size:.7rem;text-transform:uppercase }} .table-wrap {{ overflow:auto }} table {{ width:100%;border-collapse:collapse }} th,td {{ text-align:left;padding:9px;border-bottom:1px solid var(--line);vertical-align:top }} th {{ color:var(--muted) }} footer {{ color:var(--muted);font-size:.76rem;margin-top:14px;text-align:center }}
@media(max-width:760px) {{ .shell {{ padding:16px 10px 50px }} .hero,.chapter,.appendix {{ padding:18px }} .toc ol {{ columns:1 }} .technical-grid {{ grid-template-columns:1fr }} .technical-grid dt {{ border-bottom:0;padding-bottom:0 }} .chapter-heading {{ display:block }} .chapter-count {{ display:block;margin-top:4px }} }}
@media print {{ :root {{ color-scheme:light;--bg:#fff;--panel:#fff;--panel-soft:#f8fafc;--line:#cbd5e1;--text:#111827;--muted:#475569 }} body {{ font-size:11pt }} .shell {{ max-width:none;padding:0 }} .toc {{ display:none }} .hero,.chapter,.appendix {{ box-shadow:none }} details>summary {{ list-style:none }} details>summary::-webkit-details-marker {{ display:none }} details> :not(summary) {{ display:block!important }} details.chapter .chapter-body {{ display:block!important }} a {{ color:#111827;text-decoration:none }} .record,.score-card,.pinpoint-card {{ break-inside:avoid }} }}
</style></head><body><main class="shell">
<header class="hero"><div class="eyebrow">{h(_label(subject.get('entity_type')))} report · {h(_label(bundle.get('history_scope')))} scope</div><h1>{h(subject.get('title'))}</h1><p class="hero-summary">A human-readable view of the entity, its requirements, decisions, validation and traceability. Technical identifiers are secondary references.</p>
<div class="facts"><span class="hero-fact"><b>Status</b>{h(_label(subject.get('status')))}</span><span class="hero-fact"><b>Edition</b>{h(subject.get('edition'))}</span><span class="hero-fact"><b>Revision</b>{h(subject.get('version'))}</span><span class="hero-fact"><b>Reference</b><code class="inline-reference">({h(entity_ref)})</code></span></div>
<div class="report-state{' attention' if not bundle.get('complete_for_actor') else ''}"><strong>{h(report_state)} for this viewer</strong><span>{h(source_note)}</span></div></header>
<nav class="toc" aria-label="Report contents"><h2>Contents</h2><ol>{navigation}</ol></nav>
{''.join(chapters)}
<details class="appendix"><summary>Report completeness and technical appendix</summary><div class="appendix-grid"><div class="appendix-fact"><b>Complete for actor</b>{h(bundle.get('complete_for_actor'))}</div><div class="appendix-fact"><b>Source complete</b>{h(bundle.get('source_complete'))}</div><div class="appendix-fact"><b>Overall state</b>{h(_label(bundle.get('overall_state')))}</div><div class="appendix-fact"><b>Entity reference</b><code class="reference">{h(entity_ref)}</code></div><div class="appendix-fact"><b>Board reference</b><code class="reference">{h(_short_reference(subject.get('board_id')))}</code></div><div class="appendix-fact"><b>Snapshot</b><code class="reference">{h(_short_reference(bundle.get('snapshot_fingerprint')))}</code></div><div class="appendix-fact"><b>Generated at</b>{h(bundle.get('generated_at'))}</div><div class="appendix-fact"><b>Bundle digest</b><code class="reference">{h(_short_reference(bundle.get('bundle_digest')))}</code></div></div><div class="table-wrap"><table><thead><tr><th>Section</th><th>State</th><th>Records</th><th>Reason</th></tr></thead><tbody>{manifest_rows}</tbody></table></div></details>
<footer>Okto Pulse · Contract {h(_short_reference(bundle.get('contract_version')))}</footer>
</main></body></html>"""


ENTITY_EXPORT_HTML_CSP = _HTML_CSP

__all__ = [
    "ENTITY_EXPORT_HTML_CSP",
    "render_entity_export_html",
    "render_entity_export_markdown",
]
