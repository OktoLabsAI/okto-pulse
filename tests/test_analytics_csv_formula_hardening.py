from __future__ import annotations

import csv
import io
from types import SimpleNamespace

import pytest

import okto_pulse.community.api.analytics as analytics_api


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
        return SimpleNamespace(
            data=[{"week": "@malicious-week", "impl": 1, "test": 2}]
        )

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
    quality_rows = {
        row[0]: row for row in rows if row and row[0].startswith("card-")
    }

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
