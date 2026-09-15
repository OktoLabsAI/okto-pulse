# Cognitive Action Center: review knowledge gaps

The center is a board-scoped review workspace, not a consolidation executor.
It explains which source records still need knowledge processing, which failed,
and which were explicitly waived. Opening, filtering and refreshing are read-only.

## User workflow

1. Start with **Needs attention**. Open the named source to inspect its decisions,
   evidence and reusable learning. For consolidation, follow the Pulse workflow
   with an agent; no synthetic "Consolidate" button bypasses that process.
2. For **Processing failed**, open the existing failed-processing inspector.
   Review its actual error before using its separately authorized redrive controls.
   For **Graph update still pending**, open KG Health. The center does not repair,
   retry, cancel or rebuild the graph automatically.
3. **Waive or schedule review** is an explicit human decision. Choose a reason
   and enter a justification; there is no preselected reason. Reasons requiring
   more evidence/context require a future local date, sent to the API as UTC ISO.
4. **Reconsider waiver** shows a confirmation first. It removes the current
   waiver metadata and returns the cognitive item to pending. It does not reopen
   the Spec/task, erase the graph, or synchronously execute consolidation.

| Section | Contents |
| --- | --- |
| Needs attention | Cognitive pending/failed/in-progress, technical failures, open graph update debt, overdue or invalid/missing-date review waivers |
| Waived or scheduled | Non-time-limited waivers and future review waivers |
| History | Completed/terminal records; no waiver buttons |
| All records | The full source-record projection |

Sections group records, not unique artifacts or backend readiness verdicts. A
historical record can coexist with a failure for the same artifact. Completion
impact always uses the backend's `would_block_done`; advisory mode is not proof
that a technical failure was fixed. In-progress work cannot be waived from this UI.
Pending records whose artifact has a technical blocker also cannot be waived.
Reconsidering an existing waiver remains allowed under its own permission, as it
cannot hide technical debt.

## Navigation, help and state

- Titles are enriched only for the visible page through existing authorized
  source GETs, deduplicated and limited to four concurrent calls (25 records/page).
  Wrong-board responses are discarded. Missing/deleted/forbidden sources retain
  an explicit reference fallback; there is no cross-board unrestricted lookup.
- Source buttons use the existing modal stack. DLQ controls reuse the existing
  inspector, including its authorization and confirmation rules.
- Search currently matches references, artifact IDs and reason codes, **not
  titles**. The search help makes this limitation explicit. Section/search changes
  reset pagination. The UI exposes the filtered total and Previous/Next controls.
- Board counters remain board-wide. Duplicate processing attempts count as
  separate records. Metrics failure does not hide an otherwise successful list;
  unavailable counters show a dash, not a false zero.
- Refresh is explicit with a last-loaded timestamp. Requests are aborted on
  filter/board changes; stale results cannot overwrite newer results. Changing
  board remounts the view and discards forms. Mutations report their actual effect
  and refresh the list; an emptied last page returns to the first page.
- Help opens with hover, focus or tap, closes on Escape/blur/scroll, and is rendered
  in a fixed portal above the screen. It cannot resize or displace the modal.
- Raw codes, aliases, backend precedence and technical metrics remain available
  in collapsed diagnostics, not as the primary workflow vocabulary.

## API changes and installation

The existing `/api/v1/kg/{board_id}/cognitive-readiness/items` query accepts two
additional `signal` filters: `attention` and `deferred`. Grouping runs in the
provider-neutral Core read model **before pagination**, using the same service
clock as readiness. Existing filters retain their meanings; verdicts and write
policies are unchanged. Optional `justification` and `actor` are projected from
the existing cognitive ledger as row details, never metric labels.

No new Settings options, store, migration, engine dependency or write endpoint.
Deploy the paired Core and Community changes together; older Core versions do not
recognize the new section filters. The running production instance was not
reinstalled or restarted during implementation/qualification.

## Validation

September 13, 2026: 26 Core/REST tests, 33 frontend tests (including the reused
failed-processing inspector) and 2 isolated Chromium tests passed. These are
targeted affected-area checks, not a claim of full-repository regression.

Targeted coverage includes partitioning and expiry boundaries, invalid review
dates, pagination beyond 200 records, unchanged backend verdicts, audit fields,
REST authorization and errors; frontend permissions, history/running eligibility,
explicit confirmations, missing/future dates, UTC conversion, source navigation,
stale requests and board isolation, partial metrics failure and portal help.

Browser tests run against the actual component on a frontend-only fixture server
at port 5189, with intercepted synthetic APIs and no production proxy:

```sh
npm test -- src/components/knowledge/CognitiveActionCenterView.test.tsx src/components/knowledge/DeadLetterInspectorModal.test.tsx
npx playwright test --config playwright.architecture.config.ts cognitive-center.spec.ts
npm run build
npm run verify:frontend-dist
```
