import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SpecDecompositionHistory } from '../SpecDecompositionHistory';
import { SpecDecompositionPanel } from '../SpecDecompositionPanel';
import type { SpecDecompositionOpinion, SpecEvaluationList } from '@/types';

const api = vi.hoisted(() => ({ listSpecEvaluations: vi.fn(), submitSpecEvaluation: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
const opinion: SpecDecompositionOpinion = {
  id: 'opinion-1', evaluator_id: 'human-1', evaluator_name: 'Reviewer One', evaluator_type: 'user',
  spec_edition: 2, spec_version: 8, overall_score: 81, overall_justification: 'Overall rationale retained verbatim.',
  recommendation: 'request_changes', lifecycle_state: 'current', created_at: '2026-10-14T12:00:00Z',
  dimensions: {
    breakdown_completeness: { score: 89, justification: 'Every requirement has a planned owner.' },
    granularity: { score: 83, justification: 'Tasks have independent delivery boundaries.' },
    dependency_coherence: { score: 77, justification: 'One dependency needs a clarified direction.' },
    test_coverage_quality: { score: 82, justification: 'Each failure scenario has a Test planned.' },
  },
};
const page: SpecEvaluationList = { spec_id: 'spec-1', spec_status: 'approved', current_edition: 2,
  active_count: 1, previous_count: 1, total: 1, offset: 0, limit: 10, view: 'current', evaluations: [opinion] };
beforeEach(() => { vi.clearAllMocks(); api.listSpecEvaluations.mockResolvedValue(page); });

describe('Native decomposition opinions', () => {
  it('expands all original scores, justifications and authorship without an aggregate score', async () => {
    render(<SpecDecompositionHistory specId="spec-1" edition={2} view="current" />);
    fireEvent.click(await screen.findByText('Reviewer One'));
    expect(screen.getByText('Overall rationale retained verbatim.')).toBeVisible();
    for (const dimension of Object.values(opinion.dimensions)) expect(screen.getByText(dimension.justification)).toBeVisible();
    expect(screen.getByLabelText('Dependency coherence score 77 of 100')).toBeVisible();
    expect(api.listSpecEvaluations).toHaveBeenCalledWith('spec-1', expect.objectContaining({ view: 'current', offset: 0, limit: 10 }));
  });

  it('loads previous editions lazily and clears a protected result on read failure', async () => {
    const view = render(<SpecDecompositionPanel specId="spec-1" edition={2} canSubmit={false} refreshKey={0} onRecorded={() => {}} />);
    await screen.findByText('Reviewer One');
    expect(api.listSpecEvaluations).toHaveBeenCalledTimes(1);
    api.listSpecEvaluations.mockResolvedValue({ ...page, view: 'previous', evaluations: [{ ...opinion, id: 'older', spec_edition: 1, lifecycle_state: 'previous' }] });
    fireEvent.click(screen.getByRole('button', { name: /Previous decomposition opinions/ }));
    await waitFor(() => expect(api.listSpecEvaluations).toHaveBeenCalledWith('spec-1', expect.objectContaining({ view: 'previous' })));
    api.listSpecEvaluations.mockRejectedValue(new Error('Permission denied'));
    view.rerender(<SpecDecompositionPanel specId="spec-1" edition={2} canSubmit={false} refreshKey={1} onRecorded={() => {}} />);
    await waitFor(() => expect(screen.queryByText('Reviewer One')).not.toBeInTheDocument());
    expect(await screen.findAllByText('Permission denied')).toHaveLength(2);
  });

  it('discards late responses after switching subjects and validates returned edition', async () => {
    let resolve!: (data: SpecEvaluationList) => void;
    api.listSpecEvaluations.mockReturnValueOnce(new Promise<SpecEvaluationList>(done => { resolve = done; }));
    const view = render(<SpecDecompositionHistory specId="spec-1" edition={2} view="current" />);
    view.rerender(<SpecDecompositionHistory specId="spec-2" edition={3} view="current" />);
    await screen.findByRole('alert'); // Mock reply belongs to spec-1; it cannot be rendered for spec-2.
    resolve(page);
    await waitFor(() => expect(screen.queryByText('Reviewer One')).not.toBeInTheDocument());
  });

  it('navigates bounded pages without dropping the view', async () => {
    api.listSpecEvaluations.mockResolvedValue({ ...page, total: 11, evaluations: Array.from({ length: 10 }, (_, index) => ({ ...opinion, id: `opinion-${index}` })) });
    render(<SpecDecompositionHistory specId="spec-1" edition={2} view="current" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(api.listSpecEvaluations).toHaveBeenCalledWith('spec-1', expect.objectContaining({ view: 'current', offset: 10, limit: 10 })));
  });
});
