# CLI issue closeout — September 13, 2026

Scope: Community issues [#78](https://github.com/OktoLabsAI/okto-pulse/issues/78),
[#79](https://github.com/OktoLabsAI/okto-pulse/issues/79),
[#80](https://github.com/OktoLabsAI/okto-pulse/issues/80),
[#81](https://github.com/OktoLabsAI/okto-pulse/issues/81) and
[#82](https://github.com/OktoLabsAI/okto-pulse/issues/82).
These corrections do not change Grafx's multi-reader/writer or durability protocol.

## Status: human and machine-readable observations

`okto-pulse status` retains its human-readable fields and banner.
`okto-pulse status --json` emits exactly one JSON object without the banner.
Both use the same observation. Fields:

| Field | Contract |
| --- | --- |
| `data_dir`, `source`, `db_path` | Explicit local runtime location and origin. |
| `db_size_kb` | File size divided by 1024, or null when unavailable. |
| `boards`, `cards`, `specs`, `agents` | Counts from one read-only SQLite snapshot; null if no complete observation is available. |
| `database_status` | `ready`, `missing`, `uninitialized`, or `error`. |
| `error` | null, or an object with exception `type` and `message`. |
| `runtime_identity` | Existing serve-lock identity inspection result. |
| `api_up`, `mcp_up` | Port reachability; not proof that the listener owns this data home. |

SQLite is opened with `mode=ro`; status never creates a database. Only an actual
`no such table` OperationalError yields the existing initialization hint. A locked,
corrupt or unreadable database yields exit **1**, with its real error on stderr
in text mode or in the JSON `error` field. Missing/uninitialized databases retain
exit 0 and their explicit status; consumers needing readiness must check that field.
The SQLite busy timeout is one second. No graph open, migration or repair occurs.

## Metrics windows

`okto-pulse metrics status --window-days N` accepts integers **1 through 400**,
default **30**. Invalid values fail before telemetry composition/dispatch with
exit **2**. Direct `cmd_metrics` callers receive the same validation. CLI and REST
share constants from `community.metrics_limits`; the API retains its 422 validation
response. Existing settings and retained metrics are unchanged.

## Reset ownership and path safety

`okto-pulse reset [-y]` resolves graph ownership from the SQLite `boards.id` catalog
**before deleting it**. Under the existing exclusive serve lock it preflights and
removes only those boards' directories beneath the configured `kg_base_dir/boards`,
then performs the existing SQLite/uploads removal and reseed. A custom `kg_base_dir`
is supported. The whole boards root is never recursively deleted.

Unsafe IDs, symlink/reparse/junction paths, unreadable trees, changed directory
identities, or an unreadable ownership catalog cause refusal. The complete target
set is checked before the first deletion and rechecked before applying the plan.
Custom relational database locations are refused rather than treating a different
SQLite file as ownership authority. Unknown/pre-existing orphan directories,
other installations' board directories, Global Discovery and quarantine/backups
are preserved; this is not a general-purpose orphan garbage collector.

This remains an explicitly destructive offline command, not a cross-filesystem
atomic transaction. An I/O failure during deletion can leave a partial reset;
the error propagates and reseeding does not report success. Back up first. The
serve lock coordinates cooperating Pulse processes, not hostile filesystem mutation.

## Schema migration admission

`okto-pulse kg migrate-schema` already checked for a live server in the command
module. That refusal now uses the standard `ERROR [serve-lock]` message and exit
**2**, before registry composition, database initialization or migration. Both
single-board and all-board modes retain their existing no-server behavior.

## Qualification

New isolated checks are in `tests/test_cli_issue_closeout.py`; existing CLI,
serve-lock, metrics and Code Traceability consumer regression is grouped at this
checkpoint. All reset tests use disposable fixtures, never production data.
Final results and commit references are recorded with the GitHub closeout comments.

Source checkpoint: **169 Community tests passed**, zero failures/errors/skips,
114.31 seconds. Selection: new issue tests, CLI reset/status/init/serve,
schema-migration command, metrics CLI/store/port, serve-lock, Code Traceability
persistence/REST/transport parity. The 402 existing Python SQLite datetime-adapter
deprecation warnings do not represent failed assertions. Paired Core qualification
passed **103 tests**, including V2 blocking transitions to validation/done.

Receipts under the Grafx checkout's `.grafx-tmp/` are
`pulse-issues-community-regression.xml` and `pulse-issues-core-qualified.xml`.
The initial failed observations are retained: one invocation selected the wrong
sibling Community worktree; corrected tests now select both repositories explicitly.
Other corrections fixed a test fixture's unclosed SQLite connections on Windows,
the exact legacy text spacing, and receipt-bound observation timestamps in Core
test inputs. No assertion was skipped to obtain the passing checkpoint.

No live Pulse reset, migration, database mutation, installation or restart was
performed. The correction is a source-branch delivery, not a main/PyPI release.

The exact staged-index exports were then tested independently of the worktrees'
other uncommitted Grafx-adoption changes: **46 CLI/metrics checks passed** and
**103 paired Core checks passed** (overlapping the source selections above).
The actual staged CLI process also emitted a single valid JSON object with exit
0 against a disposable missing database, with no banner or database creation.
Staged receipts: `pulse-issues-community-staged.xml`, `pulse-issues-core-staged.xml`.
The commits intentionally exclude unrelated pending adoption work.
