# Disposable comparative measurements

`test_delivery_comparison.py` executes the same authenticated delivery segment
against either installed pair. It does not replace the integrated benchmark.
The selected pair must first pass byte-for-byte source/wheel/install verification.
Use a new output directory and temporary database directory for each run.

In PowerShell, set `PYTHONPATH` to the selected Core `src` followed by the selected
Community `src`, separated by `;`. Set `PULSE_BENCHMARK_TEST_HELPERS` to that
Community checkout's `tests` directory. Use that pair's qualified interpreter:

```text
rtk proxy <python> -X utf8 <community>/scripts/measure_mcp_fixture.py <new-output-dir> <community>/scripts/benchmarks/test_delivery_comparison.py --confcutdir=<community>/scripts/benchmarks -q -x --basetemp=<new-temp-dir> --junitxml=<new-report.xml>
```

The ledger/command fixture functions in `test_delivery_evidence_integration.py`
and the complete `test_code_traceability_persistence.py` and
`test_evidence_v2_adapter.py` helpers must match between the two compared
revisions. Their checked hashes are included in the committed comparison receipt.
The collector records actual imported package origins. No baseline checkout is
modified, no production lifespan is entered, and all writes target fixture SQLite.

Captures retain synthetic application payloads, including all discovered tool
pages and resources actually read. Do not use this collector with real credentials
or production data. SQL counts describe serial request windows; setup, external
implementation and authorship are excluded. Token counts use `cl100k_base` and
are reproducible estimates, not model billing. Do not infer full-flow savings or
statistical latency gains from a segment's single observation.

Current results and raw compressed captures live in the sibling Core repository's
`docs/pulse-simplification/benchmark-delivery-comparison.json` and its referenced
artifact. The implementation ledger records remaining original benchmark scope.
