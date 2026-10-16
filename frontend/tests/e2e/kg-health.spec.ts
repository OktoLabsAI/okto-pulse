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
  health_schema_version: '1.1',
  materialization_state: 'materialized',
  materialization_generation: 'kg-gen-1',
  probe_reason_codes: {},
  overall_state: 'healthy',
  graph_state: 'healthy',
  discovery_state: 'healthy',
  metric_status: 'available',
  current_kg_generation_id: null,
  global_outbox_dead_letter_count: 0,
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
  decay_scheduler_diagnostics: {
    status: 'ok',
    severity: 'info',
    last_success_at: new Date(Date.now() - 6 * 60 * 60 * 1000).toISOString(),
    last_failure_at: null,
    last_error: null,
    next_scheduled_at: new Date(Date.now() + 2 * 60 * 60 * 1000).toISOString(),
    stale_tolerance_seconds: 24 * 60 * 60,
    recommended_action: 'none',
    operational_debt: false,
    graph_recovery_required: false,
    reason: 'latest_success_recent',
    source: 'kg_tick_runs',
  },
};

const BOARD_FIXTURE = {
  id: 'board-1',
  name: 'test-board',
  description: null,
  owner_id: 'user-1',
  settings: {},
  created_at: '2026-06-07T00:00:00Z',
  updated_at: '2026-06-07T00:00:00Z',
  cards: [],
  agents: [],
};

const BOARD_SUMMARY_FIXTURE = {
  id: BOARD_FIXTURE.id,
  name: BOARD_FIXTURE.name,
  description: BOARD_FIXTURE.description,
  owner_id: BOARD_FIXTURE.owner_id,
  settings: BOARD_FIXTURE.settings,
  created_at: BOARD_FIXTURE.created_at,
  updated_at: BOARD_FIXTURE.updated_at,
};

const EMPTY_COLUMNS_FIXTURE = {
  board_id: 'board-1',
  columns: {
    not_started: [],
    started: [],
    in_progress: [],
    validation: [],
    on_hold: [],
    done: [],
    cancelled: [],
  },
};

test('KGHealthView passes accessibility, responsive navigation, help and maintenance absence', async ({ page }) => {
  test.setTimeout(60_000);
  const pageErrors: string[] = [];
  const graphWrites: string[] = [];
  const maintenanceRequests: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  page.on('request', (request) => {
    if (/\/(rebuild|historical-consolidation|tick)(\/|\?|$)/.test(request.url()) || request.url().includes('/settings/runtime')) {
      maintenanceRequests.push(request.url());
    }
    if (request.url().includes('/api/v1/kg/') && request.method() !== 'GET') {
      graphWrites.push(request.url());
    }
  });
  // No test action is ever allowed to write to a running Pulse backend.
  await page.route('**/api/v1/**', (route, request) => {
    if (request.method() === 'GET') return route.fulfill({ status: 404, json: { detail: 'Unmocked reads blocked by the UI test' } });
    return route.fulfill({ status: 403, json: { detail: 'Unmocked writes blocked by the UI test' } });
  });
  await page.addInitScript(() => {
    localStorage.setItem('okto.onboarding.completed.v1', 'true');
    localStorage.setItem('okto-pulse:metrics-opt-in-prompt-dismissed:1.1.0', new Date().toISOString());
    localStorage.setItem('okto.guided-help.progress.v1', JSON.stringify({
      schemaVersion: 1,
      updatedAt: new Date().toISOString(),
      skippedAll: true,
      skippedAllAt: new Date().toISOString(),
      tours: {},
    }));
  });
  await page.route('**/api/v1/kg/health*', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(HEALTH_FIXTURE) }),
  );
  await page.route('**/api/v1/kg/cognitive-pending?**', (route) => route.fulfill({ json: {
    board_id: 'board-1', readonly: true, selected_kg_generation_id: null, legacy_mode: false,
    counts: { pending: 0, in_progress: 0, consolidated: 0, skipped: 0, failed: 0, total: 0 }, items: [],
  } }));
  await page.route('**/api/v1/kg/cognitive-pending/candidate-decisions**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        board_id: 'board-1',
        readonly: true,
        counts: {
          proposed: 0,
          promoted: 0,
          linked: 0,
          dismissed: 0,
          no_action_required: 0,
          total: 0,
        },
        items: [],
      }),
    }),
  );
  await page.route('**/api/v1/me/permissions**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ board_id: 'board-1', preset_name: 'test', owner_review_required: false, flags: {
        kg: { operations: { health: { read: true }, cognitive: { read: true }, tick: { run: true },
          rebuild: { preflight: true, confirm: true, run: true }, historical: { read: true, cancel: true } } },
        runtime: { settings: { read: true } },
      } }),
    }),
  );
  await page.route('**/api/v1/kg/boards/*/events', (route) =>
    route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' }),
  );
  // Boards endpoint precisa retornar pelo menos 1 board para o currentBoard
  // ficar populado e o overlay sair do empty state.
  await page.route('**/api/v1/boards**', (route, request) => {
    if (request.method() !== 'GET') return route.fallback();
    const url = new URL(request.url());
    const path = url.pathname.replace(/\/$/, '');
    if (path === '/api/v1/boards/board-1/columns') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(EMPTY_COLUMNS_FIXTURE),
      });
    }
    if (path === '/api/v1/boards/board-1') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(BOARD_FIXTURE),
      });
    }
    if (path === '/api/v1/boards/board-1/stories') {
      return route.fulfill({ json: { items: [], total_filtered: 0, total_overall: 0, offset: 0, limit: 25, has_more: false } });
    }
    if (path === '/api/v1/boards/board-1/topics') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([]),
      });
    }
    if (path !== '/api/v1/boards') {
      return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'not mocked' }) });
    }
    const view = url.searchParams.get('view');
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(view === 'shared' ? [] : [BOARD_SUMMARY_FIXTURE]),
    });
  });

  await page.goto('/?accept_terms=1');
  await expect(page.getByRole('button', { name: 'Stories' })).toBeVisible({ timeout: 10_000 });
  await page.evaluate(() => {
    window.history.pushState({}, '', '/kg-health');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });

  const heading = page.getByRole('heading', { name: /KG Health Dashboard/i });
  await expect(heading).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole('heading', { name: 'Operational' })).toBeVisible();
  await expect(page.getByTestId('kg-open-decay-settings')).toHaveCount(0);
  await expect(page.getByTestId('kg-tick-run-now')).toHaveCount(0);
  await expect(page.getByRole('button', { name: /rebuild|recovery/i })).toHaveCount(0);

  const navigation = page.getByRole('navigation', { name: 'KG Health sections' });
  const content = page.getByTestId('kg-health-scroll-content');
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const name of ['Processing & knowledge', 'Diagnostics', 'Overview']) {
      await navigation.getByRole('link', { name, exact: true }).click();
      expect(await content.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
    }
  }
  const dimensions = () => content.evaluate((el) => ({ width: el.scrollWidth, height: el.scrollHeight }));
  const initialDimensions = await dimensions();
  const help = page.getByRole('button', { name: 'Help: About indexed nodes' });
  await help.focus();
  await expect(page.getByRole('tooltip')).toBeVisible();
  await expect(page.getByRole('tooltip')).toHaveCSS('position', 'fixed');
  expect(await dimensions()).toEqual(initialDimensions);
  await help.press('Escape');
  await expect(page.getByRole('tooltip')).toHaveCount(0);

  for (const dark of [false, true]) {
    await page.evaluate((value) => document.documentElement.classList.toggle('dark', value), dark);
    const results = await new AxeBuilder({ page })
      .include('[data-testid="kg-health-view"]')
      .analyze();
    const blocking = results.violations.filter((v) => ['critical', 'serious'].includes(v.impact ?? ''));
    expect(blocking, JSON.stringify(blocking, null, 2)).toHaveLength(0);
  }

  await page.getByRole('button', { name: /refresh kg data now/i }).click();
  await expect(heading).toBeVisible();
  await expect(page.getByTestId('runtime-settings-panel')).toHaveCount(0);
  expect(maintenanceRequests).toEqual([]);
  expect(pageErrors).toEqual([]);
  expect(graphWrites).toEqual([]);
});
