import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { QualitySummaryBadges } from '../QualitySummaryBadges';

describe('QualitySummaryBadges', () => {
  it('preserves authorization omission and an authorized empty projection', () => {
    const { rerender } = render(<QualitySummaryBadges />);
    expect(screen.queryByTestId('quality-summary-badges')).not.toBeInTheDocument();

    rerender(<QualitySummaryBadges summaries={{}} />);
    expect(screen.queryByTestId('quality-summary-badges')).not.toBeInTheDocument();
  });

  it('renders native current/stale badges and omits legacy spec validation summaries', () => {
    render(
      <QualitySummaryBadges
        summaries={{
          ambiguity: {
            receipt_id: 'receipt-a',
            subject_version: 4,
            currentness: 'stale',
            score: 3.5,
            scale: {
              kind: 'ambiguity_score',
              min: 1,
              max: 5,
              direction: 'lower_better',
            },
            head_revision: 2,
          },
          spec_validation: {
            receipt_id: 'receipt-b',
            subject_version: 5,
            currentness: 'current',
            score: 92,
            scale: {
              kind: 'percentage',
              min: 0,
              max: 100,
              direction: 'higher_better',
            },
            head_revision: 6,
          },
          requirement_lint: {
            receipt_id: 'receipt-c',
            subject_version: 6,
            currentness: 'current',
            score: 2,
            scale: {
              kind: 'finding_count',
              min: 0,
              max: 13,
              direction: 'lower_better',
            },
            head_revision: 7,
          },
        }}
      />,
    );

    expect(screen.getByTestId('quality-summary-ambiguity')).toHaveTextContent(
      'Ambiguity3.5stale',
    );
    expect(screen.queryByTestId('quality-summary-spec_validation')).not.toBeInTheDocument();
    expect(screen.getByTestId('quality-summary-requirement_lint')).toHaveTextContent(
      'Requirement lint2',
    );
    expect(screen.getByTestId('quality-summary-ambiguity')).toHaveAttribute(
      'title',
      expect.stringContaining('1–5'),
    );
  });
});
