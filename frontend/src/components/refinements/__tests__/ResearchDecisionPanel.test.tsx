import {
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  ResearchDecisionContent,
  ResearchDecisionEntry,
  ResearchDecisionPage,
  ResearchDecisionWriteResponse,
} from '@/types/research-decisions';
import { ResearchDecisionApiError } from '@/services/research-decisions-api';

const apiMock = vi.hoisted(() => ({
  listResearchDecisions: vi.fn(),
  appendResearchDecision: vi.fn(),
  supersedeResearchDecision: vi.fn(),
  resolveResearchDecisionHead: vi.fn(),
}));

const permissionsMock = vi.hoisted(() => ({
  has: vi.fn((_permission: string) => true),
  isLoading: false,
  error: null as Error | null,
  ownerReviewRequired: false,
}));

const toastMock = vi.hoisted(() => ({
  error: vi.fn(),
  success: vi.fn(),
}));

vi.mock('@/services/research-decisions-api', async () => {
  const actual = await vi.importActual<
    typeof import('@/services/research-decisions-api')
  >('@/services/research-decisions-api');
  return {
    ...actual,
    useResearchDecisionsApi: () => apiMock,
  };
});

vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    preset: 'Full Control',
    isLoading: permissionsMock.isLoading,
    error: permissionsMock.error,
    ownerReviewRequired: permissionsMock.ownerReviewRequired,
    has: permissionsMock.has,
  }),
}));

vi.mock('react-hot-toast', () => ({
  default: toastMock,
}));

import { ResearchDecisionPanel } from '../ResearchDecisionPanel';

const content: ResearchDecisionContent = {
  unknown: 'Which retry policy should be used?',
  status: 'open',
  anchor: {
    anchor_type: 'functional_requirement',
    anchor_ref: 'FR-1',
  },
  evidence_refs: [],
  alternatives: [],
  decision: null,
  rationale: null,
  confidence: null,
  evidence_absence_justification: null,
};

function entry(
  overrides: Partial<ResearchDecisionEntry> = {},
): ResearchDecisionEntry {
  return {
    id: 'entry-1',
    ledger_id: 'ledger-1',
    board_id: 'board-1',
    refinement_id: 'refinement-1',
    refinement_version: 2,
    predecessor_entry_id: null,
    content,
    content_digest: 'a'.repeat(64),
    created_by: 'user-1',
    created_at: '2026-07-28T12:00:00Z',
    ...overrides,
  };
}

function page(
  items: ResearchDecisionEntry[],
  overrides: Partial<ResearchDecisionPage> = {},
): ResearchDecisionPage {
  return {
    items,
    offset: 0,
    limit: 25,
    total_filtered: items.length,
    total_overall: items.length,
    ...overrides,
  };
}

function writeResponse(
  resolvedEntry = entry(),
  overrides: Partial<ResearchDecisionWriteResponse> = {},
): ResearchDecisionWriteResponse {
  return {
    entry_id: resolvedEntry.id,
    thread_id: resolvedEntry.ledger_id,
    sequence: 1,
    head_revision: 1,
    refinement_version: 3,
    replayed: false,
    entry: resolvedEntry,
    ...overrides,
  };
}

function renderPanel(
  overrides: Partial<React.ComponentProps<typeof ResearchDecisionPanel>> = {},
) {
  return render(
    <ResearchDecisionPanel
      boardId="board-1"
      refinementId="refinement-1"
      refinementStatus="draft"
      {...overrides}
    />,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  permissionsMock.isLoading = false;
  permissionsMock.error = null;
  permissionsMock.ownerReviewRequired = false;
  permissionsMock.has.mockImplementation((_permission: string) => true);
  apiMock.listResearchDecisions.mockResolvedValue(page([entry()]));
  apiMock.resolveResearchDecisionHead.mockResolvedValue({
    entry: entry(),
    headRevision: 1,
  });
  apiMock.appendResearchDecision.mockResolvedValue(writeResponse());
  apiMock.supersedeResearchDecision.mockResolvedValue(writeResponse());
});

describe('ResearchDecisionPanel', () => {
  it('fails the new read leaf closed while keeping legacy strings separate', () => {
    permissionsMock.has.mockImplementation(
      (permission: string) =>
        permission !== 'refinement.research_decisions.read',
    );

    renderPanel({
      legacyDecisions: ['Use the historical retry policy.'],
    });

    expect(
      screen.getByTestId('research-decision-read-denied'),
    ).toBeInTheDocument();
    expect(screen.getByTestId('legacy-refinement-decisions')).toHaveTextContent(
      'Historical read-only strings',
    );
    expect(apiMock.listResearchDecisions).not.toHaveBeenCalled();
  });

  it('honors the read and append leaves independently', async () => {
    permissionsMock.has.mockImplementation(
      (permission: string) =>
        permission === 'refinement.research_decisions.read',
    );

    renderPanel();

    expect(
      await screen.findByTestId('research-decision-entry-entry-1'),
    ).toBeInTheDocument();
    expect(screen.queryByText('New research thread')).not.toBeInTheDocument();
    expect(
      screen.getByText(/research_decisions\.append.*required/),
    ).toBeInTheDocument();
  });

  it('applies ledger/status filters and the frozen page sizes', async () => {
    apiMock.listResearchDecisions.mockResolvedValue(
      page([entry()], {
        total_filtered: 60,
        total_overall: 60,
      }),
    );
    renderPanel();
    await screen.findByTestId('research-decision-entry-entry-1');

    fireEvent.change(
      screen.getByPlaceholderText('Filter one immutable thread'),
      { target: { value: 'ledger-1' } },
    );
    fireEvent.click(
      screen.getByRole('button', { name: 'Apply research decision filters' }),
    );
    await waitFor(() =>
      expect(apiMock.listResearchDecisions).toHaveBeenLastCalledWith(
        'refinement-1',
        expect.objectContaining({
          offset: 0,
          limit: 25,
          ledgerId: 'ledger-1',
          status: null,
        }),
      ),
    );

    fireEvent.change(
      screen.getByRole('combobox', {
        name: 'Filter research decisions by status',
      }),
      { target: { value: 'resolved' } },
    );
    fireEvent.change(
      screen.getByRole('combobox', {
        name: 'Research decision page size',
      }),
      { target: { value: '50' } },
    );
    await waitFor(() =>
      expect(apiMock.listResearchDecisions).toHaveBeenLastCalledWith(
        'refinement-1',
        expect.objectContaining({
          offset: 0,
          limit: 50,
          ledgerId: 'ledger-1',
          status: 'resolved',
        }),
      ),
    );
  });

  it('appends a new thread with an idempotency key and revision-zero fence', async () => {
    const onVersionChanged = vi.fn();
    renderPanel({ onRefinementVersionChanged: onVersionChanged });
    await screen.findByTestId('research-decision-entry-entry-1');

    fireEvent.click(screen.getByText('New research thread'));
    fireEvent.change(screen.getByPlaceholderText('What remains unknown?'), {
      target: { value: 'Should the timeout be configurable?' },
    });
    fireEvent.change(screen.getByPlaceholderText('FR-1, AC-2, TR-3, Q6'), {
      target: { value: 'TR-9' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Append thread' }));

    await waitFor(() =>
      expect(apiMock.appendResearchDecision).toHaveBeenCalledTimes(1),
    );
    expect(apiMock.appendResearchDecision).toHaveBeenCalledWith(
      'refinement-1',
      expect.objectContaining({
        operation: 'append',
        expected_head_revision: 0,
        idempotency_key: expect.any(String),
        entry: expect.objectContaining({
          unknown: 'Should the timeout be configurable?',
          status: 'open',
          anchor: {
            anchor_type: 'functional_requirement',
            anchor_ref: 'TR-9',
          },
        }),
      }),
    );
    expect(onVersionChanged).toHaveBeenCalledWith(3);
  });

  it('reuses session idempotency keys by intent, including after switching back', async () => {
    apiMock.appendResearchDecision.mockRejectedValue(
      new Error('temporary transport failure'),
    );
    renderPanel();
    await screen.findByTestId('research-decision-entry-entry-1');

    fireEvent.click(screen.getByText('New research thread'));
    const unknown = screen.getByPlaceholderText('What remains unknown?');
    fireEvent.change(unknown, {
      target: { value: 'Should retry delay be configurable?' },
    });
    fireEvent.change(screen.getByPlaceholderText('FR-1, AC-2, TR-3, Q6'), {
      target: { value: 'TR-9' },
    });
    const submit = screen.getByRole('button', { name: 'Append thread' });

    fireEvent.click(submit);
    await waitFor(() =>
      expect(apiMock.appendResearchDecision).toHaveBeenCalledTimes(1),
    );
    const firstKey =
      apiMock.appendResearchDecision.mock.calls[0][1].idempotency_key;

    await screen.findByTestId('research-decision-write-error');
    fireEvent.click(submit);
    await waitFor(() =>
      expect(apiMock.appendResearchDecision).toHaveBeenCalledTimes(2),
    );
    expect(
      apiMock.appendResearchDecision.mock.calls[1][1].idempotency_key,
    ).toBe(firstKey);

    fireEvent.change(unknown, {
      target: { value: 'Should retry jitter be configurable?' },
    });
    fireEvent.click(submit);
    await waitFor(() =>
      expect(apiMock.appendResearchDecision).toHaveBeenCalledTimes(3),
    );
    expect(
      apiMock.appendResearchDecision.mock.calls[2][1].idempotency_key,
    ).not.toBe(firstKey);

    fireEvent.change(unknown, {
      target: { value: 'Should retry delay be configurable?' },
    });
    fireEvent.click(submit);
    await waitFor(() =>
      expect(apiMock.appendResearchDecision).toHaveBeenCalledTimes(4),
    );
    expect(
      apiMock.appendResearchDecision.mock.calls[3][1].idempotency_key,
    ).toBe(firstKey);
  });

  it('keeps a draft but hides every writer affordance when status changes', async () => {
    const view = renderPanel();
    await screen.findByTestId('research-decision-entry-entry-1');
    fireEvent.click(screen.getByText('New research thread'));
    fireEvent.change(screen.getByPlaceholderText('What remains unknown?'), {
      target: { value: 'Draft retained across status refresh' },
    });

    view.rerender(
      <ResearchDecisionPanel
        boardId="board-1"
        refinementId="refinement-1"
        refinementStatus="review"
      />,
    );
    expect(
      screen.queryByPlaceholderText('What remains unknown?'),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/only be changed while the Refinement is an/))
      .toBeInTheDocument();

    view.rerender(
      <ResearchDecisionPanel
        boardId="board-1"
        refinementId="refinement-1"
        refinementStatus="draft"
      />,
    );
    expect(screen.getByDisplayValue('Draft retained across status refresh'))
      .toBeInTheDocument();
  });

  it('resolves the actual thread head before posting a supersede CAS', async () => {
    const visibleHistorical = entry({
      id: 'entry-old',
      predecessor_entry_id: null,
    });
    const current = entry({
      id: 'entry-current',
      predecessor_entry_id: 'entry-old',
      refinement_version: 5,
      content: {
        ...content,
        unknown: 'Current head question',
        status: 'investigating',
      },
    });
    apiMock.listResearchDecisions.mockResolvedValue(page([visibleHistorical]));
    apiMock.resolveResearchDecisionHead.mockResolvedValue({
      entry: current,
      headRevision: 4,
    });
    apiMock.supersedeResearchDecision.mockResolvedValue(
      writeResponse(current, {
        sequence: 5,
        head_revision: 5,
        refinement_version: 6,
      }),
    );
    renderPanel();
    await screen.findByTestId('research-decision-entry-entry-old');

    fireEvent.click(
      screen.getByRole('button', {
        name: 'Supersede current head of ledger ledger-1',
      }),
    );
    expect(await screen.findByDisplayValue('Current head question')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Supersede head' }));

    await waitFor(() =>
      expect(apiMock.supersedeResearchDecision).toHaveBeenCalledTimes(1),
    );
    expect(apiMock.resolveResearchDecisionHead).toHaveBeenCalledWith(
      'refinement-1',
      'ledger-1',
    );
    expect(apiMock.supersedeResearchDecision).toHaveBeenCalledWith(
      'refinement-1',
      expect.objectContaining({
        operation: 'supersede',
        expected_head_revision: 4,
        ledger_id: 'ledger-1',
        supersedes_entry_id: 'entry-current',
        idempotency_key: expect.any(String),
        entry: expect.objectContaining({
          unknown: 'Current head question',
          status: 'investigating',
        }),
      }),
    );
  });

  it('refreshes the CAS fence after a concurrent supersede conflict', async () => {
    const firstHead = entry({ id: 'entry-current' });
    const latestHead = entry({
      id: 'entry-concurrent',
      predecessor_entry_id: 'entry-current',
      refinement_version: 6,
    });
    apiMock.resolveResearchDecisionHead
      .mockResolvedValueOnce({ entry: firstHead, headRevision: 4 })
      .mockResolvedValueOnce({ entry: latestHead, headRevision: 5 });
    apiMock.supersedeResearchDecision.mockRejectedValue(
      new ResearchDecisionApiError({
        status: 409,
        code: 'version_conflict',
        message: 'version_conflict',
        retryable: true,
        details: {
          reason_code: 'research_decision_head_revision_conflict',
        },
      }),
    );
    renderPanel();
    await screen.findByTestId('research-decision-entry-entry-1');

    fireEvent.click(
      screen.getByRole('button', {
        name: 'Supersede current head of ledger ledger-1',
      }),
    );
    await screen.findByDisplayValue(content.unknown);
    fireEvent.click(screen.getByRole('button', { name: 'Supersede head' }));

    expect(
      await screen.findByTestId('research-decision-write-error'),
    ).toHaveTextContent('latest CAS head was loaded');
    expect(apiMock.resolveResearchDecisionHead).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByRole('button', { name: 'Supersede head' }));
    await waitFor(() =>
      expect(apiMock.supersedeResearchDecision).toHaveBeenLastCalledWith(
        'refinement-1',
        expect.objectContaining({
          expected_head_revision: 5,
          supersedes_entry_id: 'entry-concurrent',
        }),
      ),
    );
  });

  it('does not treat another retryable 409 as a head-revision CAS retry', async () => {
    apiMock.supersedeResearchDecision.mockRejectedValue(
      new ResearchDecisionApiError({
        status: 409,
        code: 'version_conflict',
        message: 'The predecessor entry no longer matches.',
        retryable: true,
        details: {
          reason_code: 'research_decision_head_entry_conflict',
        },
      }),
    );
    renderPanel();
    await screen.findByTestId('research-decision-entry-entry-1');
    fireEvent.click(
      screen.getByRole('button', {
        name: 'Supersede current head of ledger ledger-1',
      }),
    );
    const draft = await screen.findByDisplayValue(content.unknown);
    fireEvent.click(screen.getByRole('button', { name: 'Supersede head' }));

    expect(await screen.findByTestId('research-decision-write-error'))
      .toHaveTextContent('The predecessor entry no longer matches.');
    expect(apiMock.resolveResearchDecisionHead).toHaveBeenCalledTimes(1);
    expect(draft).toBeInTheDocument();
  });
});
