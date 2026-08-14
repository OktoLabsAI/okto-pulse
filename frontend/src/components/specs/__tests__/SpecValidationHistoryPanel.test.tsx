import { fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { SpecValidationList } from '@/types';
import { SpecValidationHistoryPanel } from '../SpecValidationHistoryPanel';

const apiMock = vi.hoisted(() => ({
  listSpecValidations: vi.fn(),
  getCurrentSpecValidation: vi.fn(),
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
    apiMock.getCurrentSpecValidation.mockResolvedValue({
      spec_id: 'spec-1',
      edition: 1,
      lifecycle_state: 'current',
      current_validation: {
        ...validationHistory.validations[0],
        edition: 1,
        lifecycle_state: 'current',
      },
      previous_count: 0,
    });
  });

  it('renders every validation dimension as a circular score out of 100', async () => {
    render(<SpecValidationHistoryPanel specId="spec-1" />);

    const completeness = await screen.findByTestId(
      'spec-validation-score-completeness',
    );
    const assertiveness = screen.getByTestId(
      'spec-validation-score-assertiveness',
    );
    const ambiguity = screen.getByTestId('spec-validation-score-ambiguity');

    expect(completeness).toHaveAccessibleName(
      'Completeness score 92 out of 100, Minimum 80',
    );
    expect(completeness).toHaveClass(
      'h-20',
      'w-20',
      'rounded-full',
      'border-4',
      'border-emerald-400',
    );
    expect(assertiveness).toHaveAccessibleName(
      'Assertiveness score 76 out of 100, Minimum 80',
    );
    expect(assertiveness).toHaveClass('border-red-400');
    expect(ambiguity).toHaveAccessibleName(
      'Ambiguity score 18 out of 100, Maximum 30',
    );
    expect(ambiguity).toHaveClass('border-emerald-400');

    expect(screen.getAllByText('Minimum 80')).toHaveLength(2);
    expect(screen.getByText('Maximum 30')).toBeInTheDocument();
    expect(within(completeness).getByText('/100')).toBeInTheDocument();
  });

  it('preserves expandable per-dimension justifications for legacy history', async () => {
    render(<SpecValidationHistoryPanel specId="spec-1" />);

    fireEvent.click(
      await screen.findByRole('button', {
        name: 'View metric justifications',
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

  it('renders five current metrics, their justifications and metric-tagged pinpoints', async () => {
    apiMock.getCurrentSpecValidation.mockResolvedValue({
      spec_id: 'spec-1',
      edition: 2,
      lifecycle_state: 'current',
      current_validation: {
        ...validationHistory.validations[0],
        id: 'validation-five-metrics',
        edition: 2,
        lifecycle_state: 'current',
        confidence: 93,
        confidence_justification: 'The evidence supports a confident assessment.',
        clarity: 88,
        clarity_justification: 'The problem and solution are clearly framed.',
        assertiveness: 84,
        assertiveness_justification: 'Requirements use direct measurable language.',
        decidability: 76,
        decidability_justification: 'One capacity decision still needs a bound.',
        ambiguity: 18,
        ambiguity_justification: 'Only a small interpretation gap remains.',
        pinpoints: [{
          metric: 'decidability',
          anchor_type: 'structured_child',
          anchor_ref: 'fr-availability',
          detail: 'The scaling requirement does not define minimum capacity.',
        }],
        resolved_thresholds: {
          min_spec_confidence: 70,
          min_spec_clarity: 80,
          min_spec_assertiveness: 80,
          min_spec_decidability: 80,
          max_spec_ambiguity: 30,
        },
      },
      previous_count: 1,
    });

    render(
      <SpecValidationHistoryPanel
        specId="spec-1"
        currentEdition={2}
        view="current"
        anchorTexts={{
          'fr-availability': 'FR-4: Run across three availability zones.',
        }}
      />,
    );

    expect(await screen.findByTestId('spec-validation-score-confidence'))
      .toHaveAccessibleName('Confidence score 93 out of 100, Minimum 70');
    expect(screen.getByTestId('spec-validation-score-clarity'))
      .toHaveAccessibleName('Clarity score 88 out of 100, Minimum 80');
    expect(screen.getByTestId('spec-validation-score-decidability'))
      .toHaveAccessibleName('Decidability score 76 out of 100, Minimum 80');
    expect(screen.getByTestId('spec-validation-score-decidability'))
      .toHaveClass('border-red-400');
    expect(screen.getByText('decidability')).toBeInTheDocument();
    expect(screen.getByTestId('spec-validation-pinpoint-target'))
      .toHaveTextContent(
        'FR-4: Run across three availability zones. (fr-availability)',
      );
    expect(screen.getByText(/minimum capacity/)).toBeInTheDocument();
    expect(screen.getByText(/confident assessment/)).toBeInTheDocument();
    expect(screen.getByText(/capacity decision/)).toBeInTheDocument();
    expect(screen.queryByRole('button', {
      name: 'View metric justifications',
    })).not.toBeInTheDocument();
  });

  it('keeps previous validation justifications collapsible', async () => {
    apiMock.listSpecValidations.mockResolvedValue({
      ...validationHistory,
      current_validation_id: null,
      validations: [{
        ...validationHistory.validations[0],
        edition: 1,
        lifecycle_state: 'previous',
        active: false,
      }],
    });

    render(
      <SpecValidationHistoryPanel
        specId="spec-1"
        currentEdition={2}
        view="previous"
      />,
    );

    const toggle = await screen.findByRole('button', {
      name: 'View metric justifications',
    });
    expect(screen.getByText(/Previous edition · Attempt 1/))
      .toBeInTheDocument();
    expect(screen.queryByText('All required sections are present.'))
      .not.toBeInTheDocument();
    fireEvent.click(toggle);
    expect(screen.getByText('All required sections are present.'))
      .toBeInTheDocument();
  });

  it('labels a replaced result from the current edition as a superseded attempt', async () => {
    apiMock.listSpecValidations.mockResolvedValue({
      ...validationHistory,
      current_validation_id: 'validation-current-2',
      validations: [{
        ...validationHistory.validations[0],
        id: 'validation-superseded-2',
        edition: 2,
        lifecycle_state: 'previous',
        active: false,
      }],
    });

    render(
      <SpecValidationHistoryPanel
        specId="spec-1"
        currentEdition={2}
        view="previous"
      />,
    );

    expect(await screen.findByText(/Superseded attempt · Attempt 1/))
      .toBeInTheDocument();
    expect(screen.getByText('Edition 2')).toBeInTheDocument();
    expect(screen.queryByText(/invalid/i)).not.toBeInTheDocument();
  });

  it('loads only the current summary when the current detail is opened', async () => {
    render(
      <SpecValidationHistoryPanel
        specId="spec-1"
        currentEdition={1}
        view="current"
      />,
    );

    expect(await screen.findByText('Edition 1')).toBeInTheDocument();
    expect(apiMock.getCurrentSpecValidation).toHaveBeenCalledTimes(1);
    expect(apiMock.listSpecValidations).not.toHaveBeenCalled();
  });

  it('renders the canonical score and summary without legacy dimension noise', async () => {
    apiMock.getCurrentSpecValidation.mockResolvedValue({
      spec_id: 'spec-1',
      edition: 2,
      lifecycle_state: 'current',
      current_validation: {
        id: 'validation-formal-2',
        validation_id: 'validation-formal-2',
        validation_edition: 2,
        is_current: true,
        spec_id: 'spec-1',
        board_id: 'board-1',
        reviewer_id: 'reviewer-2',
        reviewer_name: 'Independent reviewer',
        score: 88,
        summary: 'The current edition is clear and ready to proceed.',
        outcome: 'success',
        threshold_violations: [],
        created_at: '2026-08-11T12:00:00Z',
        edition: 2,
        lifecycle_state: 'current',
        active: true,
      },
      previous_count: 1,
    });

    render(
      <SpecValidationHistoryPanel
        specId="spec-1"
        currentEdition={2}
        view="current"
      />,
    );

    const score = await screen.findByTestId('spec-validation-score-overall');
    expect(score).toHaveAccessibleName(
      'Validation score 88 out of 100, No board threshold',
    );
    expect(screen.getByText(
      'The current edition is clear and ready to proceed.',
    )).toBeInTheDocument();
    expect(screen.queryByTestId('spec-validation-score-completeness'))
      .not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'View metric justifications' }))
      .not.toBeInTheDocument();
  });

  it('keeps a null-edition legacy validation in Previous and never promotes it to Current', async () => {
    const legacyValidation = {
      ...validationHistory.validations[0],
      id: 'legacy-validation',
      edition: null,
      lifecycle_state: 'current' as const,
      active: true,
    };
    apiMock.listSpecValidations.mockResolvedValue({
      ...validationHistory,
      current_validation_id: 'legacy-validation',
      validations: [legacyValidation],
    });

    const previousRender = render(
      <SpecValidationHistoryPanel
        specId="spec-1"
        currentEdition={2}
        view="previous"
      />,
    );

    expect(await screen.findByText('Legacy')).toBeInTheDocument();
    expect(screen.getByText(/Historical result · Attempt 1/))
      .toBeInTheDocument();
    expect(screen.queryByText('Edition 1')).not.toBeInTheDocument();
    previousRender.unmount();

    render(
      <SpecValidationHistoryPanel
        specId="spec-1"
        currentEdition={2}
        view="current"
        currentValidation={legacyValidation}
      />,
    );

    expect(await screen.findByText(
      'No current validation result for Edition 2.',
    )).toBeInTheDocument();
    expect(screen.queryByText('Legacy')).not.toBeInTheDocument();
  });

  it('refetches the bounded Current result when the edition changes on the same Spec', async () => {
    const { rerender } = render(
      <SpecValidationHistoryPanel
        specId="spec-1"
        currentEdition={1}
        view="current"
      />,
    );

    expect(await screen.findByText('Edition 1')).toBeInTheDocument();
    expect(apiMock.getCurrentSpecValidation).toHaveBeenCalledTimes(1);

    apiMock.getCurrentSpecValidation.mockResolvedValue({
      spec_id: 'spec-1',
      edition: 2,
      lifecycle_state: 'current',
      current_validation: {
        ...validationHistory.validations[0],
        id: 'validation-2',
        edition: 2,
        lifecycle_state: 'current',
      },
      previous_count: 1,
    });
    rerender(
      <SpecValidationHistoryPanel
        specId="spec-1"
        currentEdition={2}
        view="current"
      />,
    );

    expect(await screen.findByText('Edition 2')).toBeInTheDocument();
    expect(apiMock.getCurrentSpecValidation).toHaveBeenCalledTimes(2);
  });
});
