# Refinement citations and SQLite contention — 2026-09-14

## 1. Citation-only updates: implemented and verified installed

Adding, replacing or removing closed `evidence:<id>` tokens, without changing
the remaining prose, no longer increments the Refinement version. Whitespace
around citations is normalized for this comparison. Prose/title/scope/decision
changes retain normal invalidation. Draft-only editing and exact receipt version,
source, expiry, revocation and materiality checks remain in force.

The smallest compatible change reuses the existing citation syntax instead of
introducing another entity or UI workflow. Agents should finish their narrative
first, obtain the version-bound receipt/evidence, then append citation tokens.
Receipt/source details are available through the referenced immutable evidence;
adding new explanatory prose afterward still changes the semantic version.

Both layers were corrected:

- Core `services/main.py`, `RefinementService.update_refinement`, uses the shared
  `is_evidence_citation_only_change` rule in `services/code_traceability_gate.py`.
  Citation edits remain in activity/history but do not emit a semantic-change event.
- Community `adapters/sqlalchemy_policy_subject_versioning.py`, `_before_flush`,
  applies the same rule to the actual ORM history. Unknown previous state is not
  exempted. Other changed fields and related-object invalidations are unaffected.

The first live verification caught the second layer: the response said version 3,
but the production session advanced it to 4 at commit. That intermediate result
was **not** accepted. A production-session regression was added before reinstalling.

Final live result on E2E Refinement `245b65f4-edb6-49ed-acd8-858e4608441c`:

- Before/after citation replacement: version **4 → 4**, including fresh readback.
- Current evidence: `code_evidence_4478e12c6b824730be2ba7348c265992`.
- Current receipt: `code_receipt_29dbbaa873fb43d7b1cf34ac3848fa92`.
- `refinement_evidence`: **passed=true**, blockers empty, receipt current.
- Earlier evidence was explicitly superseded after a fresh source observation;
  no stale receipt was grandfathered and no version was reset.

This closes the citation finding, not the entire remaining Spec/Task/TEST E2E.
Agent instructions were updated in Core's Code Traceability reference and
Refinement workflow. Python-user and uv-tool installations contain the correction.

## 2. SQLite: concrete contention mechanism reproduced

`CommunitySqlAlchemyConsolidationPersistence.queue_claim_is_current_and_unfenced`
executes a conditional no-op UPDATE of `consolidation_queue`. Its purpose is to
atomically validate claim ownership and the deletion tombstone. Its own contract
explicitly keeps SQLite's writer slot until worker ACK commit.

Core `_commit_consolidation_with_board_graph_lifecycle` acquires this fence before
`commit_consolidation` and the graph durability verification. The relational
transaction therefore spans potentially slow external graph work. In SQLite WAL,
there is one writer for the entire database file, not one per board.

Code Investigation admission concurrently executes:

```sql
UPDATE boards SET id = id WHERE id = :board_id
```

It waits behind the worker even for a **different board**. Community configures
`busy_timeout=30000`; if the writer remains held, the admission fails. The adapter
wraps the SQLAlchemy error as `code_investigation_admission_lock_failed`; the
current MCP projection reports a generic non-retryable persistence error.

### Reproduction and attribution limits

New test `test_worker_fence_blocks_investigation_on_an_unrelated_board` uses the
real Community worker fence with two sessions against a disposable WAL database:

1. Worker claims the writer slot for board A.
2. The identical admission UPDATE for board B fails with `SQLITE_BUSY` (code 5).
3. Worker rollback releases the slot; the same UPDATE succeeds.

The test uses a 50 ms timeout to reproduce the mechanism without waiting 30 s or
loading a real graph. All 28 worker-fencing tests pass. No production transaction
was artificially held open for this reproduction.

This proves a concrete application cause matching the observed timeout. The
original incident did not record the writer owner, so exclusive attribution of
that historical occurrence remains unproven. Following restart, the tested live
investigation/evidence requests succeeded; no new live lock failure was observed
during this verification window.

The long-lived Global Outbox checkout is **not sufficient evidence of a writer**:
its initial claim is a SELECT and events are detached records. It still incurs
slow graph work and shutdown drain timeouts, but those are distinct findings.

### Safe correction direction (not implemented in this diagnostic task)

Do not replace the worker's UPDATE with a SELECT or commit it early without a
replacement fence: that would reopen deletion/publication races. Increasing the
30 s timeout only masks the problem.

The durable fix needs a lifecycle/reservation contract spanning graph mutation
without retaining a SQLite write transaction: short claim transaction, independent
cross-process deletion/rebuild fencing during graph work, and short CAS ACK with
revalidation and crash-safe retry/compensation. Ordinary independent writers must
remain supported; this must not become a new board-wide serialization of every
write. All delete, cancellation, expired claim, recovery and ACK tests must remain.
SQLITE_BUSY should also have a bounded retryable transport classification, with
retry at a fresh whole-UoW boundary, not halfway through a failed transaction.

## Diagnostics

Community-only opt-in environment flag `OKTO_PULSE_SQLITE_WRITER_TRACE=1` installs
writer tracking. It records successful DML ownership, task name, duration until
commit/rollback request and tracked owners on SQLITE_BUSY/LOCKED. SQL text,
parameters and data values are not logged. Off by default. Commit events are
pre-commit callbacks, hence the label `writer_end_requested`, not a durability ACK.
Other processes/engines and writes hidden in CTEs are outside this observation.
Async/greenlet stacks may omit application call sites; blank sites are not proof
that no application writer exists.

Runtime logs used: `pulse-install-20260914/writer-diagnostic*.stderr.log` under the
workspace parent. The earlier `board-delete-final.stderr.log` remains historical.

## Verification

| Test group | Passed |
| --- | ---: |
| Core Refinement REST, evidence gates and contextual outcomes | 52 |
| Core human lifecycle and MCP Refinement UoW | 44 |
| Community production semantic session and writer diagnostics | 7 |
| Community governed worker fencing, including cross-board SQLite reproduction | 28 |

**131 tests passed**, plus installed MCP receipt → evidence → citation → fresh
gate readback. Community tests explicitly used the paired Core checkout
`okto-pulse-core-kg5-codex`; an initial invocation resolving the older sibling Core
failed collection and was corrected, not counted as a passing run.

No Grafx code, user board policy, historical version, or graph data was reset.
No commit/push was requested or performed for this task.

Final normal runtime: PID 28724, API/UI 8100 and MCP 8101 confirmed listening,
startup complete. Diagnostic flags disabled for that process. A final MCP read
after this restart still reports Refinement version 4 and the evidence gate passing.
