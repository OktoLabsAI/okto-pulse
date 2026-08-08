#!/usr/bin/env python3
"""Installed-runtime release probe for SQLite lifecycle and KG parity.

The controller is intentionally kept outside the package under test.  The
release gate runs this file with the isolated virtual environment's Python and
an empty ``PYTHONPATH``; every ``okto_pulse`` import therefore resolves from the
freshly installed wheel pair.

Worker modes are implementation details used to exercise real process
boundaries.  They let the controller prove concurrent cold starts and an
abrupt process exit during data bootstrap without sharing in-process singleton
state.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

RESULT_PREFIX = "INSTALLED_RUNTIME_MATRIX="
CRASH_EXIT_CODE = 91
CRASH_BOOTSTRAP_STEP = "_bootstrap_default_discovery_intents"

_SKA_TABLE_PREFIXES = (
    "quality_",
    "research_decision_",
    "checklist_",
)
_SEMANTIC_TABLES = (
    "permission_presets",
    "discovery_intents",
    "checklist_template_versions",
)


def _database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.resolve().as_posix()}"


async def _initialize_database(path: Path) -> None:
    # Import the Community app first so every productive ORM model is
    # registered on Base.metadata before the create_all boundary runs.
    import okto_pulse.community.app as _community_app  # noqa: F401
    from okto_pulse.community.adapters.relational_schema_lifecycle import (
        register_community_relational_schema_lifecycle,
    )
    from okto_pulse.community.adapters.sqlalchemy_database import (
        close_db,
        configure_community_database,
        init_db,
    )

    configure_community_database(_database_url(path))
    register_community_relational_schema_lifecycle()
    try:
        await init_db()
    finally:
        await close_db()


async def _crash_during_bootstrap(path: Path, marker: Path) -> None:
    import okto_pulse.community.app as _community_app  # noqa: F401
    from okto_pulse.community.adapters.data_bootstrapper import (
        CommunityDataBootstrapper,
        build_community_data_bootstrap_ledger,
        make_community_data_bootstrapper,
    )
    from okto_pulse.community.adapters.relational_schema_migrator import (
        make_community_relational_schema_migrator,
    )
    from okto_pulse.community.adapters.sqlalchemy_database import (
        configure_community_database,
    )

    configure_community_database(_database_url(path))
    migrator = make_community_relational_schema_migrator(
        target="release-crash-resume"
    )
    migration_result = await migrator.aexecute(
        migrator.plan(target="release-crash-resume")
    )
    if not migration_result.is_success:
        raise RuntimeError(
            "crash fixture could not establish schema: "
            f"{migration_result.failure_reason}"
        )

    real_bootstrapper = make_community_data_bootstrapper(
        target="release-crash-resume"
    )
    callables = dict(real_bootstrapper._callables)

    def _abrupt_exit() -> None:
        marker.write_text(CRASH_BOOTSTRAP_STEP, encoding="utf-8")
        os._exit(CRASH_EXIT_CODE)

    callables[CRASH_BOOTSTRAP_STEP] = _abrupt_exit
    crashing = CommunityDataBootstrapper(
        steps=build_community_data_bootstrap_ledger(),
        callables=callables,
        target="release-crash-resume",
    )
    await crashing.aexecute(crashing.plan(target="release-crash-resume"))
    raise RuntimeError("injected bootstrap crash did not terminate the process")


def _worker_main(args: argparse.Namespace) -> int:
    if args.init_worker is not None:
        asyncio.run(_initialize_database(args.init_worker))
        print("SQLITE_INIT_OK")
        return 0
    if args.crash_worker is not None:
        if args.crash_marker is None:
            raise ValueError("--crash-marker is required with --crash-worker")
        asyncio.run(_crash_during_bootstrap(args.crash_worker, args.crash_marker))
        return 2
    return -1


def _sha256_json(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sqlite_snapshot(path: Path) -> dict[str, Any]:
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        schema_rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        ).fetchall()
        schema = [
            {
                "type": str(row["type"]),
                "name": str(row["name"]),
                "table": str(row["tbl_name"]),
                "sql": " ".join(str(row["sql"] or "").split()),
            }
            for row in schema_rows
        ]
        table_names = {
            row["name"]
            for row in schema
            if row["type"] == "table"
        }
        semantic_rows: dict[str, list[dict[str, Any]]] = {}
        for table in _SEMANTIC_TABLES:
            if table not in table_names:
                semantic_rows[table] = []
                continue
            columns = [
                str(row[1])
                for row in connection.execute(
                    f'PRAGMA table_info("{table}")'
                ).fetchall()
            ]
            volatile = {
                "created_at",
                "updated_at",
                "last_used_at",
            }
            if table in {"permission_presets", "discovery_intents"}:
                # Built-in seed identities are intentionally generated on first
                # materialization. Their natural keys and governed payloads,
                # not random UUID allocation order, define replay parity.
                volatile.add("id")
            selected = [column for column in columns if column not in volatile]
            order_by = (
                "name"
                if "name" in selected
                else "version"
                if "version" in selected
                else selected[0]
            )
            projection = ", ".join(
                '"' + column.replace('"', '""') + '"'
                for column in selected
            )
            rows = connection.execute(
                f"SELECT {projection} "
                f'FROM "{table}" ORDER BY "{order_by}"'
            ).fetchall()
            semantic_rows[table] = [
                {column: row[column] for column in selected}
                for row in rows
            ]
        foreign_key_errors = [
            tuple(row)
            for row in connection.execute("PRAGMA foreign_key_check").fetchall()
        ]

    semantic = {
        "schema": schema,
        "seed_rows": semantic_rows,
        "foreign_key_errors": foreign_key_errors,
    }
    return {
        "schema_object_count": len(schema),
        "table_count": sum(row["type"] == "table" for row in schema),
        "seed_counts": {
            table: len(rows)
            for table, rows in semantic_rows.items()
        },
        "foreign_key_errors": foreign_key_errors,
        "schema_sha256": _sha256_json(schema),
        "logical_sha256": _sha256_json(semantic),
    }


def _run_worker(
    script: Path,
    *arguments: object,
    timeout: int = 240,
    expected_codes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        (sys.executable, str(script), *(str(value) for value in arguments)),
        cwd=script.parent,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode not in expected_codes:
        raise RuntimeError(
            f"runtime matrix worker failed ({completed.returncode}): "
            f"stdout={completed.stdout[-1500:]!r} "
            f"stderr={completed.stderr[-1500:]!r}"
        )
    return completed


def _prepare_upgrade_fixture(path: Path) -> dict[str, Any]:
    sentinel_board = "release-upgrade-board"
    sentinel_spec = "release-upgrade-spec"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO boards (id, name, owner_id) VALUES (?, ?, ?)",
            (sentinel_board, "Release upgrade sentinel", "release-gate"),
        )
        connection.execute(
            "INSERT INTO specs "
            "(id, board_id, title, status, version, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                sentinel_spec,
                sentinel_board,
                "Release upgrade sentinel",
                "draft",
                1,
                "release-gate",
            ),
        )
        connection.commit()

        all_tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ).fetchall()
        ]
        dropped = [
            table
            for table in all_tables
            if table.startswith(_SKA_TABLE_PREFIXES)
        ]
        connection.execute("PRAGMA foreign_keys=OFF")
        for table in dropped:
            connection.execute(f'DROP TABLE "{table}"')
        connection.commit()
    if not dropped:
        raise RuntimeError("upgrade fixture removed no SK-A tables")
    return {
        "sentinel_board_id": sentinel_board,
        "sentinel_spec_id": sentinel_spec,
        "dropped_table_count": len(dropped),
        "dropped_tables": dropped,
    }


def _assert_upgrade_sentinel(path: Path, fixture: dict[str, Any]) -> None:
    with sqlite3.connect(path) as connection:
        board = connection.execute(
            "SELECT name FROM boards WHERE id=?",
            (fixture["sentinel_board_id"],),
        ).fetchone()
        spec = connection.execute(
            "SELECT title, status, edition, version FROM specs WHERE id=?",
            (fixture["sentinel_spec_id"],),
        ).fetchone()
    if board != ("Release upgrade sentinel",):
        raise RuntimeError(f"upgrade board sentinel drifted: {board!r}")
    if spec != ("Release upgrade sentinel", "draft", 1, 1):
        raise RuntimeError(f"upgrade spec sentinel drifted: {spec!r}")


def _concurrent_fresh_start(script: Path, path: Path) -> dict[str, Any]:
    commands = [
        (sys.executable, str(script), "--init-worker", str(path))
        for _ in range(2)
    ]
    processes = [
        subprocess.Popen(
            command,
            cwd=script.parent,
            env=dict(os.environ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for command in commands
    ]
    results: list[dict[str, Any]] = []
    for process in processes:
        try:
            stdout, stderr = process.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            for running in processes:
                if running.poll() is None:
                    running.kill()
            stdout, stderr = process.communicate()
            raise RuntimeError(
                "concurrent SQLite initialization timed out: "
                f"stdout={stdout[-1000:]!r} stderr={stderr[-1000:]!r}"
            )
        results.append(
            {
                "returncode": process.returncode,
                "stdout_tail": stdout[-300:],
                "stderr_tail": stderr[-500:],
            }
        )
    failures = [result for result in results if result["returncode"] != 0]
    if failures:
        raise RuntimeError(
            "concurrent SQLite initialization failed closed: "
            + json.dumps(failures, sort_keys=True)
        )
    return {
        "workers": len(results),
        "returncodes": [result["returncode"] for result in results],
    }


def _non_sqlite_rejection() -> dict[str, Any]:
    from okto_pulse.community.adapters.sqlalchemy_database import (
        build_community_engine,
        is_database_runtime_configured,
    )

    configured_before = is_database_runtime_configured()
    try:
        build_community_engine(
            "postgresql+asyncpg://release:release@127.0.0.1/release"
        )
    except ValueError as exc:
        error = str(exc)
    else:
        raise RuntimeError("non-SQLite backend was accepted")
    configured_after = is_database_runtime_configured()
    if error != "community_database_requires_sqlite":
        raise RuntimeError(f"unexpected non-SQLite rejection: {error!r}")
    if configured_after != configured_before:
        raise RuntimeError("non-SQLite rejection mutated relational runtime state")
    return {
        "rejected": True,
        "error": error,
        "before_bootstrap": True,
        "runtime_configured_before": configured_before,
        "runtime_configured_after": configured_after,
    }


def _normalize_worker_result(result: object) -> dict[str, Any]:
    def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            rows,
            key=lambda row: json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ),
        )

    nodes = _sort_rows(
        [
            (
                {
                    key: value
                    for key, value in asdict(node).items()
                    if key != "candidate_id"
                }
                | {"candidate_id": node.candidate_id}
            )
            for node in result.nodes
        ]
    )
    edges = _sort_rows([asdict(edge) for edge in result.edges])
    missing = _sort_rows(
        [asdict(item) for item in result.missing_link_candidates]
    )
    active_set = result.relational_projection_active_set_intent
    return {
        "nodes": nodes,
        "edges": edges,
        "missing_links": missing,
        "content_hash": result.content_hash,
        "raw_content_sha256": hashlib.sha256(
            result.raw_content.encode("utf-8")
        ).hexdigest(),
        "spec_lineage_parent_intent": result.spec_lineage_parent_intent.value,
        "relational_projection_candidate_ids": sorted(
            result.relational_projection_candidate_ids
        ),
        "relational_projection_active_set": (
            asdict(active_set) if active_set is not None else None
        ),
    }


def _kg_projection_parity() -> dict[str, Any]:
    """Compare incremental-final and rebuild-final deterministic projections.

    This is the installed-runtime TS24-C11 oracle only.  It compares the
    deterministic projection produced after an incremental churn with a clean
    deterministic rebuild.  It intentionally does NOT materialize or purge
    productive Kuzu; the permanent TS21/TS24 real-Kuzu tests retain that wider
    integration coverage.
    """

    from okto_pulse.core.application.processors.deterministic_kg import (
        DeterministicWorker,
    )

    board_id = "release-parity-board"
    quality = [
        {
            "assessment_kind": "ambiguity",
            "score": 1,
            "scale_kind": "ambiguity_score",
            "scale_min": 1,
            "scale_max": 5,
            "scale_direction": "lower_better",
            "subject_version": 2,
            "receipt_id": "quality-release-1",
            "head_revision": 1,
            "currentness": "current",
        }
    ]
    decisions = [
        {
            "board_id": board_id,
            "refinement_id": "refinement-release",
            "refinement_version": 2,
            "ledger_id": "ledger-storage-release",
            "entry_id": "rdl-release-1",
            "head_revision": 1,
            "predecessor_entry_id": None,
            "unknown": "Which storage backend belongs to Community?",
            "status": "resolved",
            "anchor_type": "technical_requirement",
            "anchor_ref": "tr-release",
            "decision": "Use SQLite for the Local First edition.",
            "rationale": "The release runtime is intentionally local-first.",
            "evidence_refs": ["quality:quality-release-1"],
            "alternatives": ["PostgreSQL"],
            "confidence": 1.0,
            "evidence_absence_justification": None,
            "projection_fingerprint": "d" * 64,
        }
    ]
    final_payloads: dict[str, dict[str, Any]] = {
        "story": {
            "id": "story-release",
            "board_id": board_id,
            "title": "Release story",
            "description": "A complete installed-runtime release.",
            "status": "done",
        },
        "ideation": {
            "id": "ideation-release",
            "board_id": board_id,
            "title": "Release ideation",
            "description": "Prove ambiguity is current.",
            "status": "done",
            "quality_assessments": quality,
        },
        "refinement": {
            "id": "refinement-release",
            "board_id": board_id,
            "ideation_id": "ideation-release",
            "title": "Release refinement",
            "description": "Resolve the storage choice.",
            "analysis": "The Local First boundary remains SQLite-only.",
            "status": "done",
            "quality_assessments": quality,
            "research_decisions": decisions,
        },
        "spec": {
            "id": "spec-release",
            "board_id": board_id,
            "refinement_id": "refinement-release",
            "title": "Release spec",
            "description": "Freeze the release contract.",
            "context": "Installed runtime.",
            "status": "done",
            "version": 2,
            "functional_requirements": [
                {"id": "fr-release", "text": "Runtime evidence is complete."}
            ],
            "technical_requirements": [
                {"id": "tr-release", "text": "SQLite startup is repeatable."}
            ],
            "acceptance_criteria": [
                {"id": "ac-release", "text": "All release gates pass."}
            ],
            "quality_assessments": quality,
        },
        "sprint": {
            "id": "sprint-release",
            "board_id": board_id,
            "title": "Release sprint",
            "description": "Ship the verified artifact.",
            "objective": "Close the release.",
            "status": "done",
        },
        "card": {
            "id": "card-release",
            "board_id": board_id,
            "spec_id": "spec-release",
            "title": "Release card",
            "description": "Execute the release.",
            "status": "done",
            "card_type": "task",
        },
    }
    worker = DeterministicWorker()
    incremental_final: dict[str, dict[str, Any]] = {}
    rebuild_final: dict[str, dict[str, Any]] = {}
    for artifact_type, payload in final_payloads.items():
        process = getattr(worker, f"process_{artifact_type}")
        earlier = dict(payload)
        earlier["status"] = "draft"
        earlier["title"] = f"{payload['title']} draft"
        process(earlier)
        incremental_final[artifact_type] = _normalize_worker_result(
            process(dict(payload))
        )
        rebuild_final[artifact_type] = _normalize_worker_result(
            DeterministicWorker().process_artifact(
                artifact_type,
                dict(payload),
            )
        )

    mismatches = sorted(
        artifact_type
        for artifact_type in final_payloads
        if incremental_final[artifact_type] != rebuild_final[artifact_type]
    )
    if mismatches:
        raise RuntimeError(
            f"installed incremental/rebuild projection drift: {mismatches}"
        )
    refinement_projection = rebuild_final["refinement"]
    node_types = {
        node["node_type"]
        for node in refinement_projection["nodes"]
    }
    if not {"Decision", "Alternative"} <= node_types:
        raise RuntimeError(
            "SK-A RDL nodes missing from installed rebuild projection"
        )
    for artifact_type in ("ideation", "refinement", "spec"):
        contexts = " ".join(
            str(node.get("context") or "")
            for node in rebuild_final[artifact_type]["nodes"]
        )
        if "ambiguity" not in contexts.lower():
            raise RuntimeError(
                f"SK-A quality context missing for {artifact_type}"
            )

    aggregate_incremental = _sha256_json(incremental_final)
    aggregate_rebuild = _sha256_json(rebuild_final)
    if aggregate_incremental != aggregate_rebuild:
        raise RuntimeError("installed aggregate projection hashes diverged")
    family_hashes = {
        artifact_type: {
            "incremental_sha256": _sha256_json(
                incremental_final[artifact_type]
            ),
            "rebuild_sha256": _sha256_json(rebuild_final[artifact_type]),
            "match": (
                incremental_final[artifact_type]
                == rebuild_final[artifact_type]
            ),
        }
        for artifact_type in final_payloads
    }
    if not all(result["match"] for result in family_hashes.values()):
        raise RuntimeError("installed per-family projection hashes diverged")
    return {
        "coverage": "TS24-C11 covered",
        "fail_closed": True,
        "scope": "installed deterministic projection parity",
        "oracle": {
            "comparison": (
                "incremental churn final projection versus clean rebuild "
                "final projection"
            ),
            "productive_kuzu_materialized": False,
            "productive_kuzu_purged": False,
            "boundary": (
                "compara projecao deterministica; NAO materializa/purga "
                "Kuzu produtivo"
            ),
        },
        "real_kuzu_regression_refs": [
            {
                "task": "[TEST] TS21",
                "coverage": (
                    "real-Kuzu incremental churn and active-set reconciliation"
                ),
                "path": (
                    "tests/test_c8_projection_active_set_graph_transaction.py"
                ),
                "test": (
                    "test_real_kuzu_projection_active_set_is_exact_and_compensable"
                ),
            },
            {
                "task": "[TEST] TS24",
                "coverage": (
                    "clean deterministic rebuild and structural graph hash diff"
                ),
                "path": (
                    "../okto-pulse-core/tests/"
                    "test_kg_rebuild_deterministic.py"
                ),
                "test": (
                    "test_rebuilder_returns_deterministic_result_for_repeated_runs"
                ),
            },
        ],
        "fingerprint_parity_regression_refs": [
            {
                "path": (
                    "tests/test_sqlalchemy_research_decision_ledger.py"
                ),
                "test": (
                    "test_consolidation_projection_loads_current_rdl_heads_"
                    "with_two_queries"
                ),
                "coverage": (
                    "incremental current RDL fingerprint equals rebuild reader"
                ),
            },
            {
                "path": "tests/test_sqlalchemy_quality_assessment.py",
                "test": (
                    "test_consolidation_projection_loads_only_current_quality_"
                    "head_in_one_query"
                ),
                "coverage": (
                    "incremental current quality fingerprint equals rebuild "
                    "reader"
                ),
            },
            {
                "path": "tests/test_f05_domain_event_delivery_adapter.py",
                "test": "test_cognitive_facts_match_rebuild_source_hashes",
                "coverage": (
                    "incremental cognitive content hash equals rebuild source "
                    "rows"
                ),
            },
        ],
        "artifact_families": list(final_payloads),
        "artifact_family_count": len(final_payloads),
        "family_projection_hashes": family_hashes,
        "incremental_projection_sha256": aggregate_incremental,
        "rebuild_projection_sha256": aggregate_rebuild,
        "mismatches": mismatches,
        "ska": {
            "quality": "covered",
            "research_decision_ledger": "covered",
            "checklist": (
                "relational_governance_only_not_in_deterministic_kg_contract"
            ),
        },
    }


def run_matrix(work_dir: Path) -> dict[str, Any]:
    script = Path(__file__).resolve()
    work_dir.mkdir(parents=True, exist_ok=False)

    fresh_db = work_dir / "fresh.sqlite3"
    _run_worker(script, "--init-worker", fresh_db)
    fresh = _sqlite_snapshot(fresh_db)
    if fresh["foreign_key_errors"]:
        raise RuntimeError(
            f"fresh SQLite lifecycle has FK errors: {fresh['foreign_key_errors']}"
        )

    _run_worker(script, "--init-worker", fresh_db)
    rerun = _sqlite_snapshot(fresh_db)
    if rerun["logical_sha256"] != fresh["logical_sha256"]:
        raise RuntimeError(
            "SQLite lifecycle rerun drifted: "
            f"{fresh['logical_sha256']} != {rerun['logical_sha256']}"
        )

    upgrade_db = work_dir / "upgrade.sqlite3"
    _run_worker(script, "--init-worker", upgrade_db)
    fixture = _prepare_upgrade_fixture(upgrade_db)
    _run_worker(script, "--init-worker", upgrade_db)
    _assert_upgrade_sentinel(upgrade_db, fixture)
    upgrade = _sqlite_snapshot(upgrade_db)
    if upgrade["foreign_key_errors"]:
        raise RuntimeError(
            f"upgraded SQLite lifecycle has FK errors: "
            f"{upgrade['foreign_key_errors']}"
        )
    if upgrade["schema_sha256"] != fresh["schema_sha256"]:
        raise RuntimeError(
            "upgraded schema does not converge to fresh schema: "
            f"{upgrade['schema_sha256']} != {fresh['schema_sha256']}"
        )

    crash_db = work_dir / "crash-resume.sqlite3"
    crash_marker = work_dir / "crash.marker"
    crashed = _run_worker(
        script,
        "--crash-worker",
        crash_db,
        "--crash-marker",
        crash_marker,
        expected_codes=(CRASH_EXIT_CODE,),
    )
    if not crash_marker.is_file():
        raise RuntimeError("crash worker exited before reaching injected step")
    _run_worker(script, "--init-worker", crash_db)
    crash_resume = _sqlite_snapshot(crash_db)
    if crash_resume["logical_sha256"] != fresh["logical_sha256"]:
        raise RuntimeError(
            "crash-resume lifecycle did not converge to fresh state"
        )

    concurrent_db = work_dir / "concurrent.sqlite3"
    concurrent = _concurrent_fresh_start(script, concurrent_db)
    concurrent_snapshot = _sqlite_snapshot(concurrent_db)
    _run_worker(script, "--init-worker", concurrent_db)
    concurrent_rerun = _sqlite_snapshot(concurrent_db)
    for stage, snapshot in (
        ("two_workers", concurrent_snapshot),
        ("third_rerun", concurrent_rerun),
    ):
        if snapshot["logical_sha256"] != fresh["logical_sha256"]:
            raise RuntimeError(
                f"concurrent cold start {stage} logical state did not "
                "converge to the single-start baseline"
            )
        if snapshot["schema_sha256"] != fresh["schema_sha256"]:
            raise RuntimeError(
                f"concurrent cold start {stage} schema did not converge "
                "to the single-start baseline"
            )

    return {
        "status": "passed",
        "sqlite": {
            "fresh": fresh,
            "upgrade": {
                **upgrade,
                **fixture,
            },
            "rerun": rerun,
            "crash_resume": {
                **crash_resume,
                "injected_step": crash_marker.read_text(encoding="utf-8"),
                "crash_exit_code": crashed.returncode,
            },
            "concurrency": {
                **concurrent,
                "two_workers_snapshot": concurrent_snapshot,
                "third_rerun_snapshot": concurrent_rerun,
                "single_start_baseline": {
                    "logical_sha256": fresh["logical_sha256"],
                    "schema_sha256": fresh["schema_sha256"],
                },
                "both_workers_succeeded": (
                    concurrent["returncodes"] == [0, 0]
                ),
                "hash_identical_to_single": True,
            },
            "non_sqlite": _non_sqlite_rejection(),
        },
        "kg_parity": _kg_projection_parity(),
    }


def _module_origins() -> dict[str, str]:
    origins: dict[str, str] = {}
    for module_name in (
        "okto_pulse.core",
        "okto_pulse.community",
        "okto_pulse.community.adapters.sqlalchemy_database",
    ):
        spec = importlib.util.find_spec(module_name)
        if spec is None or spec.origin is None:
            raise RuntimeError(f"installed runtime module missing: {module_name}")
        origins[module_name] = str(Path(spec.origin).resolve())
    return origins


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--init-worker", type=Path)
    parser.add_argument("--crash-worker", type=Path)
    parser.add_argument("--crash-marker", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    worker_result = _worker_main(args)
    if worker_result >= 0:
        return worker_result
    if args.work_dir is None:
        raise ValueError("--work-dir is required in controller mode")
    started = time.monotonic()
    evidence = run_matrix(args.work_dir.resolve())
    evidence["module_origins"] = _module_origins()
    evidence["elapsed_seconds"] = round(time.monotonic() - started, 3)
    print(RESULT_PREFIX + json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
