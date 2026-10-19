# SQLite transaction lock implementation ledger

Date: 2026-10-19. Plan: [SQLITE_TRANSACT_LOCK_PLAN.md](SQLITE_TRANSACT_LOCK_PLAN.md). Branch: `feature/v0.4.1` in Core and Community.

| Step | Status | Evidence / result |
| --- | --- | --- |
| Record plan before code changes | Done | Plan saved in workspace root. |
| Create 0.4.1 milestone branches and bump versions | Done | Both branches created and pushed. Core version milestone `99565824`; Community version milestone `2d419483`. Frontend rebuilt, packaged distribution verified (79 files), Core and Community 0.4.1 wheels built. |
| Audit code paths and recovery contracts | Done | Identified the long queue fence, early graph-only removal commit, stale-claim recovery, Spec/Card and board deletion, graph guard TTL, and post-commit audit staging. |
| Make accepted SQLite commits durable across OS/power failure | Done | Community connection PRAGMA changed from WAL/NORMAL to WAL/FULL. Updated parity tests; `test_r01b_engine_session_parity.py` passed (6 tests). |
| Add persistent operation protocol | Implemented; integration validation ongoing | A short SQLite write stores a prepared operation and recovery output in internal `domain_events`. Native Grafx commit metadata carries the operation ID; native history resolves the receipt after reopen. Finalized and absent markers are durable and idempotent. No relational schema fingerprint change. |
| Refactor graph commit and SQLite ACK | Implemented; integration validation ongoing | Normal Spec/Card worker uses a read-only claim preflight, prepares the journal immediately before native graph commit, releases the SQLite writer, and stages cognitive source plus audit only after graph close/reopen durability. An exact claim ACK and finalization marker share that short transaction. The removal-only commit path is journaled as progress. Newer pending revisions survive older ACK. |
| Coordinate delete/rebuild and stale owners | Implemented; integration validation ongoing | Governed artifact/board deletion refuses an unsettled board operation. Production Community graph guards retain a kernel file lock for the entire writer lifetime; a paused live process cannot be superseded by TTL alone. Recovery runs at worker startup and periodically. Boards with unsettled operations are excluded from new claims. |
| Run concurrency and crash tests | In progress | Real SQLite writer-release while the simulated graph is paused, claim-replacement, journaled worker ACK, absence/presence/removal-progress/newer-revision recovery, and native receipt after graph reopen passed. Focused Community worker/Grafx/lock suite: 101 passed; the added newer-revision cases passed (4). Focused Core suite: 40 passed before the final staging-order adjustment; its directly affected tests passed again (27). |

## Notes

- `okto-pulse-core` was clean on `main` at start. `okto-pulse` had a pre-existing untracked `docs/pulse-simplification/IMPLEMENTATION_LEDGER.md`; leave it intact.
- The legacy unrelated-board fencing test remains valid for the old adapter default; Community runtime enables the durable protocol. A full runtime lock-duration test is still required.
- Native Grafx receipt test passed after closing and reopening the graph. Provenance refusal test passed when commit history is unavailable. Core and Community changed interfaces compile.
- SQLite writer diagnostics now start timing at explicit `BEGIN IMMEDIATE`, before the request's first DML. The event reports commit/rollback requested; it is a lower bound, not a proof of durable completion.
- An exact current SQLite schema fingerprint rejects ad-hoc table additions; any new table requires a governed schema conversion or a journal outside the source database. The existing queue is already source-transactional.
- Validation so far: Community `test_governed_queue_worker_fencing.py` (35 plus added recovery and stalled-graph cases), `test_coordination_single_writer_renewal.py` (25), `test_af29_s2_write_lock_quarantine_adapters.py` (12), targeted Grafx receipt tests (3), SQLite PRAGMA parity (7), worker registry/runner tests (22); Core removal and transaction-recovery tests (14), worker transaction/resilience tests (13), ports tests (13). Community core-import boundary test passes after declaring the new public exception.
- An unrelated pre-existing Core test expects an exact rebuild source set without `sprint`; another uses a plain `object()` as a DB for a source path requiring `get()`. These fail in the selected Core suite and are not caused by this protocol.
- Community release-gate tests: 6 passed, 1 external gate could not run because neither `OKTO_E2E_GRAFX_WHEEL` nor `OKTO_E2E_GRAFX_REPO` was configured. Individual Core/Community 0.4.1 wheels built successfully.
- The persistent kernel rendezvous sits outside the board directory so Windows board erasure can remove the graph directory while the guard is held; direct test passed.
- Remaining: execute a full real Spec projection with stalled native graph commit and measure unrelated SQLite write latency; verify crash after native commit before ACK with the actual audit adapter; inspect other source-invalidation and cleanup paths; review mixed-version deployment behavior. Until these pass, completion is not established.
