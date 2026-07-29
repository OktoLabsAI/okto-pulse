import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { MetricScoreRing } from '../MetricScoreRing';

describe('MetricScoreRing', () => {
  it('renders a higher-is-better score with its default maximum and passing threshold', () => {
    render(
      <MetricScoreRing
        label="Completeness"
        value={86}
        direction="higher-is-better"
        threshold={80}
        testId="completeness-score"
      />,
    );

    const ring = screen.getByTestId('completeness-score');
    expect(ring).toHaveAccessibleName(
      'Completeness score 86 out of 100, higher is better, Minimum 80, threshold met',
    );
    expect(ring).toHaveClass(
      'h-20',
      'w-20',
      'rounded-full',
      'border-4',
      'border-emerald-400',
    );
    expect(ring).toHaveAttribute('data-direction', 'higher-is-better');
    expect(ring).toHaveAttribute('data-status', 'met');
    expect(within(ring).getByText('/100')).toBeInTheDocument();
    expect(screen.getByText('Higher is better')).toBeInTheDocument();
    expect(screen.getByText('Minimum 80')).toBeInTheDocument();
    expect(screen.getByText('· Met')).toBeInTheDocument();
  });

  it('uses a danger tone when a higher-is-better threshold is not met', () => {
    render(
      <MetricScoreRing
        label="Confidence"
        value={69}
        direction="higher-is-better"
        threshold={70}
      />,
    );

    const ring = screen.getByTestId('metric-score-ring');
    expect(ring).toHaveClass('border-red-400');
    expect(ring).toHaveAttribute('data-status', 'not-met');
    expect(ring).toHaveAccessibleName(
      'Confidence score 69 out of 100, higher is better, Minimum 70, threshold not met',
    );
    expect(screen.getByText('· Not met')).toBeInTheDocument();
  });

  it('treats a lower-is-better threshold as a maximum', () => {
    render(
      <MetricScoreRing
        label="Drift"
        value={24}
        direction="lower-is-better"
        threshold={30}
        testId="drift-score"
      />,
    );

    const ring = screen.getByTestId('drift-score');
    expect(ring).toHaveClass('border-emerald-400');
    expect(ring).toHaveAccessibleName(
      'Drift score 24 out of 100, lower is better, Maximum 30, threshold met',
    );
    expect(screen.getByText('Lower is better')).toBeInTheDocument();
    expect(screen.getByText('Maximum 30')).toBeInTheDocument();
  });

  it('stays neutral when no threshold, status, or tone is supplied', () => {
    render(
      <MetricScoreRing
        label="Completeness"
        value={72}
        direction="higher-is-better"
      />,
    );

    const ring = screen.getByTestId('metric-score-ring');
    expect(ring).toHaveClass('border-surface-300');
    expect(ring).not.toHaveClass('border-emerald-400', 'border-red-400');
    expect(ring).toHaveAttribute('data-status', 'neutral');
    expect(ring).toHaveAccessibleName(
      'Completeness score 72 out of 100, higher is better, No threshold configured',
    );
    expect(screen.getByText('No threshold configured')).toBeInTheDocument();
  });

  it('supports an externally resolved status or a presentational tone when no threshold exists', () => {
    const { rerender } = render(
      <MetricScoreRing
        label="Confidence"
        value={91}
        direction="higher-is-better"
        status="met"
      />,
    );

    let ring = screen.getByTestId('metric-score-ring');
    expect(ring).toHaveClass('border-emerald-400');
    expect(ring).toHaveAccessibleName(
      'Confidence score 91 out of 100, higher is better, No threshold configured, status met',
    );

    rerender(
      <MetricScoreRing
        label="Confidence"
        value={91}
        direction="higher-is-better"
        tone="warning"
      />,
    );

    ring = screen.getByTestId('metric-score-ring');
    expect(ring).toHaveClass('border-amber-400');
    expect(ring).toHaveAttribute('data-status', 'neutral');
  });

  it('lets a configured threshold override an explicit status and tone', () => {
    render(
      <MetricScoreRing
        label="Drift"
        value={55}
        direction="lower-is-better"
        threshold={50}
        status="met"
        tone="success"
      />,
    );

    const ring = screen.getByTestId('metric-score-ring');
    expect(ring).toHaveClass('border-red-400');
    expect(ring).not.toHaveClass('border-emerald-400');
    expect(ring).toHaveAttribute('data-status', 'not-met');
  });

  it('honors a custom maximum and preserves decimal scores', () => {
    render(
      <MetricScoreRing
        label="Risk"
        value={2.75}
        max={5}
        direction="lower-is-better"
        testId="risk-score"
      />,
    );

    const ring = screen.getByTestId('risk-score');
    expect(ring).toHaveAccessibleName(
      'Risk score 2.75 out of 5, lower is better, No threshold configured',
    );
    expect(within(ring).getByText('/5')).toBeInTheDocument();
  });
});
