"""Atomic filesystem publication for the portable logical graph artifact.

The Core codec deliberately owns no paths or file handles.  This Community
adapter adds exactly that physical boundary: write a complete candidate beside
the destination, flush it durably, verify the bytes through the Core streaming
decoder, and only then replace the visible artifact.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from okto_pulse.core.kg.logical_transfer import (
    ArtifactEvent,
    ArtifactIntegrityError,
    LogicalArtifactHeader,
    LogicalArtifactManifest,
    LogicalCounts,
    LogicalNode,
    LogicalRelation,
    LogicalSchema,
    LogicalTransferError,
    PhasedTransferError,
    TransferFailedError,
    decode_records,
    encode_artifact,
)


@dataclass(frozen=True, slots=True)
class LogicalGraphFileCertificate:
    """Claims proved by consuming the published candidate through its manifest."""

    scope: str
    counts: LogicalCounts
    schema_digest: str
    fingerprint: str
    stream_checksum: str


def publish_logical_graph_file(
    path: str | os.PathLike[str],
    schema: LogicalSchema,
    nodes: Iterable[LogicalNode],
    relations: Iterable[LogicalRelation],
    *,
    counts: LogicalCounts,
    optional_features: Iterable[str] = (),
) -> LogicalGraphFileCertificate:
    """Publish one fully verified artifact without exposing a partial file.

    The temporary file is created in the destination directory, so
    :func:`os.replace` cannot cross filesystems.  There is deliberately no
    fallible step after the replace: every reported failure leaves the previous
    visible generation untouched.
    """

    destination = Path(path).expanduser()
    temporary: Path | None = None
    failure: PhasedTransferError | None = None
    cause: BaseException | None = None
    certificate: LogicalGraphFileCertificate | None = None

    try:
        descriptor, raw_temporary = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temporary = Path(raw_temporary)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            _write_artifact(
                stream,
                schema,
                nodes,
                relations,
                counts=counts,
                optional_features=optional_features,
            )
            stream.flush()
            _fsync_file(stream.fileno())
        certificate = _verify_complete_file(temporary)
        _replace_file(temporary, destination)
        # The source name ceased to exist atomically.  Clearing it is part of
        # the replace step itself: the finally block must perform no fallible
        # filesystem operation after publication became visible.
        temporary = None
    except PhasedTransferError as exc:
        failure = exc
        cause = exc.__cause__
    except Exception as exc:
        failure = TransferFailedError(
            "logical graph file publication failed",
            phase="write",
            detail=f"{type(exc).__name__}: {exc}",
        )
        cause = exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                if failure is None:
                    failure = TransferFailedError(
                        "logical graph temporary file cleanup failed",
                        phase="write",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                    cause = exc
                else:
                    failure.add_note(
                        "temporary cleanup also failed: " f"{type(exc).__name__}: {exc}"
                    )

    if failure is not None:
        raise failure from cause
    assert certificate is not None
    return certificate


def verify_logical_graph_file(
    path: str | os.PathLike[str],
) -> LogicalGraphFileCertificate:
    """Consume and verify an existing artifact without materialising its graph."""

    try:
        return _verify_complete_file(Path(path).expanduser())
    except LogicalTransferError:
        raise
    except Exception as exc:
        raise TransferFailedError(
            "logical graph file could not be read",
            phase="reopen",
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc


def _write_artifact(
    stream: TextIO,
    schema: LogicalSchema,
    nodes: Iterable[LogicalNode],
    relations: Iterable[LogicalRelation],
    *,
    counts: LogicalCounts,
    optional_features: Iterable[str],
) -> None:
    for line in encode_artifact(
        schema,
        nodes,
        relations,
        counts=counts,
        optional_features=optional_features,
    ):
        stream.write(line)
        stream.write("\n")


def _artifact_lines(stream: TextIO) -> Iterator[str]:
    for raw_line in stream:
        if raw_line.endswith("\n"):
            yield raw_line[:-1]
        else:
            yield raw_line


def _verify_complete_file(path: Path) -> LogicalGraphFileCertificate:
    header: LogicalArtifactHeader | None = None
    manifest: LogicalArtifactManifest | None = None
    with path.open("r", encoding="utf-8", newline="") as stream:
        for event in decode_records(_artifact_lines(stream)):
            header, manifest = _capture_terminal_claims(event, header, manifest)
    if header is None or manifest is None:
        raise ArtifactIntegrityError(
            "verified artifact omitted terminal claims",
            detail=path.name,
        )
    return LogicalGraphFileCertificate(
        scope=header.scope,
        counts=manifest.counts,
        schema_digest=header.schema_digest,
        fingerprint=manifest.fingerprint,
        stream_checksum=manifest.stream_checksum,
    )


def _capture_terminal_claims(
    event: ArtifactEvent,
    header: LogicalArtifactHeader | None,
    manifest: LogicalArtifactManifest | None,
) -> tuple[LogicalArtifactHeader | None, LogicalArtifactManifest | None]:
    if event.header is not None:
        header = event.header
    if event.manifest is not None:
        manifest = event.manifest
    return header, manifest


def _fsync_file(descriptor: int) -> None:
    os.fsync(descriptor)


def _replace_file(source: Path, destination: Path) -> None:
    os.replace(source, destination)


__all__ = [
    "LogicalGraphFileCertificate",
    "publish_logical_graph_file",
    "verify_logical_graph_file",
]
