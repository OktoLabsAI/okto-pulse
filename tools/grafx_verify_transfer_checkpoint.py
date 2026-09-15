"""Reconcile an existing isolated board restore without importing or binding it."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
from time import perf_counter

import okto_grafx
from okto_pulse.community.adapters.grafx_relationship_layout import PULSE_RELATIONSHIP_LAYOUT
from okto_pulse.community.adapters.logical_graph_file import verify_logical_graph_file
from okto_pulse.community.adapters.logical_graph_transfer import backup_logical_graph_file
from okto_pulse.community.adapters.logical_transfer_grafx import CommunityGrafxLogicalSnapshotSource
from okto_pulse.community.adapters.logical_transfer_schema import board_logical_schema


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint", type=Path)
    args = parser.parse_args()
    root = args.checkpoint.resolve(strict=True)
    # Exclusive directory creation prevents overwriting any earlier evidence.
    evidence = root / "cold-reconciliation"
    evidence.mkdir(exist_ok=False)
    expected = verify_logical_graph_file(root / "board.jsonl")
    layouts = {(entry.logical_type, entry.from_type, entry.to_type): entry.physical_table
               for entry in PULSE_RELATIONSHIP_LAYOUT.entries}
    start = perf_counter()
    with okto_grafx.connect(root / "restored", read_only=True, page_size=8192,
                           descriptor_revalidation="generation") as db:
        opened = perf_counter() - start
        print(json.dumps({"phase": "cold_open", "seconds": opened}), flush=True)
        start = perf_counter()
        verification = db.verify("all")
        if verification.clean is not True:
            raise RuntimeError(f"Candidate is not clean: {len(verification.findings)} findings")
        verified = perf_counter() - start
        print(json.dumps({"phase": "verify_all", "seconds": verified, "clean": True}), flush=True)
        source = CommunityGrafxLogicalSnapshotSource(
            db, schema=board_logical_schema(), relationship_tables=layouts,
            scan_batch_size=500, temporary_parent=evidence,
        )
        start = perf_counter()
        observed = backup_logical_graph_file(evidence / "restored.jsonl", source, batch_size=500)
        compared = perf_counter() - start
        for field in ("scope", "counts", "schema_digest", "fingerprint"):
            if getattr(expected, field) != getattr(observed, field):
                raise RuntimeError(f"Candidate differs from backup: {field}")
    report = {"phase": "cold_reconciliation_passed", "bound": False,
              "read_only": True, "counts": asdict(observed.counts),
              "fingerprint": observed.fingerprint, "schema_digest": observed.schema_digest,
              "cold_open_seconds": opened, "verify_all_seconds": verified,
              "logical_compare_seconds": compared,
              "original_restore_exit_status": "not_observed"}
    with (evidence / "report.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
