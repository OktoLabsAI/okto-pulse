/**
 * KGHealthView unit tests — covers TS1..TS12 + TS14 (spec d754d004).
 *
 * TS13 (axe) lives as a Playwright e2e in tests/e2e/kg-health.spec.ts.
 *
 * Mocks getKGHealth at the module boundary so we control fetch latency
 * and outcomes without hitting the network. Mocks useDashboardStore to
 * inject currentBoard scenarios (valid id, null).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

import { KGHealthView } from '../KGHealthView';
import * as kgHealthApi from '@/services/kg-health-api';
import * as dashboardStore from '@/store/dashboard';
import type { KGHealth, KGCognitivePendingCounts } from '@/services/kg-health-api';

vi.mock('@/services/kg-health-api');
vi.mock('@/store/dashboard');

const baseHealth: KGHealth = {
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
  schema_version: '1.0',
  contradict_warn_count: 2,
  last_decay_tick_at: new Date(Date.now() - 6 * 60 * 60 * 1000).toISOString(),
  nodes_recomputed_in_last_tick: 142,
  metric_status: 'available',
  health_issues: [],
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
  storage_footprint_proxy: {
    source: 'file_size_proxy',
    status: 'available',
    percentage: 12.5,
    high_water_mark_pct: 12.5,
    graph_lbug_bytes: 1024,
    sidecar_bytes: 1024,
    total_bytes: 2048,
    configured_max_db_size_bytes: 4096,
    configured_max_db_size_gb: 1,
    is_direct_memory_telemetry: false,
    description: 'On-disk storage footprint proxy derived from graph.lbug file sizes.',
    tooltip: 'This is not live Ladybug memory telemetry. It is a file-size proxy used as an early warning signal.',
    unavailable_reason: null,
  },
  kg_layer_counts: {
    status: 'ok',
    by_layer: { canonical: 1840, working: 7 },
    by_maturity_status: { canonical_eligible: 1840, working_immature: 7 },
  },
  canonical_debt: {
    open_count: 0,
    retryable_count: 0,
    blocked_count: 0,
    retry_scheduled_count: 0,
    terminal_count: 0,
    by_state: {},
  },
  rebuild_diagnostics: {
    last_outcome: 'rebuild_complete',
    canonical_open_debt_count: 0,
    layer_counts_status: 'ok',
    operator_action: 'none',
  },
};

function mockBoard(id: string | null) {
  vi.mocked(dashboardStore.useDashboardStore).mockImplementation((selector: any) =>
    selector({
      currentBoard: id ? { id, name: 'test-board' } : null,
    } as any),
  );
}

function mockApi(impl: (boardId: string, signal?: AbortSignal) => Promise<KGHealth>) {
  vi.mocked(kgHealthApi.getKGHealth).mockImplementation(impl);
}

function cognitiveCounts(
  overrides: Partial<KGCognitivePendingCounts> = {},
): KGCognitivePendingCounts {
  return {
    pending: 0,
    in_progress: 0,
    consolidated: 0,
    skipped: 0,
    failed: 0,
    total: 0,
    ...overrides,
  };
}

function mockCognitivePending(counts: KGCognitivePendingCounts) {
  vi.mocked(kgHealthApi.getKGCognitivePendingItems).mockResolvedValue({
    board_id: 'b1',
    selected_kg_generation_id: 'gen1',
    readonly: true,
    legacy_mode: false,
    counts,
    items: [],
  });
}

beforeEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
  mockCognitivePending(cognitiveCounts());
  vi.mocked(kgHealthApi.runRebuildPreflight).mockResolvedValue({
    board_id: 'b1',
    outcome: 'ready',
    action_required: 'none',
    reason: null,
    base_state: 'fresh',
    metric_status: 'available',
    current_kg_generation_id: 'gen1',
    eligible_source_count: 1,
    skipped_cancelled_count: 0,
    has_non_deterministic_inputs: false,
    canonical_source_count: 1,
    working_source_count: 0,
    skipped_by_maturity_count: 0,
    skipped_expired_working_count: 0,
    legacy_unknown_count: 0,
    preflight_hash: 'hash1',
    generated_at: new Date().toISOString(),
    manifest_ref: 'manifest1',
    source_set_hash: 'sourcehash1',
  });
});

afterEach(() => {
  cleanup();
});

describe('TS1 — mount inicial dispara 1 fetch e renderiza cards principais', () => {
  it('faz 1 fetch e mostra Scheduler/Queue/Health/Footprint/Debt', async () => {
    mockBoard('b1');
    mockApi(() => Promise.resolve(baseHealth));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);

    await waitFor(() => expect(kgHealthApi.getKGHealth).toHaveBeenCalledTimes(1));
    expect(kgHealthApi.getKGHealth).toHaveBeenCalledWith('b1', expect.any(AbortSignal));

    await waitFor(() => {
      expect(screen.getByText('Decay Scheduler')).toBeInTheDocument();
      expect(screen.getByText('Queue & Dead Letter')).toBeInTheDocument();
      expect(screen.getAllByText('KG Health').length).toBeGreaterThan(0);
      expect(screen.getByText('Storage Footprint Proxy')).toBeInTheDocument();
      expect(screen.getByText('Canonical Debt')).toBeInTheDocument();
    });
  });
});

describe('TS2 — polling dispara fetches periódicos', () => {
  it('dispara 5 fetches em 1.05s com pollIntervalMs=200', async () => {
    vi.useFakeTimers();
    mockBoard('b1');
    let resolves: Array<() => void> = [];
    mockApi(
      () =>
        new Promise<KGHealth>((resolve) => {
          resolves.push(() => resolve(baseHealth));
        }),
    );

    render(<KGHealthView pollIntervalMs={200} onClose={() => {}} />);

    // First tick fires synchronously inside the effect.
    await vi.advanceTimersByTimeAsync(0);
    resolves.shift()?.();
    expect(kgHealthApi.getKGHealth).toHaveBeenCalledTimes(1);

    for (let i = 0; i < 5; i++) {
      await vi.advanceTimersByTimeAsync(210);
      resolves.shift()?.();
    }

    expect(vi.mocked(kgHealthApi.getKGHealth).mock.calls.length).toBeGreaterThanOrEqual(4);
    expect(vi.mocked(kgHealthApi.getKGHealth).mock.calls.length).toBeLessThanOrEqual(6);
  });
});

describe('TS3 — visibility hidden suspende polling, visible retoma com catch-up', () => {
  it('zero novos fetches durante hidden + 1 catch-up imediato ao voltar visible', async () => {
    vi.useFakeTimers();
    mockBoard('b1');
    let pending: Array<() => void> = [];
    mockApi(
      () =>
        new Promise<KGHealth>((resolve) => {
          pending.push(() => resolve(baseHealth));
        }),
    );

    render(<KGHealthView pollIntervalMs={200} onClose={() => {}} />);
    await vi.advanceTimersByTimeAsync(0);
    pending.shift()?.();
    const initialCount = vi.mocked(kgHealthApi.getKGHealth).mock.calls.length;

    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
    document.dispatchEvent(new Event('visibilitychange'));

    await vi.advanceTimersByTimeAsync(1000);
    expect(vi.mocked(kgHealthApi.getKGHealth).mock.calls.length).toBe(initialCount);

    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(0);
    pending.shift()?.();

    expect(vi.mocked(kgHealthApi.getKGHealth).mock.calls.length).toBe(initialCount + 1);
  });
});

describe('TS4 — unmount cancela polling e aborta requests pendentes', () => {
  it('chama clearInterval e abort no cleanup', async () => {
    mockBoard('b1');
    mockApi(() => new Promise(() => {})); // never resolves

    const clearSpy = vi.spyOn(globalThis, 'clearInterval');
    const abortSpy = vi.spyOn(AbortController.prototype, 'abort');

    const { unmount } = render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);
    await waitFor(() => expect(kgHealthApi.getKGHealth).toHaveBeenCalled());
    unmount();

    expect(clearSpy).toHaveBeenCalled();
    expect(abortSpy).toHaveBeenCalled();
    clearSpy.mockRestore();
    abortSpy.mockRestore();
  });
});

describe('TS5 — scheduler diagnostics and legacy tick fallback', () => {
  it('null → "Tick has never run"', async () => {
    mockBoard('b1');
    mockApi(() => Promise.resolve({
      ...baseHealth,
      decay_scheduler_diagnostics: undefined,
      last_decay_tick_at: null,
    }));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('Tick has never run')).toBeInTheDocument());
  });

  it('age > 24h → "Stale tick: Xh ago" amber', async () => {
    mockBoard('b1');
    const thirtyHoursAgo = new Date(Date.now() - 30 * 60 * 60 * 1000).toISOString();
    mockApi(() => Promise.resolve({
      ...baseHealth,
      decay_scheduler_diagnostics: undefined,
      last_decay_tick_at: thirtyHoursAgo,
    }));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText(/Stale tick: 30h ago/)).toBeInTheDocument();
    });
  });

  it('age ≤ 24h → "Last tick: Xh ago" neutral', async () => {
    mockBoard('b1');
    const sixHoursAgo = new Date(Date.now() - 6 * 60 * 60 * 1000).toISOString();
    mockApi(() => Promise.resolve({
      ...baseHealth,
      decay_scheduler_diagnostics: undefined,
      last_decay_tick_at: sixHoursAgo,
    }));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText(/Last tick: 6h ago/)).toBeInTheDocument();
    });
  });

  it('uses backend diagnostics instead of the local 24h heuristic when present', async () => {
    mockBoard('b1');
    const thirtyHoursAgo = new Date(Date.now() - 30 * 60 * 60 * 1000).toISOString();
    const sixHoursAgo = new Date(Date.now() - 6 * 60 * 60 * 1000).toISOString();
    mockApi(() => Promise.resolve({
      ...baseHealth,
      last_decay_tick_at: thirtyHoursAgo,
      decay_scheduler_diagnostics: {
        ...baseHealth.decay_scheduler_diagnostics!,
        status: 'ok',
        last_success_at: sixHoursAgo,
        reason: 'latest_success_recent',
        operational_debt: false,
        graph_recovery_required: false,
      },
    }));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText(/Last success: 6h ago/)).toBeInTheDocument();
    });
    expect(screen.queryByText(/Stale tick: 30h ago/)).toBeNull();
    expect(screen.getByText(/operational debt only/i)).toBeInTheDocument();
  });
});

describe('TS6 — schema banner', () => {
  it('match (1.0) → no banner', async () => {
    mockBoard('b1');
    mockApi(() => Promise.resolve({ ...baseHealth, schema_version: '1.0' }));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('Decay Scheduler')).toBeInTheDocument());
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('mismatch (2.0) → red full-width banner with exact text', async () => {
    mockBoard('b1');
    mockApi(() => Promise.resolve({ ...baseHealth, schema_version: '2.0' }));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);
    await waitFor(() => {
      const alert = screen.getByRole('alert');
      expect(alert).toBeInTheDocument();
      expect(alert).toHaveTextContent(/Schema outdated/);
      expect(alert).toHaveTextContent(/1\.0/);
      expect(alert).toHaveTextContent(/2\.0/);
    });
  });
});

describe('KG-HS.3 — scheduler debt and storage-footprint clarity', () => {
  it('renders canonical debt and graph layer counts', async () => {
    mockBoard('b1');
    mockApi(() => Promise.resolve({
      ...baseHealth,
      canonical_debt: {
        open_count: 4,
        retryable_count: 3,
        blocked_count: 1,
        retry_scheduled_count: 0,
        terminal_count: 9,
        by_state: { failed: 2, deferred: 1, blocked: 1, committed: 9 },
      },
      kg_layer_counts: {
        status: 'ok',
        by_layer: { canonical: 120, working: 4 },
        by_maturity_status: { canonical_eligible: 120, working_immature: 4 },
      },
      rebuild_diagnostics: {
        last_outcome: 'rebuild_complete_with_canonical_debt',
        canonical_open_debt_count: 4,
        layer_counts_status: 'ok',
        operator_action: 'inspect_canonical_debt',
      },
    }));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText('Canonical Debt')).toBeInTheDocument());
    expect(screen.getByText('Open debt')).toBeInTheDocument();
    expect(screen.getByText('3 / 1')).toBeInTheDocument();
    expect(screen.getByText(/canonical 120 · working 4/)).toBeInTheDocument();
    expect(screen.getByText('Rebuild Complete With Canonical Debt')).toBeInTheDocument();
  });

  it('renders storage footprint proxy copy without memory/buffer telemetry claims', async () => {
    mockBoard('b1');
    mockApi(() => Promise.resolve({
      ...baseHealth,
      storage_footprint_proxy: {
        ...baseHealth.storage_footprint_proxy!,
        percentage: 82.4,
        high_water_mark_pct: 82.4,
        total_bytes: 824,
        configured_max_db_size_bytes: 1000,
      },
    }));

    const { container } = render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText('Storage Footprint Proxy')).toBeInTheDocument());
    expect(screen.getByText('82.4%')).toBeInTheDocument();
    expect(screen.getByText(/On-disk file-size proxy/i)).toBeInTheDocument();
    const rendered = container.textContent?.toLowerCase() ?? '';
    expect(rendered).not.toContain('direct memory pressure');
    expect(rendered).not.toContain('buffer-pool telemetry');
    expect(rendered).not.toContain('raw buffer pressure');
  });

  it('separates telemetry, DLQ backlog and scheduler debt from recovery-needed messaging', async () => {
    mockBoard('b1');
    mockApi(() =>
      Promise.resolve({
        ...baseHealth,
        overall_state: 'at_risk',
        graph_state: 'at_risk',
        discovery_state: 'at_risk',
        metric_status: 'unavailable',
        dead_letter_count: 189,
        classification_reason: 'metric.unavailable',
        current_kg_generation_id: 'gen1',
        decay_scheduler_diagnostics: {
          ...baseHealth.decay_scheduler_diagnostics!,
          status: 'stale',
          severity: 'warning',
          operational_debt: true,
          graph_recovery_required: false,
          reason: 'latest_success_too_old',
          recommended_action: 'run_tick_now',
        },
        health_issues: [
          {
            code: 'telemetry_unavailable',
            component: 'health_telemetry',
            severity: 'warning',
            reason: 'metric_status:unavailable',
            description: 'Telemetry unavailable',
            operator_action: 'inspect_telemetry',
          },
          {
            code: 'dead_letter_backlog',
            component: 'consolidation_queue',
            severity: 'warning',
            reason: 'dead_letter_count_gt_zero',
            description: 'Dead-letter backlog',
            operator_action: 'inspect_dead_letters',
          },
          {
            code: 'decay_scheduler_stale',
            component: 'decay_scheduler',
            severity: 'warning',
            reason: 'decay_scheduler:latest_success_too_old',
            description: 'Scheduler debt',
            operator_action: 'run_tick_now',
          },
        ],
      }),
    );
    mockCognitivePending(cognitiveCounts({
      pending: 2,
      in_progress: 1,
      consolidated: 56,
      total: 59,
    }));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText('At risk')).toBeInTheDocument());
    expect(screen.queryByText('Recovery needed')).toBeNull();
    expect(screen.getAllByText('unavailable').length).toBeGreaterThan(0);
    expect(screen.getByText('189')).toBeInTheDocument();
    expect(screen.getByText(/3 signals/)).toBeInTheDocument();
    expect(screen.getByText(/Run Tick Now/)).toBeInTheDocument();
    expect(await screen.findByText('3 pending')).toBeInTheDocument();
    const cognitiveRow = screen.getByText('Cognitive state').parentElement;
    expect(cognitiveRow).not.toBeNull();
    expect(cognitiveRow!).toHaveTextContent('pending');
  });
});

describe('KG-HS.4 — Runtime Settings cadence handoff', () => {
  it('opens Runtime Settings Decay Tick tab without rendering scheduler edit fields locally', async () => {
    mockBoard('b1');
    mockApi(() => Promise.resolve({
      ...baseHealth,
      decay_scheduler_diagnostics: {
        ...baseHealth.decay_scheduler_diagnostics!,
        stale_tolerance_seconds: 7 * 24 * 60 * 60,
        next_scheduled_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
      },
    }));
    const handler = vi.fn();
    const onClose = vi.fn();
    window.addEventListener('okto:open-runtime-settings', handler as EventListener);

    render(<KGHealthView pollIntervalMs={30000} onClose={onClose} />);

    await waitFor(() =>
      expect(screen.getByText('Cadence is edited in Settings.')).toBeInTheDocument(),
    );
    expect(screen.getByText(/Runtime Settings > Decay Tick/i)).toBeInTheDocument();
    expect(screen.queryByTestId('input-tick-interval-minutes')).toBeNull();
    expect(screen.queryByTestId('input-tick-staleness-days')).toBeNull();

    fireEvent.click(screen.getByTestId('kg-open-decay-settings'));

    expect(handler).toHaveBeenCalledTimes(1);
    expect((handler.mock.calls[0][0] as CustomEvent).detail).toEqual({
      initialTab: 'decaytick',
    });
    expect(onClose).toHaveBeenCalledTimes(1);

    window.removeEventListener('okto:open-runtime-settings', handler as EventListener);
  });
});

describe('TS7 — disconnected nodes panel removed', () => {
  it('does not render the disconnected-node panel even when payload includes rows', async () => {
    mockBoard('b1');
    const ten = Array.from({ length: 10 }, (_, i) => ({
      id: `entity_${i}`,
      type: 'Decision',
      degree: i,
    }));
    mockApi(() => Promise.resolve({ ...baseHealth, top_disconnected_nodes: ten }));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('Decay Scheduler')).toBeInTheDocument());
    expect(screen.queryByText('Top 10 most disconnected nodes')).not.toBeInTheDocument();
    expect(screen.queryByText('No disconnected nodes')).not.toBeInTheDocument();
    expect(screen.queryByText('entity_9')).not.toBeInTheDocument();
    expect(screen.queryByText(/Disconnected nodes/i)).not.toBeInTheDocument();
  });
});

describe('TS9 — refresh button dispara fetch sem reset polling', () => {
  it('count cresce em cada click do refresh, sem alterar intervalo', async () => {
    mockBoard('b1');
    mockApi(() => Promise.resolve(baseHealth));

    render(<KGHealthView pollIntervalMs={60_000} onClose={() => {}} />);
    await waitFor(() => expect(kgHealthApi.getKGHealth).toHaveBeenCalledTimes(1));

    const refreshBtn = await screen.findByRole('button', { name: /refresh kg data now/i });
    fireEvent.click(refreshBtn);
    await waitFor(() => expect(kgHealthApi.getKGHealth).toHaveBeenCalledTimes(2));

    fireEvent.click(refreshBtn);
    await waitFor(() => expect(kgHealthApi.getKGHealth).toHaveBeenCalledTimes(3));
  });
});

describe('TS10 — error shows panel + Try again re-fetch', () => {
  it('error panel with message + Try again button, click re-fetches', async () => {
    mockBoard('b1');
    let call = 0;
    mockApi(() => {
      call += 1;
      return call === 1 ? Promise.reject(new Error('boom')) : Promise.resolve(baseHealth);
    });

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText(/Failed to load KG health/i)).toBeInTheDocument());
    expect(screen.getByText('boom')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /try again/i }));
    await waitFor(() => expect(screen.getByText('Decay Scheduler')).toBeInTheDocument());
    expect(screen.queryByText(/Failed to load KG health/i)).toBeNull();
  });
});

describe('TS11 — skeleton em loading inicial', () => {
  it('mostra skeleton cards enquanto fetch pendente', async () => {
    mockBoard('b1');
    mockApi(() => new Promise(() => {})); // never resolves

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getAllByTestId('skeleton-card')).toHaveLength(5);
    });
  });

  it('skeleton some após primeiro resolve', async () => {
    mockBoard('b1');
    const resolvers: Array<(value: KGHealth) => void> = [];
    mockApi(
      () =>
        new Promise<KGHealth>((resolve) => {
          resolvers.push(resolve);
        }),
    );

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);
    await waitFor(() => expect(screen.getAllByTestId('skeleton-card')).toHaveLength(5));
    resolvers[0]?.(baseHealth);
    await waitFor(() => expect(screen.getByText('Decay Scheduler')).toBeInTheDocument());
    expect(screen.queryAllByTestId('skeleton-card')).toHaveLength(0);
  });
});

describe('TS12 — empty state sem currentBoard suprime polling', () => {
  it('texto centralizado e zero fetches', async () => {
    mockBoard(null);
    const spy = vi.mocked(kgHealthApi.getKGHealth);

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);
    expect(
      screen.getByText(/Select a board to view KG health/),
    ).toBeInTheDocument();

    await new Promise((r) => setTimeout(r, 50));
    expect(spy).not.toHaveBeenCalled();
  });
});

describe('KG recovery panel — health and cognitive rebuild state', () => {
  it('labels at_risk health as At risk, not Recovery needed', async () => {
    mockBoard('b1');
    mockApi(() =>
      Promise.resolve({
        ...baseHealth,
        overall_state: 'at_risk',
        graph_state: 'at_risk',
        discovery_state: 'at_risk',
        classification_reason: 'metric.unavailable',
        current_kg_generation_id: 'gen1',
      }),
    );
    mockCognitivePending(cognitiveCounts({
      consolidated: 59,
      total: 59,
    }));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText('At risk')).toBeInTheDocument());
    expect(screen.queryByText('Recovery needed')).toBeNull();
  });

  it('adds explanatory tooltips to each KG Recovery metric', async () => {
    mockBoard('b1');
    mockApi(() =>
      Promise.resolve({
        ...baseHealth,
        overall_state: 'at_risk',
        graph_state: 'at_risk',
        discovery_state: 'at_risk',
        classification_reason: 'metric.unavailable',
        current_kg_generation_id: 'gen1',
      }),
    );
    mockCognitivePending(cognitiveCounts({
      consolidated: 59,
      total: 59,
    }));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);

    const graphMetric = await screen.findByTestId('kg-recovery-metric-board-graph');
    expect(graphMetric.getAttribute('title')).toContain('graph.lbug');
    expect(graphMetric.getAttribute('title')).toContain('metric.unavailable');

    const discoveryMetric = screen.getByTestId('kg-recovery-metric-global-discovery');
    expect(discoveryMetric.getAttribute('title')).toContain('discovery.lbug');

    const cognitiveMetric = screen.getByTestId('kg-recovery-metric-cognitive');
    expect(cognitiveMetric.getAttribute('title')).toContain('consolidated');
  });

  it('surfaces an empty board graph explicitly when health total_nodes is zero', async () => {
    mockBoard('b1');
    mockApi(() =>
      Promise.resolve({
        ...baseHealth,
        total_nodes: 0,
        overall_state: 'at_risk',
        graph_state: 'at_risk',
        discovery_state: 'at_risk',
        classification_reason: 'graph:metric.unavailable',
        current_kg_generation_id: 'gen1',
      }),
    );

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);

    const graphMetric = await screen.findByTestId('kg-recovery-metric-board-graph');
    expect(graphMetric).toHaveTextContent('empty');
    expect(graphMetric).toHaveTextContent('0 nodes indexed');
    expect(graphMetric.getAttribute('title')).toContain('total_nodes=0');
  });

  it('shows consolidated cognitive state from the generation counts', async () => {
    mockBoard('b1');
    mockApi(() =>
      Promise.resolve({
        ...baseHealth,
        overall_state: 'at_risk',
        current_kg_generation_id: 'gen1',
      }),
    );
    mockCognitivePending(cognitiveCounts({
      consolidated: 59,
      total: 59,
    }));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);

    await waitFor(() =>
      expect(screen.getByText('consolidated after rebuild')).toBeInTheDocument(),
    );
    const cognitiveRow = screen.getByText('Cognitive state').parentElement;
    expect(cognitiveRow).not.toBeNull();
    expect(cognitiveRow!).toHaveTextContent('consolidated');
  });

  it('shows pending cognitive state when the generation has active items', async () => {
    mockBoard('b1');
    mockApi(() =>
      Promise.resolve({
        ...baseHealth,
        overall_state: 'at_risk',
        current_kg_generation_id: 'gen1',
      }),
    );
    mockCognitivePending(cognitiveCounts({
      pending: 2,
      in_progress: 1,
      consolidated: 56,
      total: 59,
    }));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText('3 pending')).toBeInTheDocument());
    const cognitiveRow = screen.getByText('Cognitive state').parentElement;
    expect(cognitiveRow).not.toBeNull();
    expect(cognitiveRow!).toHaveTextContent('pending');
  });
});
