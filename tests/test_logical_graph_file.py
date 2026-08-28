"""Atomic publication gate for ``okto-pulse-logical-graph/1`` files."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from okto_pulse.core.kg.logical_transfer import (
    ArtifactIntegrityError,
    LogicalNode,
    LogicalNodeType,
    LogicalPropertyDef,
    LogicalRelation,
    LogicalRelationLayout,
    LogicalSchema,
    PhasedTransferError,
    count_graph,
)

from okto_pulse.community.adapters import logical_graph_file


def _schema() -> LogicalSchema:
    return LogicalSchema(
        scope="board",
        node_types=(
            LogicalNodeType(
                name="Card",
                key="id",
                properties=(
                    LogicalPropertyDef("id", "string", nullable=False),
                    LogicalPropertyDef("title", "string"),
                ),
            ),
        ),
        relation_layouts=(
            LogicalRelationLayout(
                name="blocks",
                source_type="Card",
                target_type="Card",
            ),
        ),
    )


def _nodes() -> tuple[LogicalNode, ...]:
    return (
        LogicalNode("Card", "c1", {"id": "c1", "title": ""}),
        LogicalNode("Card", "c2", {"id": "c2"}),
    )


def _relations() -> tuple[LogicalRelation, ...]:
    return (
        LogicalRelation("blocks", "Card", "Card", "c1", "c2"),
        LogicalRelation("blocks", "Card", "Card", "c1", "c2"),
        LogicalRelation("blocks", "Card", "Card", "c1", "c1"),
    )


def _failing_nodes() -> Iterator[LogicalNode]:
    yield _nodes()[0]
    raise OSError("injected write failure")


@pytest.mark.parametrize("fault", ["success", "write", "fsync", "verify", "replace"])
def test_atomic_logical_graph_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    destination = tmp_path / "portable.logical-graph.jsonl"
    previous = b"previous-generation\n"
    destination.write_bytes(previous)
    schema = _schema()
    nodes = _nodes()
    relations = _relations()

    if fault == "write":
        published_nodes = _failing_nodes()
    else:
        published_nodes = iter(nodes)

    if fault == "fsync":
        monkeypatch.setattr(
            logical_graph_file,
            "_fsync_file",
            lambda _descriptor: (_ for _ in ()).throw(
                OSError("injected fsync failure")
            ),
        )
    elif fault == "verify":
        monkeypatch.setattr(
            logical_graph_file,
            "_verify_complete_file",
            lambda _path: (_ for _ in ()).throw(
                ArtifactIntegrityError("injected verification failure")
            ),
        )
    elif fault == "replace":
        monkeypatch.setattr(
            logical_graph_file,
            "_replace_file",
            lambda _source, _destination: (_ for _ in ()).throw(
                OSError("injected replace failure")
            ),
        )

    if fault == "success":
        certificate = logical_graph_file.publish_logical_graph_file(
            destination,
            schema,
            published_nodes,
            relations,
            counts=count_graph(nodes, relations),
        )

        assert destination.read_bytes() != previous
        assert logical_graph_file.verify_logical_graph_file(destination) == certificate
        assert certificate.scope == "board"
        assert certificate.counts == count_graph(nodes, relations)
        assert len(certificate.schema_digest) == 64
        assert len(certificate.fingerprint) == 64
        assert len(certificate.stream_checksum) == 64
    else:
        with pytest.raises(PhasedTransferError) as caught:
            logical_graph_file.publish_logical_graph_file(
                destination,
                schema,
                published_nodes,
                relations,
                counts=count_graph(nodes, relations),
            )

        assert caught.value.phase == "write"
        assert destination.read_bytes() == previous

    assert list(tmp_path.glob(f".{destination.name}.*.tmp")) == []
