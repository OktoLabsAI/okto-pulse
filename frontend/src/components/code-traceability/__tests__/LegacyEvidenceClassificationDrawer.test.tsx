import { useRef, useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';

import { AuthenticatedFetchError } from '@/lib/authFetch';
import { LegacyEvidenceClassificationConflictError } from '@/services/api';
import type {
  LegacyEvidenceClassificationBatchRequest,
  LegacyEvidenceClassificationBatchResult,
} from '@/types';
import {
  LegacyEvidenceClassificationDrawer,
  type LegacyEvidenceClassificationDrawerProps,
  type LegacyEvidenceClassificationSnapshot,
} from '../LegacyEvidenceClassificationDrawer';

const result: LegacyEvidenceClassificationBatchResult = {
  batch_id: 'batch-1',
  board_id: 'board-1',
  classified_by: 'operator-1',
  classified_at: '2026-08-22T18:00:00Z',
  request_sha256: 'sha256:request-1',
  classifications: [],
  replayed: false,
};

const snapshot: LegacyEvidenceClassificationSnapshot = {
  classificationInputs: [
    {
      evidence_id: 'evidence-1',
      expected_evidence_payload_sha256: 'a'.repeat(64),
      expected_classification_revision: 2,
      baseline_provenance: {
        presence: 'committed_snapshot',
        workspace_state_id: 'sha256:workspace-1',
        provenance_note: null,
        provenance_note_required: false,
      },
    },
    {
      evidence_id: 'evidence-2',
      expected_evidence_payload_sha256: 'b'.repeat(64),
      expected_classification_revision: 0,
      baseline_provenance: {
        presence: 'preexisting_worktree',
        workspace_state_id: 'sha256:workspace-2',
        provenance_note: null,
        provenance_note_required: true,
      },
    },
  ],
  effectiveItems: [{
    evidence_id: 'evidence-1',
    source_role: 'current_implementation',
    relevance_summary: 'Previously classified relevance.',
    scope_relation: 'Previously classified relation.',
    source_origin: 'Previously classified origin.',
    interpretation_limit: null,
    baseline_provenance: {
      presence: 'committed_snapshot',
      workspace_state_id: 'sha256:workspace-1',
      provenance_note: null,
    },
    context_origin: 'human_legacy_classification',
    context_contract_version: 2,
    evidence_applicable: true,
    classification_revision: 2,
  }],
  evidence: [
    {
      id: 'evidence-1',
      investigation_receipt_id: 'receipt-1',
      source_ref: 'repository:payments',
      parent_type: 'refinement',
      parent_id: 'refinement-1',
      parent_version: 4,
      evidence_type: 'code_observation',
      claim: 'Existing payment authorization behavior.',
      selector_kind: 'qualified_symbol',
      relative_path: 'src/payments/authorize.ts',
      language: 'typescript',
      symbol_kind: 'function',
      qualified_symbol: 'authorizePayment',
      attestation_state: 'agent_attested',
      lifecycle_status: 'active',
      supersedes_evidence_id: null,
    },
    {
      id: 'evidence-2',
      investigation_receipt_id: 'receipt-2',
      source_ref: 'repository:payments',
      parent_type: 'refinement',
      parent_id: 'refinement-1',
      parent_version: 4,
      evidence_type: 'code_observation',
      claim: 'Generated payment service scaffold.',
      selector_kind: 'relative_path',
      relative_path: 'src/payments/service.ts',
      language: 'typescript',
      symbol_kind: null,
      qualified_symbol: null,
      attestation_state: 'agent_attested',
      lifecycle_status: 'active',
      supersedes_evidence_id: null,
    },
  ],
};

function oneItemSnapshot(): LegacyEvidenceClassificationSnapshot {
  return {
    classificationInputs: [snapshot.classificationInputs[0]],
    effectiveItems: [snapshot.effectiveItems[0]],
    evidence: [snapshot.evidence[0]],
  };
}

function defaultProps(
  overrides: Partial<LegacyEvidenceClassificationDrawerProps> = {},
): LegacyEvidenceClassificationDrawerProps {
  return {
    snapshot,
    canClassify: true,
    onClose: vi.fn(),
    onApplyBatch: vi.fn().mockResolvedValue(result),
    onCanonicalRefetch: vi.fn().mockResolvedValue(snapshot),
    createIdempotencyKey: vi.fn()
      .mockReturnValueOnce('classification-key-1')
      .mockReturnValueOnce('classification-key-2'),
    ...overrides,
  };
}

function fillItem(
  index: number,
  role: 'current_implementation' | 'existing_scaffold' | 'reference_pattern',
) {
  fireEvent.click(screen.getByTestId(`legacy-classification-item-${index}`));
  const roleLabel = {
    current_implementation: /^Existing implementation/,
    existing_scaffold: /^Existing scaffold/,
    reference_pattern: /^Reference pattern/,
  }[role];
  fireEvent.click(within(
    screen.getByRole('group', { name: `Source role for evidence ${index}` }),
  ).getByRole('radio', { name: roleLabel }));
  fireEvent.change(screen.getByLabelText(`Relevance summary for evidence ${index}`), {
    target: { value: `Relevance ${index}` },
  });
  fireEvent.change(screen.getByLabelText(`Scope relation for evidence ${index}`), {
    target: { value: `Scope relation ${index}` },
  });
  fireEvent.change(screen.getByLabelText(`Source origin for evidence ${index}`), {
    target: { value: `Source origin ${index}` },
  });
  if (role === 'existing_scaffold' || role === 'reference_pattern') {
    fireEvent.change(screen.getByLabelText(`Interpretation limit for evidence ${index}`), {
      target: { value: `Interpretation limit ${index}` },
    });
  }
}

function fillOneItemAndReview() {
  fillItem(1, 'current_implementation');
  fireEvent.change(screen.getByLabelText('Classification justification'), {
    target: { value: 'Reviewed against the frozen baseline.' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Review batch' }));
}

function NestedDrawerHarness() {
  const [open, setOpen] = useState(false);
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Parent refinement"
      data-testid="parent-refinement-dialog"
    >
      <button type="button" onClick={() => setOpen(true)}>Open classification</button>
      {open && (
        <LegacyEvidenceClassificationDrawer
          {...defaultProps()}
          opener={document.activeElement as HTMLElement | null}
          onClose={() => setOpen(false)}
        />
      )}
    </div>
  );
}

function RemovedOpenerHarness() {
  const [open, setOpen] = useState(false);
  const [opener, setOpener] = useState<HTMLElement | null>(null);
  const fallbackRef = useRef<HTMLHeadingElement>(null);
  return (
    <div>
      <h2 ref={fallbackRef} tabIndex={-1}>Code evidence fallback</h2>
      {!open && (
        <button
          type="button"
          onClick={(event) => {
            setOpener(event.currentTarget);
            setOpen(true);
          }}
        >
          Open and remove opener
        </button>
      )}
      {open && (
        <LegacyEvidenceClassificationDrawer
          {...defaultProps({ snapshot: oneItemSnapshot() })}
          opener={opener}
          focusFallback={fallbackRef.current}
          onClose={() => setOpen(false)}
        />
      )}
    </div>
  );
}

describe('LegacyEvidenceClassificationDrawer', () => {
  afterEach(() => {
    cleanup();
    document.body.style.overflow = '';
  });

  it('ts_bd449ee2 — uses a responsive portal, masks its parent, traps focus and restores the opener on Escape', async () => {
    const { container } = render(<NestedDrawerHarness />);
    const opener = screen.getByRole('button', { name: 'Open classification' });
    opener.focus();
    fireEvent.click(opener);

    const drawer = await screen.findByRole('dialog', {
      name: 'Classify legacy Evidence',
    });
    const parent = screen.getByTestId('parent-refinement-dialog');
    expect(drawer).toHaveAttribute('aria-modal', 'true');
    expect(drawer).toHaveAttribute('aria-describedby');
    expect(drawer).toHaveClass('w-full', 'max-w-2xl');
    expect(drawer.querySelector('footer')).toHaveClass('flex-col', 'sm:flex-row');
    expect(drawer.querySelector('footer')).not.toHaveClass('flex-col-reverse');
    expect(screen.getByText('Classify').closest('li')).toHaveAttribute('aria-current', 'step');
    expect(parent).toHaveAttribute('aria-hidden', 'true');
    expect(parent).not.toHaveAttribute('aria-modal');
    expect(container.querySelector('[data-testid="legacy-evidence-classification-drawer"]')).toBeNull();
    expect(document.body.style.overflow).toBe('hidden');

    const close = screen.getByRole('button', { name: 'Close legacy evidence classification' });
    await waitFor(() => expect(close).toHaveFocus());
    fireEvent.keyDown(drawer, { key: 'Tab', shiftKey: true });
    expect(screen.getByRole('button', { name: 'Review batch' })).toHaveFocus();

    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: 'Classify legacy Evidence' }))
        .not.toBeInTheDocument();
    });
    expect(parent).toHaveAttribute('aria-modal', 'true');
    expect(parent).not.toHaveAttribute('aria-hidden');
    expect(opener).toHaveFocus();
    expect(document.body.style.overflow).toBe('');
  });

  it('ts_4822298b — preserves an audited role for reclassification and leaves unclassified legacy blank', () => {
    render(<LegacyEvidenceClassificationDrawer {...defaultProps()} />);

    const firstRoles = screen.getByRole('group', { name: 'Source role for evidence 1' });
    expect(within(firstRoles).getByRole('radio', { name: /^Existing implementation/ }))
      .toBeChecked();
    expect(screen.getByDisplayValue('Previously classified relevance.')).toBeInTheDocument();
    expect(screen.getByText('Committed snapshot')).toBeInTheDocument();
    expect(screen.getByText(
      'Add explicit delivery meaning to historical observations. Nothing in the original Evidence will be edited.',
    )).toBeInTheDocument();
    expect(screen.getByText('Observed claim')).toBeInTheDocument();
    expect(screen.getByText('Origin:')).toBeInTheDocument();
    expect(screen.getByText('Source investigation')).toBeInTheDocument();
    expect(screen.getByText('Observed workspace')).toBeInTheDocument();
    expect(screen.getAllByText('Investigation baseline')).toHaveLength(2);
    for (const value of ['evidence-1', 'sha256:workspace-1']) {
      expect(screen.getAllByText(value).every((node) => node.closest('details') !== null)).toBe(true);
    }
    expect(screen.getAllByText('Technical details')).toHaveLength(1);
    expect(screen.queryByLabelText('Interpretation limit for evidence 1')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Baseline provenance note for evidence 1')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('legacy-classification-item-2'));
    const secondRoles = screen.getByRole('group', { name: 'Source role for evidence 2' });
    expect(within(secondRoles).getAllByRole('radio').every((role) => (
      !(role as HTMLInputElement).checked
    ))).toBe(true);
    expect(screen.getByText('Pre-existing worktree')).toBeInTheDocument();
    expect(screen.getByLabelText('Baseline provenance note for evidence 2')).toBeInTheDocument();
    expect(screen.queryByRole('group', { name: 'Source role for evidence 1' }))
      .not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('legacy-classification-item-1'));
    const restoredFirstRoles = screen.getByRole('group', { name: 'Source role for evidence 1' });
    fireEvent.click(within(restoredFirstRoles).getByRole('radio', { name: /^Existing scaffold/ }));
    expect(screen.getByLabelText('Interpretation limit for evidence 1')).toBeInTheDocument();
    fireEvent.click(within(restoredFirstRoles).getByRole('radio', { name: /^Existing constraint/ }));
    expect(screen.queryByLabelText('Interpretation limit for evidence 1')).not.toBeInTheDocument();
  });

  it('ts_4822298b — keeps a focused batch navigator and follows the normative human-first copy', () => {
    render(<LegacyEvidenceClassificationDrawer {...defaultProps()} />);

    expect(screen.getByRole('navigation', { name: 'Evidence selected for classification' }))
      .toBeInTheDocument();
    expect(screen.getAllByRole('group', { name: /Source role for evidence/ })).toHaveLength(1);
    expect(screen.getByText('What does this observation represent?')).toBeInTheDocument();
    expect(screen.getByText('Current behavior that already delivers the scope.')).toBeInTheDocument();
    expect(screen.getByText('Reusable structure that does not prove delivery.')).toBeInTheDocument();
    expect(screen.getByText('A boundary the solution must respect.')).toBeInTheDocument();
    expect(screen.getByText('A comparison that does not prove this scope exists.')).toBeInTheDocument();
    expect(screen.getByText('Why is it relevant?')).toBeInTheDocument();
    expect(screen.getByText('Relationship to this scope')).toBeInTheDocument();
    expect(screen.getByText('Your changes are not saved until you review and apply the complete batch.'))
      .toBeInTheDocument();

    fireEvent.click(screen.getByTestId('legacy-classification-item-2'));
    expect(screen.getByTestId('legacy-classification-item-2')).toHaveAttribute('aria-current', 'step');
    expect(screen.getByRole('group', { name: 'Source role for evidence 2' })).toBeInTheDocument();
    expect(screen.queryByRole('group', { name: 'Source role for evidence 1' }))
      .not.toBeInTheDocument();
  });

  it.each(['Cancel', 'Escape'] as const)(
    'ts_bd449ee2 — restores a deterministic fallback when %s removes the opener',
    async (closeMethod) => {
      render(<RemovedOpenerHarness />);
      fireEvent.click(screen.getByRole('button', { name: 'Open and remove opener' }));
      await screen.findByRole('dialog', { name: 'Classify legacy Evidence' });

      if (closeMethod === 'Cancel') {
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      } else {
        fireEvent.keyDown(document, { key: 'Escape' });
      }

      await waitFor(() => expect(screen.getByRole('heading', { name: 'Code evidence fallback' }))
        .toHaveFocus());
    },
  );

  it('ts_4822298b / ts_15a6a9dc — validates conditional fields, freezes Review and applies one atomic batch', async () => {
    const onApplyBatch = vi.fn().mockResolvedValue(result);
    const onCanonicalRefetch = vi.fn().mockResolvedValue(snapshot);
    const onApplied = vi.fn();
    const onClose = vi.fn();
    render(<LegacyEvidenceClassificationDrawer {...defaultProps({
      onApplyBatch,
      onCanonicalRefetch,
      onApplied,
      onClose,
    })} />);

    fireEvent.click(screen.getByRole('button', { name: 'Review batch' }));
    fireEvent.click(screen.getByTestId('legacy-classification-item-2'));
    expect(screen.getByText('Choose how this source may be interpreted.')).toBeInTheDocument();
    expect(screen.getByText('A governance justification is required.')).toBeInTheDocument();

    fillItem(1, 'current_implementation');
    fillItem(2, 'existing_scaffold');
    fireEvent.change(screen.getByLabelText('Baseline provenance note for evidence 2'), {
      target: { value: 'The scaffold was present in the frozen worktree.' },
    });
    fireEvent.change(screen.getByLabelText('Classification justification'), {
      target: { value: 'Reviewed against the frozen baseline.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Review batch' }));

    const review = screen.getByTestId('legacy-classification-review-step');
    expect(within(review).queryByRole('textbox')).not.toBeInTheDocument();
    expect(within(review).queryByRole('radio')).not.toBeInTheDocument();
    expect(within(review).getByText(/This adds an audited classification\. The original Evidence remains unchanged\./i))
      .toBeInTheDocument();
    expect(within(review).getByText(/one atomic batch/i)).toBeInTheDocument();
    expect(screen.getByText('Review').closest('li')).toHaveAttribute('aria-current', 'step');
    expect(within(review).getAllByText('evidence-1').every(
      (node) => node.closest('details') !== null,
    )).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: 'Apply classification' }));
    await waitFor(() => expect(onApplyBatch).toHaveBeenCalledTimes(1));
    const [request, signal] = onApplyBatch.mock.calls[0] as [
      LegacyEvidenceClassificationBatchRequest,
      AbortSignal,
    ];
    expect(signal).toBeInstanceOf(AbortSignal);
    expect(request).toMatchObject({
      idempotency_key: 'classification-key-1',
      justification: 'Reviewed against the frozen baseline.',
      items: [
        {
          evidence_id: 'evidence-1',
          source_role: 'current_implementation',
          interpretation_limit: null,
          baseline_provenance: {
            presence: 'committed_snapshot',
            workspace_state_id: 'sha256:workspace-1',
            provenance_note: null,
          },
        },
        {
          evidence_id: 'evidence-2',
          source_role: 'existing_scaffold',
          interpretation_limit: 'Interpretation limit 2',
          baseline_provenance: {
            presence: 'preexisting_worktree',
            workspace_state_id: 'sha256:workspace-2',
            provenance_note: 'The scaffold was present in the frozen worktree.',
          },
        },
      ],
    });
    await waitFor(() => expect(onCanonicalRefetch).toHaveBeenCalledTimes(1));
    expect(onApplied).toHaveBeenCalledWith(result);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('ts_bd449ee2 — moves focus deterministically across Classify and Review step transitions', async () => {
    render(<LegacyEvidenceClassificationDrawer {...defaultProps({
      snapshot: oneItemSnapshot(),
    })} />);
    fillOneItemAndReview();

    const review = await screen.findByTestId('legacy-classification-review-step');
    await waitFor(() => expect(review).toHaveFocus());
    fireEvent.click(screen.getByRole('button', { name: 'Back to classify' }));

    const classify = await screen.findByTestId('legacy-classification-classify-step');
    const focusedItem = classify.querySelector<HTMLElement>('[data-legacy-step-focus="classify"]');
    await waitFor(() => expect(focusedItem).toHaveFocus());
  });

  it('ts_8b303869 — retries a network-ambiguous submission with the exact request and no pre-refetch', async () => {
    const onApplyBatch = vi.fn()
      .mockRejectedValueOnce(new Error('The connection closed before a response arrived.'))
      .mockResolvedValueOnce(result);
    const onCanonicalRefetch = vi.fn().mockResolvedValue(oneItemSnapshot());
    render(<LegacyEvidenceClassificationDrawer {...defaultProps({
      snapshot: oneItemSnapshot(),
      onApplyBatch,
      onCanonicalRefetch,
    })} />);
    fillOneItemAndReview();

    fireEvent.click(screen.getByRole('button', { name: 'Apply classification' }));
    expect(await screen.findByText('Submission outcome not confirmed')).toBeInTheDocument();
    expect(onCanonicalRefetch).not.toHaveBeenCalled();
    const firstRequest = onApplyBatch.mock.calls[0][0] as LegacyEvidenceClassificationBatchRequest;

    fireEvent.click(screen.getByRole('button', { name: 'Retry exact batch' }));
    await waitFor(() => expect(onApplyBatch).toHaveBeenCalledTimes(2));
    const secondRequest = onApplyBatch.mock.calls[1][0] as LegacyEvidenceClassificationBatchRequest;
    expect(secondRequest).toBe(firstRequest);
    expect(JSON.stringify(secondRequest)).toBe(JSON.stringify(firstRequest));
    expect(secondRequest.idempotency_key).toBe('classification-key-1');
    await waitFor(() => expect(onCanonicalRefetch).toHaveBeenCalledTimes(1));
  });

  it('ts_8b303869 — rotates the reviewed key when the operator edits after a network-ambiguous outcome', async () => {
    const onApplyBatch = vi.fn()
      .mockRejectedValueOnce(new Error('The connection closed before a response arrived.'))
      .mockResolvedValueOnce(result);
    const onCanonicalRefetch = vi.fn().mockResolvedValue(oneItemSnapshot());
    render(<LegacyEvidenceClassificationDrawer {...defaultProps({
      snapshot: oneItemSnapshot(),
      onApplyBatch,
      onCanonicalRefetch,
    })} />);
    fillOneItemAndReview();

    fireEvent.click(screen.getByRole('button', { name: 'Apply classification' }));
    await screen.findByText('Submission outcome not confirmed');
    const firstRequest = onApplyBatch.mock.calls[0][0] as LegacyEvidenceClassificationBatchRequest;

    fireEvent.click(screen.getByRole('button', { name: 'Edit and review again' }));
    expect(screen.getByDisplayValue('Relevance 1')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Relevance summary for evidence 1'), {
      target: { value: 'Edited relevance after the interrupted response.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Review batch' }));
    fireEvent.click(screen.getByRole('button', { name: 'Apply classification' }));

    await waitFor(() => expect(onApplyBatch).toHaveBeenCalledTimes(2));
    const secondRequest = onApplyBatch.mock.calls[1][0] as LegacyEvidenceClassificationBatchRequest;
    expect(secondRequest).not.toBe(firstRequest);
    expect(secondRequest.idempotency_key).toBe('classification-key-2');
    expect(secondRequest.items[0].relevance_summary).toBe(
      'Edited relevance after the interrupted response.',
    );
    await waitFor(() => expect(onCanonicalRefetch).toHaveBeenCalledTimes(1));
  });

  it.each([
    'code_evidence_legacy_classification_payload_conflict',
    'code_evidence_legacy_classification_revision_conflict',
  ] as const)('ts_8b303869 — handles typed canonical 409 %s with one refresh, preserved draft and a new key', async (code) => {
    const transportError = new AuthenticatedFetchError({
      message: 'The Evidence payload changed.',
      status: 409,
      code,
    });
    const conflict = new LegacyEvidenceClassificationConflictError(
      code,
      transportError,
    );
    const refreshedSnapshot: LegacyEvidenceClassificationSnapshot = {
      ...oneItemSnapshot(),
      classificationInputs: [{
        ...oneItemSnapshot().classificationInputs[0],
        expected_evidence_payload_sha256: 'c'.repeat(64),
        expected_classification_revision: 3,
      }],
    };
    const onApplyBatch = vi.fn()
      .mockRejectedValueOnce(conflict)
      .mockResolvedValueOnce(result);
    const onCanonicalRefetch = vi.fn()
      .mockResolvedValueOnce(refreshedSnapshot)
      .mockResolvedValueOnce(refreshedSnapshot);
    render(<LegacyEvidenceClassificationDrawer {...defaultProps({
      snapshot: oneItemSnapshot(),
      onApplyBatch,
      onCanonicalRefetch,
    })} />);
    fillOneItemAndReview();

    fireEvent.click(screen.getByRole('button', { name: 'Apply classification' }));
    expect(await screen.findByText('The canonical Evidence changed after review')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Apply classification' })).not.toBeInTheDocument();
    const firstRequest = onApplyBatch.mock.calls[0][0] as LegacyEvidenceClassificationBatchRequest;

    fireEvent.click(screen.getByRole('button', { name: 'Refresh canonical context' }));
    await waitFor(() => expect(onCanonicalRefetch).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/Review the preserved draft again/i)).toBeInTheDocument();
    expect(screen.getByDisplayValue('Relevance 1')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Scope relation 1')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Apply classification' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Review batch' })).toBeInTheDocument();

    fireEvent.click(screen.getByText('Technical details'));
    expect(screen.getByText('c'.repeat(64))).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Review batch' }));
    fireEvent.click(screen.getByRole('button', { name: 'Apply classification' }));

    await waitFor(() => expect(onApplyBatch).toHaveBeenCalledTimes(2));
    const secondRequest = onApplyBatch.mock.calls[1][0] as LegacyEvidenceClassificationBatchRequest;
    expect(secondRequest.items[0]).toMatchObject({
      expected_evidence_payload_sha256: 'c'.repeat(64),
      expected_classification_revision: 3,
    });
    expect(secondRequest.idempotency_key).toBe('classification-key-2');
    expect(secondRequest.idempotency_key).not.toBe(firstRequest.idempotency_key);
    await waitFor(() => expect(onCanonicalRefetch).toHaveBeenCalledTimes(2));
  });

  it('ts_8b303869 — handles an idempotency 409 without a pre-refetch and requires a new reviewed key', async () => {
    const code = 'code_evidence_legacy_classification_idempotency_conflict';
    const conflict = new LegacyEvidenceClassificationConflictError(
      code,
      new AuthenticatedFetchError({
        message: 'The reviewed key was already used for a different request.',
        status: 409,
        code,
      }),
    );
    const onApplyBatch = vi.fn()
      .mockRejectedValueOnce(conflict)
      .mockResolvedValueOnce(result);
    const onCanonicalRefetch = vi.fn().mockResolvedValue(oneItemSnapshot());
    render(<LegacyEvidenceClassificationDrawer {...defaultProps({
      snapshot: oneItemSnapshot(),
      onApplyBatch,
      onCanonicalRefetch,
    })} />);
    fillOneItemAndReview();

    fireEvent.click(screen.getByRole('button', { name: 'Apply classification' }));
    expect(await screen.findByText('This submission no longer matches its reviewed key'))
      .toBeInTheDocument();
    expect(onCanonicalRefetch).not.toHaveBeenCalled();
    const firstRequest = onApplyBatch.mock.calls[0][0] as LegacyEvidenceClassificationBatchRequest;

    fireEvent.click(screen.getByRole('button', { name: 'Review as new submission' }));
    expect(screen.getByDisplayValue('Relevance 1')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Review batch' }));
    fireEvent.click(screen.getByRole('button', { name: 'Apply classification' }));

    await waitFor(() => expect(onApplyBatch).toHaveBeenCalledTimes(2));
    const secondRequest = onApplyBatch.mock.calls[1][0] as LegacyEvidenceClassificationBatchRequest;
    expect(secondRequest.idempotency_key).toBe('classification-key-2');
    expect(secondRequest.idempotency_key).not.toBe(firstRequest.idempotency_key);
    await waitFor(() => expect(onCanonicalRefetch).toHaveBeenCalledTimes(1));
  });

  it.each([
    [403, 'Permission to classify is no longer available', /board administrator/i],
    [404, 'Evidence is no longer available', /reload the Refinement/i],
  ] as const)('ts_8b303869 — maps HTTP %s to a terminal human recovery without retry or refetch', async (
    status,
    title,
    guidance,
  ) => {
    const onApplyBatch = vi.fn().mockRejectedValue(new AuthenticatedFetchError({
      message: `HTTP ${status}`,
      status,
    }));
    const onCanonicalRefetch = vi.fn();
    render(<LegacyEvidenceClassificationDrawer {...defaultProps({
      snapshot: oneItemSnapshot(),
      onApplyBatch,
      onCanonicalRefetch,
    })} />);
    fillOneItemAndReview();
    fireEvent.click(screen.getByRole('button', { name: 'Apply classification' }));

    const heading = await screen.findByText(title);
    expect(heading.closest('[role="alert"]')).toHaveAttribute('aria-live', 'assertive');
    expect(screen.getByText(guidance)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry exact batch' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Refresh canonical context' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Apply classification' })).not.toBeInTheDocument();
    expect(onCanonicalRefetch).not.toHaveBeenCalled();
  });

  it('ts_8b303869 — anchors and focuses a field returned by HTTP 422 without submitting a partial batch', async () => {
    const onApplyBatch = vi.fn().mockRejectedValue(new AuthenticatedFetchError({
      message: 'Request validation failed.',
      status: 422,
      details: [{
        loc: ['body', 'items', 0, 'source_role'],
        msg: 'Choose an allowed source role.',
      }],
    }));
    const onCanonicalRefetch = vi.fn();
    render(<LegacyEvidenceClassificationDrawer {...defaultProps({
      snapshot: oneItemSnapshot(),
      onApplyBatch,
      onCanonicalRefetch,
    })} />);
    fillOneItemAndReview();
    fireEvent.click(screen.getByRole('button', { name: 'Apply classification' }));

    expect(await screen.findByText('Review the highlighted classification fields'))
      .toBeInTheDocument();
    expect(screen.getByText('Choose an allowed source role.')).toBeInTheDocument();
    await waitFor(() => expect(within(
      screen.getByRole('group', { name: 'Source role for evidence 1' }),
    ).getByRole('radio', { name: /^Existing implementation/ })).toHaveFocus());
    expect(screen.getByRole('button', { name: 'Review batch' })).toBeInTheDocument();
    expect(onApplyBatch).toHaveBeenCalledTimes(1);
    expect(onCanonicalRefetch).not.toHaveBeenCalled();
  });

  it('ts_8b303869 — anchors and focuses the batch when HTTP 422 has no editable field target', async () => {
    const onApplyBatch = vi.fn().mockRejectedValue(new AuthenticatedFetchError({
      message: 'Batch validation failed.',
      status: 422,
      details: { errors: [{
        loc: ['body', 'items'],
        msg: 'The batch contains evidence that is no longer eligible.',
      }] },
    }));
    const onCanonicalRefetch = vi.fn();
    render(<LegacyEvidenceClassificationDrawer {...defaultProps({
      snapshot: oneItemSnapshot(),
      onApplyBatch,
      onCanonicalRefetch,
    })} />);
    fillOneItemAndReview();
    fireEvent.click(screen.getByRole('button', { name: 'Apply classification' }));

    expect(await screen.findByText('The batch contains evidence that is no longer eligible.'))
      .toBeInTheDocument();
    await waitFor(() => expect(
      screen.getByTestId('legacy-classification-classify-step'),
    ).toHaveFocus());
    expect(onCanonicalRefetch).not.toHaveBeenCalled();
  });

  it('ts_8b303869 — opens, anchors and focuses a read-only canonical fence returned by HTTP 422', async () => {
    const onApplyBatch = vi.fn().mockRejectedValue(new AuthenticatedFetchError({
      message: 'Request validation failed.',
      status: 422,
      details: [{
        loc: ['body', 'items', 0, 'expected_evidence_payload_sha256'],
        msg: 'The observed Evidence changed after review.',
      }],
    }));
    render(<LegacyEvidenceClassificationDrawer {...defaultProps({
      snapshot: oneItemSnapshot(),
      onApplyBatch,
    })} />);
    fillOneItemAndReview();
    fireEvent.click(screen.getByRole('button', { name: 'Apply classification' }));

    expect(await screen.findByText('The observed Evidence changed after review.'))
      .toBeInTheDocument();
    const fence = document.querySelector<HTMLElement>(
      '[data-validation-field="expected_evidence_payload_sha256"]',
    );
    expect(fence).not.toBeNull();
    await waitFor(() => expect(fence).toHaveFocus());
    expect(fence?.closest('details')).toHaveAttribute('open');
  });

  it.each(['classify', 'review'] as const)(
    'ts_bd449ee2 — Cancel from %s closes with zero submit and zero canonical refetch',
    (stage) => {
      const onApplyBatch = vi.fn();
      const onCanonicalRefetch = vi.fn();
      const onClose = vi.fn();
      render(<LegacyEvidenceClassificationDrawer {...defaultProps({
        snapshot: oneItemSnapshot(),
        onApplyBatch,
        onCanonicalRefetch,
        onClose,
      })} />);
      if (stage === 'review') fillOneItemAndReview();

      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

      expect(onClose).toHaveBeenCalledTimes(1);
      expect(onApplyBatch).not.toHaveBeenCalled();
      expect(onCanonicalRefetch).not.toHaveBeenCalled();
    },
  );

  it('ts_8b303869 — does not treat an untyped status-shaped 409 as the canonical conflict flow', async () => {
    const onApplyBatch = vi.fn().mockRejectedValue({
      status: 409,
      message: 'Untyped conflict-shaped value.',
    });
    const onCanonicalRefetch = vi.fn();
    render(<LegacyEvidenceClassificationDrawer {...defaultProps({
      snapshot: oneItemSnapshot(),
      onApplyBatch,
      onCanonicalRefetch,
    })} />);
    fillOneItemAndReview();
    fireEvent.click(screen.getByRole('button', { name: 'Apply classification' }));

    expect(await screen.findByText('Submission outcome not confirmed')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry exact batch' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Refresh canonical context' })).not.toBeInTheDocument();
    expect(onCanonicalRefetch).not.toHaveBeenCalled();
  });

  it('ts_8b303869 / ts_15a6a9dc — never reapplies a known-success batch when its single canonical refetch fails', async () => {
    const onApplyBatch = vi.fn().mockResolvedValue(result);
    const onCanonicalRefetch = vi.fn().mockRejectedValue(
      new Error('Canonical projection unavailable.'),
    );
    render(<LegacyEvidenceClassificationDrawer {...defaultProps({
      snapshot: oneItemSnapshot(),
      onApplyBatch,
      onCanonicalRefetch,
    })} />);
    fillOneItemAndReview();
    fireEvent.click(screen.getByRole('button', { name: 'Apply classification' }));

    expect(await screen.findByText('Classification applied; refresh failed')).toBeInTheDocument();
    expect(screen.getByText(/Do not apply this batch again/i)).toBeInTheDocument();
    expect(onApplyBatch).toHaveBeenCalledTimes(1);
    expect(onCanonicalRefetch).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: 'Apply classification' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry exact batch' })).not.toBeInTheDocument();
  });

  it('ts_f817bea4 / ts_bd449ee2 — fails closed, announces busy state and clears the drawer when permission is lost', async () => {
    let submittedSignal: AbortSignal | null = null;
    const onApplyBatch = vi.fn((
      _request: LegacyEvidenceClassificationBatchRequest,
      signal: AbortSignal,
    ) => {
      submittedSignal = signal;
      return new Promise<LegacyEvidenceClassificationBatchResult>(() => undefined);
    });
    const onCanonicalRefetch = vi.fn();
    const onClose = vi.fn();
    const props = defaultProps({
      snapshot: oneItemSnapshot(),
      onApplyBatch,
      onCanonicalRefetch,
      onClose,
    });
    const { rerender } = render(<LegacyEvidenceClassificationDrawer {...props} />);
    fillOneItemAndReview();
    fireEvent.click(screen.getByRole('button', { name: 'Apply classification' }));
    await waitFor(() => expect(onApplyBatch).toHaveBeenCalledTimes(1));
    expect(screen.getByText('Applying classification.')).toHaveAttribute('aria-live', 'polite');

    rerender(<LegacyEvidenceClassificationDrawer {...props} canClassify={false} />);

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId('legacy-evidence-classification-drawer')).not.toBeInTheDocument();
    expect(submittedSignal).not.toBeNull();
    expect((submittedSignal as unknown as AbortSignal).aborted).toBe(true);
    expect(onCanonicalRefetch).not.toHaveBeenCalled();
    expect(screen.queryByDisplayValue('Relevance 1')).not.toBeInTheDocument();
    expect(document.body.style.overflow).toBe('');
  });
});
