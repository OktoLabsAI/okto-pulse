import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { SpecValidationSummary } from '@/types';
import { SpecValidationSummaryCard } from '../SpecPlanningSummary';

const summary: SpecValidationSummary = {
  contract_version: 'validation-summary/v1', edition: 2, verdict: 'validated',
  planning_state: 'ready', reason_code: 'validation_completed',
  blockers_count: 0, exception_count: 0, next_action: null,
};

describe('Canonical validation result', () => {
  it.each([
    ['validated', 'Spec validated'],
    ['validated_with_exception', 'Spec validated with exceptions'],
    ['ready_to_conclude', 'Ready to conclude validation'],
    ['not_validated', 'Spec not yet validated'],
    ['unavailable', 'Validation could not be confirmed'],
    ['not_required', 'Validation not required'],
  ] as const)('presents the server verdict %s without inferring a lifecycle', (verdict, title) => {
    render(<SpecValidationSummaryCard summary={{ ...summary, verdict }} edition={2} loading={false} />);
    expect(screen.getByRole('heading', { name: title })).toBeInTheDocument();
  });

  it('does not use approval of an earlier edition or reveal its counts', () => {
    render(<SpecValidationSummaryCard summary={{ ...summary, edition: 1, exception_count: 7 }} edition={2} loading={false} />);
    expect(screen.getByText('Validation could not be confirmed')).toBeInTheDocument();
    expect(screen.queryByText(/7 recorded/)).not.toBeInTheDocument();
    expect(screen.queryByRole('img', { name: 'Validation succeeded' })).not.toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Validation unavailable' })).toBeInTheDocument();
  });

  it.each([
    ['validated', 'Validation succeeded', 'lucide-circle-check'],
    ['validated_with_exception', 'Validation succeeded', 'lucide-circle-check'],
    ['not_validated', 'Validation not complete', 'lucide-circle-x'],
    ['ready_to_conclude', 'Validation not complete', 'lucide-circle-x'],
    ['unavailable', 'Validation unavailable', 'lucide-circle-help'],
    ['not_required', 'Validation not required', 'lucide-circle-minus'],
  ] as const)('shows an accessible status icon for %s', (verdict, label, iconClass) => {
    render(<SpecValidationSummaryCard summary={{ ...summary, verdict }} edition={2} loading={false} />);
    expect(screen.getByRole('img', { name: label })).toHaveClass(iconClass);
  });

  it('requires both a server action and a permitted callback', () => {
    const { rerender } = render(<SpecValidationSummaryCard summary={summary} edition={2} loading={false} onAction={vi.fn()} />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    rerender(<SpecValidationSummaryCard summary={{ ...summary, next_action: 'submit_spec_validation' }} edition={2} loading={false} />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});
