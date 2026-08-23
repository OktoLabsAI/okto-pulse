from __future__ import annotations

import csv
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import okto_pulse.community.api.analytics as analytics_api
import okto_pulse.core.application.use_cases.analytics_helpers as analytics_helpers


async def _response_csv(response) -> list[list[str]]:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return list(csv.reader(io.StringIO("".join(chunks))))


def _flatten(rows: list[list[str]]) -> list[str]:
    return [cell for row in rows for cell in row]


@pytest.mark.asyncio
async def test_overview_csv_neutralizes_every_formula_prefix_and_preserves_safe_values(
    monkeypatch,
):
    safe_serialized = 'safe, "quoted"\nline'
    data = {
        "total_ideations": 1,
        "total_specs": 2,
        "total_cards_impl": 3,
        "total_cards_test": 4,
        "avg_completeness": 95.5,
        "avg_drift": -7,
        "funnel": {
            "=SUM(A1,A2)": 1,
            "+cmd": 2,
            "-formula": 3,
            "@function": 4,
            "safe-stage": 5,
            " =leading-space": 6,
            safe_serialized: 7,
        },
        "velocity": [
            {"week": "safe-week", "impl": 1, "test": 2},
        ],
        "boards": [
            {
                "board_id": "safe-board-id",
                "board_name": "Safe Board",
                "ideations": 1,
                "specs": 2,
                "cards": 3,
                "cards_done": 4,
            }
        ],
    }

    async def execute(_self, _command, *, actor, uow):
        return SimpleNamespace(data=data)

    monkeypatch.setattr(analytics_api.AnalyticsOverviewUseCase, "execute", execute)

    response = await analytics_api.analytics_overview_export(
        date_from=None,
        date_to=None,
        user_id="csv-user",
        uow=object(),
    )
    cells = _flatten(await _response_csv(response))

    assert "'=SUM(A1,A2)" in cells
    assert "'+cmd" in cells
    assert "'-formula" in cells
    assert "'@function" in cells
    assert "safe-stage" in cells
    assert " =leading-space" in cells
    assert safe_serialized in cells
    assert "Safe Board" in cells
    assert "-7" in cells
    assert "'-7" not in cells


@pytest.mark.asyncio
async def test_board_csv_neutralizes_formula_prefixes_in_quality_titles(
    monkeypatch,
):
    quality = {
        "conclusion_reported": [
            {
                "card_id": "card-equals",
                "title": "=SUM(A1:A2)",
                "completeness": 91,
                "drift": -1,
            },
            {
                "card_id": "card-plus",
                "title": "+cmd",
                "completeness": 92,
                "drift": -2,
            },
            {
                "card_id": "card-minus",
                "title": "-formula",
                "completeness": 93,
                "drift": -3,
            },
            {
                "card_id": "card-at",
                "title": "@function",
                "completeness": 94,
                "drift": -4,
            },
        ]
    }

    async def funnel_execute(_self, _command, *, actor, uow):
        return SimpleNamespace(data={"=malicious-stage": 1})

    async def quality_execute(_self, _command, *, actor, uow):
        return SimpleNamespace(data=quality)

    async def velocity_execute(_self, _command, *, actor, uow):
        return SimpleNamespace(data=[{"week": "@malicious-week", "impl": 1, "test": 2}])

    monkeypatch.setattr(analytics_api.BoardFunnelUseCase, "execute", funnel_execute)
    monkeypatch.setattr(analytics_api.BoardQualityUseCase, "execute", quality_execute)
    monkeypatch.setattr(analytics_api.BoardVelocityUseCase, "execute", velocity_execute)

    response = await analytics_api.board_analytics_export(
        board_id="board-id",
        date_from=None,
        date_to=None,
        user_id="csv-user",
        uow=object(),
    )
    rows = await _response_csv(response)
    quality_rows = {row[0]: row for row in rows if row and row[0].startswith("card-")}

    assert quality_rows["card-equals"][1] == "'=SUM(A1:A2)"
    assert quality_rows["card-plus"][1] == "'+cmd"
    assert quality_rows["card-minus"][1] == "'-formula"
    assert quality_rows["card-at"][1] == "'@function"
    assert quality_rows["card-minus"][3] == "-3"
    assert ["'=malicious-stage", "1"] in rows
    assert ["'@malicious-week", "1", "2"] in rows


@pytest.mark.asyncio
async def test_entity_detail_csv_neutralizes_every_formula_prefix_in_all_row_shapes(
    monkeypatch,
):
    data = {
        "equals": "=SUM(A1:A2)",
        "plus": "+cmd",
        "minus": "-formula",
        "at": "@function",
        "safe": "safe-value",
        "leading_space": " =safe",
        "already_neutralized": "'=safe",
        "csv_serialized": 'safe, "quoted"\nline',
        "negative_number": -9,
        "items": [
            {
                "=header": "=nested",
                "+header": "+nested",
                "-header": "-nested",
                "@header": "@nested",
                "safe-header": "safe-nested",
            }
        ],
    }

    async def execute(_self, _command, *, actor, uow):
        return SimpleNamespace(data=data)

    monkeypatch.setattr(analytics_api.BoardEntityDetailUseCase, "execute", execute)

    response = await analytics_api.board_entity_detail_export(
        board_id="board-id",
        entity_type="spec",
        entity_id="spec-id",
        user_id="csv-user",
        uow=object(),
    )
    cells = _flatten(await _response_csv(response))

    for unsafe in (
        "=SUM(A1:A2)",
        "+cmd",
        "-formula",
        "@function",
        "=header",
        "+header",
        "-header",
        "@header",
        "=nested",
        "+nested",
        "-nested",
        "@nested",
    ):
        assert f"'{unsafe}" in cells
        assert unsafe not in cells

    assert "safe-value" in cells
    assert " =safe" in cells
    assert "'=safe" in cells
    assert 'safe, "quoted"\nline' in cells
    assert "safe-header" in cells
    assert "safe-nested" in cells
    assert "-9" in cells
    assert "'-9" not in cells


@pytest.mark.asyncio
async def test_sparse_board_validations_json_redacts_task_validation_metrics(
    monkeypatch,
):
    secret = "private task-validation rejection rationale"
    raw = {
        "spec_validation_gate": {"visible_spec_signal": 3},
        "task_validation_gate": {
            "total_submitted": 9,
            "total_success": 4,
            "total_failed": 5,
            "success_rate": 44.4,
            "avg_attempts_per_card": 2.5,
            "first_pass_rate": 20.0,
            "avg_scores": {
                "confidence": 97,
                "completeness": 96,
                "drift": 3,
            },
            "rejection_reasons": {
                "confidence_below": 1,
                "completeness_below": 2,
                "drift_above": 3,
                "reject_recommendation": 4,
            },
            "cards_with_validation": 7,
            "per_card": [
                {
                    "card_id": "card-private",
                    "last_confidence": 97,
                    "last_justification": secret,
                    "request_digest": "private-digest",
                    "idempotency_key": "private-key",
                }
            ],
        },
        "spec_evaluation": {"visible_spec_evaluation": True},
        "sprint_evaluation": {"visible_sprint_evaluation": True},
    }
    analytics = SimpleNamespace(validations=AsyncMock(return_value=raw))
    uow = SimpleNamespace(
        services=SimpleNamespace(
            analytics=analytics,
            resolve_user_permissions=AsyncMock(return_value=[]),
        )
    )
    monkeypatch.setattr(
        analytics_helpers,
        "_ensure_board_access",
        AsyncMock(return_value=None),
    )

    result = await analytics_api.board_validations(
        board_id="board-id",
        date_from=None,
        date_to=None,
        user_id="sparse-reader",
        uow=uow,
    )

    assert result["spec_validation_gate"] == {"visible_spec_signal": 3}
    assert result["spec_evaluation"] == {"visible_spec_evaluation": True}
    assert result["sprint_evaluation"] == {"visible_sprint_evaluation": True}
    assert result["task_validation_gate"] == {
        "total_submitted": 0,
        "total_success": 0,
        "total_failed": 0,
        "success_rate": None,
        "avg_attempts_per_card": None,
        "first_pass_rate": None,
        "avg_scores": {
            "confidence": None,
            "completeness": None,
            "drift": None,
        },
        "rejection_reasons": {
            "confidence_below": 0,
            "completeness_below": 0,
            "drift_above": 0,
            "reject_recommendation": 0,
        },
        "cards_with_validation": 0,
        "per_card": [],
        "redacted": True,
    }
    serialized = json.dumps(result)
    assert secret not in serialized
    assert "private-digest" not in serialized
    assert "private-key" not in serialized


@pytest.mark.asyncio
async def test_sparse_card_entity_json_and_csv_hide_validation_ledger_and_scores(
    monkeypatch,
):
    secret = "private human validation explanation"
    raw = {
        "card_id": "card-id",
        "title": "Human-readable card title",
        "status": "rejected",
        "completeness": 61,
        "drift": 4,
        "conclusions": [{"summary": "Public implementation conclusion"}],
        "validations": [
            {
                "id": "validation-private",
                "confidence": 97,
                "estimated_completeness": 96,
                "estimated_drift": 3,
                "general_justification": secret,
                "request_digest": "private-digest",
                "idempotency_key": "private-key",
                "response": {
                    "validation_outcome": "failed",
                    "rejection_cause": {"summary": secret},
                },
            }
        ],
        "rejection_records": [{"id": "cause-private", "summary": secret}],
        "current_rejection_kind": "task_validation",
        "current_rejection_id": "cause-private",
        "current_rejection_code": "task_validation_failed",
        "current_rejection_summary": secret,
        "validations_count": 1,
        "validations_fail_count": 1,
        "validations_has_pass": False,
        "first_pass_confidence": 97,
        "first_pass_completeness": 96,
        "first_pass_drift": 3,
    }
    analytics = SimpleNamespace(entity_detail=AsyncMock(return_value=raw))
    uow = SimpleNamespace(
        services=SimpleNamespace(
            analytics=analytics,
            resolve_user_permissions=AsyncMock(return_value=[]),
        )
    )
    monkeypatch.setattr(
        analytics_helpers,
        "_ensure_board_access",
        AsyncMock(return_value=None),
    )

    detail = await analytics_api.board_entity_detail(
        board_id="board-id",
        entity_type="card",
        entity_id="card-id",
        user_id="sparse-reader",
        uow=uow,
    )
    serialized = json.dumps(detail)
    assert detail["title"] == "Human-readable card title"
    assert detail["status"] == "rejected"
    assert detail["conclusions"] == [{"summary": "Public implementation conclusion"}]
    assert detail["validations"] is None
    assert detail["rejection_records"] == []
    assert detail["current_rejection_kind"] is None
    assert detail["current_rejection_id"] is None
    assert detail["current_rejection_code"] is None
    assert detail["current_rejection_summary"] is None
    assert detail["validations_count"] == 0
    assert detail["validations_fail_count"] == 0
    assert detail["first_pass_confidence"] is None
    assert detail["first_pass_completeness"] is None
    assert detail["first_pass_drift"] is None
    for forbidden in (
        secret,
        "validation-private",
        "cause-private",
        "private-digest",
        "private-key",
        "task_validation_failed",
    ):
        assert forbidden not in serialized

    csv_response = await analytics_api.board_entity_detail_export(
        board_id="board-id",
        entity_type="card",
        entity_id="card-id",
        user_id="sparse-reader",
        uow=uow,
    )
    csv_rows = await _response_csv(csv_response)
    csv_cells = _flatten(csv_rows)
    csv_text = "\n".join(",".join(row) for row in csv_rows)

    assert "Human-readable card title" in csv_cells
    assert "rejected" in csv_cells
    assert "Public implementation conclusion" in csv_text
    assert "97" not in csv_cells
    assert "96" not in csv_cells
    for forbidden in (
        secret,
        "validation-private",
        "cause-private",
        "private-digest",
        "private-key",
        "task_validation_failed",
        "request_digest",
        "idempotency_key",
    ):
        assert forbidden not in csv_text
