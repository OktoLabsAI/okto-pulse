# KG read phases — 2026-09-07

## Scope and implementation

The cold Knowledge Graph load remains unresolved. This checkpoint makes its
cost observable without changing Grafx transaction semantics or consuming the
21 reserved pending specs. Work proceeds without Claude/Nexus.

Community `api/kg_routes.py` dispatches subgraph and census synchronous graph
work through the existing backend-agnostic `run_blocking_graph_io` Core port.
The port propagates context and drains native work before returning cancellation.
Authorization remains before dispatch. Completed phases emit `kg.read.phase`
with board, operation, phase and elapsed monotonic milliseconds; they do not
log queries, parameters, rows, credentials or exception content. Enable INFO
and a handler for `okto_pulse.api.kg_routes` to collect these events. They are
not enabled by configuring the Uvicorn access logger alone.

Phases: authority, dispatch delay, nodes, edges for subgraph; authority,
dispatch delay, schema, nodes, node counts and edge counts for stats. The final
stats phase includes calculating averages over the selected node page.
No completed-phase event is a claim that the entire request succeeded.
Only the existing paged-node ValueError boundary produces invalid-cursor 410;
center, edge and authority failures are not relabeled as cursor failures.

## Live evidence, not a controlled performance comparison

Pulse 0.3.3 PID 22748 used source Community/Core/Grafx, default data home,
descriptor revalidation `generation`, 64 MiB Grafx buffer and no extra options.
One cold browser navigation measured:

| Phase | Subgraph (ms) | Stats (ms) |
| --- | ---: | ---: |
| Authority | 1.0 | 0.6 |
| Dispatch | 0.8 | 2.7 |
| Schema | — | 11995.8 |
| Nodes | 19849.5 | 7340.9 |
| Node counts | — | 2446.8 |
| Edges / edge counts | 7757.1 | 12479.8 |
| Browser request | 27751 | 34351 |

Exactly one census request was observed. Authorization and executor dispatch
do not explain the cold latency in this observation. This does not prove that
they can never contend under another workload.

Separate read-only diagnostics against the same live physical generation:

- Native first 500 nodes: 1.368 s after pool admission 3.926 s; warm 0.733 s.
  Ordered access inspected 549 candidates across 11 tables, not all 2961 nodes.
- Two fresh read-only pools opened and queried concurrently in 2.886 s total;
  lane opens 1.933/2.563 s and queries 0.409/0.306 s, 500 rows each.
- Actual routed Community composition in a separate process: schema 2.278 s;
  node query 2.719 s, including admission of another read lane. Its connector
  refused any non-read-only opening, and all diagnostic pools were closed.

These processes did not run the full Pulse app or its background workers.
They are neither HTTP-equivalent benchmarks nor a causal speedup measurement.
Import time (~17 s in standalone diagnostics) is startup work, not request time.

## Short stack sample

A 15-second, 50 Hz nonblocking py-spy sample during an explicit UI Refresh
collected 261 samples with 48 sampling errors. Four samples had empty stacks;
classifying the 257 nonempty stacks by request ancestry yielded 44 subgraph,
82 stats, 43 Health, 88 other. These counts are sampled thread stacks, **not
CPU percentages**, and the interval does not cover every request's completion.

Stacks show ordered node reads, tuple decoding, count scans, path/descriptor
validation and Health source/audit work overlapping. They do not establish a
single root cause or justify weakening path validation, sharing stale authority,
disabling Health, or changing reader/writer isolation. The raw local sample is
`.grafx-tmp/pulse-kg-warm.speedscope.json` in the Grafx checkout; it was not
uploaded to a profiler service or added to version control.

The remaining bounded performance target is still cold read admission and
census/layout work under the real Pulse workload. This checkpoint adds no new
performance acceptance threshold and makes no native optimization claim.

## Quality and data preservation

51 focused tests passed in 15.46 s across phase timing, relationship layout,
Code Traceability REST authorization and KG power REST authorization. New tests
exercise real off-loop dispatch, visibility propagation, elapsed timing,
result preservation and cursor/error boundaries. Ruff and diff checks passed.
The initial centered-error fixture omitted its independent power authorization
stub; that fixture was corrected, and the authorization suite was retained.

The runtime already has phase instrumentation loaded. The final narrow cursor
error classification is source-tested and requires a later controlled restart
to load; this checkpoint does not claim that the active process hot-reloads it.
No new consolidation, backfill, redrive, rebuild, graph reset, main merge or
PyPI publication was initiated. The cognitive ledger remained at its recorded
SHA256 `4AFF1AB6EE6C6E621C6598148154A04298500B8DA92EBF0F5E90AD081B2217F4`.

See also [census request isolation](KG_CENSUS_REQUEST_ISOLATION.md). Other dirty
integration changes in the checkout are not part of this selected checkpoint.
