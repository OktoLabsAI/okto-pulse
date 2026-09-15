"""Streaming backup and out-of-place restore for logical graph files.

The Core codec owns the wire format and its integrity checks.  This Community
adapter owns the two physical lifecycles around it:

* a backup reads exactly one database snapshot and closes it before the
  verified temporary file is atomically published; and
* a restore reads exactly one open file handle into a fresh candidate sink.

Neither operation binds a candidate or retires a previous generation.  Those
are routing concerns and deliberately remain outside M-PULSE-5.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import TextIO, TypeVar

from okto_pulse.core.kg.logical_transfer import (
    ArtifactEvent,
    ArtifactSequenceError,
    LogicalCandidateSink,
    LogicalCounts,
    LogicalNode,
    LogicalRelation,
    LogicalSchema,
    LogicalSchemaError,
    LogicalSnapshotSource,
    TransferReport,
    decode_records,
    transfer_logical_graph,
)

from okto_pulse.community.adapters.logical_graph_file import (
    LogicalGraphFileCertificate,
    _artifact_lines,
    publish_logical_graph_file,
)

_DEFAULT_BATCH_SIZE = 500
_Record = TypeVar("_Record", LogicalNode, LogicalRelation)


class LogicalGraphFileSnapshotSource:
    """Open one immutable-by-handle view of a logical graph artifact."""

    def __init__(self, path: str | Path) -> None:
        self._path = _logical_file_path(path)

    def open_snapshot(self) -> LogicalGraphFileSnapshot:
        stream: TextIO | None = None
        try:
            stream = self._path.open("r", encoding="utf-8", newline="")
            return LogicalGraphFileSnapshot(stream, source_path=self._path)
        except BaseException as failure:
            if stream is not None:
                try:
                    stream.close()
                except BaseException as cleanup_failure:  # noqa: BLE001
                    failure.add_note(
                        "logical graph file close also failed: "
                        f"{type(cleanup_failure).__name__}: {cleanup_failure}"
                    )
            raise


class LogicalGraphFileSnapshot:
    """A single-pass logical snapshot backed by one already-open file handle."""

    def __init__(self, stream: TextIO, *, source_path: Path) -> None:
        self._stream: TextIO | None = stream
        self._source_path = source_path
        self._events = iter(decode_records(_artifact_lines(stream)))
        self._pending: ArtifactEvent | None = None
        self._nodes_started = False
        self._nodes_complete = False
        self._relations_started = False
        self._relations_complete = False
        self._manifest_verified = False
        self._closed = False
        self._cleanup_complete = False

        first = next(self._events)
        if first.header is None:
            raise ArtifactSequenceError(
                "logical graph file did not begin with its header",
                detail=source_path.name,
            )
        self._schema = first.header.schema
        self._counts = first.header.counts

    def schema(self) -> LogicalSchema:
        self._require_open()
        return self._schema

    def counts(self) -> LogicalCounts:
        self._require_open()
        return self._counts

    def iter_nodes(self, *, batch_size: int) -> Iterator[Sequence[LogicalNode]]:
        self._require_batch_size(batch_size)
        if self._nodes_started:
            raise LogicalSchemaError("logical graph file nodes were already consumed")
        if self._relations_started:
            raise LogicalSchemaError(
                "logical graph file relations started before nodes completed"
            )
        self._nodes_started = True
        batch: list[LogicalNode] = []
        while True:
            event = self._next_event()
            if event.node is not None:
                batch.append(event.node)
                if len(batch) == batch_size:
                    yield tuple(batch)
                    batch.clear()
                continue
            self._pending = event
            self._nodes_complete = True
            if batch:
                yield tuple(batch)
            return

    def iter_relations(self, *, batch_size: int) -> Iterator[Sequence[LogicalRelation]]:
        self._require_batch_size(batch_size)
        if not self._nodes_complete:
            raise LogicalSchemaError(
                "logical graph file relations require the complete node section"
            )
        if self._relations_started:
            raise LogicalSchemaError(
                "logical graph file relations were already consumed"
            )
        self._relations_started = True
        batch: list[LogicalRelation] = []
        while True:
            event = self._next_event()
            if event.relation is not None:
                batch.append(event.relation)
                if len(batch) == batch_size:
                    yield tuple(batch)
                    batch.clear()
                continue
            if event.manifest is None:
                raise ArtifactSequenceError(
                    "logical graph file carried a non-relation after its node section",
                    detail=event.kind,
                )
            self._finish_manifest()
            self._relations_complete = True
            if batch:
                yield tuple(batch)
            return

    def close(self) -> None:
        if self._cleanup_complete:
            return
        self._closed = True
        stream = self._stream
        if stream is None:
            self._cleanup_complete = True
            return
        try:
            stream.close()
        finally:
            if stream.closed:
                self._stream = None
                self._cleanup_complete = True

    @property
    def manifest_verified(self) -> bool:
        """Whether the terminal manifest and absence of trailing data were read."""

        return self._manifest_verified

    def _next_event(self) -> ArtifactEvent:
        self._require_open()
        pending, self._pending = self._pending, None
        if pending is not None:
            return pending
        return next(self._events)

    def _finish_manifest(self) -> None:
        # decode_records raises on every byte after the manifest.  Asking once
        # more is therefore part of verification, not an optional EOF probe.
        try:
            unexpected = next(self._events)
        except StopIteration:
            self._manifest_verified = True
            return
        raise ArtifactSequenceError(
            "logical graph decoder produced an event after the manifest",
            detail=unexpected.kind,
        )

    def _require_open(self) -> None:
        if self._closed:
            raise LogicalSchemaError("logical graph file snapshot is closed")

    def _require_batch_size(self, batch_size: int) -> None:
        self._require_open()
        if type(batch_size) is not int or batch_size < 1:
            raise LogicalSchemaError(
                "logical graph file batch_size must be a positive int",
                detail=repr(batch_size),
            )


def backup_logical_graph_file(
    path: str | Path,
    source: LogicalSnapshotSource,
    *,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    optional_features: Iterable[str] = (),
) -> LogicalGraphFileCertificate:
    """Publish one source snapshot atomically without materialising its graph."""

    _require_batch_size(batch_size)
    snapshot = source.open_snapshot()
    snapshot_closed = False

    def close_snapshot() -> None:
        nonlocal snapshot_closed
        if snapshot_closed:
            return
        snapshot.close()
        snapshot_closed = True

    def relations_then_close() -> Iterator[LogicalRelation]:
        try:
            yield from _bounded_records(
                snapshot.iter_relations,
                batch_size=batch_size,
                what="relations",
            )
        finally:
            # A successful encoder exhausts this generator before it writes the
            # manifest, verifies the temp file or replaces the destination.
            close_snapshot()

    primary_failure: BaseException | None = None
    try:
        schema = snapshot.schema()
        counts = snapshot.counts()
        return publish_logical_graph_file(
            path,
            schema,
            _bounded_records(
                snapshot.iter_nodes,
                batch_size=batch_size,
                what="nodes",
            ),
            relations_then_close(),
            counts=counts,
            optional_features=optional_features,
        )
    except BaseException as failure:
        primary_failure = failure
        raise
    finally:
        if not snapshot_closed:
            try:
                close_snapshot()
            except BaseException as cleanup_failure:
                if primary_failure is None:
                    raise
                primary_failure.add_note(
                    "logical backup snapshot close also failed: "
                    f"{type(cleanup_failure).__name__}: {cleanup_failure}"
                )


def restore_logical_graph_file(
    path: str | Path,
    sink: LogicalCandidateSink,
    *,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> TransferReport:
    """Restore a verified file into the sink's fresh, unbound generation."""

    return transfer_logical_graph(
        LogicalGraphFileSnapshotSource(path),
        sink,
        batch_size=batch_size,
    )


def _bounded_records(
    produce: Callable[..., Iterator[Sequence[_Record]]],
    *,
    batch_size: int,
    what: str,
) -> Iterator[_Record]:
    iterator = produce(batch_size=batch_size)
    for batch in iterator:
        if len(batch) > batch_size:
            raise LogicalSchemaError(
                f"logical backup source {what} batch exceeds its bound",
                detail=f"limit={batch_size} got={len(batch)}",
            )
        yield from batch


def _require_batch_size(batch_size: int) -> None:
    if type(batch_size) is not int or batch_size < 1:
        raise LogicalSchemaError(
            "logical graph file batch_size must be a positive int",
            detail=repr(batch_size),
        )


def _logical_file_path(value: str | Path) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as failure:
        raise LogicalSchemaError(
            "logical graph file path is invalid",
            detail=type(failure).__name__,
        ) from failure
    if not path.name:
        raise LogicalSchemaError("logical graph file path is too broad")
    return path


__all__ = [
    "LogicalGraphFileSnapshot",
    "LogicalGraphFileSnapshotSource",
    "backup_logical_graph_file",
    "restore_logical_graph_file",
]
