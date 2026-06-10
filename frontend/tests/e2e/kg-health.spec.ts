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

const RUNTIME_SETTINGS_FIXTURE = {
  kg_kuzu_buffer_pool_mb: 512,
  kg_kuzu_max_db_size_gb: 32,
  kg_connection_pool_size: 4,
  kg_queue_max_concurrent_workers: 4,
  kg_queue_min_interval_ms: 100,
  kg_queue_claim_timeout_s: 300,
  kg_queue_max_attempts: 5,
  kg_queue_alert_threshold: 5000,
  kg_decay_tick_interval_minutes: 1440,
  kg_decay_tick_staleness_days: 7,
  kg_decay_tick_max_age_days: 0,
  restart_required: false,
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

test('KGHealthView passa axe e abre Runtime Settings Decay Tick sem duplicar editor (TS13/KG-HS.4)', async ({ page }) => {
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
  await page.route('**/api/v1/kg/rebuild/preflight**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        board_id: 'board-1',
        outcome: 'ready',
        action_required: 'none',
        reason: null,
        base_state: 'healthy',
        metric_status: 'available',
        current_kg_generation_id: 'kg-gen-1',
        eligible_source_count: 0,
        skipped_cancelled_count: 0,
        has_non_deterministic_inputs: false,
        preflight_hash: 'pf-hash-kg-hs4',
        generated_at: new Date().toISOString(),
        manifest_ref: 'manifest://kg-hs4',
        source_set_hash: 'source-set-hash-kg-hs4',
      }),
    }),
  );
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
  await page.route('**/api/v1/settings/runtime', (route, request) => {
    if (request.method() !== 'GET') return route.continue();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(RUNTIME_SETTINGS_FIXTURE),
    });
  });
  await page.route('**/api/v1/me/permissions**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ board_id: 'board-1', preset_name: 'test', flags: {} }),
    }),
  );
  await page.route('**/api/v1/kg/boards/*/events', (route) =>
    route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' }),
  );
  // Boards endpoint precisa retornar pelo menos 1 board para o currentBoard
  // ficar populado e o overlay sair do empty state.
  await page.route('**/api/v1/boards**', (route, request) => {
    if (request.method() !== 'GET') return route.continue();
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
    if (path === '/api/v1/boards/board-1/topics' || path === '/api/v1/boards/board-1/stories') {
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
  await expect(page.getByTestId('kg-open-decay-settings')).toBeVisible({ timeout: 10_000 });

  const results = await new AxeBuilder({ page })
    .include('[data-testid="kg-health-view"]')
    .analyze();
  const blocking = results.violations.filter((v) =>
    ['critical', 'serious'].includes(v.impact ?? ''),
  );
  if (blocking.length > 0) {
    // Diagnóstico amigável quando o teste falha
    console.error('axe blocking violations:', JSON.stringify(blocking, null, 2));
  }
  expect(blocking).toHaveLength(0);

  await page.getByTestId('kg-open-decay-settings').click();

  await expect(page.getByTestId('runtime-settings-panel')).toBeVisible();
  await expect(page.getByTestId('tab-decaytick')).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByTestId('input-tick-interval-minutes')).toBeVisible();
  await expect(heading).not.toBeVisible();
});
