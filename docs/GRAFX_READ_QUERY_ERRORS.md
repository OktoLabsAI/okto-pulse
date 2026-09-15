# Read-only graph query failures

`POST /api/v1/kg/boards/{board_id}/cypher` returns RFC 7807
`application/problem+json` for typed graph-provider failures. Authorization and
Code Traceability access checks run before provider execution, including on an
invalid query. A refusal is never converted to an empty successful result and
does not schedule recovery, rebuild, redrive or cognitive consolidation.

| Failure | HTTP status | Public code |
|---|---:|---|
| Query parsing/planning refused | 400 | `graph_invalid_query` |
| Writer/reader contention | 503 | `graph_lock_contention` |
| Graph unavailable or corrupt | 503 | Original neutral graph code |
| Capability/index unavailable | 503 | Original neutral graph code |
| Typed memory pressure | 503 | `graph_memory_pressure`, bounded `Retry-After` |
| Other graph provider failure | 500 | Original neutral graph code |

Core defines only the backend-neutral `GraphInvalidQuery` type. Community's
`grafx_error_mapping.py` translates native `GrafxParseError`/`GrafxPlanError` into
it without copying native messages or query parameters into the public response.
The REST adapter catches the neutral taxonomy. A typed client query error is not
evidence of corrupt storage, and unsupported operations are not silently retried.
Existing Tier Power validation responses retain their prior codes/statuses.

The September 7 live probe used a function outside the active dialect (`type`)
and exposed an uncaught `GraphError`/plain HTTP 500. The same exact query now gets
HTTP 400 with `/errors/graph_invalid_query`; a valid typed incoming relationship
query returns HTTP 200 and 20 rows. The focused authorization/REST/transaction
slice passed 97 tests in 34.38 seconds. The error matrix also checks access
denial before execution, message confidentiality, memory retry headers and
preservation of a genuine unknown failure as structured HTTP 500.

The running source-based Pulse 0.3.3 process was restarted with Grafx commit
`1b53f57` and these Community/Core changes. This does not claim an updated public
wheel or a new PyPI release. The cognitive ledger remains byte-identical, with
21 pending items reserved for future benchmarks.

Integration dependency: Core commit `6ea25e9` provides `GraphInvalidQuery`; deploy
the Community mapping together with that Core revision (or a descendant).
The focused authorization/error file was rerun before the isolated milestone:
13 tests passed in 14.77 seconds. Only the error mapping, REST catch/import and
error-matrix test hunks enter this milestone; unrelated event-loop/UI/recovery
worktree changes remain outside it.
