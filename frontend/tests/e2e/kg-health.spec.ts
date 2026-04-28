/**
 * KGHealthView e2e — TS13 (axe sem violations critical/serious).
 * Spec d754d004 / IMPL-E.
 *
 * Mocka /api/v1/kg/health para isolar o teste de qualquer estado real
 * do backend. Aguarda o primeiro card renderizar e roda axe.
 */

import { expect, test } from '@playwright/test';
import { AxeBuilder } from '@axe-core/playwright';

const HEALTH_FIXTURE = {
  queue_depth: 3,
  oldest_pending_age_s: 12.4,
  dead_letter_count: 0,
  total_nodes: 1847,
  default_score_count: 39,
  default_score_ratio: 0.021,
  avg_relevance: 0.612,
  top_disconnected_nodes: [
    { id: 'entity_aaa', type: 'Entity', degree: 0 },
    { id: 'decision_bbb', type: 'Decision', degree: 1 },
  ],
  schema_version: '0.3.3',
  contradict_warn_count: 2,
  last_decay_tick_at: new Date(Date.now() - 6 * 60 * 60 * 1000).toISOString(),
  nodes_recomputed_in_last_tick: 142,
};

test('KGHealthView passa axe sem violations critical/serious (TS13)', async ({ page }) => {
  await page.route('**/api/v1/kg/health*', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(HEALTH_FIXTURE) }),
  );
  // Boards endpoint precisa retornar pelo menos 1 board para o currentBoard
  // ficar populado e o overlay sair do empty state.
  await page.route('**/api/v1/boards*', (route, request) => {
    if (request.method() !== 'GET') return route.continue();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([{ id: 'board-1', name: 'test-board', settings: {} }]),
    });
  });

  await page.goto('/kg-health');

  const heading = page.getByRole('heading', { name: /KG Health Dashboard/i });
  await expect(heading).toBeVisible({ timeout: 10_000 });

  const results = await new AxeBuilder({ page }).analyze();
  const blocking = results.violations.filter((v) =>
    ['critical', 'serious'].includes(v.impact ?? ''),
  );
  if (blocking.length > 0) {
    // Diagnóstico amigável quando o teste falha
    console.error('axe blocking violations:', JSON.stringify(blocking, null, 2));
  }
  expect(blocking).toHaveLength(0);
});
