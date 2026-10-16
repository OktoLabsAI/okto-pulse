import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DecisionInspectionDetails, DecisionReviewSection } from '../DecisionReviewSection';
import type { DecisionReviewProjection } from '@/types/decision-reviews';
import { AuthenticatedFetchError } from '@/lib/authFetch';

afterEach(cleanup);
const source = { reference: 'spec:s', revision: 'edition:1', sha256: 'a'.repeat(64) };
const data: DecisionReviewProjection = { board_id: 'b', spec_id: 's', edition: 1, version: 3,
  review_revision: 'b'.repeat(64), complete: true, observation_authority: 'authenticated_reviewer_declaration',
  decisions: [{ decision_id: 'choice', status: 'conflict', record_ids: ['a', 'b'],
    basis: { scope_sha256: 'c'.repeat(64), sources: [source], expected: 'Only the mock is delivered.' },
    separation: { mode: 'warn', allowed: true, warning: true, conflicts: ['decision_author'] } }], history: [] };

describe('Decision inspection', () => {
  it('replays the exact uncertain observation across revisions before permitting a new intent', async () => {
    const onSubmit = vi.fn().mockRejectedValueOnce(new Error('Network interrupted')).mockResolvedValue(undefined);
    const { rerender } = render(<DecisionReviewSection data={data} decisionId="choice" canReview onSubmit={onSubmit} />);
    fireEvent.click(screen.getByText('Record inspection'));
    fireEvent.change(screen.getByLabelText('Observed result'), { target: { value: 'Original authenticated observation.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Record observation' }));
    await screen.findByText(/observation outcome is unknown/);
    rerender(<DecisionReviewSection data={{ ...data, version: 4 }} decisionId="choice" canReview onSubmit={onSubmit} />);
    expect(screen.getByLabelText('Observed result')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Review inspection base' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Retry original observation' }));
    await waitFor(() => expect(screen.getByLabelText('Observed result')).toHaveValue(''));
    expect(onSubmit.mock.calls[1][0]).toEqual(onSubmit.mock.calls[0][0]);
    expect(onSubmit.mock.calls[1][0].expected_version).toBe(3);
  });

  it('keeps the observation while current reading is unavailable and refuses recording', () => {
    const onSubmit = vi.fn();
    const { rerender } = render(<DecisionReviewSection data={data} decisionId="choice" canReview onSubmit={onSubmit} />);
    fireEvent.click(screen.getByText('Record inspection'));
    fireEvent.change(screen.getByLabelText('Observed result'), { target: { value: 'Keep through refresh' } });
    rerender(<DecisionReviewSection data={data} decisionId="choice" canReview readCurrent={false} onSubmit={onSubmit} />);
    expect(screen.getByLabelText('Observed result')).toHaveValue('Keep through refresh');
    expect(screen.getByRole('button', { name: 'Record observation' })).toBeDisabled();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('does not let a late write response clear an observation in another Spec', async () => {
    let complete!: () => void;
    const onSubmit = vi.fn().mockImplementation(() => new Promise<void>(resolve => { complete = resolve; }));
    const { rerender } = render(<DecisionReviewSection data={data} decisionId="choice" canReview onSubmit={onSubmit} />);
    fireEvent.click(screen.getByText('Record inspection'));
    fireEvent.change(screen.getByLabelText('Observed result'), { target: { value: 'First Spec' } });
    fireEvent.click(screen.getByRole('button', { name: 'Record observation' }));
    rerender(<DecisionReviewSection data={{ ...data, spec_id: 'other' }} decisionId="choice" canReview onSubmit={onSubmit} />);
    fireEvent.click(screen.getByText('Record inspection'));
    fireEvent.change(screen.getByLabelText('Observed result'), { target: { value: 'Second Spec' } });
    await act(async () => complete());
    expect(screen.getByLabelText('Observed result')).toHaveValue('Second Spec');
  });

  it('retains an observation but requires explicit review when the source basis changes', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(<DecisionReviewSection data={data} decisionId="choice" canReview onSubmit={onSubmit} />);
    fireEvent.click(screen.getByText('Record inspection'));
    fireEvent.change(screen.getByLabelText('Observed result'), { target: { value: 'Observed the original scope.' } });
    fireEvent.click(screen.getByLabelText('Reconcile all current observations with this conclusion'));
    const revised = { ...data, version: 4, review_revision: 'd'.repeat(64), decisions: [{ ...data.decisions[0], record_ids: ['new'],
      basis: { ...data.decisions[0].basis!, expected: 'The mock now has another boundary.', scope_sha256: 'e'.repeat(64) } }] };
    rerender(<DecisionReviewSection data={revised} decisionId="choice" canReview onSubmit={onSubmit} />);
    expect(screen.getByLabelText('Observed result')).toHaveValue('Observed the original scope.');
    expect(screen.getByRole('button', { name: 'Record observation' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Review inspection base' }));
    expect(screen.getByLabelText('Reconcile all current observations with this conclusion')).not.toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: 'Record observation' }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce());
    expect(onSubmit.mock.calls[0][0]).toMatchObject({ expected_version: 4, expected_review_revision: revised.review_revision,
      entries: [{ observed: 'Observed the original scope.', expected_scope_sha256: 'e'.repeat(64), reconciles: [] }] });
  });

  it('does not reuse authored observations after the reviewer loses authority', () => {
    const { rerender } = render(<DecisionReviewSection data={data} decisionId="choice" canReview onSubmit={vi.fn()} />);
    fireEvent.click(screen.getByText('Record inspection'));
    fireEvent.change(screen.getByLabelText('Observed result'), { target: { value: 'Private local observation' } });
    rerender(<DecisionReviewSection data={data} decisionId="choice" canReview={false} onSubmit={vi.fn()} />);
    expect(screen.queryByDisplayValue('Private local observation')).not.toBeInTheDocument();
    rerender(<DecisionReviewSection data={data} decisionId="choice" canReview onSubmit={vi.fn()} />);
    fireEvent.click(screen.getByText('Record inspection'));
    expect(screen.getByLabelText('Observed result')).toHaveValue('');
  });

  it('shows every observation of the exact historical batch without claiming current verification', () => {
    const observation = { decision_id: 'choice', expected_scope_sha256: 'c'.repeat(64), observed: '<script>bounded mock</script>',
      expected: 'Old condition', result: 'failed' as const, sources: [source], reconciles: ['earlier'], separation: { warning: true, conflicts: [] } };
    const history = [{ id: 'review', edition: 1, actor_id: 'person', actor_kind: 'human', created_at: '2026-10-10T12:00:00Z',
      revoked: true, observations: [observation, { ...observation, decision_id: 'second', observed: 'Second observation' }] }];
    const { container, rerender } = render(<DecisionInspectionDetails data={{ ...data, edition: 2, history }} recordId="review" />);
    expect(screen.getByRole('article')).toHaveAttribute('data-readiness-ref', 'inspection:review');
    expect(screen.getByText(/Previous version.*Revoked/)).toBeInTheDocument();
    expect(screen.getByText('choice · Failed')).toBeInTheDocument();
    expect(screen.getByText('second · Failed')).toBeInTheDocument();
    expect(screen.getByText('<script>bounded mock</script>')).toBeInTheDocument();
    expect(container.querySelector('script')).toBeNull();
    expect(screen.getByText(/does not establish current verification/)).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    rerender(<DecisionInspectionDetails data={{ ...data, history }} recordId="missing" />);
    expect(screen.getByRole('alert')).toHaveTextContent('unavailable');
    expect(screen.queryByText('Second observation')).not.toBeInTheDocument();
  });

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

  it('keeps identical retry identity after definite rejection but changes it after editing', async () => {
    const onSubmit = vi.fn().mockRejectedValue(new AuthenticatedFetchError({ status: 422, message: 'Invalid observation' }));
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
