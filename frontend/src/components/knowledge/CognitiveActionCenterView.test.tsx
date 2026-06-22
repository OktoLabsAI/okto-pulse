/**
 * Tests for CognitiveActionCenterView (S3.3 / card 974f5146, spec 2731a346).
 *
 * Cobre as invariantes da UI: technical blockers (DLQ/open debt) NÃO oferecem
 * skip; would_block_done só aparece quando o backend marca; skip/clear via
 * write-path central; 409 mostrado sem mascarar o blocker técnico; métricas
 * bounded; aliases card/bug. A precedência/enforcement vêm do backend — a UI
 * só renderiza.
 */

import { afterEach, describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

import { CognitiveActionCenterView } from './CognitiveActionCenterView';
import * as api from '@/services/cognitive-readiness-api';
import type {
  CognitiveReadinessItem,
  CognitiveReadinessListResponse,
  CognitiveReadinessMetrics,
} from '@/types/cognitive-readiness';

function item(over: Partial<CognitiveReadinessItem>): CognitiveReadinessItem {
  return {
    artifact_id: 'card:aaaa',
    source_ref_original: 'card:aaaa',
    aliases: ['card:aaaa'],
    artifact_type: 'card',
    signal: 'cognitive_pending',
    signal_source: 'cognitive_item',
    status: 'pending',
    outcome_type: null,
    reason_code: null,
    error_cause: null,
    revisit_at: null,
    readiness_effect: 'blocking_cognitive',
    blocking: true,
    precedence_explanation: { tier: 'cognitive_active' },
    would_block_done: false,
    ...over,
  };
}

const COGNITIVE = item({ artifact_id: 'card:cog1', source_ref_original: 'card:cog1' });
const DLQ = item({
  artifact_id: 'card:dlq1',
  source_ref_original: 'card:dlq1',
  signal: 'dlq',
  signal_source: 'dlq',
  status: 'dead_lettered',
  error_cause: 'technical_dlq',
  readiness_effect: 'blocking_technical',
  blocking: true,
  would_block_done: true,
});
const SKIPPED = item({
  artifact_id: 'card:skip1',
  source_ref_original: 'bug:skip1',
  aliases: ['bug:skip1', 'card:skip1'],
  signal: 'skipped',
  status: 'skipped',
  outcome_type: 'no_action_required',
  reason_code: 'trivial_fix',
  readiness_effect: 'ready_skip',
  blocking: false,
});

const METRICS: CognitiveReadinessMetrics = {
  board_id: 'b',
  total: 3,
  by_status: { pending: 1, dead_lettered: 1, skipped: 1 },
  by_outcome_type: { none: 2, no_action_required: 1 },
  by_reason_code: { none: 2, trivial_fix: 1 },
  by_artifact_type: { card: 3 },
  by_readiness_effect: { blocking_cognitive: 1, blocking_technical: 1, ready_skip: 1 },
  by_signal: { cognitive_pending: 1, dlq: 1, skipped: 1 },
  by_signal_source: { cognitive_item: 2, dlq: 1 },
  by_age_bucket: { lt_1d: 3 },
  technical_blocking_signals: 1,
  cognitive_pending_signals: 1,
  expired_revisit_skips: 0,
  open_canonical_debt: 0,
  technical_dlq: 1,
  terminal_history: 0,
};

function listResponse(
  items: CognitiveReadinessItem[],
  enforcement = false,
): CognitiveReadinessListResponse {
  return {
    board_id: 'b',
    items,
    summary: {
      by_signal: {},
      technical_blocking_signals: 1,
      cognitive_pending_signals: 1,
      enforcement_active: enforcement,
      total: items.length,
      limit: 200,
      offset: 0,
    },
    precedence: ['technical_dlq', 'canonical_debt_open', 'cognitive_active'],
  };
}

function mockList(items: CognitiveReadinessItem[], enforcement = false) {
  vi.spyOn(api, 'getReadinessItems').mockResolvedValue(listResponse(items, enforcement));
  vi.spyOn(api, 'getReadinessMetrics').mockResolvedValue(METRICS);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('CognitiveActionCenterView', () => {
  test('renderiza linhas, counters e painel de métricas bounded', async () => {
    mockList([COGNITIVE, DLQ, SKIPPED]);
    render(<CognitiveActionCenterView boardId="b" onClose={() => {}} />);

    await waitFor(() => expect(screen.getByTestId('cac-table')).toBeInTheDocument());
    expect(screen.getByText('card:cog1')).toBeInTheDocument();
    expect(screen.getByText('card:dlq1')).toBeInTheDocument();
    expect(screen.getByTestId('cac-counter-dlq')).toHaveTextContent('1');
    expect(screen.getByTestId('cac-metrics-panel')).toBeInTheDocument();
    // métrica bounded by_reason_code mostra label clamp, não free-text
    expect(screen.getByTestId('cac-metric-reason_code')).toHaveTextContent('trivial_fix');
  });

  test('technical blocker (DLQ) NÃO oferece skip e marca would_block_done', async () => {
    mockList([DLQ], true);
    render(<CognitiveActionCenterView boardId="b" onClose={() => {}} />);

    await waitFor(() => screen.getByText('card:dlq1'));
    const row = screen.getByText('card:dlq1').closest('tr')!;
    expect(within(row).getByTestId('cac-technical-no-skip')).toBeInTheDocument();
    expect(within(row).queryByTestId('cac-skip-toggle')).toBeNull();
    expect(within(row).getByTestId('cac-would-block-done')).toBeInTheDocument();
    // error_cause técnico exibido, reason_code cognitivo ausente
    expect(within(row).getByTestId('cac-error-cause')).toHaveTextContent('technical_dlq');
  });

  test('linha cognitiva pending NÃO mostra would_block_done quando advisory', async () => {
    mockList([COGNITIVE], false);
    render(<CognitiveActionCenterView boardId="b" onClose={() => {}} />);

    await waitFor(() => screen.getByText('card:cog1'));
    const row = screen.getByText('card:cog1').closest('tr')!;
    expect(within(row).queryByTestId('cac-would-block-done')).toBeNull();
    expect(within(row).getByTestId('cac-skip-toggle')).toBeInTheDocument();
    expect(screen.getByTestId('cac-enforcement')).toHaveTextContent(/advisory/i);
  });

  test('skip via write-path central refaz fetch', async () => {
    mockList([COGNITIVE]);
    const skipSpy = vi
      .spyOn(api, 'recordCognitiveSkip')
      .mockResolvedValue({} as never);
    render(<CognitiveActionCenterView boardId="b" onClose={() => {}} />);

    await waitFor(() => screen.getByText('card:cog1'));
    fireEvent.click(screen.getByTestId('cac-skip-toggle'));
    await waitFor(() => screen.getByTestId('cac-skip-form'));
    fireEvent.click(screen.getByTestId('cac-skip-confirm'));

    await waitFor(() => expect(skipSpy).toHaveBeenCalledTimes(1));
    expect(skipSpy).toHaveBeenCalledWith(
      'b',
      expect.objectContaining({ sourceRef: 'card:cog1', reasonCode: 'no_reusable_learning' }),
    );
  });

  test('skip 409 mostra erro sem mascarar blocker técnico', async () => {
    mockList([COGNITIVE]);
    vi.spyOn(api, 'recordCognitiveSkip').mockRejectedValue(
      new api.ReadinessActionError(
        'technical_debt_cannot_be_skipped',
        'Canonical debt is OPEN for this artifact.',
        409,
      ),
    );
    render(<CognitiveActionCenterView boardId="b" onClose={() => {}} />);

    await waitFor(() => screen.getByText('card:cog1'));
    fireEvent.click(screen.getByTestId('cac-skip-toggle'));
    await waitFor(() => screen.getByTestId('cac-skip-form'));
    fireEvent.click(screen.getByTestId('cac-skip-confirm'));

    await waitFor(() => expect(screen.getByTestId('cac-action-error')).toBeInTheDocument());
    expect(screen.getByTestId('cac-action-error')).toHaveTextContent(/canonical debt is open/i);
    expect(screen.getByTestId('cac-action-error')).toHaveTextContent(/technical blocker/i);
  });

  test('clear/reopen de skip válido chama o caminho central', async () => {
    mockList([SKIPPED]);
    const clearSpy = vi
      .spyOn(api, 'clearCognitiveSkip')
      .mockResolvedValue({} as never);
    render(<CognitiveActionCenterView boardId="b" onClose={() => {}} />);

    await waitFor(() => screen.getByText('card:skip1'));
    const row = screen.getByText('card:skip1').closest('tr')!;
    // aliases card/bug visíveis
    expect(within(row).getByTestId('cac-aliases')).toBeInTheDocument();
    fireEvent.click(within(row).getByTestId('cac-clear'));
    await waitFor(() => expect(clearSpy).toHaveBeenCalledWith('b', 'bug:skip1'));
  });

  test('empty state', async () => {
    mockList([]);
    render(<CognitiveActionCenterView boardId="b" onClose={() => {}} />);
    await waitFor(() => expect(screen.getByTestId('cac-empty-state')).toBeInTheDocument());
  });

  test('error state quando fetch falha', async () => {
    vi.spyOn(api, 'getReadinessItems').mockRejectedValue(new Error('Network down'));
    vi.spyOn(api, 'getReadinessMetrics').mockRejectedValue(new Error('Network down'));
    render(<CognitiveActionCenterView boardId="b" onClose={() => {}} />);
    await waitFor(() => expect(screen.getByTestId('cac-error')).toHaveTextContent(/network down/i));
  });

  test('filtro por signal refaz fetch com o signal selecionado', async () => {
    mockList([COGNITIVE]);
    render(<CognitiveActionCenterView boardId="b" onClose={() => {}} />);
    await waitFor(() => screen.getByText('card:cog1'));
    fireEvent.click(screen.getByTestId('cac-filter-dlq'));
    await waitFor(() =>
      expect(api.getReadinessItems).toHaveBeenCalledWith(
        'b',
        expect.objectContaining({ signal: 'dlq' }),
      ),
    );
  });
});
