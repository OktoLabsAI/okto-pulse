#!/usr/bin/env python3
"""One-shot M-PULSE-4 measurement for Alternative and Assumption indexes.

The corpus and HNSW settings are the frozen Grafx recall fixture.  This is an
evidence driver, not a benchmark gate: it records observed build time and
persisted bytes and deliberately defines no SLO.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

DIMENSION = 384
CORPUS_SEED = 1337
QUERY_SEED = 4242
DEFAULT_ROWS = 8192
DEFAULT_BATCH_SIZE = 64
EF_SEARCH = 320
NODE_SPACES = (
    ("Alternative", "alternative_embedding_idx"),
    ("Assumption", "assumption_embedding_idx"),
)
_INSERT = (
    "CREATE (:{node_type} {{id: $id, graph_layer: 'canonical', "
    "embedding: $embedding}})"
)


def _load_dependencies(grafx_repo: Path, core_repo: Path) -> None:
    """Load the exact source trees named on the command line, or refuse."""

    pulse_repo = Path(__file__).resolve().parents[1]
    required = (
        grafx_repo / "src" / "okto_grafx" / "__init__.py",
        grafx_repo / "bench" / "recall_corpus.py",
        core_repo / "src" / "okto_pulse" / "core",
        pulse_repo / "src" / "okto_pulse" / "community",
    )
    missing = tuple(str(path) for path in required if not path.exists())
    if missing:
        raise ValueError(f"required source path is absent: {missing}")

    sys.path[:0] = [
        str(pulse_repo / "src"),
        str(core_repo / "src"),
        str(grafx_repo / "src"),
        str(grafx_repo),
    ]

    global Timestamp
    global ensure_current_grafx_board_schema
    global generate_vectors
    global okto_grafx
    global quantize_f32
    global sha256_hex
    global vector_bytes_f32
    global vector_bytes_f64

    import okto_grafx as loaded_grafx  # noqa: PLC0415
    from bench.recall_corpus import (  # noqa: PLC0415
        generate_vectors as loaded_generate_vectors,
    )
    from bench.recall_corpus import quantize_f32 as loaded_quantize_f32  # noqa: PLC0415
    from bench.recall_corpus import sha256_hex as loaded_sha256_hex  # noqa: PLC0415
    from bench.recall_corpus import (
        vector_bytes_f32 as loaded_bytes_f32,
    )  # noqa: PLC0415
    from bench.recall_corpus import (
        vector_bytes_f64 as loaded_bytes_f64,
    )  # noqa: PLC0415
    from okto_grafx import Timestamp as LoadedTimestamp  # noqa: PLC0415
    from okto_pulse.community.adapters.grafx_schema_bootstrap import (  # noqa: PLC0415
        ensure_current_grafx_board_schema as loaded_bootstrap,
    )

    loaded_grafx_path = Path(loaded_grafx.__file__).resolve()
    expected_grafx_src = (grafx_repo / "src").resolve()
    if expected_grafx_src not in loaded_grafx_path.parents:
        raise RuntimeError(
            "okto_grafx resolved outside --grafx-repo: " f"{loaded_grafx_path}"
        )

    okto_grafx = loaded_grafx
    Timestamp = LoadedTimestamp
    ensure_current_grafx_board_schema = loaded_bootstrap
    generate_vectors = loaded_generate_vectors
    quantize_f32 = loaded_quantize_f32
    sha256_hex = loaded_sha256_hex
    vector_bytes_f32 = loaded_bytes_f32
    vector_bytes_f64 = loaded_bytes_f64


def _git_state(root: Path) -> dict[str, object]:
    sha = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    dirty = bool(
        subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain"], text=True
        ).strip()
    )
    return {"root": str(root), "sha": sha, "dirty": dirty}


def _tree_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _index_bytes(root: Path, node_type: str, space: str) -> int:
    target = root / "index" / f"vector_{node_type}_{space}.idx"
    if not target.is_file():
        raise RuntimeError(f"expected persisted vector index is absent: {target}")
    return target.stat().st_size


def _search_statement(node_type: str, space: str) -> str:
    return (
        f"MATCH (n:{node_type}) "
        f"WHERE similarity(n.embedding, $query, space => '{space}') >= -1.0 "
        "RETURN n.id, similarity_score() AS score "
        "ORDER BY score DESC LIMIT 10"
    )


def _cold_ann(database, node_type: str, space: str, query: list[float]) -> int:
    result = database.execute(
        _search_statement(node_type, space),
        {"query": query},
    )
    if len(result.rows) != 10 or len({row[0] for row in result.rows}) != 10:
        raise RuntimeError(
            f"{node_type} cold ANN returned {len(result.rows)} rows without 10 unique hits"
        )
    return len(result.rows)


def _insert_corpus(
    database,
    *,
    node_type: str,
    vectors: list[list[float]],
    batch_size: int,
) -> int:
    commits = 0
    statement = _INSERT.format(node_type=node_type)
    for start in range(0, len(vectors), batch_size):
        transaction = database.begin("write")
        try:
            for offset, embedding in enumerate(
                vectors[start : start + batch_size], start=start
            ):
                transaction.execute(
                    statement,
                    {
                        "id": f"m4-{node_type.lower()}-{offset:05d}",
                        "embedding": embedding,
                    },
                )
            report = transaction.commit()
        except BaseException:
            if transaction.active:
                transaction.rollback()
            raise
        if not report.durable or not report.wrote:
            raise RuntimeError(
                f"{node_type} batch {commits} did not report a durable write"
            )
        commits += 1
    return commits


def _measure_one(
    root: Path,
    *,
    node_type: str,
    space: str,
    vectors: list[list[float]],
    query: list[float],
    batch_size: int,
    baseline_index_bytes: int,
    tree_bytes_before: int,
) -> dict[str, object]:
    opened_at = time.perf_counter()
    database = okto_grafx.connect(
        root,
        vector_exact_scan_threshold=0,
        vector_ef_search=EF_SEARCH,
    )
    open_seconds = time.perf_counter() - opened_at
    try:
        ingest_at = time.perf_counter()
        commits = _insert_corpus(
            database,
            node_type=node_type,
            vectors=vectors,
            batch_size=batch_size,
        )
        durable_ingest_seconds = time.perf_counter() - ingest_at

        cold_at = time.perf_counter()
        cold_hits = _cold_ann(database, node_type, space, query)
        post_ingest_cold_ann_seconds = time.perf_counter() - cold_at

        verify_at = time.perf_counter()
        findings = database.verify("all").findings
        verify_seconds = time.perf_counter() - verify_at
        if findings:
            raise RuntimeError(f"{node_type} verification returned {findings!r}")
    finally:
        close_at = time.perf_counter()
        database.close()
        close_seconds = time.perf_counter() - close_at

    persisted_index_bytes = _index_bytes(root, node_type, space)
    tree_bytes_after = _tree_bytes(root)

    reopened_at = time.perf_counter()
    reopened = okto_grafx.connect(
        root,
        vector_exact_scan_threshold=0,
        vector_ef_search=EF_SEARCH,
    )
    reopen_seconds = time.perf_counter() - reopened_at
    try:
        reopen_cold_at = time.perf_counter()
        reopen_hits = _cold_ann(reopened, node_type, space, query)
        reopen_cold_ann_seconds = time.perf_counter() - reopen_cold_at
    finally:
        reopened.close()

    return {
        "node_type": node_type,
        "space": space,
        "rows": len(vectors),
        "batch_size": batch_size,
        "commits": commits,
        "open_seconds": open_seconds,
        "durable_ingest_seconds": durable_ingest_seconds,
        "post_ingest_cold_ann_seconds": post_ingest_cold_ann_seconds,
        "verify_seconds": verify_seconds,
        "close_seconds": close_seconds,
        "reopen_seconds": reopen_seconds,
        "reopen_cold_ann_seconds": reopen_cold_ann_seconds,
        "cold_hits": cold_hits,
        "reopen_hits": reopen_hits,
        "baseline_index_bytes": baseline_index_bytes,
        "persisted_index_bytes": persisted_index_bytes,
        "persisted_index_growth_bytes": persisted_index_bytes - baseline_index_bytes,
        "database_tree_bytes_before": tree_bytes_before,
        "database_tree_bytes_after": tree_bytes_after,
        "database_tree_growth_bytes": tree_bytes_after - tree_bytes_before,
    }


def run(database_path: Path, *, rows: int, seed: int, batch_size: int) -> dict:
    root = database_path.resolve()
    if root.exists():
        raise ValueError(f"database path already exists; refusing overwrite: {root}")
    root.parent.mkdir(parents=True, exist_ok=True)

    corpus64 = generate_vectors(seed, rows, DIMENSION)
    stored = quantize_f32(corpus64)
    query = generate_vectors(QUERY_SEED, 1, DIMENSION)[0]

    bootstrap_at = time.perf_counter()
    with okto_grafx.connect(
        root,
        vector_exact_scan_threshold=0,
        vector_ef_search=EF_SEARCH,
    ) as database:
        ensure_current_grafx_board_schema(
            database,
            board_id="m4-non-public-measurement",
            bootstrapped_at=Timestamp(micros=1),
            embedding_model="uniform-int53-v1/f32",
            embedding_dimension=DIMENSION,
        )
        findings = database.verify("all").findings
        if findings:
            raise RuntimeError(f"bootstrap verification returned {findings!r}")
    bootstrap_seconds = time.perf_counter() - bootstrap_at

    baseline = {
        space: _index_bytes(root, node_type, space) for node_type, space in NODE_SPACES
    }
    tree_bytes = _tree_bytes(root)
    measurements: list[dict[str, object]] = []
    for node_type, space in NODE_SPACES:
        measured = _measure_one(
            root,
            node_type=node_type,
            space=space,
            vectors=stored,
            query=query,
            batch_size=batch_size,
            baseline_index_bytes=baseline[space],
            tree_bytes_before=tree_bytes,
        )
        measurements.append(measured)
        tree_bytes = int(measured["database_tree_bytes_after"])

    pulse_root = Path(__file__).resolve().parents[1]
    grafx_root = Path(okto_grafx.__file__).resolve().parents[2]
    return {
        "contract": "M-PULSE-4-non-public-vector-index-measurement-v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "no_slo": True,
        "measurement_boundary": {
            "durable_ingest_seconds": (
                "execute CREATE statements in fixed-size write batches through the public "
                "transaction API, including each durable commit"
            ),
            "cold_ann_seconds": (
                "one public similarity query forced to ANN by "
                "vector_exact_scan_threshold=0; the HNSW graph is derived memory state "
                "and is not included in persisted_index_bytes"
            ),
            "persisted_index_bytes": (
                "closed-database size of index/vector_<table>_<space>.idx; growth "
                "subtracts the empty index created by schema bootstrap"
            ),
        },
        "fixture": {
            "generator": "uniform-int53-v1",
            "seed": seed,
            "rows": rows,
            "dimension": DIMENSION,
            "stored_values": "float32-quantized components stored in Pulse float64 spaces",
            "source_sha256_f64": sha256_hex(vector_bytes_f64(corpus64)),
            "source_sha256_f32": sha256_hex(vector_bytes_f32(corpus64)),
            "stored_values_sha256_f64": sha256_hex(vector_bytes_f64(stored)),
            "query_seed": QUERY_SEED,
            "query_sha256_f64": sha256_hex(vector_bytes_f64([query])),
        },
        "hnsw": {
            "neighbours": 16,
            "ef_construction": 200,
            "ef_search": EF_SEARCH,
            "index_seed": "0x0C701A11F0C0FFEE",
            "exact_scan_threshold": 0,
        },
        "database_path": str(root),
        "bootstrap_seconds": bootstrap_seconds,
        "measurements": measurements,
        "source": {
            "pulse": _git_state(pulse_root),
            "grafx": _git_state(grafx_root),
            "okto_grafx_module": str(Path(okto_grafx.__file__).resolve()),
        },
        "runtime": {
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grafx-repo", required=True, type=Path)
    parser.add_argument("--core-repo", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--seed", type=int, default=CORPUS_SEED)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    arguments = parser.parse_args(argv)
    if arguments.rows < 1 or arguments.batch_size < 1:
        parser.error("--rows and --batch-size must be positive")

    output = arguments.out.resolve()
    database = arguments.database.resolve()
    if output.exists():
        parser.error(f"output already exists; refusing overwrite: {output}")
    if output == database or database in output.parents:
        parser.error("--out must be outside --database")

    grafx_repo = arguments.grafx_repo.resolve()
    core_repo = arguments.core_repo.resolve()
    _load_dependencies(grafx_repo, core_repo)

    evidence = run(
        database,
        rows=arguments.rows,
        seed=arguments.seed,
        batch_size=arguments.batch_size,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
