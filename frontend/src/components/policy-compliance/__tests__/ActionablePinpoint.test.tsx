import axe from 'axe-core';
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { MetricScoreRing } from '@/components/shared/MetricScoreRing';

import {
  ActionablePinpoint,
  PolicyComplianceReadOnlyActions,
} from '../ActionablePinpoint';
import type { SemanticPinpointViewModel } from '../semanticPolicyModel';

const digest = 'a'.repeat(64);

function pinpoint(
  overrides: Partial<SemanticPinpointViewModel> = {},
): SemanticPinpointViewModel {
  return {
    contractVersion: 'v2',
    state: 'available',
    kind: 'issue',
    title: 'Persistence responsibility leaks into Core',
    detail: 'The use case owns a Community transaction detail.',
    severity: 'high',
    remediation: 'Move session ownership to the Community adapter.',
    blocking: true,
    categoryLabel: 'Structured item',
    locationLabel: 'Technical requirement · Persistence boundary',
    excerpt: 'Core creates and commits the SQLAlchemy session.',
    navigationTarget: '/specs/spec-1?focus=technical-requirements',
    unavailableMessage: null,
    technicalDetails: {
      anchorType: 'structured_child',
      sourceVersion: '12',
      anchorReference: 'tr-secret-id',
      excerptHash: digest,
      metricResultDigest: digest,
    },
    ...overrides,
  };
}

describe('ActionablePinpoint', () => {
  it('renders issue content in the ambiguity FindingItems hierarchy', () => {
    const { container } = render(
      <ActionablePinpoint pinpoint={pinpoint()} policyState="fail" />,
    );
    const text = container.textContent ?? '';
    const ordered = [
      'high',
      'issue',
      'Structured item',
      'Current issue',
      'Persistence responsibility leaks into Core',
      'The use case owns a Community transaction detail.',
      'Location',
      'Technical requirement · Persistence boundary',
      'Core creates and commits the SQLAlchemy session.',
      'Suggested remediation:',
      'Move session ownership to the Community adapter.',
      'Technical details',
    ].map((value) => text.indexOf(value));

    expect(ordered.every((position) => position >= 0)).toBe(true);
    expect([...ordered].sort((left, right) => left - right)).toEqual(ordered);
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(screen.queryByRole('spinbutton')).not.toBeInTheDocument();
  });

  it('renders positive evidence without invented issue fields', () => {
    render(
      <ActionablePinpoint
        pinpoint={pinpoint({
          kind: 'evidence',
          severity: null,
          remediation: null,
          blocking: false,
          title: 'The boundary is explicit',
        })}
        policyState="positive_evidence"
      />,
    );

    expect(screen.getByText('evidence')).toBeInTheDocument();
    expect(screen.getByText('Current evidence')).toBeInTheDocument();
    expect(screen.queryByText('Suggested remediation:')).not.toBeInTheDocument();
    expect(screen.queryByText(/blocking/iu)).not.toBeInTheDocument();
    expect(screen.queryByText(/low|medium|high|critical/iu))
      .not.toBeInTheDocument();
  });

  it('keeps circular score semantics and read-only available actions', () => {
    const onNavigate = vi.fn();
    const onHistory = vi.fn();
    const onRetry = vi.fn();
    const onGuidance = vi.fn();
    render(
      <>
        <MetricScoreRing
          label="Boundary integrity"
          value={62}
          direction="higher-is-better"
          threshold={80}
          testId="policy-score-ring"
        />
        <ActionablePinpoint
          pinpoint={pinpoint()}
          policyState="fail"
          onNavigate={onNavigate}
        />
        <PolicyComplianceReadOnlyActions
          onViewHistory={onHistory}
          onRetry={onRetry}
          onViewReassessmentGuidance={onGuidance}
        />
      </>,
    );

    const ring = screen.getByTestId('policy-score-ring');
    expect(ring).toHaveClass('rounded-full', 'border-4', 'border-red-400');
    expect(ring).toHaveAccessibleName(
      'Boundary integrity score 62 out of 100, higher is better, Minimum 80, threshold not met',
    );

    fireEvent.click(screen.getByRole('button', { name: 'Go to location' }));
    fireEvent.click(screen.getByRole('button', { name: 'Assessment history' }));
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    fireEvent.click(screen.getByRole('button', {
      name: 'View reassessment guidance',
    }));
    expect(onNavigate).toHaveBeenCalledWith(
      '/specs/spec-1?focus=technical-requirements',
    );
    expect(onHistory).toHaveBeenCalledOnce();
    expect(onRetry).toHaveBeenCalledOnce();
    expect(onGuidance).toHaveBeenCalledOnce();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });

  it('announces copy feedback and keeps protected details collapsed', async () => {
    const onCopy = vi.fn().mockResolvedValue(undefined);
    render(
      <ActionablePinpoint
        pinpoint={pinpoint()}
        policyState="fail"
        onCopy={onCopy}
      />,
    );

    const details = screen.getByText('Technical details').closest('details');
    expect(details).not.toHaveAttribute('open');
    expect(screen.getByRole('button', { name: 'Copy technical details' }))
      .toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', {
      name: 'Copy technical details',
    }));

    await waitFor(() => expect(onCopy).toHaveBeenCalledOnce());
    expect(onCopy.mock.calls[0][0]).toContain('Anchor reference: tr-secret-id');
    expect(screen.getByRole('status', { name: '' }))
      .toHaveTextContent('Technical details copied.');
  });

  it.each([
    ['removed', 'Referenced element is no longer available.'],
    ['inaccessible', 'Location unavailable with your current access.'],
  ] as const)('disables navigation for %s locations', (state, message) => {
    render(
      <ActionablePinpoint
        pinpoint={pinpoint({
          state,
          locationLabel: state === 'inaccessible'
            ? 'Restricted assessment location'
            : 'Technical requirement · Persistence boundary',
          excerpt: state === 'inaccessible' ? null : 'Sealed safe excerpt.',
          navigationTarget: null,
          unavailableMessage: message,
          technicalDetails: state === 'inaccessible'
            ? { anchorType: 'structured_child', sourceVersion: '12' }
            : pinpoint().technicalDetails,
        })}
        policyState={state}
        onNavigate={vi.fn()}
      />,
    );

    expect(screen.getByText(message)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Go to location' }))
      .not.toBeInTheDocument();
    if (state === 'inaccessible') {
      expect(screen.queryByText('Sealed safe excerpt.')).not.toBeInTheDocument();
      expect(screen.queryByText('tr-secret-id')).not.toBeInTheDocument();
      expect(screen.queryByText(digest)).not.toBeInTheDocument();
    }
  });

  it('has no critical or serious axe violations', async () => {
    const { container } = render(
      <main>
        <h2>Policy Compliance</h2>
        <ActionablePinpoint
          pinpoint={pinpoint()}
          policyState="fail"
          onNavigate={vi.fn()}
          onCopy={vi.fn()}
        />
        <PolicyComplianceReadOnlyActions
          onViewHistory={vi.fn()}
          onRetry={vi.fn()}
          onViewReassessmentGuidance={vi.fn()}
        />
      </main>,
    );

    const result = await axe.run(container);
    expect(result.violations.filter((violation) =>
      violation.impact === 'critical' || violation.impact === 'serious'
    )).toEqual([]);
    expect(within(container).getAllByRole('button').length).toBeGreaterThan(0);
  });
});
