# Decision verification — v0.4.0 acceptance

Implemented against the sibling Core plan
`docs/pulse-simplification/DECISION_VERIFICATION_PLAN.md` (DV1–DV4).
Milestones: Community `502dd394`, `bc0117dd`, `5cf74fd8`; corresponding Core
`2d0ce031`, `437ea9d0`, `92bb4551`. Final qualification: 2026-10-10.

## Native contract

Decisions select exact active obligations and/or an observable direct inspection.
Both selected paths must pass. The Spec ledger stores authenticated observations
with native versioned sources, actor identity, CAS, replay and explicit conflict
reconciliation. Task links remain contextual; inspections add no implementation
credit. The edition adapter consumes the public `reviewer_policy` port.

The new `decision_review` constraint is part of the single 0.4.0 storage format.
A database lacking it is refused unchanged. There is no migration, compatibility
reader, converter, automatic deletion or fabricated Test Card.

## Qualification evidence

- Frontend: 50 tests passed across editor, inspection/history/permissions/retry,
  Decisions, Coverage and Implementation. TypeScript/Vite build and embedded
  distribution synchronization passed.
- Native REST/MCP inspections and coverage: 17 tests passed. Storage/CLI refusal
  plus those inspection tests: 65 passed (overlapping campaign, not 82 distinct).
  Additional exact old-constraint refusal and narrow-scope currentness: 2 passed.
- Core: 164 analytics/coverage tests; 173 domain, progress, context, KG and catalog
  tests; 20 quality/catalog regressions. Earlier DV1/DV2 campaigns are recorded in
  the Core implementation ledger. These are focused campaigns, not a claim of a
  full repository test run.
- Executable closure with both wheels: 8,825 ownership rows, no findings,
  all eight architectural budgets zero. README projections use the official renderer.
- Installed byte parity: Core 850 `.py`, Community 320 `.py`, 58 MCP resource files,
  79 frontend files. Frontend tree:
  `796c6308a12cffd898de590d947edf9c7aa2824ea0652d56b78c14fb15520047`.

Real browser acceptance used a new, isolated native home and ports 8120/8121:
authored the verification condition in Decisions, recorded an actual inspection
of the displayed Spec description, read its history, checked Not applicable
implementation and navigation from Coverage back to Decisions. The initial visual
check found a green summary bar despite incomplete evidence; fixed and covered by
a regression. Missing population now has no percentage/progress bar.

The final installed process was started after installation, without source
PYTHONPATH, and the browser loaded `index-BY8Ouj02.js`. Local artifacts are retained
in `PULSE_REFACTOR/.validation-v040/dv-*` and `output/playwright/dv-*`; they are not
package fixtures. The default home, SIM-05 and its outstanding human T33 observation
remain untouched. This acceptance does not claim that the previous mock Spec is Done.
