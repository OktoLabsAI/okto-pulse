# Knowledge-Graph Pipeline Health

Okto Pulse's Kanban-KG pipeline has **five stages** that each write state to a
different store. A single broken link between stages leaves the explorer
empty, decisions unsearchable, or the consolidation queue stuck. This
document describes the healthy state of each stage, how to inspect it, and
how to recover from the three failure modes we see most often.

## Quick diagnosis

```bash
okto-pulse verify-pipeline <board_id>
# add --json for machine-readable output
```

Exit code `0` means all five layers are healthy. Any non-zero exit prints a
per-layer table — match the failing layer against the sections below.

---

## The five layers

```
  (Spec/Card edit)
         │
         ▼
 ┌───────────────────┐     ┌───────────────────┐
 │ 1. Consolidation  │     │ 2. Kùzu per-board │
 │    queue (SQLite) │ ──▶ │    graph          │
 └───────────────────┘     └───────────────────┘
                                    │
                                    ▼
                           ┌───────────────────┐
                           │ 3. KuzuNodeRef    │
                           │    mirror (SQLite)│
                           └───────────────────┘
                                    │
                                    ▼
                           ┌───────────────────┐
                           │ 4. GlobalUpdate   │
                           │    outbox (SQLite)│
                           └───────────────────┘
                                    │
                                    ▼
                           ┌───────────────────┐
                           │ 5. Global         │
                           │    discovery Kùzu │
                           └───────────────────┘
```

The five `check_*` functions that back `verify-pipeline` and the live
`/api/v1/health/kg` endpoint all live in
[`core/kg/health.py`][health]. They are pure reads — no writes, no side
effects — so they are safe to call from a request handler or a read replica.

[health]: ../../okto_labs_pulse_core/src/okto_pulse/core/kg/health.py

### 1. Consolidation queue

**Store.** Table `consolidation_queue` in the primary SQLite DB.

**Healthy.** `pending = 0 AND claimed = 0 AND failed = 0`. Rows are allowed
to stack up briefly when an edit bursts in, but the worker empties them
within seconds.

**Inspect.**

```sql
SELECT status, COUNT(*)
FROM consolidation_queue
WHERE board_id = :board_id
GROUP BY status;
```

Or via the CLI:

```bash
okto-pulse verify-pipeline <board_id>
#   [OK ] queue           12 done, 0 pending
```

**Common failures.**

- *Backlog of `pending` rows that never drains.* The consolidation worker
  is not running — most often because an earlier start crashed before
  releasing a Kùzu handle. Restart the server and re-check; if the backlog
  persists after restart, see "Kùzu file lock" below.
- *`failed > 0`.* The worker caught an exception while consolidating that
  row. Read `consolidation_queue.error_text` for the first-line clue and
  the server log for the full traceback.

### 2. Kùzu per-board graph

**Store.** `~/.okto-pulse/boards/<board_id>/graph.kuzu` (or
`$KG_BASE_DIR/boards/<board_id>/graph.kuzu` if overridden).

**Healthy.** At least one node committed across the 11 node types. A
brand-new board that has not yet had a spec consolidated reports
`healthy=False` with `details="no nodes committed yet"` — that is the
normal pre-seed state, not a bug.

**Inspect (Cypher).**

```
MATCH (n) RETURN labels(n)[0] AS type, count(n) AS c;
```

The CLI output summarises the same count per-type:

```
[OK ] kuzu            17 nodes (Decision=4, Entity=6, Criterion=5, Constraint=2)
```

**Common failures.**

- *`graph not bootstrapped at …`.* The `.kuzu` directory does not exist.
  Run `okto-pulse init` once to create the schema, or trigger any spec
  consolidation — `BoardConnection.__init__` bootstraps on-demand.
- *`failed to open graph: IO exception: Could not set lock on file`.* Two
  Kùzu `Database` instances are trying to hold the same directory. See the
  "Kùzu file lock" troubleshooting entry.

### 3. KuzuNodeRef SQLite mirror

**Store.** Table `kuzu_node_refs` — written in the **same SQLite
transaction** as `consolidation_audit` at commit time.

**Healthy.** `(add - supersede) == per-board Kùzu node count`. Any drift
is a direct sign that a commit half-landed — Kùzu accepted the writes but
the SQLite side rolled back, or vice versa.

**Inspect.**

```sql
SELECT operation, COUNT(*)
FROM kuzu_node_refs
WHERE board_id = :board_id
GROUP BY operation;
```

**Common failures.**

- *`MISMATCH expected_live=X kuzu_live=Y`.* A `commit_consolidation` was
  not fully atomic. Look for `ConsolidationAudit` rows with matching
  `session_id` but missing `KuzuNodeRef` rows — these are the half-committed
  sessions. The fix is to replay the consolidation (edit + save the spec);
  the deterministic pipeline is re-entrant.

### 4. Global update outbox

**Store.** Table `global_update_outbox`. Same SQLite TX as the audit row,
drained by the `OutboxWorker` polling loop (5 s).

**Healthy.** `pending = 0 AND dead_letter = 0`. A `pending` count > 0
immediately after a commit is normal — the worker has not ticked yet.

**Inspect.**

```sql
-- Pending in the worker's retry window
SELECT COUNT(*) FROM global_update_outbox
WHERE board_id = :board_id
  AND processed_at IS NULL
  AND retry_count >= 0 AND retry_count < 5;

-- Dead-lettered (gave up after 5 retries)
SELECT event_id, retry_count, last_error
FROM global_update_outbox
WHERE board_id = :board_id
  AND processed_at IS NULL
  AND (retry_count >= 5 OR retry_count = -1);
```

**Common failures.**

- *`pending` never drops to zero.* The background worker is not running —
  `okto-pulse serve` starts it automatically, but a crash during startup
  can leave it dead. Restarting the server re-spawns the task. If it drops
  again, check the server log for `outbox_worker.error`.
- *Events keep dead-lettering.* Inspect `last_error`. The most common
  cause is the global Kùzu rejecting a `SET` on an indexed column — see
  "Outbox backlog" below.

### 5. Global discovery meta-graph

**Store.** `~/.okto-pulse/global/discovery.kuzu` — a second Kùzu DB holding
`DecisionDigest` / `Board` / `Topic` / `Entity` nodes for cross-board
semantic search.

**Healthy.** At least one `DecisionDigest` exists for the board.

**Inspect (Cypher).**

```
MATCH (d:DecisionDigest {board_id: $bid}) RETURN count(d);
```

**Common failures.**

- *`no DecisionDigest synced for this board yet`.* Fine if `check_outbox`
  still shows `pending > 0` — wait for the worker to tick. Otherwise the
  outbox drained but the worker hit a silent `_apply_event` error — read
  the `last_error` column of any row with `processed_at IS NULL`.
- *`failed to query global graph: IO exception: Could not set lock…`.*
  Another process holds the global Kùzu lock. See "Kùzu file lock" below.

---

## Troubleshooting

### Stub embedding provider in production

**Symptom.** Semantic search returns nothing or always returns the same
top result regardless of the query; `GET /api/v1/kg/settings` shows
`"is_stub": true`.

**Why.** The KG fell back to the deterministic hash-based stub because the
sentence-transformers model failed to load. The community image ships the
model in its HF cache so this only happens when:

1. You're running outside the community docker image and the model is not
   downloaded.
2. You set `KG_EMBEDDING_MODE=stub` explicitly (or
   `OKTO_PULSE_SKIP_DEMO_SEED=1` is tricking you — no, that only affects
   seeding).
3. The network fetch from huggingface.co failed during first model load.

**Fix.**

```bash
# Force a model download
python -c "from sentence_transformers import SentenceTransformer; \
           SentenceTransformer('all-MiniLM-L6-v2')"

# Unset the override if you set it
unset KG_EMBEDDING_MODE

# Re-check
okto-pulse verify-pipeline <board_id>
curl -s http://localhost:8100/api/v1/kg/settings | jq .embedding_provider_name
```

See **Spec 3** for the fallback rules and
[`scripts/smoke_embedding.py`](../scripts/smoke_embedding.py) for an
automated check.

### Kùzu file lock on Windows

**Symptom.** `IO exception: Could not set lock on file : …\graph.kuzu` or
`…\discovery.kuzu` surfaces from any layer that opens Kùzu.

**Why.** Kùzu acquires an OS-level lock on its DB directory for the
lifetime of the `Database` C++ object. On Windows, Python's `del db`
does not guarantee the destructor has run by the time the next
`Database()` is called — two instances then race the same lock. The issue
is specific to short-lived test or seed flows where the same process
re-opens the same path in quick succession.

**Fix.**

1. **Production.** Only one `Database` instance per `.kuzu` path should
   exist at a time. The server singleton (`open_board_connection`) handles
   this; ad-hoc scripts that imported `kuzu.Database` directly do not.
   Close any REPL/script holding a `Database` handle before running
   `okto-pulse serve`.
2. **Tests.** Call `okto_pulse.core.kg.schema.close_all_connections()` in
   teardown, then `gc.collect()`. See `tests/test_kg_pipeline_e2e.py` for
   the reference pattern.
3. **After a crash.** A hard kill can leave a stale lock file. Delete
   `<path>/graph.kuzu/.lock` (or the equivalent under `global/`) while
   nothing is running, then restart.

Spec 1 tracks the persistent fix for the in-process race — see
`core/kg/schema.py::close_all_connections` for the current workaround.

### Outbox backlog won't drain

**Symptom.** `verify-pipeline` shows `outbox: pending > 0` and the number
does not drop after a minute, or shows `dead_letter > 0` with a non-empty
`last_error`.

**Why.** The worker ticks every 5 s and retries each row up to 5 times.
Dead-letter means all retries failed — the worker gave up. The most
common root causes:

- *`Cannot set property vec in table embeddings because it is used in one
  or more indexes.`* A `MERGE` followed by `SET` on an HNSW-indexed column
  (e.g. `DecisionDigest.embedding`). Fixed in the current
  `outbox_worker.py`; if you see this error on your branch, rebase onto
  `main`.
- *`IO exception: Could not set lock on file …\discovery.kuzu`.* Another
  process holds the global Kùzu — see "Kùzu file lock" above.
- *SQLite `database is locked`.* A long-running transaction is blocking
  the worker's commit. Check the server logs for slow queries.

**Fix.**

```bash
# See what's sitting in the queue
sqlite3 ~/.okto-pulse/data/pulse.db \
  "SELECT event_id, retry_count, last_error
   FROM global_update_outbox
   WHERE processed_at IS NULL LIMIT 20;"

# Re-queue dead-lettered rows for another round of retries once the root
# cause is fixed (rebuild indexes, kill the lock holder, etc.):
sqlite3 ~/.okto-pulse/data/pulse.db \
  "UPDATE global_update_outbox
   SET retry_count = 0, last_error = NULL
   WHERE retry_count = -1;"

# Force a tick from a Python REPL if you don't want to wait 5 s
python - <<'EOF'
import asyncio
from okto_pulse.core.kg.global_discovery.outbox_worker import get_outbox_worker
asyncio.run(get_outbox_worker().process_once())
EOF
```

---

## Reference

- [`core/kg/health.py`][health] — the pure check functions.
- `core/kg/global_discovery/outbox_worker.py` — `OutboxWorker.process_once`
  + `_apply_event`.
- `tests/test_kg_pipeline_e2e.py` — end-to-end contract test that exercises
  all five layers.
- **Spec 1** (`okto-pulse-core` kanban) — connection lifecycle.
- **Spec 3** (`okto-pulse-core` kanban) — embedding provider fallback.
- **Spec 4** (`okto-pulse-core` kanban) — this pipeline integrity initiative.
