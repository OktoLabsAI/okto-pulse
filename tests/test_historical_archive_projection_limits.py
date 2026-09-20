"""Embedded historical sections cannot exceed the aggregate public read limits."""

import json

import pytest

from okto_pulse.core.ports.historical_archive import ArchiveSection, ArchiveSourceScope
from okto_pulse.core.ports.historical_archive_read import ArchiveReadLimitExceeded, ArchiveReadRequest
from okto_pulse.community.adapters.historical_archive_projection import project_historical_archive_section


@pytest.mark.parametrize("evaluations", [[{}] * 100_001, [{"detail": "x" * (25 * 1024 * 1024)}]])
def test_oversized_section_or_page_fails_before_emitting_any_partial_page(evaluations):
    request = ArchiveReadRequest(ArchiveSourceScope("local", "board", "sprint", "origin"), ArchiveSection.EVALUATIONS)
    document = {"format": "historical-relational-archive/v4", "board_id": "board", "origin_kind": "sprint", "tables": {
        "sprints": {"columns": [{"name": "id"}, {"name": "evaluations"}],
            "rows": [[["text", "origin"], ["text", json.dumps(evaluations)]]]}}}
    with pytest.raises(ArchiveReadLimitExceeded):
        project_historical_archive_section(document, request, "archive")
