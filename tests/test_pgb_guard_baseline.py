"""C14 guard provenance: prefer historical evidence at the measured scale."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_pgb():  # noqa: ANN202 - local script module
    path = Path(__file__).parents[1] / "scripts" / "pgb.py"
    spec = importlib.util.spec_from_file_location("pulse_pgb_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rest_guard_prefers_same_scale_raw_and_falls_back_to_run9(
    tmp_path: Path,
) -> None:
    run9 = tmp_path / "run9-1000"
    run10k = tmp_path / "run-10000"
    run9.mkdir()
    run10k.mkdir()
    (run9 / "rest-summary.json").write_text(
        json.dumps(
            [
                {
                    "scenario": "rare",
                    "scale": 1000,
                    "latency_ms": {"p95": 24.0},
                },
                {
                    "scenario": "run9_only",
                    "scale": 1000,
                    "latency_ms": {"p95": 10.0},
                },
            ]
        ),
        encoding="utf-8",
    )
    (run10k / "rest-raw.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "scenario": "rare",
                    "scale": 10000,
                    "ms": float(sample),
                }
            )
            for sample in range(1, 101)
        ),
        encoding="utf-8",
    )

    pgb = _load_pgb()
    same_scale, sources = pgb._rest_guard_baseline(run9, current_scale=10000)
    assert same_scale["rare"]["latency_ms"]["p95"] == 95.0
    assert same_scale["rare"]["scale"] == 10000
    assert Path(sources["rare"]).name == "rest-raw.jsonl"
    assert Path(sources["rare"]).parent.name == "run-10000"
    assert same_scale["run9_only"]["latency_ms"]["p95"] == 10.0

    fallback, _ = pgb._rest_guard_baseline(run9, current_scale=200)
    assert fallback["rare"]["latency_ms"]["p95"] == 24.0
    assert fallback["rare"]["scale"] == 1000
