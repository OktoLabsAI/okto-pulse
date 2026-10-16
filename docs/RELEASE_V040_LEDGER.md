# Pulse 0.4.0 — release promotion ledger

## 2026-10-16 — promotion in progress

Authorized scope: feature/v0.4.0 → develop → main through PRs, then tag both
repositories v0.4.0. No branch-protection bypass or failing-gate exemption.

- Core feature/develop PRs: #107, #109, #110 merged. Main PR #108 merged after
  required checks passed; main commit `63bd85f25d8ad1f0cfc1bef5703ce495d4266d58`.
- Community feature/develop PRs: #116, #118, #119, #120, #121, #122, #123, #124 merged. Main PR #117
  remains open, waiting for the required `docker build verify` check.
- No v0.4.0 tag created yet. Tag Core first after the paired qualification;
  tagging Community triggers its gated Docker/GHCR Release workflow.
- Changelogs dated 2026-10-16 retain the explicit fresh-storage breaking notice.

## Qualification corrections

- Core permission manifest omitted `get_spec_validation_cycle`; registered its
  existing Board/Spec/component visibility without changing runtime authority.
  Updated exact counts to 285 tools / 282 policies / 3 exemptions.
- Community dev extra lacked tiktoken used by measurement tests. Declared it
  and updated the lock. Focused permissions/measurement checks: 53 passed.
- Clean Core wheel exposed an incomplete SaaS fake UoW. Added write intent and
  fail-closed planning/receipt capabilities. Fixed corrupt UTF-8 preset text.
- Docker probes used retired `/api/v1/kg/settings`; both now use `/health`.
  Compose prod now pins 0.4.0; build.sh selects wheels from matching project
  versions and prints exact artifact paths. Bash syntax check passed.
- Contributor setup now documents the paired uv checkout names separately from
  Docker/pip layout; both `uv lock --check` commands passed.
- Focused fake, registry and health checks: 18 passed with `--noconftest`.
  Earlier combined invocation with Core conftest failed during unrelated global
  DB setup; it is not counted as passing qualification.
- Full Community suite first failed on stale architecture documentation (1114
  imports vs current 1154, all public). Updated the factual counts, keeping the
  private-reach-in budget zero.
- Seven architecture-transition tests used a Decision with no verification plan
  and evaluations without IDs. Fixture now declares the existing FR obligation
  and identified opinions; two gate-response expectations now use native HTTP409.
  All seven transition tests pass; production gate behavior is unchanged.
- CI now prints verbose test names and stops on the first failure for diagnosis.
  The selected full suite and markers are unchanged; a green run must execute
  all selected tests. A separate local tail subset is diagnostic only.

## Resume

Review follow-up: removed the obsolete Sprint-based `scripts/pgb.py` profiler
and its one historical-baseline-only test, plus `smoke_embedding.py`, which
always called the retired unauthenticated KG settings route. Removed the README
instruction for that dead command. No product behavior or current feature test
was removed. Preservation evidence now defaults to loaded Core/Community code
versions rather than 0.3.3. All four preservation/evidence tests pass, including
the new default-version regression; no retired version/route/Sprint model
references remain in active scripts. The running local diagnostic was collected
before the profiler retirement and may still attempt its deleted test; the full
remote suite collects the final checkout afresh.

Run 38852126320 failed after 1431 passed / 4 skipped on authenticated delivery:
the adopted fixture replaced a valid source-bound attestation with an unbound
one. Preserve its execution basis and use the actual ASGI observation through
the native signed-manifest path; no production admission predicate changed.

The diagnostic tail had ten failures (2416 passed before its ten-failure stop).
An earlier progress-tail-only status missed failures already written in the log;
subsequent monitoring scans all FAILED/ERROR lines. All ten were reproduced:
three delivery cases above; old table count/fingerprint; DDL text checked in the
former file instead of the native manifest; KG workflow checked only Community
notes instead of the composed frozen resource; four vector fixtures missing the
current `source_status` column. Fixtures now use the native contracts.

Reviewed ORM delta since the previous pinned fingerprint: exactly the validation
source revision and Spec conclusion receipt tables, plus `decision_review` in
the existing delivery-kind constraint. Pinned 153 tables and hash
`6e8e6c32a7165dfd2fb7c06dd5c23613ff9e2dc5208ae80a7ddf4dc860fea35e`;
runtime schema/migration behavior unchanged. First focused run: 61 passed, one
stale fingerprint failure. After updating that audited pin: all 13 schema/vector
matrix tests pass. Remaining diagnostic tail resumes after the vector matrix;
the complete remote suite must still pass before merge.

CI time budget increased from 75 to 180 minutes: run 38848655415 reached 19%
of the full selection after about 20 minutes before a reviewed fix superseded
it. Allow time for native storage/concurrency cases and subsequent artifact
builds; no tests, assertions, required checks or failure conditions were removed.

Pre-tag inspection found the Release workflow replayed an April legacy trace
with 250 previously successful calls to 26 removed tools, including Sprints.
Replaced that incompatible fixture with a native HTTP smoke against the freshly
initialized tagged container: version/inventory/resources, authenticated
Ideation write/read persistence, native Q&A, and rejected retired target without
mutation. Existing image scan, health, crash/handle checks and publish gates
remain required; this smoke does not replace the complete CI suite.

Validation: six verifier tests pass (JSON/SSE success and HTTP/domain/correlation/
negative-test failures). Real isolated installed-pair smoke passes: 61 MCP calls,
285 tools, 54 resources, 50 persistence round trips, native Q&A and retired-target
refusal. Byte equality proved 867 Core and 326 Community Python files. The probe
used a newly created validation home and terminated its own process; the default
Pulse home/runtime was untouched. Evidence is under
`PULSE_REFACTOR/.validation-v040/release-promotion-20261016/`.

Further review found source-mode evidence could sign stale distribution metadata
or a hardcoded 0.3.3 fallback as producer version. Evidence now identifies the
loaded Community code version. All 34 evidence-adapter tests pass, including
signed receipts with missing or stale installation metadata. README now states
0.4.0 as current and describes the fresh-storage breaking change.

Review of PR #117 found that CardModal included an unreadable linked Spec in
its revision batch, causing a 403 to close an otherwise readable Card. The
watch now includes the Spec only with `spec.entity.read`; the backend remains
unchanged. Regression covers absent, granted and revoked Spec access while
preserving Card access. All 93 CardModal tests pass; TypeScript and production
frontend build pass, and the packaged SPA was regenerated.

Core main CI run 38846216617 failed only because paired Community main still
has version 0.3.3. Rerun it after Community main is promoted; Core PR checks
passed against paired 0.4.0 develop. Community full run 38848655415 remains
in progress at this entry. No tags have been created.

Community run 38846767938: 1375 passed, 4 skipped, then a stale cognitive-closeout
test passed the removed `graph_state` argument. Updated the test to the native
item record and source-observation contract, with an explicit signature guard
against graph-health admission. Seven cases pass (empty/pending/corrupt source,
provider/missing-generation/missing-capability failures and the signature guard).
The four graph-health parameter values were duplicate tests of a removed input;
they are replaced by the guard, without changing production semantics.

Check latest PR #117 head/checks and unresolved review threads. Continue only
with concrete failures; do not relax gates. After merge, fetch and verify both
main versions and trees, create/push annotated Core tag before Community tag,
then verify remote tag targets and Release status. No default home or runtime
was altered during promotion.
