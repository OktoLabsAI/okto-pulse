"""Community read adapters expose the canonical KB governance envelope."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import (
    CommunitySqlAlchemyResourceGateAdapter,
)
from okto_pulse.community.adapters.sqlalchemy_traceability_read_model import (
    _artifact_refs,
)
from okto_pulse.core.domain.knowledge_fingerprint import (
    knowledge_content_sha256,
)


def _valid_metadata() -> dict[str, object]:
    return {
        "contract_version": 1,
        "authority": "advisory",
        "classification": "technical_reference",
        "purpose": "Describe the reference contract",
        "audience": ["agent", "maintainer"],
        "relevance_reason": "Needed to reproduce the baseline",
        "provenance": [{"kind": "code", "reference": "repo:core@abc123"}],
        "as_of": "2026-07-22T20:00:00-03:00",
        "version_ref": "commit:abc123",
        "version_not_applicable_reason": None,
        "scope": "Knowledge Base reads and writes",
        "limitations": "Advisory evidence only",
        "stable_references": [],
        "lifecycle_state": "current",
        "superseded_by": None,
        "superseded_reason": None,
        "exclusive_authority_check": "passed",
        "normative_destinations": [],
    }


@pytest.mark.parametrize("as_mapping", [False, True])
@pytest.mark.parametrize(
    ("raw_metadata", "expected_status"),
    [(None, "legacy_incomplete"), (_valid_metadata(), "complete")],
)
def test_traceability_kb_artifacts_use_canonical_governance_projection(
    as_mapping: bool,
    raw_metadata: dict[str, object] | None,
    expected_status: str,
) -> None:
    values = {
        "id": "kb-1",
        "title": "Reference",
        "description": "A reference artifact",
        "mime_type": "text/markdown",
        "governance_metadata": raw_metadata,
    }
    kb = values if as_mapping else SimpleNamespace(**values)
    entity = SimpleNamespace(
        knowledge_bases=[kb],
        screen_mockups=[],
        architecture_designs=[],
    )

    projected = _artifact_refs(entity)["knowledge_bases"][0]

    assert projected["governance"]["authority"] == "advisory"
    assert projected["governance"]["metadata_status"] == expected_status
    if raw_metadata is None:
        assert projected["governance"]["missing_fields"] == [
            "governance_metadata"
        ]
        assert projected["governance"]["metadata"] is None
    else:
        assert projected["governance"]["missing_fields"] == []
        assert projected["governance"]["metadata"] == raw_metadata


@pytest.mark.parametrize("as_mapping", [False, True])
def test_traceability_kb_artifacts_preserve_revision_hash_lineage(
    as_mapping: bool,
) -> None:
    values = {
        "id": "kb-child",
        "title": "Reference",
        "description": "A reference artifact",
        "content": "Body",
        "mime_type": "text/markdown",
        "source_version": 3,
        "source_kb_id": "kb-parent",
        "root_source_kb_id": "kb-root",
        "immediate_parent_kb_id": "kb-parent",
        "content_hash": None,
        "governance_metadata": None,
    }
    kb = values if as_mapping else SimpleNamespace(**values)
    entity = SimpleNamespace(
        knowledge_bases=[kb],
        screen_mockups=[],
        architecture_designs=[],
    )

    projected = _artifact_refs(entity)["knowledge_bases"][0]

    assert projected["source_version"] == 3
    assert projected["source_kb_id"] == "kb-parent"
    assert projected["root_source_kb_id"] == "kb-root"
    assert projected["immediate_parent_kb_id"] == "kb-parent"
    assert projected["content_hash"] == knowledge_content_sha256(kb)


@pytest.mark.asyncio
async def test_resource_gate_projects_card_snapshot_governance() -> None:
    governed = {
        "id": "kb-governed",
        "title": "Governed",
        "governance_metadata": _valid_metadata(),
    }
    legacy = {"id": "kb-legacy", "title": "Legacy"}
    source = SimpleNamespace(
        entity_type="card",
        entity_id="card-1",
        entity=SimpleNamespace(knowledge_bases=[governed, legacy]),
    )
    adapter = CommunitySqlAlchemyResourceGateAdapter(object())

    async def load_source(*_args: object) -> object:
        return source

    adapter._load_source_entity_ref = load_source  # type: ignore[method-assign]

    governed_result = await adapter.hydrate_effective_resource(
        board_id="board-1",
        resource_type="knowledge_base",
        ref={"id": "kb-governed"},
    )
    legacy_result = await adapter.hydrate_effective_resource(
        board_id="board-1",
        resource_type="knowledge_base",
        ref={"id": "kb-legacy"},
    )

    assert governed_result is not None
    assert governed_result["governance"]["metadata_status"] == "complete"
    assert governed_result["governance"]["metadata"] == _valid_metadata()
    assert legacy_result is not None
    assert legacy_result["governance"] == {
        "authority": "advisory",
        "metadata_status": "legacy_incomplete",
        "missing_fields": ["governance_metadata"],
        "metadata": None,
    }


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _StubDb:
    def __init__(self, value: object) -> None:
        self._value = value

    async def execute(self, _statement: object) -> _ScalarResult:
        return _ScalarResult(self._value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_metadata", "expected_status"),
    [(None, "legacy_incomplete"), (_valid_metadata(), "complete")],
)
async def test_resource_gate_projects_relational_kb_governance(
    raw_metadata: dict[str, object] | None,
    expected_status: str,
) -> None:
    timestamp = datetime(2026, 7, 22, 20, 0, tzinfo=timezone.utc)
    kb = SimpleNamespace(
        id="kb-1",
        title="Reference",
        description="A reference artifact",
        content="Body",
        mime_type="text/markdown",
        source_type=None,
        source_id=None,
        source_title=None,
        source_version=None,
        source_kb_id=None,
        root_source_kb_id=None,
        immediate_parent_kb_id=None,
        governance_metadata=raw_metadata,
        created_by="agent",
        created_at=timestamp,
        updated_at=timestamp,
    )
    source = SimpleNamespace(
        entity_type="spec",
        entity_id="spec-1",
        entity=SimpleNamespace(),
    )
    adapter = CommunitySqlAlchemyResourceGateAdapter(_StubDb(kb))

    async def load_source(*_args: object) -> object:
        return source

    adapter._load_source_entity_ref = load_source  # type: ignore[method-assign]

    projected = await adapter.hydrate_effective_resource(
        board_id="board-1",
        resource_type="knowledge_base",
        ref={"id": "kb-1"},
    )

    assert projected is not None
    assert projected["governance"]["metadata_status"] == expected_status
    assert projected["governance"]["metadata"] == raw_metadata
    assert projected["content_hash"] == knowledge_content_sha256(kb)
    assert projected["source_content_sha256"] == projected["content_hash"]
