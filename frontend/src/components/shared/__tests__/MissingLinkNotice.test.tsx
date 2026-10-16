import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MissingLinkNotice } from '../MissingLinkNotice';
import type { MissingLinkContext } from '@/types';

const context: MissingLinkContext = {
  status: 'available', mode: 'blocking', authority: 'relational_source', would_block_done: true,
  finding_count: 1, truncated: false, findings: [{ source_ref: 'spec:s:decisions:d',
    field: 'linked_requirements', target_ref: 'fr-1', reason: 'target_absent',
    correction_operation: 'update_spec_entity' }],
};

describe('MissingLinkNotice', () => {
  it('shows exact references and current policy, then clears a corrected observation', () => {
    const { rerender } = render(<MissingLinkNotice context={context} />);
    expect(screen.getByText('Reference: fr-1')).toBeInTheDocument();
    expect(screen.getByText('Correct these references before completion.')).toBeInTheDocument();
    rerender(<MissingLinkNotice context={{ ...context, mode: 'advisory', would_block_done: false }} />);
    expect(screen.getByText('Advisory: these references do not block completion.')).toBeInTheDocument();
    rerender(<MissingLinkNotice context={{ ...context, finding_count: 0, findings: [], would_block_done: false }} />);
    expect(screen.queryByRole('region', { name: 'Missing reference checks' })).not.toBeInTheDocument();
  });
  it('does not mistake unavailability for zero or expose denied details', () => {
    const { rerender } = render(<MissingLinkNotice context={{ ...context, status: 'unavailable', findings: [], finding_count: null }} />);
    expect(screen.getByRole('status')).toHaveTextContent('Completion requires a successful check');
    expect(screen.queryByText('Reference: fr-1')).not.toBeInTheDocument();
    rerender(<MissingLinkNotice context={{ ...context, status: 'not_authorized' }} />);
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.queryByText('Reference: fr-1')).not.toBeInTheDocument();
  });
  it('preserves the full count when details are bounded', () => {
    render(<MissingLinkNotice context={{ ...context, finding_count: 100, truncated: true }} />);
    expect(screen.getByText('100 unresolved declared reference(s)')).toBeInTheDocument();
    expect(screen.getByText(/Showing a bounded selection/)).toBeInTheDocument();
  });
});
