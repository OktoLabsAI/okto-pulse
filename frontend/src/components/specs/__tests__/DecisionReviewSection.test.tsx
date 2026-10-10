import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DecisionReviewSection } from '../DecisionReviewSection';
import type { DecisionReviewProjection } from '@/types/decision-reviews';

afterEach(cleanup);
const source = { reference: 'spec:s', revision: 'edition:1', sha256: 'a'.repeat(64) };
const data: DecisionReviewProjection = { board_id: 'b', spec_id: 's', edition: 1, version: 3,
  review_revision: 'b'.repeat(64), complete: true, observation_authority: 'authenticated_reviewer_declaration',
  decisions: [{ decision_id: 'choice', status: 'conflict', record_ids: ['a', 'b'],
    basis: { scope_sha256: 'c'.repeat(64), sources: [source], expected: 'Only the mock is delivered.' },
    separation: { mode: 'warn', allowed: true, warning: true, conflicts: ['decision_author'] } }], history: [] };

describe('Decision inspection', () => {
  it('requires a real observation and explicit reconciliation, submitting the exact server base', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<DecisionReviewSection data={data} decisionId="choice" canReview onSubmit={onSubmit} />);
    fireEvent.click(screen.getByText('Record inspection'));
    expect(screen.getByRole('button', { name: 'Record observation' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Observed result'), { target: { value: 'Inspected the versioned mock manifest.' } });
    fireEvent.change(screen.getByLabelText('Inspection conclusion'), { target: { value: 'passed' } });
    fireEvent.click(screen.getByLabelText('Reconcile all current observations with this conclusion'));
    fireEvent.click(screen.getByRole('button', { name: 'Record observation' }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce());
    expect(onSubmit.mock.calls[0][0]).toMatchObject({ expected_version: 3, expected_edition: 1,
      expected_review_revision: data.review_revision, entries: [{ decision_id: 'choice', sources: [source],
        result: 'passed', reconciles: ['a', 'b'], expected_scope_sha256: 'c'.repeat(64) }] });
    expect(onSubmit.mock.calls[0][0]).not.toHaveProperty('actor_id');
  });

  it('keeps identical retry identity on failure but changes it after editing', async () => {
    const onSubmit = vi.fn().mockRejectedValue(new Error('Connection interrupted'));
    render(<DecisionReviewSection data={data} decisionId="choice" canReview onSubmit={onSubmit} />);
    fireEvent.click(screen.getByText('Record inspection'));
    fireEvent.change(screen.getByLabelText('Observed result'), { target: { value: 'Observed bounded scope.' } });
    const button = screen.getByRole('button', { name: 'Record observation' });
    fireEvent.click(button);
    await screen.findByRole('alert');
    fireEvent.click(button);
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(2));
    expect(onSubmit.mock.calls[0][0]).toEqual(onSubmit.mock.calls[1][0]);
    await waitFor(() => expect(button).not.toBeDisabled());
    fireEvent.change(screen.getByLabelText('Observed result'), { target: { value: 'Corrected observation.' } });
    fireEvent.click(button);
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(3));
    expect(onSubmit.mock.calls[2][0].idempotency_key).not.toBe(onSubmit.mock.calls[0][0].idempotency_key);
  });

  it('exposes no write form without authority or enforced independence', () => {
    const { rerender } = render(<DecisionReviewSection data={data} decisionId="choice" canReview={false} onSubmit={vi.fn()} />);
    expect(screen.queryByText('Record inspection')).not.toBeInTheDocument();
    rerender(<DecisionReviewSection data={{ ...data, decisions: [{ ...data.decisions[0], separation: {
      mode: 'enforce', allowed: false, warning: false, conflicts: ['decision_author'] } }] }} decisionId="choice" canReview onSubmit={vi.fn()} />);
    expect(screen.queryByText('Record inspection')).not.toBeInTheDocument();
    expect(screen.getByText(/independent reviewer/)).toBeInTheDocument();
  });

  it('shows full observations and prior editions in separate disclosures', () => {
    const observation = { decision_id: 'choice', expected_scope_sha256: 'c'.repeat(64), observed: 'Inspected old scope.',
      expected: 'Old condition', result: 'failed' as const, sources: [source], reconciles: [], separation: { warning: false, conflicts: [] } };
    render(<DecisionReviewSection data={{ ...data, edition: 2, history: [{ id: 'old', edition: 1, actor_id: 'person', actor_kind: 'human',
      created_at: '2026-10-10T12:00:00Z', revoked: false, observations: [observation] }] }} decisionId="choice" canReview={false} onSubmit={vi.fn()} />);
    const previous = screen.getByText('Previous versions').closest('details');
    expect(previous).not.toHaveAttribute('open');
    fireEvent.click(screen.getByText('Previous versions'));
    expect(screen.getByText('Old condition')).toBeInTheDocument();
    expect(screen.getByText('Inspected old scope.')).toBeInTheDocument();
  });
});
