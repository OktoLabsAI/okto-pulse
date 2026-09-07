"""Read-only real-board export and cold-certified restore to a NEW unbound path.

No live graph binding is created/changed. The source is always opened read-only;
if it cannot safely open, the checkpoint refuses instead of recovering/mutating it.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
from time import perf_counter

import okto_grafx
from okto_pulse.community.adapters.grafx_logical_sink import CommunityGrafxLogicalCandidateSink
from okto_pulse.community.adapters.grafx_relationship_layout import PULSE_RELATIONSHIP_LAYOUT
from okto_pulse.community.adapters.logical_graph_transfer import (
    backup_logical_graph_file, restore_logical_graph_file,
)
from okto_pulse.community.adapters.logical_transfer_grafx import CommunityGrafxLogicalSnapshotSource
from okto_pulse.community.adapters.logical_transfer_schema import board_logical_schema


def emit(phase, **fields):
    print(json.dumps({"phase": phase, **fields}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source_path = args.source.resolve(strict=True)
    output = args.output.resolve()
    if output == source_path or output.is_relative_to(source_path):
        parser.error("output must be outside the source graph")
    output.mkdir(parents=False, exist_ok=False)
    schema = board_logical_schema()
    layouts = {(entry.logical_type, entry.from_type, entry.to_type): entry.physical_table
               for entry in PULSE_RELATIONSHIP_LAYOUT.entries}
    start = perf_counter()
    with okto_grafx.connect(source_path, read_only=True, page_size=8192,
                           descriptor_revalidation="generation") as db:
        emit("readonly_open", seconds=perf_counter() - start, source=str(source_path),
             grafx_source=okto_grafx.__file__)
        source = CommunityGrafxLogicalSnapshotSource(
            db, schema=schema, relationship_tables=layouts,
            scan_batch_size=500, temporary_parent=output,
        )
        start = perf_counter()
        artifact = backup_logical_graph_file(output / "board.jsonl", source, batch_size=500)
        emit("backup_verified", seconds=perf_counter() - start,
             counts=asdict(artifact.counts), fingerprint=artifact.fingerprint,
             bytes=(output / "board.jsonl").stat().st_size)
    candidate = CommunityGrafxLogicalCandidateSink(
        output / "restored", expected_schema=schema, relationship_tables=layouts,
        max_batch_size=500, temporary_parent=output,
    )
    start = perf_counter()
    emit("restore_started", candidate=str(output / "restored"), bound=False)
    restored = restore_logical_graph_file(output / "board.jsonl", candidate, batch_size=500)
    assert restored.fingerprint == artifact.fingerprint
    assert restored.schema_digest == artifact.schema_digest
    assert restored.counts == artifact.counts
    emit("restore_cold_certified", seconds=perf_counter() - start,
         report=asdict(restored), bound=False)


if __name__ == "__main__":
    main()
