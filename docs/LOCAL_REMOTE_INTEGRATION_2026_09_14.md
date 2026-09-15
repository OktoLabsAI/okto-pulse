# Local / remote integration — Community 0.3.3

## Scope and preservation

The standard checkout `okto-pulse`, previously at `0401e41`, was advanced by
fast-forward to `origin/feature/v0.3.3` (`a2cc1f9`). All twelve dirty files were
first preserved in the local Git stash
`4a2c418d09a6ff3d75f79d691ad8b4627809f3c7`
(`safety-local-before-feature-v033-integration-20260914`). The stash is retained;
it is a recovery copy, not a patch to blindly reapply to the integrated tree.

No production database, installed package or running Pulse process was changed.
Core remains storage-neutral and unchanged by this integration.

## Resolution decisions

| Local change | Integrated disposition |
| --- | --- |
| Writer heartbeat coordination | Serialize each port's short per-board manifest protocol before taking the cross-process mutex; preserve non-reentrancy, token and expiry checks. Other boards retain separate gates. |
| Transient manifest reads | At most three observations with 50/100 ms backoffs while the file exists; missing or persistently unreadable authority is still refused. |
| Exact rebuild ACK | Count both added and superseding nodes in audit/reference proofs. Atomically restore an adopted live queue intent instead of deleting it with the rebuild membership. |
| Cognitive restore | Record an integrity failure when durable replay reports an error or failed records. Replaying the effect returns the same persisted failure receipt. |
| Recovery reservation | Validate one complete identity/expiry snapshot instead of racing a second inspection against heartbeat replacement. |
| Empty exact-drain observations | Allow a bounded transient idle window (at most 30 seconds, within the overall deadline), continuously retaining the existing fence and source checks. |
| Quarantine serializer versions | Explicitly accept 0.3.2 or 0.3.3, require string values, and retain the remote rule that paired original/restore manifests have the same version. Reject future, malformed and mismatched versions. |
| Offline installation fingerprint | Align the expected Grafx release with the declared and locked `okto-grafx[accel]==0.0.6`. |
| Deleted Kuzu store/transaction and native tests | Keep deleted. Their temporal binding and atomic payload-replacement purposes are already implemented in the current Grafx adapters. Validate the Grafx equivalents, including incident-edge preservation and rollback after uncertain mutations. Originals remain recoverable in the stash. |

Historical queue-only tests construct archival binding evidence below the runtime
admission boundary. They do not open Ladybug or change the executable's refusal
of that backend. Recovery tests now use the composed Grafx provider where native
storage is needed. The Spec tab-order regression includes Delivery evidence.

## Validation

Tests use the paired Core checkout and disposable storage, not the active user's
data home. This is a combined integration regression, not a claim that every
Python test in both repositories was executed.

| Validation group | Result |
| --- | --- |
| Writer renewal, exact relational ACK, offline recovery CLI, Grafx store/transaction and uncertain payload replacement | 362 passed; installed-launcher case rerun separately below |
| Rebuild effects, coordination, queue reconciliation, consolidation fencing, Grafx-only boundary, Delivery Evidence, issues 84–88, import boundary, MCP host and exploration REST | 210 passed |
| Historical recovery executor, malformed evidence and archival queue-only reconciliation | 111 passed |
| Core delivery contracts/domain/lifecycle, discovery writer leases and public manifest | 66 passed |
| Native installed launcher / process-oracle follow-up | 2 oracle tests passed; 1 installed test explicitly skipped at the downstream ancestry phase because live Pulse listeners correctly prevent offline execution |
| Complete frontend unit regression (228 files / 1,871 tests) | 1,870 initially passed; the sole outdated tab-order assertion was corrected, then all 16 tests in its file passed. No frontend implementation was changed to accommodate the assertion. |
| Frontend production build and packaged-tree verification | Passed; 78 files, SHA-256 `a0ecae10bcc25ee7cba773574300054b00cb3fbea68fa7de11b80e4add64cbb1` |
| Scoped Ruff, formatting, Git whitespace and offline dependency-lock check | Passed |

For the native installation test, Grafx is built from the isolated **v0.0.6 tag**,
not the user's active 0.0.7 development checkout. The test successfully builds and
installs the three wheels in a disposable environment, authenticates their
fingerprint, and observes a no-side-effect refusal while ports 8100/8101 are live.
It does **not** prove the real second-launcher branch on this occupied workstation;
that branch remains executable when run offline. No safety guard is bypassed.

The non-overlapping Python groups above total **749 passing tests**; the two
process-oracle follow-up cases are reruns, not additional distinct cases. The
frontend result combines the full run with the corrected-file rerun rather than
claiming a second full run. Existing global frontend-lint baseline warnings are
not represented as a clean global lint run by this report.

Reproduction: set `OKTO_PULSE_COMMUNITY_REPO` and `OKTO_PULSE_CORE_REPO` to the
paired source checkouts and include their `src` directories in `PYTHONPATH`.
For the installed-wheel test, set `OKTO_PULSE_GRAFX_REPO` to a checkout of tag
`v0.0.6`. Run `npm ci`, `npm test -- --maxWorkers=2`, `npm run build` and
`npm run verify:frontend-dist` in `frontend`. Validate the lock with
`uv lock --check --offline --find-links ../okto_grafx/.grafx-tmp/language-wheel`.
