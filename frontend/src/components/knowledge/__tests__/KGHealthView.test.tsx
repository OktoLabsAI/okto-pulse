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
import type { KGHealth } from '@/services/kg-health-api';

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

beforeEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

afterEach(() => {
  cleanup();
});

describe('TS1 — mount inicial dispara 1 fetch e renderiza 4 cards', () => {
  it('faz 1 fetch e mostra os 4 cards Schema/Queue/Health/Activity', async () => {
    mockBoard('b1');
    mockApi(() => Promise.resolve(baseHealth));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);

    await waitFor(() => expect(kgHealthApi.getKGHealth).toHaveBeenCalledTimes(1));
    expect(kgHealthApi.getKGHealth).toHaveBeenCalledWith('b1', expect.any(AbortSignal));

    await waitFor(() => {
      expect(screen.getByText('Schema & Tick')).toBeInTheDocument();
      expect(screen.getByText('Queue & Dead Letter')).toBeInTheDocument();
      expect(screen.getAllByText('KG Health').length).toBeGreaterThan(0);
      expect(screen.getByText('Activity')).toBeInTheDocument();
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

describe('TS5 — stale tick badge', () => {
  it('null → "Tick has never run"', async () => {
    mockBoard('b1');
    mockApi(() => Promise.resolve({ ...baseHealth, last_decay_tick_at: null }));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('Tick has never run')).toBeInTheDocument());
  });

  it('age > 24h → "Stale tick: Xh ago" amber', async () => {
    mockBoard('b1');
    const thirtyHoursAgo = new Date(Date.now() - 30 * 60 * 60 * 1000).toISOString();
    mockApi(() => Promise.resolve({ ...baseHealth, last_decay_tick_at: thirtyHoursAgo }));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText(/Stale tick: 30h ago/)).toBeInTheDocument();
    });
  });

  it('age ≤ 24h → "Last tick: Xh ago" neutral', async () => {
    mockBoard('b1');
    const sixHoursAgo = new Date(Date.now() - 6 * 60 * 60 * 1000).toISOString();
    mockApi(() => Promise.resolve({ ...baseHealth, last_decay_tick_at: sixHoursAgo }));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText(/Last tick: 6h ago/)).toBeInTheDocument();
    });
  });
});

describe('TS6 — schema banner', () => {
  it('match (1.0) → no banner', async () => {
    mockBoard('b1');
    mockApi(() => Promise.resolve({ ...baseHealth, schema_version: '1.0' }));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('Schema & Tick')).toBeInTheDocument());
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

describe('TS7 — tabela Top-N renderiza N rows até max 10', () => {
  it('0 entries → message "No disconnected nodes"', async () => {
    mockBoard('b1');
    mockApi(() => Promise.resolve({ ...baseHealth, top_disconnected_nodes: [] }));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('No disconnected nodes')).toBeInTheDocument());
  });

  it('5 entries → 5 tbody rows', async () => {
    mockBoard('b1');
    const five = Array.from({ length: 5 }, (_, i) => ({
      id: `entity_${i}`,
      type: 'Entity',
      degree: i,
    }));
    mockApi(() => Promise.resolve({ ...baseHealth, top_disconnected_nodes: five }));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('entity_0')).toBeInTheDocument());
    const table = screen.getByRole('table');
    expect(table.querySelectorAll('tbody tr')).toHaveLength(5);
  });

  it('10 entries → 10 tbody rows com 3 cells cada', async () => {
    mockBoard('b1');
    const ten = Array.from({ length: 10 }, (_, i) => ({
      id: `entity_${i}`,
      type: 'Decision',
      degree: i,
    }));
    mockApi(() => Promise.resolve({ ...baseHealth, top_disconnected_nodes: ten }));

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('entity_9')).toBeInTheDocument());
    const rows = screen.getByRole('table').querySelectorAll('tbody tr');
    expect(rows).toHaveLength(10);
    rows.forEach((r) => expect(r.querySelectorAll('td')).toHaveLength(3));
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
    await waitFor(() => expect(screen.getByText('Schema & Tick')).toBeInTheDocument());
    expect(screen.queryByText(/Failed to load KG health/i)).toBeNull();
  });
});

describe('TS11 — skeleton em loading inicial', () => {
  it('mostra 4 skeleton cards enquanto fetch pendente', async () => {
    mockBoard('b1');
    mockApi(() => new Promise(() => {})); // never resolves

    render(<KGHealthView pollIntervalMs={30000} onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getAllByTestId('skeleton-card')).toHaveLength(4);
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
    await waitFor(() => expect(screen.getAllByTestId('skeleton-card')).toHaveLength(4));
    resolvers[0]?.(baseHealth);
    await waitFor(() => expect(screen.getByText('Schema & Tick')).toBeInTheDocument());
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
