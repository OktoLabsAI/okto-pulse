# KG Health: observe, diagnose and recover

KG Health (`/kg-health`) is the board-scoped operations dashboard. It answers
three different questions: is the graph healthy, what knowledge work is pending,
and does an operator need to take a maintenance or recovery action?

## Reading the page

| Section | Purpose | What it does not mean |
| --- | --- | --- |
| Overview | Reported overall state, measured node count, processing queue and independent Global Discovery health; suggested next step | A healthy database does not imply all cognitive work is finished |
| Processing & knowledge | Consolidation/global outbox queues, canonical debt, cognitive progress and candidate decisions | These populations can overlap; adding their counts does not give unique pending artifacts |
| Diagnostics | Graph metrics, integrity signals, storage footprint and relevance scheduler | Missing metrics are not zero; disk footprint is not process RAM |
| Recovery | Storage/generation identity, historical recovery controls, preflight and audited rebuild preparation | An absent rebuild-linked generation is not by itself a failure or a reason to rebuild |

The sticky section navigation moves within the same mounted dashboard. It does
not restart polling, discard the entered audit reason or initiate work. The
**What can I do here?** guide explains the workflow. Contextual help opens in a
fixed layer outside the panel, by hover or keyboard focus; Escape dismisses it
without resizing the panel. Layouts support narrow screens and both themes.

## Actions and impact

- **Refresh** reloads observations, not jobs. Automatic health polling retains its
  configured interval (30 seconds by default), pauses when the browser tab is
  hidden and avoids overlapping health requests. If refresh fails, the previous
  snapshot remains visible with an error and a stale-snapshot explanation.
- **Cognitive Action Center** opens the existing board-scoped review workflow.
  See [the center's guide](COGNITIVE_ACTION_CENTER.md) for waiver, review and
  failed-processing actions. The dashboard does not add an automatic consolidation
  or dead-letter retry.
- **Run tick now** recalculates relevance. It writes scores; it is not a rebuild.
  **Open Decay Tick settings** opens the existing settings tab instead of adding
  another configuration editor here.
- **Stop recovery** retains the existing confirmation and fencing contract for
  historical recovery. Already committed graph data and cognitive audit debt are
  not deleted by cancellation. The stopped control does not become another
  “start rebuild” button.
- **Preflight** checks source eligibility. On opening the page the existing
  preflight endpoint is queried, subject to permission. It is not a rebuild; the
  offline executor produces its authoritative manifest.
- **Prepare offline rebuild** requires the existing audit reason and permissions.
  For the offline execution contract, preparation explains that Pulse must be
  stopped before promotion; it does not call online confirm/run. A real rebuild
  promotes a new graph generation and may leave cognitive work to complete.

## Availability and permissions

The overview uses the backend health state rather than inferring integrity from
node counts. Only `metric_status=available` displays a measured node count,
including a legitimate zero. Unrecognized health states remain unknown; a failed
refresh does not produce a fresh health verdict from the previous response.
Global Discovery state is reported independently. Grafx attribution is displayed
when the board's returned storage route identifies Grafx.

Existing `kg.operations.*` and `runtime.settings.read` permissions remain in force
for reading health/cognitive data, running ticks, inspecting preflight, confirming
or running rebuild, reading/cancelling historical recovery and opening settings.
Backend authorization remains authoritative. This layout revision introduces no
Core dependency, API endpoint, configuration key or storage behavior change.

## Validation

- Presentation tests cover healthy, at-risk, unknown, recovery-required and stale
  snapshots; unavailable versus measured-zero counts; navigation order, binding
  attribution and portal help.
- Existing feature tests retain cancellation confirmation, offline rebuild
  refusal of online execution, permission gating, generation feedback, polling,
  cognitive pagination and candidate decision handling.
- Chromium E2E covers 360/768/1440 px navigation without horizontal overflow,
  fixed help without scroll-area resizing, accessibility in light/dark themes
  (no serious/critical axe violations) and the Decay Tick settings handoff.
  Graph action requests are monitored and unmocked writes are blocked.
- The page was also inspected through a separate frontend preview against the
  running E2E board, without starting recovery, consolidation or maintenance.

Run from `frontend/`:

```powershell
npm test -- src/components/knowledge/__tests__ src/components/analytics
npm run build
npm run verify:frontend-dist
# With a frontend dev server running and a local Pulse API available:
$env:E2E_BASE_URL = 'http://127.0.0.1:5175'
npx playwright test tests/e2e/kg-health.spec.ts --project=chromium --workers=1
```

The E2E uses simulated graph/board responses; live visual inspection is separate
evidence and does not substitute for the action-contract tests.
