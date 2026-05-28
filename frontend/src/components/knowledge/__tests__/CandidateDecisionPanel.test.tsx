/**
 * CandidateDecisionPanel tests — KG-03A.6.
 *
 * Coverage:
 *   - ONE batch HTTP request via useCandidateDecisions per (boardId)
 *   - Empty / loading / error renderings
 *   - proposed → exposes 4 actions; terminal → no actions
 *   - Promote command POSTs the right body and refreshes the list
 *   - Validation: spec_id required, reason_code required
 *   - Status counts render
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';

import { CandidateDecisionPanel } from '../CandidateDecisionPanel';
import * as api from '@/services/candidate-decisions-api';
import type {
  CandidateDecisionItem,
  ListCandidateDecisionsResponse,
} from '@/services/candidate-decisions-api';

vi.mock('@/services/candidate-decisions-api', async () => {
  const actual = await vi.importActual<typeof api>(
    '@/services/candidate-decisions-api',
  );
  return {
    ...actual,
    listCandidateDecisions: vi.fn(),
    submitCandidateDecisionCommand: vi.fn(),
  };
});

const BOARD_ID = 'board-kg03a6';

function buildItem(
  overrides: Partial<CandidateDecisionItem> = {},
): CandidateDecisionItem {
  return {
    candidate_id: 'cand_abc',
    board_id: BOARD_ID,
    source_ref: 'spec:s1',
    source_generation_id:
      '11111111-2222-4333-8444-555555555555',
    consolidation_session_id: 'sess_test_session',
    title: 'Adopt SQLite WAL for analytics ledger',
    rationale: 'Concurrent writes are bottlenecked.',
    evidence_refs: [],
    status: 'proposed',
    created_by_agent_id: 'agent-test',
    created_at: '2026-05-26T00:00:00Z',
    updated_at: '2026-05-26T00:00:00Z',
    formal_decision_ref: null,
    dismissed_reason_code: null,
    audit_ref: null,
    ...overrides,
  };
}

function buildResponse(
  items: CandidateDecisionItem[],
): ListCandidateDecisionsResponse {
  return {
    board_id: BOARD_ID,
    readonly: true,
    counts: {
      proposed: items.filter((i) => i.status === 'proposed').length,
      promoted: items.filter((i) => i.status === 'promoted').length,
      linked: items.filter((i) => i.status === 'linked').length,
      dismissed: items.filter((i) => i.status === 'dismissed').length,
      no_action_required: items.filter(
        (i) => i.status === 'no_action_required',
      ).length,
      total: items.length,
    },
    items,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  cleanup();
});

describe('CandidateDecisionPanel', () => {
  it('returns null when boardId is missing', () => {
    const { container } = render(<CandidateDecisionPanel boardId={null} />);
    expect(container.firstChild).toBeNull();
    expect(api.listCandidateDecisions).not.toHaveBeenCalled();
  });

  it('issues one batched GET per boardId mount', async () => {
    vi.mocked(api.listCandidateDecisions).mockResolvedValue(
      buildResponse([buildItem()]),
    );
    render(<CandidateDecisionPanel boardId={BOARD_ID} />);
    await waitFor(() => {
      expect(api.listCandidateDecisions).toHaveBeenCalledTimes(1);
    });
    expect(api.listCandidateDecisions).toHaveBeenCalledWith(
      BOARD_ID,
      expect.objectContaining({ status: null }),
      expect.any(AbortSignal),
    );
  });

  it('renders the empty placeholder when the list is empty', async () => {
    vi.mocked(api.listCandidateDecisions).mockResolvedValue(
      buildResponse([]),
    );
    render(<CandidateDecisionPanel boardId={BOARD_ID} />);
    await waitFor(() => {
      expect(
        screen.getByText('No candidate decisions for this board.'),
      ).toBeInTheDocument();
    });
    expect(
      screen.queryByTestId('candidate-decision-actions'),
    ).not.toBeInTheDocument();
  });

  it('renders the error message when the GET fails', async () => {
    vi.mocked(api.listCandidateDecisions).mockRejectedValue(
      new Error('boom'),
    );
    render(<CandidateDecisionPanel boardId={BOARD_ID} />);
    const err = await screen.findByTestId('candidate-decision-error');
    expect(err.textContent).toContain('boom');
  });

  it('shows four explicit actions for a proposed candidate', async () => {
    vi.mocked(api.listCandidateDecisions).mockResolvedValue(
      buildResponse([buildItem()]),
    );
    render(<CandidateDecisionPanel boardId={BOARD_ID} />);
    const actions = await screen.findByTestId('candidate-decision-actions');
    expect(actions.textContent).toContain('Promote');
    expect(actions.textContent).toContain('Link');
    expect(actions.textContent).toContain('Dismiss');
    expect(actions.textContent).toContain('No action');
  });

  it('hides actions for a terminal (promoted) candidate', async () => {
    vi.mocked(api.listCandidateDecisions).mockResolvedValue(
      buildResponse([
        buildItem({
          status: 'promoted',
          formal_decision_ref: 'dec_99',
        }),
      ]),
    );
    render(<CandidateDecisionPanel boardId={BOARD_ID} />);
    await waitFor(() => {
      expect(
        screen.queryByTestId('candidate-decision-actions'),
      ).not.toBeInTheDocument();
    });
    const ref = screen.getByTestId('candidate-decision-formal-ref');
    expect(ref.textContent).toBe('dec_99');
  });

  it('hides actions for a dismissed candidate', async () => {
    vi.mocked(api.listCandidateDecisions).mockResolvedValue(
      buildResponse([
        buildItem({
          status: 'dismissed',
          dismissed_reason_code: 'duplicate',
        }),
      ]),
    );
    render(<CandidateDecisionPanel boardId={BOARD_ID} />);
    await waitFor(() => {
      expect(
        screen.queryByTestId('candidate-decision-actions'),
      ).not.toBeInTheDocument();
    });
  });

  it('renders the status-count line for the panel', async () => {
    vi.mocked(api.listCandidateDecisions).mockResolvedValue(
      buildResponse([
        buildItem({ candidate_id: 'cand_1' }),
        buildItem({
          candidate_id: 'cand_2',
          status: 'promoted',
          formal_decision_ref: 'dec_x',
        }),
      ]),
    );
    render(<CandidateDecisionPanel boardId={BOARD_ID} />);
    const counts = await screen.findByTestId('candidate-decision-counts');
    expect(counts.textContent).toContain('1 proposed');
    expect(counts.textContent).toContain('1 promoted');
    expect(counts.textContent).toContain('2 total');
  });

  it('promote action requires spec_id before submit', async () => {
    vi.mocked(api.listCandidateDecisions).mockResolvedValue(
      buildResponse([buildItem()]),
    );
    render(<CandidateDecisionPanel boardId={BOARD_ID} />);
    fireEvent.click(await screen.findByText('Promote'));
    fireEvent.click(screen.getByTestId('candidate-decision-command-submit'));
    const err = await screen.findByTestId('candidate-decision-command-error');
    expect(err.textContent).toContain('spec_id');
    expect(api.submitCandidateDecisionCommand).not.toHaveBeenCalled();
  });

  it('promote action POSTs the right body and refreshes the list', async () => {
    vi.mocked(api.listCandidateDecisions).mockResolvedValue(
      buildResponse([buildItem()]),
    );
    vi.mocked(api.submitCandidateDecisionCommand).mockResolvedValue({
      candidate_id: 'cand_abc',
      board_id: BOARD_ID,
      action: 'promote',
      status: 'promoted',
      formal_decision_ref: 'dec_42',
      formal_decision: { id: 'dec_42' },
      dismissed_reason_code: null,
      audit_ref: 'audit_cmd_xyz',
      updated_at: '2026-05-26T00:01:00Z',
    });
    render(<CandidateDecisionPanel boardId={BOARD_ID} />);
    fireEvent.click(await screen.findByText('Promote'));
    fireEvent.change(screen.getByTestId('candidate-decision-spec-id'), {
      target: { value: 'spec_target' },
    });
    fireEvent.click(screen.getByTestId('candidate-decision-command-submit'));
    await waitFor(() => {
      expect(api.submitCandidateDecisionCommand).toHaveBeenCalledTimes(1);
    });
    expect(api.submitCandidateDecisionCommand).toHaveBeenCalledWith(
      'cand_abc',
      expect.objectContaining({
        board_id: BOARD_ID,
        action: 'promote_to_spec_decision',
        spec_id: 'spec_target',
      }),
    );
    // After success the panel refreshes — second call to GET.
    await waitFor(() => {
      expect(api.listCandidateDecisions).toHaveBeenCalledTimes(2);
    });
  });

  it('dismiss action requires reason_code', async () => {
    vi.mocked(api.listCandidateDecisions).mockResolvedValue(
      buildResponse([buildItem()]),
    );
    render(<CandidateDecisionPanel boardId={BOARD_ID} />);
    fireEvent.click(await screen.findByText('Dismiss'));
    fireEvent.click(screen.getByTestId('candidate-decision-command-submit'));
    const err = await screen.findByTestId('candidate-decision-command-error');
    expect(err.textContent).toContain('reason_code');
    expect(api.submitCandidateDecisionCommand).not.toHaveBeenCalled();
  });

  it('link_existing_decision action requires both spec_id and formal_decision_id', async () => {
    vi.mocked(api.listCandidateDecisions).mockResolvedValue(
      buildResponse([buildItem()]),
    );
    vi.mocked(api.submitCandidateDecisionCommand).mockResolvedValue({
      candidate_id: 'cand_abc',
      board_id: BOARD_ID,
      action: 'link_existing',
      status: 'linked',
      formal_decision_ref: 'dec_old',
      formal_decision: null,
      dismissed_reason_code: null,
      audit_ref: 'audit_cmd_link',
      updated_at: '2026-05-26T00:02:00Z',
    });
    render(<CandidateDecisionPanel boardId={BOARD_ID} />);
    fireEvent.click(await screen.findByText('Link'));
    fireEvent.change(screen.getByTestId('candidate-decision-spec-id'), {
      target: { value: 'spec_target' },
    });
    fireEvent.change(
      screen.getByTestId('candidate-decision-formal-decision-id'),
      { target: { value: 'dec_old' } },
    );
    fireEvent.click(screen.getByTestId('candidate-decision-command-submit'));
    await waitFor(() => {
      expect(api.submitCandidateDecisionCommand).toHaveBeenCalledWith(
        'cand_abc',
        expect.objectContaining({
          action: 'link_existing_decision',
          spec_id: 'spec_target',
          formal_decision_id: 'dec_old',
        }),
      );
    });
  });
});
