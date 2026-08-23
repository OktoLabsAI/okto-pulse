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

  it('renders scores only for current results and omits spec validation summaries', () => {
    render(
      <QualitySummaryBadges
        summaries={{
          ambiguity: {
            edition: 3,
            state: 'previous',
            previous_count: 1,
            current_result: {
              score: 3.5,
              scale: {
                kind: 'ambiguity_score',
                min: 1,
                max: 5,
                direction: 'lower_better',
              },
            },
          },
          spec_validation: {
            edition: 3,
            state: 'current',
            current_result: {
              score: 92,
              scale: {
                kind: 'percentage',
                min: 0,
                max: 100,
                direction: 'higher_better',
              },
            },
          },
          requirement_lint: {
            edition: 3,
            state: 'current',
            current_result: {
              score: 2,
              scale: {
                kind: 'finding_count',
                min: 0,
                max: 13,
                direction: 'lower_better',
              },
            },
          },
        }}
      />,
    );

    expect(screen.getByTestId('quality-summary-ambiguity')).toHaveTextContent(
      'AmbiguityNot assessed1 previous result',
    );
    expect(screen.getByTestId('quality-summary-ambiguity')).not.toHaveTextContent('3.5');
    expect(screen.queryByTestId('quality-summary-spec_validation')).not.toBeInTheDocument();
    expect(screen.getByTestId('quality-summary-requirement_lint')).toHaveTextContent(
      'Requirement lint2',
    );
    expect(screen.getByTestId('quality-summary-requirement_lint')).toHaveAttribute(
      'title',
      expect.stringContaining('0–13'),
    );
  });

  it('shows an explicitly empty current edition without inventing a score', () => {
    render(
      <QualitySummaryBadges
        summaries={{
          ambiguity: {
            edition: 2,
            state: 'not_started',
            previous_count: 1,
            current_result: null,
          },
        }}
      />,
    );

    const badge = screen.getByTestId('quality-summary-ambiguity');
    expect(badge).toHaveTextContent('AmbiguityNot assessed1 previous result');
    expect(badge).not.toHaveTextContent(/\d+(?:\.\d+)?\s*of/i);
    expect(badge).toHaveAttribute(
      'title',
      'Ambiguity: no current result · Edition 2; 1 previous result',
    );
  });
});
