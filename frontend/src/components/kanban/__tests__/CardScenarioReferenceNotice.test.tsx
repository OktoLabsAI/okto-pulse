import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { Card } from '../../../types';
import { CardScenarioReferenceNotice } from '../CardScenarioReferenceNotice';

const context: NonNullable<Card['scenario_reference_context']> = {
  contract_version: 'card-scenario-reference-context/v1', status: 'available',
  source_fingerprint: 'a'.repeat(64), finding_count: 1, truncated: false,
  findings: [{ finding_id: 'finding', source_selector: 'card:c:test_scenario_ids',
    target_ref: 'spec:s:test_scenario:missing', reason_code: 'target_absent', correction_surface: 'card_scenario_links' }],
};

describe('Card scenario reference context', () => {
  it('shows exact source and target and clears the issue after an updated source read', () => {
    const { rerender } = render(<CardScenarioReferenceNotice context={context} />);
    expect(screen.getByText('Reference: spec:s:test_scenario:missing')).toBeInTheDocument();
    expect(screen.getByText('Source: card:c:test_scenario_ids')).toBeInTheDocument();
    expect(screen.getByText(/Review this Card’s scenario links/)).toBeInTheDocument();
    rerender(<CardScenarioReferenceNotice context={{ ...context, finding_count: 0, findings: [], source_fingerprint: 'b'.repeat(64) }} />);
    expect(screen.queryByRole('region', { name: 'Scenario reference issues' })).not.toBeInTheDocument();
  });
  it('does not present unavailable as an empty successful check or expose denied content', () => {
    const { rerender } = render(<CardScenarioReferenceNotice context={null} />);
    expect(screen.getByRole('status')).toHaveTextContent('unavailable');
    rerender(<CardScenarioReferenceNotice context={{ ...context, status: 'unavailable', finding_count: null, findings: [] }} />);
    expect(screen.getByRole('status')).toHaveTextContent('unavailable');
    rerender(<CardScenarioReferenceNotice context={{ ...context, status: 'not_authorized' }} />);
    expect(screen.queryByText(/missing/)).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });
  it('keeps total and truncation visible even if no exact entry fits the response', () => {
    render(<CardScenarioReferenceNotice context={{ ...context, finding_count: 25, findings: [], truncated: true }} />);
    expect(screen.getByText('25 scenario reference issue(s)')).toBeInTheDocument();
    expect(screen.getByText(/Showing a bounded selection/)).toBeInTheDocument();
  });
  it('points ambiguous identities to the source Spec without offering graph repair', () => {
    render(<CardScenarioReferenceNotice context={{ ...context, findings: [{ ...context.findings[0],
      reason_code: 'target_ambiguous', correction_surface: 'spec_test_scenarios' }] }} />);
    expect(screen.getByText(/Review the test scenarios in the Spec/)).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});

it('shows source disagreement without treating the observed link as completed work', () => {
  const { rerender } = render(<CardScenarioReferenceNotice context={{ ...context, findings: [{
    ...context.findings[0], reason_code: 'source_disagreement', correction_surface: 'card_and_spec_scenario_links',
  }] }} />);
  expect(screen.getByText(/Card and Spec declare different scenario links/)).toHaveTextContent('does not prove execution');
  expect(screen.getByText(/Review the scenario links in both/)).toHaveTextContent('current evidence checks');
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
  rerender(<CardScenarioReferenceNotice context={{ ...context, finding_count: 0, findings: [] }} />);
  expect(screen.queryByRole('region', { name: 'Scenario reference issues' })).not.toBeInTheDocument();
});
