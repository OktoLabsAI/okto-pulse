/**
 * KGHealthCognitivePendingPanel unit tests — KG-03.5 / api_897dde99.
 *
 * Mocks getKGCognitivePendingItems at the module boundary. Asserts the
 * UI states (loading | empty | ready | error), counts rendering, legacy
 * mode badge, and — critically — that the panel exposes NO mutation
 * affordance: no complete/skip/fail button, no free-text cognitive
 * judgement input. The cognitive item mutation surface remains the MCP
 * tool only (br_2065f80b + AC9).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';

import { KGHealthCognitivePendingPanel } from '../KGHealthCognitivePendingPanel';
import * as kgHealthApi from '@/services/kg-health-api';
import type { KGCognitivePendingResponse } from '@/services/kg-health-api';
import {
  KG_COGNITIVE_PENDING_PANEL_METRIC_LABELS,
  getKGCognitivePendingPanelEventCount,
  getKGCognitivePendingPanelSamples,
  resetKGCognitivePendingPanelTelemetry,
} from '@/services/kg-cognitive-pending-telemetry';

vi.mock('@/services/kg-health-api');

const BOARD = 'board-kg03-5';

function baseResponse(
  partial: Partial<KGCognitivePendingResponse> = {},
): KGCognitivePendingResponse {
  return {
    board_id: BOARD,
    selected_kg_generation_id: '8c1f0000-0000-4000-8000-000000000000',
    readonly: true,
    legacy_mode: false,
    counts: {
      pending: 2,
      in_progress: 0,
      consolidated: 1,
      skipped: 0,
      failed: 0,
      total: 3,
    },
    items: [
      {
        item_id: 'cogn_aaaaaaaaaaaa_bbbbbb',
        source_ref: 'spec:s1',
        artifact_type: 'spec',
        status: 'pending',
        recorded_at: '2026-05-26T12:00:00+00:00',
        updated_at: null,
        updated_by_agent_id: null,
        consolidation_session_id: null,
        reason_code: null,
      },
      {
        item_id: 'cogn_cccccccccccc_dddddd',
        source_ref: 'refinement:r1',
        artifact_type: 'refinement',
        status: 'consolidated',
        recorded_at: '2026-05-26T12:00:00+00:00',
        updated_at: '2026-05-26T12:05:00+00:00',
        updated_by_agent_id: 'agent-007',
        consolidation_session_id: 'sess-1',
        reason_code: null,
      },
    ],
    ...partial,
  };
}

function mockApi(
  impl: (
    boardId: string,
    options?: kgHealthApi.GetKGCognitivePendingOptions,
    signal?: AbortSignal,
  ) => Promise<KGCognitivePendingResponse>,
) {
  vi.mocked(kgHealthApi.getKGCognitivePendingItems).mockImplementation(impl);
}

beforeEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
  resetKGCognitivePendingPanelTelemetry();
});

afterEach(() => {
  cleanup();
  resetKGCognitivePendingPanelTelemetry();
});


// -------- UI states ------------------------------------------------------


describe('UI state: ready', () => {
  it('renders counts row + items list after a successful fetch', async () => {
    mockApi(() => Promise.resolve(baseResponse()));
    render(
      <KGHealthCognitivePendingPanel
        boardId={BOARD}
        selectedKgGenerationId={null}
        pollIntervalMs={30000}
      />,
    );
    await waitFor(() =>
      expect(kgHealthApi.getKGCognitivePendingItems).toHaveBeenCalledTimes(1),
    );
    await waitFor(() => {
      expect(
        screen.getByTestId('kg-cognitive-pending-counts'),
      ).toBeInTheDocument();
      expect(
        screen.getByTestId('kg-cognitive-pending-items'),
      ).toBeInTheDocument();
    });
    // counts.total = 3 → exposed in the Total tile.
    const totalTile = screen.getByTestId('kg-cognitive-pending-count-total');
    expect(totalTile.textContent).toContain('3');
    // pending = 2, consolidated = 1 (from baseResponse).
    expect(screen.getByTestId('kg-cognitive-pending-count-pending').textContent).toContain('2');
    expect(screen.getByTestId('kg-cognitive-pending-count-consolidated').textContent).toContain('1');
  });

  it('renders every status bucket in counts row even when zero', async () => {
    mockApi(() => Promise.resolve(baseResponse()));
    render(
      <KGHealthCognitivePendingPanel
        boardId={BOARD}
        selectedKgGenerationId={null}
        pollIntervalMs={30000}
      />,
    );
    await waitFor(() =>
      expect(screen.getByTestId('kg-cognitive-pending-counts')).toBeInTheDocument(),
    );
    for (const tile of [
      'count-pending',
      'count-in_progress',
      'count-consolidated',
      'count-skipped',
      'count-failed',
      'count-total',
    ]) {
      expect(
        screen.getByTestId(`kg-cognitive-pending-${tile}`),
      ).toBeInTheDocument();
    }
  });
});


describe('UI state: empty', () => {
  it('shows the empty hint when counts.total is 0', async () => {
    mockApi(() =>
      Promise.resolve(
        baseResponse({
          items: [],
          counts: {
            pending: 0,
            in_progress: 0,
            consolidated: 0,
            skipped: 0,
            failed: 0,
            total: 0,
          },
        }),
      ),
    );
    render(
      <KGHealthCognitivePendingPanel
        boardId={BOARD}
        selectedKgGenerationId={null}
        pollIntervalMs={30000}
      />,
    );
    await waitFor(() =>
      expect(
        screen.getByTestId('kg-cognitive-pending-empty'),
      ).toBeInTheDocument(),
    );
  });
});


describe('UI state: error', () => {
  it('renders a non-blocking error state when the first fetch fails', async () => {
    mockApi(() => Promise.reject(new Error('cognitive_pending_unavailable')));
    render(
      <KGHealthCognitivePendingPanel
        boardId={BOARD}
        selectedKgGenerationId={null}
        pollIntervalMs={30000}
      />,
    );
    await waitFor(() =>
      expect(
        screen.getByTestId('kg-cognitive-pending-error'),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText(/cognitive_pending_unavailable/i)).toBeInTheDocument();
  });
});


// -------- Legacy mode ---------------------------------------------------


describe('legacy_mode badge', () => {
  it('renders the legacy badge when the response carries legacy_mode=true', async () => {
    mockApi(() =>
      Promise.resolve(baseResponse({ legacy_mode: true })),
    );
    render(
      <KGHealthCognitivePendingPanel
        boardId={BOARD}
        selectedKgGenerationId={null}
        pollIntervalMs={30000}
      />,
    );
    await waitFor(() =>
      expect(
        screen.getByTestId('kg-cognitive-pending-legacy-badge'),
      ).toBeInTheDocument(),
    );
  });

  it('hides the legacy badge when legacy_mode=false', async () => {
    mockApi(() => Promise.resolve(baseResponse({ legacy_mode: false })));
    render(
      <KGHealthCognitivePendingPanel
        boardId={BOARD}
        selectedKgGenerationId={null}
        pollIntervalMs={30000}
      />,
    );
    await waitFor(() =>
      expect(screen.getByTestId('kg-cognitive-pending-counts')).toBeInTheDocument(),
    );
    expect(
      screen.queryByTestId('kg-cognitive-pending-legacy-badge'),
    ).not.toBeInTheDocument();
  });
});


// -------- Generation id display -----------------------------------------


describe('selected generation id display', () => {
  it('annotates "(latest)" when caller did not pin a generation', async () => {
    mockApi(() => Promise.resolve(baseResponse()));
    render(
      <KGHealthCognitivePendingPanel
        boardId={BOARD}
        selectedKgGenerationId={null}
        pollIntervalMs={30000}
      />,
    );
    await waitFor(() =>
      expect(
        screen.getByTestId('kg-cognitive-pending-generation'),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByTestId('kg-cognitive-pending-generation').textContent,
    ).toContain('latest');
  });

  it('omits the "(latest)" annotation when caller pinned a generation', async () => {
    mockApi(() => Promise.resolve(baseResponse()));
    render(
      <KGHealthCognitivePendingPanel
        boardId={BOARD}
        selectedKgGenerationId="8c1f0000-0000-4000-8000-000000000000"
        pollIntervalMs={30000}
      />,
    );
    await waitFor(() =>
      expect(
        screen.getByTestId('kg-cognitive-pending-generation'),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByTestId('kg-cognitive-pending-generation').textContent,
    ).not.toContain('latest');
  });
});


// -------- No mutation affordance (br_2065f80b + AC9) --------------------


describe('br_2065f80b — panel exposes no cognitive mutation affordance', () => {
  it('renders NO complete / skip / fail button and NO free-text input', async () => {
    mockApi(() => Promise.resolve(baseResponse()));
    const { container } = render(
      <KGHealthCognitivePendingPanel
        boardId={BOARD}
        selectedKgGenerationId={null}
        pollIntervalMs={30000}
      />,
    );
    await waitFor(() =>
      expect(screen.getByTestId('kg-cognitive-pending-items')).toBeInTheDocument(),
    );

    // Only buttons in the panel are the Refresh button + (on error) Retry.
    const buttons = container.querySelectorAll('button');
    const labels = Array.from(buttons).map((b) =>
      (b.textContent || '').trim().toLowerCase(),
    );
    const forbiddenButtonText = [
      'complete',
      'consolidate',
      'mark consolidated',
      'skip',
      'mark skipped',
      'fail',
      'mark failed',
    ];
    for (const forbidden of forbiddenButtonText) {
      for (const label of labels) {
        expect(label.includes(forbidden)).toBe(false);
      }
    }
    // No text input / textarea (no free-text cognitive judgement).
    expect(container.querySelector('input[type="text"]')).toBeNull();
    expect(container.querySelector('textarea')).toBeNull();
    // No select element (no inline status dropdown).
    expect(container.querySelector('select')).toBeNull();
  });
});


// -------- Item rendering ------------------------------------------------


describe('item list rendering', () => {
  it('shows each item with status badge + artifact type + source_ref', async () => {
    mockApi(() => Promise.resolve(baseResponse()));
    render(
      <KGHealthCognitivePendingPanel
        boardId={BOARD}
        selectedKgGenerationId={null}
        pollIntervalMs={30000}
      />,
    );
    await waitFor(() =>
      expect(
        screen.getByTestId('kg-cognitive-pending-items'),
      ).toBeInTheDocument(),
    );
    const items = screen.getAllByTestId('kg-cognitive-pending-item');
    expect(items).toHaveLength(2);
    // spec:s1 pending + refinement:r1 consolidated.
    expect(screen.getByText('spec:s1')).toBeInTheDocument();
    expect(screen.getByText('refinement:r1')).toBeInTheDocument();
    expect(
      screen.getByTestId('kg-cognitive-pending-item-status-pending'),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId('kg-cognitive-pending-item-status-consolidated'),
    ).toBeInTheDocument();
  });

  it('does NOT render the storage-only reason field even if present', async () => {
    mockApi(() =>
      Promise.resolve(
        baseResponse({
          items: [
            {
              item_id: 'cogn_xxxx',
              source_ref: 'spec:s1',
              artifact_type: 'spec',
              status: 'skipped',
              recorded_at: '2026-05-26T12:00:00+00:00',
              updated_at: '2026-05-26T12:05:00+00:00',
              updated_by_agent_id: 'agent-007',
              consolidation_session_id: null,
              reason_code: 'agent_choice',
              // Defensive: even if the server accidentally leaks free-text
              // reason, the panel must not render it because it consumes the
              // typed KGCognitivePendingItem shape.
              reason: 'this should not be rendered anywhere',
            } as any,
          ],
        }),
      ),
    );
    const { container } = render(
      <KGHealthCognitivePendingPanel
        boardId={BOARD}
        selectedKgGenerationId={null}
        pollIntervalMs={30000}
      />,
    );
    await waitFor(() =>
      expect(
        screen.getByTestId('kg-cognitive-pending-items'),
      ).toBeInTheDocument(),
    );
    expect(container.textContent).not.toContain('this should not be rendered');
    expect(container.textContent).toContain('agent_choice');
  });
});


// -------- Non-blocking failure after successful first fetch -------------


describe('non-blocking failure (Codex audit val_ed0f9548)', () => {
  it('preserves prior counts/items + shows inline warning when refresh fails', async () => {
    let callCount = 0;
    mockApi(() => {
      callCount += 1;
      if (callCount === 1) {
        return Promise.resolve(baseResponse());
      }
      return Promise.reject(new Error('flaky network on refresh'));
    });

    const { container } = render(
      <KGHealthCognitivePendingPanel
        boardId={BOARD}
        selectedKgGenerationId={null}
        pollIntervalMs={30000}
      />,
    );

    // 1st fetch succeeds — counts visible.
    await waitFor(() =>
      expect(
        screen.getByTestId('kg-cognitive-pending-counts'),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByTestId('kg-cognitive-pending-count-total').textContent,
    ).toContain('3');

    // Trigger refresh — invokes 2nd fetch which rejects.
    const refreshButton = screen.getByRole('button', { name: /refresh/i });
    refreshButton.click();

    // Inline warning eventually renders, prior snapshot stays intact.
    await waitFor(() =>
      expect(
        screen.getByTestId('kg-cognitive-pending-inline-warning'),
      ).toBeInTheDocument(),
    );
    // Items list still rendered (non-blocking).
    expect(
      screen.getByTestId('kg-cognitive-pending-items'),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId('kg-cognitive-pending-count-total').textContent,
    ).toContain('3');
    // The blocking error state is NOT shown.
    expect(
      screen.queryByTestId('kg-cognitive-pending-error'),
    ).not.toBeInTheDocument();
    // The warning carries the failure message.
    expect(container.textContent).toContain('flaky network on refresh');
  });
});


// -------- or_229dfe09 — bounded panel-state telemetry -------------------


describe('or_229dfe09 telemetry — kg_health_cognitive_pending_panel_state_total', () => {
  it('exports exactly the bounded label set (state, has_generation)', () => {
    expect([...KG_COGNITIVE_PENDING_PANEL_METRIC_LABELS]).toEqual([
      'state',
      'has_generation',
    ]);
  });

  it('emits state=ready and has_generation=true on a successful fetch', async () => {
    mockApi(() => Promise.resolve(baseResponse()));
    render(
      <KGHealthCognitivePendingPanel
        boardId={BOARD}
        selectedKgGenerationId={null}
        pollIntervalMs={30000}
      />,
    );
    await waitFor(() =>
      expect(getKGCognitivePendingPanelSamples().length).toBeGreaterThanOrEqual(1),
    );
    expect(
      getKGCognitivePendingPanelEventCount({
        state: 'ready',
        has_generation: 'true',
      }),
    ).toBe(1);
  });

  it('emits state=empty when the response carries zero items', async () => {
    mockApi(() =>
      Promise.resolve(
        baseResponse({
          items: [],
          counts: {
            pending: 0,
            in_progress: 0,
            consolidated: 0,
            skipped: 0,
            failed: 0,
            total: 0,
          },
        }),
      ),
    );
    render(
      <KGHealthCognitivePendingPanel
        boardId={BOARD}
        selectedKgGenerationId={null}
        pollIntervalMs={30000}
      />,
    );
    await waitFor(() =>
      expect(getKGCognitivePendingPanelEventCount({ state: 'empty' })).toBe(1),
    );
  });

  it('emits state=error on first-fetch failure', async () => {
    mockApi(() => Promise.reject(new Error('boom')));
    render(
      <KGHealthCognitivePendingPanel
        boardId={BOARD}
        selectedKgGenerationId={null}
        pollIntervalMs={30000}
      />,
    );
    await waitFor(() =>
      expect(getKGCognitivePendingPanelEventCount({ state: 'error' })).toBe(1),
    );
  });

  it('emits state=error on refresh failure AFTER a previous success', async () => {
    let calls = 0;
    mockApi(() => {
      calls += 1;
      if (calls === 1) return Promise.resolve(baseResponse());
      return Promise.reject(new Error('boom'));
    });
    render(
      <KGHealthCognitivePendingPanel
        boardId={BOARD}
        selectedKgGenerationId={null}
        pollIntervalMs={30000}
      />,
    );
    await waitFor(() =>
      expect(
        screen.getByTestId('kg-cognitive-pending-counts'),
      ).toBeInTheDocument(),
    );
    screen.getByRole('button', { name: /refresh/i }).click();
    await waitFor(() =>
      expect(getKGCognitivePendingPanelEventCount({ state: 'error' })).toBe(1),
    );
    // The success sample is still recorded — outcomes are NOT overwritten.
    expect(
      getKGCognitivePendingPanelEventCount({ state: 'ready' }),
    ).toBe(1);
  });

  it('emits has_generation=true even when prop is null but API resolves latest', async () => {
    // selectedKgGenerationId=null but API response carries
    // selected_kg_generation_id from latest fallback.
    mockApi(() => Promise.resolve(baseResponse()));
    render(
      <KGHealthCognitivePendingPanel
        boardId={BOARD}
        selectedKgGenerationId={null}
        pollIntervalMs={30000}
      />,
    );
    await waitFor(() =>
      expect(getKGCognitivePendingPanelSamples().length).toBeGreaterThanOrEqual(1),
    );
    const last = getKGCognitivePendingPanelSamples().at(-1)!;
    expect(last.has_generation).toBe('true');
  });

  it('emits has_generation=false when neither prop nor API has a generation', async () => {
    mockApi(() =>
      Promise.resolve(
        baseResponse({
          selected_kg_generation_id: null,
          items: [],
          counts: {
            pending: 0,
            in_progress: 0,
            consolidated: 0,
            skipped: 0,
            failed: 0,
            total: 0,
          },
        }),
      ),
    );
    render(
      <KGHealthCognitivePendingPanel
        boardId={BOARD}
        selectedKgGenerationId={null}
        pollIntervalMs={30000}
      />,
    );
    await waitFor(() =>
      expect(getKGCognitivePendingPanelSamples().length).toBeGreaterThanOrEqual(1),
    );
    const last = getKGCognitivePendingPanelSamples().at(-1)!;
    expect(last.has_generation).toBe('false');
  });

  it('samples carry ONLY (state, has_generation) — no high-cardinality labels', async () => {
    mockApi(() => Promise.resolve(baseResponse()));
    render(
      <KGHealthCognitivePendingPanel
        boardId={BOARD}
        selectedKgGenerationId="8c1f0000-0000-4000-8000-000000000000"
        pollIntervalMs={30000}
      />,
    );
    await waitFor(() =>
      expect(getKGCognitivePendingPanelSamples().length).toBeGreaterThanOrEqual(1),
    );
    const samples = getKGCognitivePendingPanelSamples();
    const forbidden = [
      'item_id',
      'source_ref',
      'agent',
      'agent_id',
      'reason',
      'reason_code',
      'kg_generation_id',
      'board_id',
      'message',
    ];
    for (const sample of samples) {
      expect(Object.keys(sample)).toEqual(['state', 'has_generation']);
      for (const key of forbidden) {
        expect(Object.keys(sample)).not.toContain(key);
      }
    }
  });
});


// -------- API call shape ------------------------------------------------


describe('API call surface', () => {
  it('passes selectedKgGenerationId through to the API as kgGenerationId', async () => {
    mockApi(() => Promise.resolve(baseResponse()));
    render(
      <KGHealthCognitivePendingPanel
        boardId={BOARD}
        selectedKgGenerationId="8c1f0000-0000-4000-8000-000000000000"
        pollIntervalMs={30000}
      />,
    );
    await waitFor(() =>
      expect(kgHealthApi.getKGCognitivePendingItems).toHaveBeenCalled(),
    );
    const args = vi.mocked(kgHealthApi.getKGCognitivePendingItems).mock.calls[0];
    expect(args[0]).toBe(BOARD);
    expect(args[1]?.kgGenerationId).toBe('8c1f0000-0000-4000-8000-000000000000');
  });

  it('paginates item rows through limit + offset while keeping counts global', async () => {
    mockApi((_boardId, options) => {
      const offset = options?.offset ?? 0;
      const itemIndex = offset + 1;
      return Promise.resolve(
        baseResponse({
          counts: {
            pending: 59,
            in_progress: 0,
            consolidated: 0,
            skipped: 0,
            failed: 0,
            total: 59,
          },
          items: [
            {
              item_id: `cogn_page_${itemIndex}`,
              source_ref: `spec:s${itemIndex}`,
              artifact_type: 'spec',
              status: 'pending',
              recorded_at: '2026-05-26T12:00:00+00:00',
              updated_at: null,
              updated_by_agent_id: null,
              consolidation_session_id: null,
              reason_code: null,
            },
          ],
        }),
      );
    });

    render(
      <KGHealthCognitivePendingPanel
        boardId={BOARD}
        selectedKgGenerationId={null}
        pollIntervalMs={30000}
      />,
    );

    await waitFor(() =>
      expect(
        screen.getByTestId('kg-cognitive-pending-pagination'),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByTestId('kg-cognitive-pending-pagination-summary'),
    ).toHaveTextContent('Showing 1-1 of 59');
    expect(screen.getByText('spec:s1')).toBeInTheDocument();

    screen.getByRole('button', { name: /next cognitive pending page/i }).click();

    await waitFor(() =>
      expect(kgHealthApi.getKGCognitivePendingItems).toHaveBeenCalledTimes(2),
    );
    const secondCall = vi.mocked(kgHealthApi.getKGCognitivePendingItems).mock.calls[1];
    expect(secondCall[1]?.limit).toBe(25);
    expect(secondCall[1]?.offset).toBe(25);
    await waitFor(() => expect(screen.getByText('spec:s26')).toBeInTheDocument());
    expect(
      screen.getByTestId('kg-cognitive-pending-pagination-page'),
    ).toHaveTextContent('Page 2 of 3');
  });
});
