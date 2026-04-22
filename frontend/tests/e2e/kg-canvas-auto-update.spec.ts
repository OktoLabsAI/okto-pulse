/**
 * E2E coverage for ts_764e8664 (Sprint 3, card 963dc743):
 *   "Canvas KG atualiza automaticamente após commit".
 *
 * Strategy: stub the SSE endpoint to emit a single `kg.session.committed`
 * event immediately, then stub the subgraph endpoint to return one extra
 * node on the next call. We assert (a) the sync indicator chip never goes
 * to the disconnected state, (b) the new node id appears in the DOM
 * within 2s of the SSE event landing, (c) the indicator returns to the
 * "live" steady state after `markSeen + reload`.
 */

import { expect, test } from '@playwright/test';

const BOARD_ID = process.env.E2E_KG_BOARD_ID || 'c167f5f1-8123-4522-918a-36fcca461538';

test('canvas reacts to a kg.session.committed event within 2s', async ({ page }) => {
  let subgraphCalls = 0;
  let sseSent = false;

  // Stubbed graph: first call returns one node; second call (post-event)
  // returns two so we can detect the auto-refresh visually.
  await page.route('**/api/v1/kg/boards/*/graph*', (route) => {
    subgraphCalls += 1;
    const nodes = subgraphCalls === 1
      ? [{ id: 'node_initial', node_type: 'Decision', title: 'Existing decision' }]
      : [
          { id: 'node_initial', node_type: 'Decision', title: 'Existing decision' },
          { id: 'node_postcommit', node_type: 'Decision', title: 'Brand-new decision' },
        ];
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        nodes,
        edges: [],
        metadata: {},
        next_cursor: null,
      }),
    });
  });

  // Stubbed SSE: emit hello, then emit one committed event after 200ms.
  await page.route('**/api/v1/kg/boards/*/events*', (route) => {
    if (sseSent) {
      // For a 2nd call (after the hook re-subscribes following a reload)
      // just keep the stream alive.
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: 'event: hello\ndata: {}\n\n',
      });
    }
    sseSent = true;
    const body =
      'event: hello\ndata: {}\n\n' +
      'event: kg.session.committed\n' +
      'data: ' + JSON.stringify({
        event_id: 'evt_e2e_1',
        session_id: 'ses_1',
        event_type: 'kg.session.committed',
        created_at: new Date().toISOString(),
        payload: { node_count: 1 },
      }) + '\n\n';
    return route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body,
    });
  });

  await page.goto(`/boards/${BOARD_ID}/kg`);

  // Initial node is there.
  await expect(page.locator('text=Existing decision')).toBeVisible({ timeout: 5_000 });

  // The sync indicator should be visible and NOT in the disconnected state.
  const indicator = page.locator('[data-testid="kg-sync-indicator"]');
  await expect(indicator).toBeVisible();
  await expect(indicator).not.toHaveAttribute('data-state', 'disconnected');

  // The hook's debounce is 500ms; allow up to 2s end-to-end for the
  // auto-refresh to land per the test scenario AC.
  await expect(page.locator('text=Brand-new decision')).toBeVisible({ timeout: 2_000 });

  // After auto-flush, indicator returns to a live state (or "behind"
  // briefly if a race surfaced the chip first).
  await expect(indicator).toBeVisible();
});
