# M-PULSE-4 V5 vector evidence

Status: **PASS** on 2026-08-27. This evidence is limited to the frozen V5
recall gate, the representative Pulse eligibility checks, and the one-time
non-public-index measurement driver. It does not activate a provider or widen
the M-PULSE-4 acceptance matrix.

## Pinned source

- Grafx repository: `D:\Projetos\Techridy\okto-grafx-mpulse4-rebuild`
- Grafx commit: `caa1f869e9c6840f2f4cfc288c4e98eacfa0ba69`
- Grafx upstream at execution: `origin/milestone/mpulse4-vector-rebuild`
- Upstream commit at execution: `caa1f869e9c6840f2f4cfc288c4e98eacfa0ba69`
- Grafx worktree at execution: clean
- Pulse evidence commit before publication: `2c2ced79cfe3e8135808dd7cf21615b977f79439`
- Pulse origin: `https://github.com/OktoLabsAI/okto-pulse.git`
- Publication branch: `milestone/grafx-mpulse4-vector-recall`

The relevant pinned Grafx blobs were:

| File | Git blob |
|---|---|
| `src/okto_grafx/domain/vector/hnsw.py` | `6bc68bcfa81e6203a1fa2bc0f75af2d84f68cef9` |
| `src/okto_grafx/engine/vector_engine.py` | `cc49436d159ee2223c4879d860a1654913ebd88c` |
| `bench/harness/recall_worker.py` | `07c37ab27ff2fad7b6c0856262734f1e4b26ed88` |
| `bench/recall_corpus.py` | `8f27c09e8ce907061770dd645ce51ac501adf6a3` |

## Frozen calibrated recall gate

The existing Grafx oracle and harness were used without modification. From the
pinned Grafx root, PowerShell ran:

```powershell
$env:PYTHONPATH = 'D:\Projetos\Techridy\okto-grafx-mpulse4-rebuild\src'
$env:OPENBLAS_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
$env:NUMEXPR_NUM_THREADS = '1'
python -m bench.harness.recall_worker `
  --profile full `
  --gt auto `
  --out 'D:\Projetos\Techridy\okto-pulse-grafx-mpulse4-recall\docs\evidence\m4-grafx-full-recall.verdict.json'
```

Observed wall interval: `2026-08-27T20:32:40.6467428Z` through
`2026-08-27T21:19:13.7570366Z`, or `2793.110294` seconds. The worker exited
zero and emitted `m4-grafx-full-recall.verdict.json` (965 bytes).

| Frozen/observed field | Value |
|---|---:|
| Corpus | 8,192 x 384 |
| Queries | 256 |
| k | 10 |
| Corpus seed | 1337 |
| Query seed | 4242 |
| HNSW index seed | `0x0C701A11F0C0FFEE` |
| HNSW neighbours / efConstruction / efSearch | 16 / 200 / 320 |
| Oracle | NumPy brute force after 16 x 1,024 pure-fsum differential |
| NumPy | 2.5.1 |
| Mean recall@10 | **0.9546875** |
| Required mean recall@10 | **>= 0.90** |
| Minimum per-query recall@10 | 0.7 |
| Queries below perfect recall | 92 |
| Float32 dtype overlap, mean / minimum | 1.0 / 1.0 |
| Verdict | **PASS** (`ok=true`, empty failure) |

Frozen input digests:

| Payload | SHA-256 |
|---|---|
| Corpus float64 | `654adaa7b189618c7698da9108c9e69a378ff7256f114409622a48b43cdcc1ed` |
| Corpus float32 | `a17bd95c6bada8e5bdb8185d695d9391a79fb5c572e23aee50dba5146a7a1468` |
| Queries float64 | `5207fa9897934bc4400549476e9ece7334bdc84c9855ba845d3b7f04b278af87` |
| Queries float32 | `1995f66674d68f34062a495a6b92cd38c28f6b653ce7ea12898da544938521c5` |

The frozen verdict schema records recall and dtype stability, but does not
record `VectorSearchResult.regime`, `achieved_k`, exact-fallback counters, or an
underfill count. Consequently those counts are **not measured by this artifact**
and are not reconstructed from recall. Grafx's two-regime planner itself has no
approximate-to-exact fallback; the harness's 8,192 unfiltered rows exceed its
4,096 exact threshold and therefore select the approximate regime. This is a
source-contract statement, not a replacement for a missing observation.

## Pulse representative eligibility evidence

The two focused tests use the real Grafx backend with
`vector_exact_scan_threshold=0`, request two results, and exercise cold, warm,
and reopened searches:

- `tests/test_grafx_board_vector_search.py::test_real_grafx_ann_is_stable_cold_warm_and_after_reopen`
  covers `Decision`, canonical eligibility, working/superseded/tombstoned/null
  exclusions, stable order, and exactly two returned hits in all three states.
- `tests/test_grafx_global_discovery.py::test_global_ann_is_stable_cold_warm_after_reopen_and_never_leaks`
  covers `DecisionDigest`, board ACL, layer/revocation/null exclusions, stable
  order, and exactly two returned hits in all three states.

Across those six observable result pages, underfilled pages were 0 and
ineligible hits were 0. The adapter's internal exhaustive-fallback invocation
count is not instrumented by these tests, so no fallback count is claimed.

## Discarded infrastructure attempts

Two earlier executions produced no usable verdict and are not part of the PASS:

1. The run started at `2026-08-27T17:11:23.0877269Z` and completed its
   calculation in `3337.1217273` seconds, but JSON publication failed because
   the intended absolute output parent had not been created. This was an
   infrastructure/publication failure with no verdict, neither recall PASS nor
   recall FAIL.
2. The run started at `2026-08-27T18:07:29.440417Z` and was interrupted safely
   after its Grafx worktree was found to be under active edit. It emitted no
   verdict and has no evidentiary value.

The successful run began only after the absolute output parent was resolved and
verified and the Grafx path plus commit were explicitly pinned.

## Non-public index measurement

`scripts/measure_m4_non_public_vector_indexes.py` is the reproducible one-time
driver for `Alternative` and `Assumption`. It uses this same 8,192 x 384 corpus,
seed 1337, float32 quantization, query seed 4242, efSearch 320, and public Pulse
and Grafx APIs. It records durable ingest/build time, cold/reopen ANN time,
verification time, physical persisted-index bytes, and database-tree growth.
It deliberately defines no latency or size SLO. The full-size run was isolated
from the recall gate and used Grafx `origin/main` at
`caa1f869e9c6840f2f4cfc288c4e98eacfa0ba69`.

From the Pulse evidence worktree, PowerShell ran:

```powershell
python scripts\measure_m4_non_public_vector_indexes.py `
  --grafx-repo 'D:\Projetos\Techridy\okto-grafx-mpulse4-rebuild' `
  --core-repo 'D:\Projetos\Techridy\okto-pulse-core-grafx-contract' `
  --database 'D:\Projetos\Techridy\okto-pulse-grafx-mpulse4-recall\scripts\.tmp\m4-non-public-8192' `
  --out 'D:\Projetos\Techridy\okto-pulse-grafx-mpulse4-recall\docs\evidence\m4-non-public-vector-indexes.json' `
  --rows 8192 `
  --seed 1337 `
  --batch-size 64
```

The run started at `2026-08-27T21:35:52.3894431Z`, ended at
`2026-08-28T01:37:17.8884277Z`, exited zero, and took `14485.498985`
seconds. Each index received 8,192 rows through 128 durable commits.

| Index | Durable build/ingest | Empty baseline | Persisted size | Physical growth |
|---|---:|---:|---:|---:|
| `Alternative` | `1279.6239901 s` | 532,480 B | **794,624 B (776 KiB)** | 262,144 B (256 KiB) |
| `Assumption` | `1415.9925527 s` | 532,480 B | **802,816 B (784 KiB)** | 270,336 B (264 KiB) |

For both indexes, `verify("all")` returned no findings and the post-ingest and
reopened cold validations each returned ten unique hits. Their raw phase times
are retained in `m4-non-public-vector-indexes.json` for auditability, but are
not a latency benchmark, acceptance threshold, or SLO. The HNSW graph is
derived memory state and is excluded from the physical persisted-index bytes.

The raw artifact is 4,291 bytes with SHA-256
`e010ef6fb4a46b9e7a7bb770c9d4f6007b5b1af6415efb9952e3b7c369a0bede`.
The 156,167,513-byte temporary database was deleted after its two exact physical
index paths and sizes matched the JSON; it is not a release artifact.
