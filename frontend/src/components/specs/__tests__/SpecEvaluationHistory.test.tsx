import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SpecEvaluationHistory } from '../SpecEvaluationHistory';

const api = vi.hoisted(() => ({ listSpecEvaluations: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
const props = { specId: 'spec', edition: 3, version: 4, canRead: true };
const previous = { id: 'old', evaluator_id: 'reviewer', overall_score: 95,
  recommendation: 'approve', overall_justification: 'Original verdict retained',
  created_at: '2026-09-23', lifecycle_state: 'previous' };

describe('Decomposition evaluation editions', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listSpecEvaluations.mockResolvedValue({ current_edition: 3, active_count: 0,
      previous_count: 1, evaluations: [previous] });
  });
  it('preserves an earlier approval as Previous without presenting it as current approval', async () => {
    render(<SpecEvaluationHistory {...props} />);
    expect(await screen.findByText(/No current evaluation/)).toBeInTheDocument();
    expect(screen.getByText('Original verdict retained')).toBeInTheDocument();
    expect(screen.getByText(/Original edition unknown/)).toBeInTheDocument();
    expect(screen.getByText('Edition 3: 0 current, 1 previous.')).toBeInTheDocument();
  });
  it('keeps current rejection and approval visible together without inventing gate success', async () => {
    api.listSpecEvaluations.mockResolvedValue({ current_edition: 3, active_count: 2, previous_count: 1,
      evaluations: [previous, { ...previous, id: 'rejection', spec_edition: 3, lifecycle_state: 'current', recommendation: 'reject' },
        { ...previous, id: 'approval', spec_edition: 3, lifecycle_state: 'current' }] });
    render(<SpecEvaluationHistory {...props} />);
    expect(await screen.findByText('Edition 3: 2 current, 1 previous.')).toBeInTheDocument();
    expect(screen.getByText(/reviewer: reject/)).toBeInTheDocument();
    expect(screen.queryByText(/gate passed|ready to start/i)).not.toBeInTheDocument();
  });
  it('does not fetch without authority and removes data when permission is revoked', async () => {
    const view = render(<SpecEvaluationHistory {...props} canRead={false} />);
    expect(api.listSpecEvaluations).not.toHaveBeenCalled();
    view.rerender(<SpecEvaluationHistory {...props} />);
    await screen.findByText('Original verdict retained');
    const signal = api.listSpecEvaluations.mock.calls[0][1] as AbortSignal;
    view.rerender(<SpecEvaluationHistory {...props} canRead={false} />);
    expect(screen.queryByText('Original verdict retained')).not.toBeInTheDocument();
    expect(signal.aborted).toBe(true);
  });
  it('clears old verdicts on edition change and reports failure as unknown', async () => {
    const view = render(<SpecEvaluationHistory {...props} />);
    await screen.findByText('Original verdict retained');
    api.listSpecEvaluations.mockRejectedValue(new Error('Unavailable'));
    view.rerender(<SpecEvaluationHistory {...props} edition={4} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Current approval is unknown');
    expect(screen.queryByText('Original verdict retained')).not.toBeInTheDocument();
    await waitFor(() => expect(api.listSpecEvaluations).toHaveBeenCalledTimes(2));
  });
});
