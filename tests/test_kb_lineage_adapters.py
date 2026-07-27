"""Community adapters preserve KB revision/hash lineage end to end."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from okto_pulse.community.adapters.sqlalchemy_effective_resource import (
    CommunitySqlAlchemyEffectiveResourcePersistence,
)
from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import (
    CommunitySqlAlchemyResourceGateAdapter,
)
from okto_pulse.community.adapters.sqlalchemy_spec_resource_propagation import (
    CommunitySqlAlchemySpecResourcePropagationStore,
)
from okto_pulse.core.domain.knowledge_fingerprint import (
    knowledge_content_sha256,
)
from okto_pulse.core.services.resource_lineage import LineageEntityRef


class _RowsResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _RowsResult:
        return self

    def mappings(self) -> _RowsResult:
        return self

    def all(self) -> list[Any]:
        return self._rows


class _RowsContext:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    async def execute(self, _statement: Any) -> _RowsResult:
        return _RowsResult(self._rows)


def _legacy_row(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": "kb-child",
        "title": "Canonical reference",
        "description": "Description",
        "content": "Body",
        "mime_type": "text/markdown",
        "source_version": 7,
        "source_kb_id": "kb-parent",
        "root_source_kb_id": "kb-root",
        "immediate_parent_kb_id": "kb-parent",
        "content_hash": None,
        "governance_metadata": None,
        "created_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_effective_resource_loader_lazily_hashes_and_keeps_lineage() -> None:
    row = _legacy_row()
    adapter = CommunitySqlAlchemyEffectiveResourcePersistence()

    payloads = await adapter.load_knowledge_bases(
        _RowsContext([row]),
        source_entity_type="spec",
        source_entity_id="spec-1",
    )

    assert len(payloads) == 1
    payload = payloads[0]
    expected_hash = knowledge_content_sha256(row)
    assert payload["source_version"] == 7
    assert payload["source_kb_id"] == "kb-parent"
    assert payload["root_source_kb_id"] == "kb-root"
    assert payload["immediate_parent_kb_id"] == "kb-parent"
    assert payload["content_hash"] == expected_hash
    assert payload["root_resource_id"] == "kb-root"
    assert payload["immediate_parent_resource_id"] == "kb-parent"
    assert payload["source_revision"] == 7
    assert payload["source_content_sha256"] == expected_hash


@pytest.mark.asyncio
async def test_effective_resource_legacy_root_fallback_never_promotes_parent() -> None:
    row = _legacy_row(root_source_kb_id=None, immediate_parent_kb_id=None)

    payloads = await CommunitySqlAlchemyEffectiveResourcePersistence().load_knowledge_bases(
        _RowsContext([row]),
        source_entity_type="spec",
        source_entity_id="spec-1",
    )

    assert payloads[0]["root_resource_id"] == "kb-child"
    assert payloads[0]["immediate_parent_resource_id"] == "kb-parent"


@pytest.mark.asyncio
async def test_spec_resource_store_transports_revision_and_resolved_hash() -> None:
    row = _legacy_row()
    store = CommunitySqlAlchemySpecResourcePropagationStore()

    facts = await store.list_spec_knowledge_bases(
        _RowsContext([row]),
        spec_id="spec-1",
    )

    assert len(facts) == 1
    fact = facts[0]
    assert fact.source_version == 7
    assert fact.source_kb_id == "kb-parent"
    assert fact.root_source_kb_id == "kb-root"
    assert fact.immediate_parent_kb_id == "kb-parent"
    assert fact.content_hash == knowledge_content_sha256(row)


@pytest.mark.asyncio
async def test_resource_gate_relational_refs_emit_legacy_and_neutral_fields() -> None:
    row = vars(_legacy_row())
    adapter = CommunitySqlAlchemyResourceGateAdapter(_RowsContext([row]))
    owner = LineageEntityRef(
        entity_type="spec",
        entity_id="spec-1",
        entity=SimpleNamespace(),
    )

    refs = await adapter._knowledge_refs(owner)

    assert len(refs) == 1
    ref = refs[0]
    expected_hash = knowledge_content_sha256(row)
    assert ref["source_version"] == 7
    assert ref["source_kb_id"] == "kb-parent"
    assert ref["root_source_kb_id"] == "kb-root"
    assert ref["immediate_parent_kb_id"] == "kb-parent"
    assert ref["content_hash"] == expected_hash
    assert ref["root_resource_id"] == "kb-root"
    assert ref["immediate_parent_resource_id"] == "kb-parent"
    assert ref["source_revision"] == 7
    assert ref["source_content_sha256"] == expected_hash


@pytest.mark.asyncio
async def test_resource_gate_card_ref_derives_legacy_root_without_rewriting() -> None:
    item = vars(
        _legacy_row(
            id="cardkb-child",
            root_source_kb_id=None,
            immediate_parent_kb_id=None,
        )
    )
    owner = LineageEntityRef(
        entity_type="card",
        entity_id="card-1",
        entity=SimpleNamespace(knowledge_bases=[item]),
    )
    adapter = CommunitySqlAlchemyResourceGateAdapter(object())

    refs = await adapter._knowledge_refs(owner)

    ref = refs[0]
    assert "root_source_kb_id" not in ref
    assert ref["source_kb_id"] == "kb-parent"
    assert ref["root_resource_id"] == "cardkb-child"
    assert ref["immediate_parent_resource_id"] == "kb-parent"
    assert ref["source_content_sha256"] == knowledge_content_sha256(item)
    assert item["root_source_kb_id"] is None


@pytest.mark.asyncio
async def test_v2_filter_keeps_only_selected_parent_source_effective() -> None:
    root = LineageEntityRef(
        entity_type="card",
        entity_id="card-1",
        title="Card",
        entity=SimpleNamespace(board_id="board-1", spec_id="spec-1"),
    )
    parent = LineageEntityRef(
        entity_type="spec",
        entity_id="spec-1",
        title="Spec",
        entity=SimpleNamespace(board_id="board-1"),
    )
    stamp = SimpleNamespace(
        root_id="root-selected",
        immediate_parent_id="kb-parent",
        source_revision="rev-2",
        source_content_sha256="a" * 64,
    )
    assignment = SimpleNamespace(
        assignment_id="assignment-1",
        source_knowledge_id="kb-old",
        origin_class=SimpleNamespace(value="v2"),
        mode=SimpleNamespace(value="snapshot"),
    )
    resolved = SimpleNamespace(
        assignment=assignment,
        resolved_source_knowledge_id="kb-selected",
        revision_stamp=stamp,
        state=SimpleNamespace(value="stale"),
        content_bytes=(
            b'{"content":"frozen","description":null,"id":"kb-selected",'
            b'"mime_type":"text/markdown","title":"Selected"}'
        ),
    )
    read = SimpleNamespace(effective_assignments=(resolved,))

    class _V2Adapter(CommunitySqlAlchemyResourceGateAdapter):
        async def _active_knowledge_read(self, _ref):
            return read

    adapter = _V2Adapter(object())
    refs = await adapter.filter_inherited_refs(
        root,
        parent,
        {
            "architecture": [],
            "mockup": [],
            "knowledge_base": [
                {"id": "kb-selected", "title": "Selected current"},
                {"id": "kb-unselected", "title": "Not selected"},
            ],
        },
    )

    by_id = {item["id"]: item for item in refs["knowledge_base"]}
    assert by_id["kb-selected"]["effective"] is True
    assert by_id["kb-selected"]["root_resource_id"] == "root-selected"
    assert by_id["kb-selected"]["source_revision"] == "rev-2"
    assert by_id["kb-selected"]["knowledge_assignment_stale"] is True
    assert by_id["kb-unselected"]["effective"] is False
