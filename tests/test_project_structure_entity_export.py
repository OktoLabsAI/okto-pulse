"""Whole-Spec Project structure export and human-safe rendering."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters.sqlalchemy_entity_export import (
    CommunitySqlAlchemyEntityExportReader,
    _definitions,
)
from okto_pulse.community.services.entity_export_renderer import (
    render_entity_export_html,
    render_entity_export_markdown,
)
from okto_pulse.core.domain.entity_export import (
    EntityExportHistoryScope,
    EntityExportRequest,
    EntityExportType,
)


def _node(
    node_id: str,
    *,
    parent_id: str | None,
    position: int,
    kind: str,
    name: str,
    note: str,
    classification: str = "as_is",
) -> dict[str, object]:
    return {
        "id": node_id,
        "parent_id": parent_id,
        "position": position,
        "kind": kind,
        "name": name,
        "note": note,
        "classification": classification,
        "state": "existing" if classification == "as_is" else "planned",
        "interpretation_limit": (
            "Pattern only" if classification == "reference_scaffold" else None
        ),
        "status": "active",
        "task_references": [],
        "test_references": [],
        "evidence_ids": [],
    }


def _bundle(payload: dict[str, object], *, status: str = "included") -> dict:
    return {
        "subject": {
            "entity_type": "spec",
            "title": "Project structure export",
            "status": "draft",
            "edition": 1,
        },
        "history_scope": "complete",
        "sections": [
            {
                "section_key": "project_structure",
                "schema_version": "project-structure-export/v1",
                "payload": payload,
            }
        ],
        "manifest": {
            "entries": [
                {
                    "section_key": "project_structure",
                    "status": status,
                    "total_count": len(payload.get("nodes", [])),
                }
            ]
        },
        "complete_for_actor": True,
        "source_complete": True,
        "generated_at": "2026-08-23T12:00:00+00:00",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "expected_state", "expected_revision", "expected_count"),
    [
        (None, "absent", None, 0),
        ([], "authored_empty", 4, 0),
    ],
)
async def test_project_structure_export_preserves_optional_state(
    stored,
    expected_state: str,
    expected_revision: int | None,
    expected_count: int,
) -> None:
    definition = next(
        item
        for item in _definitions(EntityExportType.SPEC)
        if item.key == "project_structure"
    )
    reader = CommunitySqlAlchemyEntityExportReader(None)  # type: ignore[arg-type]

    payload, count, materialized = await reader._collect_definition(
        definition,
        request=EntityExportRequest(
            board_id="board-export",
            entity_type=EntityExportType.SPEC,
            entity_id="spec-export",
            history_scope=EntityExportHistoryScope.COMPLETE,
        ),
        base_row=SimpleNamespace(
            project_structure=stored,
            project_structure_revision=4,
            project_structure_digest=None,
        ),
        base_payload={},
    )

    assert definition.schema_version == "project-structure-export/v1"
    assert payload["manifest"] == {
        "project_structure_state": expected_state,
        "structure_revision": expected_revision,
        "structure_digest": payload["manifest"]["structure_digest"],
        "active_node_count": expected_count,
    }
    assert count == materialized == expected_count


def test_project_structure_whole_spec_render_is_complete_ordered_and_safe() -> None:
    nodes = [
        _node(
            "psn_root",
            parent_id=None,
            position=0,
            kind="folder",
            name="src <script>alert(1)</script>",
            note="Application root\x00",
        ),
        _node(
            "psn_child_a",
            parent_id="psn_root",
            position=0,
            kind="file",
            name="api_[draft].py",
            note="REST *boundary*",
            classification="to_be",
        ),
        _node(
            "psn_child_b",
            parent_id="psn_root",
            position=1,
            kind="artifact",
            name="schema.json",
            note="Reference shape",
            classification="reference_scaffold",
        ),
    ]
    payload = {
        "manifest": {
            "project_structure_state": "populated",
            "structure_revision": 7,
            "structure_digest": "a" * 64,
            "active_node_count": 3,
        },
        "nodes": nodes,
    }

    html = render_entity_export_html(_bundle(payload))
    markdown = render_entity_export_markdown(_bundle(payload))

    assert html.index("src &lt;script&gt;") < html.index("api_[draft].py")
    assert html.index("api_[draft].py") < html.index("schema.json")
    assert '<ul class="project-structure-tree' in html
    assert '<details class="project-structure-folder" open>' in html
    assert "<script>alert(1)</script>" not in html
    assert "\x00" not in html
    assert "psn_root" not in html
    assert "aaaaaaaaaa" not in html
    assert html.count("Note / Description:") == 3
    assert "Pattern only" in html
    assert "Reference shape" not in html
    assert "Interpretation limit" not in html
    assert "[Folder]" in markdown
    assert "[File]" in markdown
    assert "[Artifact]" in markdown
    assert markdown.index("src \\<script\\>") < markdown.index("api\\_\\[draft\\].py")
    assert markdown.count("**Note / Description:**") == 3
    assert "Pattern only" in markdown
    assert "Reference shape" not in markdown
    assert "Interpretation limit" not in markdown
    assert "psn_root" not in markdown
    assert "a" * 64 not in markdown


def test_absent_or_authored_empty_structure_has_no_report_body() -> None:
    payload = {
        "manifest": {
            "project_structure_state": "authored_empty",
            "structure_revision": 1,
            "structure_digest": "b" * 64,
            "active_node_count": 0,
        },
        "nodes": [],
    }

    html = render_entity_export_html(_bundle(payload, status="empty"))
    markdown = render_entity_export_markdown(_bundle(payload, status="empty"))

    assert 'id="section-project_structure"' not in html
    assert "## Project structure" not in markdown
