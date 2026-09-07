"""Finite synthetic integration checkpoint; requires a NEW, unbound output path.

No runtime registry, source Spec, binding, live Pulse path or performance gate is
used. Results are measurements plus assertions of logical equality/durability.
Run with the intended Community/Core/Grafx source roots on PYTHONPATH.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
from pathlib import Path
import random
import threading
from time import perf_counter, sleep

import okto_grafx
from okto_grafx.errors import GrafxLeaseTimeout, GrafxWriteConflict
from okto_pulse.core.kg.logical_transfer import (
    LogicalNode, LogicalNodeType, LogicalPropertyDef, LogicalRelation,
    LogicalRelationLayout, LogicalSchema, LogicalVector, LogicalVectorSpace,
    count_graph, fingerprint_graph, transfer_logical_graph,
)
from okto_pulse.community.adapters.grafx_logical_sink import CommunityGrafxLogicalCandidateSink
from okto_pulse.community.adapters.logical_graph_transfer import (
    backup_logical_graph_file, restore_logical_graph_file,
)
from okto_pulse.community.adapters.logical_transfer_grafx import CommunityGrafxLogicalSnapshotSource

DIM = 384
LAYOUTS = {("links", "N", "N"): "links__N__N"}


def vector(seed):
    rng = random.Random(seed)
    return tuple(rng.randrange(-8, 9) / 8 for _ in range(DIM))


def schema():
    return LogicalSchema(
        scope="board",
        node_types=(LogicalNodeType("N", "id", (
            LogicalPropertyDef("id", "string", nullable=False),
            LogicalPropertyDef("title", "string"),
            LogicalPropertyDef("embedding", "vector", vector_space="semantic"),
        )),),
        relation_layouts=(LogicalRelationLayout("links", "N", "N"),),
        vector_spaces=(LogicalVectorSpace("semantic", "float32", DIM, "cosine", False),),
    )


class SeedSnapshot:
    def __init__(self, count):
        self.nodes = tuple(LogicalNode("N", f"n{i}", {
            "id": f"n{i}", "title": f"Synthetic {i}",
            "embedding": LogicalVector("semantic", "float32", vector(i)),
        }) for i in range(count))
        self.relations = tuple(LogicalRelation("links", "N", "N", f"n{i}", f"n{(i+1)%count}")
                               for i in range(count) for _ in range(2))
        self.closed = False

    def open_snapshot(self):
        return self

    def schema(self):
        return schema()

    def counts(self):
        return count_graph(self.nodes, self.relations)

    def iter_nodes(self, *, batch_size):
        for start in range(0, len(self.nodes), batch_size):
            yield self.nodes[start:start + batch_size]

    def iter_relations(self, *, batch_size):
        for start in range(0, len(self.relations), batch_size):
            yield self.relations[start:start + batch_size]

    def close(self):
        self.closed = True


def sink(path):
    return CommunityGrafxLogicalCandidateSink(
        path, expected_schema=schema(), relationship_tables=LAYOUTS,
        max_batch_size=64, temporary_parent=path.parent,
    )


def source(db, root):
    return CommunityGrafxLogicalSnapshotSource(
        db, schema=schema(), relationship_tables=LAYOUTS,
        scan_batch_size=64, temporary_parent=root,
    )


def vector_measure(path, threshold):
    opened = perf_counter()
    with okto_grafx.connect(path, page_size=8192, read_only=True,
                           descriptor_revalidation="generation",
                           vector_exact_scan_threshold=threshold) as db:
        open_seconds = perf_counter() - opened
        observations = []
        for _ in range(3):
            started = perf_counter()
            with db.begin("read") as tx:
                result = db.search_vectors(tx, space="semantic", query=vector(991), k=5)
                hits = [(hit.record_id, hit.score) for hit in result.hits]
            assert len(hits) == 5 and len({hit[0] for hit in hits}) == 5
            observations.append({"seconds": perf_counter() - started, "hits": hits})
        assert all(item["hits"] == observations[0]["hits"] for item in observations)
        return {"open_seconds": open_seconds, "observations": observations}


def concurrency_measure(path, initial_count):
    ready = threading.Barrier(4)
    first_commit = threading.Event()
    done = [threading.Event(), threading.Event()]

    def reader(slot):
        elapsed = []
        with okto_grafx.connect(path, page_size=8192, read_only=True,
                               descriptor_revalidation="generation") as db:
            with db.begin("read") as tx:
                def check():
                    start = perf_counter()
                    count = tx.execute("MATCH (n:N) RETURN count(*)").rows[0][0]
                    assert count == initial_count, (slot, count, initial_count)
                    elapsed.append(perf_counter() - start)
                check()
                ready.wait(30)
                assert first_commit.wait(30), "writers made no acknowledged progress"
                check()
                assert all(event.wait(30) for event in done), "writer did not finish"
                check()
        return {"role": "reader", "slot": slot, "snapshot_counts": [initial_count] * 3,
                "read_seconds": elapsed}

    def writer(slot):
        acknowledged = []
        durations = []
        conflicts = 0
        try:
            with okto_grafx.connect(path, page_size=8192,
                                   descriptor_revalidation="generation") as db:
                ready.wait(30)
                for batch in range(4):
                    ids = [f"writer{slot}-{batch}-{offset}" for offset in range(2)]
                    start = perf_counter()
                    for attempt in range(8):
                        tx = None
                        try:
                            with db.begin("write") as tx:
                                for identity in ids:
                                    tx.execute("CREATE (:N {id:$id,title:$title,embedding:$v})", {
                                        "id": identity, "title": "Synthetic concurrent write",
                                        "v": list(vector(1000 + slot * 20 + batch)),
                                    })
                            assert tx.report is not None and tx.report.durable
                            acknowledged.extend(ids)
                            durations.append(perf_counter() - start)
                            first_commit.set()
                            break
                        except (GrafxWriteConflict, GrafxLeaseTimeout) as exc:
                            if not exc.retryable or (tx is not None and tx.report is not None
                                                     and tx.report.durable):
                                raise
                            conflicts += 1
                            if attempt == 7:
                                raise
                            sleep(0.005 * (attempt + 1))
        finally:
            done[slot].set()
        return {"role": "writer", "slot": slot, "acknowledged": acknowledged,
                "batch_seconds_including_retries": durations, "conflicts": conflicts}

    start = perf_counter()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(reader, slot) for slot in range(2)]
        futures += [pool.submit(writer, slot) for slot in range(2)]
        reports = [future.result(timeout=120) for future in futures]
    # Reader-only cold open deliberately cannot replay an uncheckpointed WAL.
    # Exercise the normal writable recovery door, then certify a readonly reopen.
    recovery_started = perf_counter()
    with okto_grafx.connect(path, page_size=8192) as recovered:
        recovery_open_seconds = perf_counter() - recovery_started
        checkpoint_started = perf_counter()
        recovered.checkpoint()
        checkpoint_seconds = perf_counter() - checkpoint_started
    with okto_grafx.connect(path, page_size=8192, read_only=True) as db:
        stored = [row[0] for row in db.execute("MATCH (n:N) RETURN n.id").rows]
        ack = {item for report in reports if report["role"] == "writer"
               for item in report["acknowledged"]}
        expected = {f"n{i}" for i in range(initial_count)} | ack
        assert len(ack) == 16
        assert len(stored) == len(set(stored)) and set(stored) == expected
        assert db.verify("all").clean
    return {"seconds": perf_counter() - start, "participants": reports,
            "recovery_open_seconds": recovery_open_seconds,
            "checkpoint_seconds": checkpoint_seconds,
            "cold_rows": len(stored), "cold_verify_clean": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--nodes", type=int, default=128)
    args = parser.parse_args()
    if not 8 <= args.nodes <= 2000:
        parser.error("--nodes must be 8..2000 for this bounded checkpoint")
    root = args.output.resolve()
    root.mkdir(parents=False, exist_ok=False)
    seed = SeedSnapshot(args.nodes)
    start = perf_counter()
    seeded = transfer_logical_graph(seed, sink(root / "source"), batch_size=64)
    seed_seconds = perf_counter() - start
    assert seed.closed
    assert seeded.fingerprint == fingerprint_graph(schema(), seed.nodes, seed.relations)
    exact = vector_measure(root / "source", args.nodes + 1)
    ann = vector_measure(root / "source", 0)
    exact_ids = {hit[0] for hit in exact["observations"][0]["hits"]}
    ann_ids = {hit[0] for hit in ann["observations"][0]["hits"]}
    with okto_grafx.connect(root / "source", page_size=8192, read_only=True) as db:
        start = perf_counter()
        backup = backup_logical_graph_file(root / "graph.jsonl", source(db, root), batch_size=64)
        backup_seconds = perf_counter() - start
    start = perf_counter()
    restored = restore_logical_graph_file(root / "graph.jsonl", sink(root / "restored"), batch_size=64)
    restore_seconds = perf_counter() - start
    assert seeded.fingerprint == backup.fingerprint == restored.fingerprint
    assert seeded.counts == backup.counts == restored.counts
    concurrency = concurrency_measure(root / "source", args.nodes)
    print(json.dumps({"grafx_source": okto_grafx.__file__, "root": str(root),
                      "counts": asdict(seeded.counts), "fingerprint": seeded.fingerprint,
                      "seed_seconds": seed_seconds, "exact_vector": exact,
                      "ann_vector": ann, "observed_recall_at_5": len(exact_ids & ann_ids) / 5,
                      "backup_seconds": backup_seconds, "restore_cold_certified_seconds": restore_seconds,
                      "backup_bytes": (root / "graph.jsonl").stat().st_size,
                      "concurrency": concurrency}, indent=2))


if __name__ == "__main__":
    main()
