"""Streaming file snapshot plus backup/restore lifecycle regressions."""

from __future__ import annotations

import shutil
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from okto_pulse.core.kg.logical_transfer import (
    CandidateCertificate,
    LogicalCounts,
    LogicalNode,
    LogicalNodeType,
    LogicalPropertyDef,
    LogicalRelation,
    LogicalRelationLayout,
    LogicalSchema,
    PhasedTransferError,
    count_graph,
    fingerprint_graph,
)

from okto_pulse.community.adapters import logical_graph_file
from okto_pulse.community.adapters.logical_graph_file import (
    publish_logical_graph_file,
)
from okto_pulse.community.adapters.logical_graph_transfer import (
    LogicalGraphFileSnapshotSource,
    backup_logical_graph_file,
    restore_logical_graph_file,
)


def _schema() -> LogicalSchema:
    return LogicalSchema(
        scope="board",
        node_types=(
            LogicalNodeType(
                "Card",
                "id",
                (
                    LogicalPropertyDef("id", "string", nullable=False),
                    LogicalPropertyDef("title", "string"),
                ),
            ),
        ),
        relation_layouts=(LogicalRelationLayout("blocks", "Card", "Card"),),
    )


def _nodes() -> tuple[LogicalNode, ...]:
    return (
        LogicalNode("Card", "c1", {"id": "c1", "title": ""}),
        LogicalNode("Card", "c2", {"id": "c2", "title": "second"}),
    )


def _relations() -> tuple[LogicalRelation, ...]:
    return (
        LogicalRelation("blocks", "Card", "Card", "c1", "c2"),
        LogicalRelation("blocks", "Card", "Card", "c1", "c2"),
        LogicalRelation("blocks", "Card", "Card", "c1", "c1"),
    )


class _Snapshot:
    def __init__(self, *, oversize: bool = False) -> None:
        self.closed = False
        self.oversize = oversize

    def schema(self) -> LogicalSchema:
        return _schema()

    def counts(self) -> LogicalCounts:
        return count_graph(_nodes(), _relations())

    def iter_nodes(self, *, batch_size: int) -> Iterator[Sequence[LogicalNode]]:
        if self.oversize:
            yield (*_nodes(), LogicalNode("Card", "c3", {"id": "c3"}))
            return
        yield from _batches(_nodes(), batch_size)

    def iter_relations(self, *, batch_size: int) -> Iterator[Sequence[LogicalRelation]]:
        yield from _batches(_relations(), batch_size)

    def close(self) -> None:
        self.closed = True


class _Source:
    def __init__(self, snapshot: _Snapshot) -> None:
        self.snapshot = snapshot

    def open_snapshot(self) -> _Snapshot:
        return self.snapshot


class _CandidateSink:
    def __init__(self, candidate: Path) -> None:
        self.candidate = candidate
        self.schema: LogicalSchema | None = None
        self.nodes: list[LogicalNode] = []
        self.relations: list[LogicalRelation] = []
        self.aborts = 0
        self.finalized = False

    def begin_candidate(self, schema: LogicalSchema) -> None:
        self.candidate.mkdir()
        self.schema = schema

    def write_nodes(self, nodes: Sequence[LogicalNode]) -> None:
        self.nodes.extend(nodes)

    def write_relations(self, relations: Sequence[LogicalRelation]) -> None:
        self.relations.extend(relations)

    def checkpoint(self) -> None:
        return None

    def certify(self) -> CandidateCertificate:
        assert self.schema is not None
        return CandidateCertificate(
            cold_reopen_completed=True,
            verify_succeeded=True,
            schema=self.schema,
            counts=count_graph(self.nodes, self.relations),
            vector_spaces=(),
            fingerprint=fingerprint_graph(self.schema, self.nodes, self.relations),
        )

    def finalize(self) -> None:
        self.finalized = True

    def abort(self) -> None:
        self.aborts += 1
        if self.candidate.exists():
            shutil.rmtree(self.candidate)


def _batches(records: tuple, batch_size: int):
    for start in range(0, len(records), batch_size):
        yield records[start : start + batch_size]


def _publish(path: Path) -> None:
    publish_logical_graph_file(
        path,
        _schema(),
        _nodes(),
        _relations(),
        counts=count_graph(_nodes(), _relations()),
    )


def test_file_snapshot_is_single_pass_bounded_and_terminally_verified(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "graph.jsonl"
    _publish(artifact)

    snapshot = LogicalGraphFileSnapshotSource(artifact).open_snapshot()
    try:
        assert snapshot.schema() == _schema()
        assert snapshot.counts() == count_graph(_nodes(), _relations())
        node_batches = tuple(snapshot.iter_nodes(batch_size=1))
        assert tuple(record for batch in node_batches for record in batch) == _nodes()
        assert max(map(len, node_batches)) == 1
        assert snapshot.manifest_verified is False

        relation_batches = tuple(snapshot.iter_relations(batch_size=2))
        assert (
            tuple(record for batch in relation_batches for record in batch)
            == _relations()
        )
        assert max(map(len, relation_batches)) == 2
        assert snapshot.manifest_verified is True
    finally:
        snapshot.close()


def test_backup_closes_snapshot_before_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "graph.jsonl"
    snapshot = _Snapshot()
    original_replace = logical_graph_file._replace_file

    def observed_replace(source: Path, destination: Path) -> None:
        assert snapshot.closed is True
        original_replace(source, destination)

    monkeypatch.setattr(logical_graph_file, "_replace_file", observed_replace)
    certificate = backup_logical_graph_file(
        artifact,
        _Source(snapshot),
        batch_size=1,
    )

    assert snapshot.closed is True
    assert certificate.counts == count_graph(_nodes(), _relations())
    published = LogicalGraphFileSnapshotSource(artifact).open_snapshot()
    try:
        assert published.counts() == certificate.counts
    finally:
        published.close()


def test_backup_rejects_an_oversize_source_batch_before_publication(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "graph.jsonl"
    previous = b"previous-generation\n"
    artifact.write_bytes(previous)
    snapshot = _Snapshot(oversize=True)

    with pytest.raises(PhasedTransferError) as caught:
        backup_logical_graph_file(artifact, _Source(snapshot), batch_size=2)

    assert caught.value.phase == "write"
    assert artifact.read_bytes() == previous
    assert snapshot.closed is True


@pytest.mark.parametrize("corrupt", [False, True], ids=["clean", "corrupt"])
def test_restore_is_out_of_place_and_preserves_previous(
    tmp_path: Path,
    corrupt: bool,
) -> None:
    artifact = tmp_path / "graph.jsonl"
    _publish(artifact)
    if corrupt:
        with artifact.open("a", encoding="utf-8", newline="") as stream:
            stream.write("{}\n")

    previous = tmp_path / "previous"
    previous.mkdir()
    sentinel = previous / "generation.bin"
    sentinel.write_bytes(b"previous-generation")
    candidate = tmp_path / "candidate"
    sink = _CandidateSink(candidate)

    if corrupt:
        with pytest.raises(PhasedTransferError) as caught:
            restore_logical_graph_file(artifact, sink, batch_size=1)
        assert caught.value.phase == "write"
        assert sink.aborts == 1
        assert not candidate.exists()
    else:
        report = restore_logical_graph_file(artifact, sink, batch_size=1)
        assert report.counts == count_graph(_nodes(), _relations())
        assert report.fingerprint == fingerprint_graph(
            _schema(), _nodes(), _relations()
        )
        assert sink.finalized is True
        assert candidate.is_dir()

    assert sentinel.read_bytes() == b"previous-generation"
