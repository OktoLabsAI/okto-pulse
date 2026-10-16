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
