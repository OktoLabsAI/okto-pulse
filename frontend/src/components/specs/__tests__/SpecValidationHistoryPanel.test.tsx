import { fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { SpecValidationList } from '@/types';
import { SpecValidationHistoryPanel } from '../SpecValidationHistoryPanel';

const apiMock = vi.hoisted(() => ({
  listSpecValidations: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn() },
}));

const validationHistory: SpecValidationList = {
  spec_id: 'spec-1',
  current_validation_id: 'validation-1',
  validations: [
    {
      id: 'validation-1',
      spec_id: 'spec-1',
      board_id: 'board-1',
      reviewer_id: 'reviewer-1',
      reviewer_name: 'Reviewer',
      completeness: 92,
      completeness_justification: 'All required sections are present.',
      assertiveness: 76,
      assertiveness_justification: 'Some requirements remain tentative.',
      ambiguity: 18,
      ambiguity_justification: 'Residual ambiguity is sufficiently low.',
      general_justification: 'The spec is ready for implementation.',
      recommendation: 'reject',
      outcome: 'failed',
      threshold_violations: ['Assertiveness must be at least 80.'],
      resolved_thresholds: {
        min_spec_completeness: 80,
        min_spec_assertiveness: 80,
        max_spec_ambiguity: 30,
      },
      created_at: '2026-07-28T12:00:00Z',
      active: true,
    },
  ],
};

describe('SpecValidationHistoryPanel score presentation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.listSpecValidations.mockResolvedValue(validationHistory);
  });

  it('renders every validation dimension as a circular score out of 100', async () => {
    render(<SpecValidationHistoryPanel specId="spec-1" />);

    await screen.findByText('Validation History');

    const completeness = screen.getByTestId(
      'spec-validation-score-completeness',
    );
    const assertiveness = screen.getByTestId(
      'spec-validation-score-assertiveness',
    );
    const ambiguity = screen.getByTestId('spec-validation-score-ambiguity');

    expect(completeness).toHaveAccessibleName(
      'Completeness score 92 out of 100, Minimum 80, threshold met',
    );
    expect(completeness).toHaveClass(
      'h-20',
      'w-20',
      'rounded-full',
      'border-4',
      'border-emerald-400',
    );
    expect(assertiveness).toHaveAccessibleName(
      'Assertiveness score 76 out of 100, Minimum 80, threshold not met',
    );
    expect(assertiveness).toHaveClass('border-red-400');
    expect(ambiguity).toHaveAccessibleName(
      'Ambiguity score 18 out of 100, Maximum 30, threshold met',
    );
    expect(ambiguity).toHaveClass('border-emerald-400');

    expect(screen.getAllByText('Minimum 80')).toHaveLength(2);
    expect(screen.getByText('Maximum 30')).toBeInTheDocument();
    expect(within(completeness).getByText('/100')).toBeInTheDocument();
  });

  it('preserves the expandable per-dimension justifications', async () => {
    render(<SpecValidationHistoryPanel specId="spec-1" />);

    fireEvent.click(
      await screen.findByRole('button', {
        name: 'Show per-dimension justifications',
      }),
    );

    expect(
      screen.getByText('All required sections are present.'),
    ).toBeInTheDocument();
    expect(
      screen.getByText('Some requirements remain tentative.'),
    ).toBeInTheDocument();
    expect(
      screen.getByText('Residual ambiguity is sufficiently low.'),
    ).toBeInTheDocument();
  });
});
