import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ActivityLogList } from '../ActivityLogList';
import type { ActivityLogEntry } from '@/services/api';

const baseEntry: ActivityLogEntry = {
  id: 'act-1',
  action: 'structured_entity_updated',
  actor_type: 'agent',
  actor_id: 'agent-1',
  actor_name: 'Validator Agent',
  created_at: '2026-05-29T10:15:00Z',
  summary: 'structured_entity updated type=functional_requirement field=description',
  trigger: 'structured_entity_updated',
  details: {
    field: 'description',
    before: { text: 'old' },
    after: { text: 'new' },
    token: '[redacted]',
  },
};

describe('ActivityLogList', () => {
  it('renders summary as primary text and metadata separately', () => {
    render(<ActivityLogList entries={[baseEntry]} />);

    expect(screen.getByText(baseEntry.summary)).toBeInTheDocument();
    expect(screen.getByText('Validator Agent')).toBeInTheDocument();
    expect(screen.getByText('agent')).toBeInTheDocument();
    expect(screen.getByText('structured_entity_updated')).toBeInTheDocument();
  });

  it('keeps details collapsed by default and expands safe JSON on demand', () => {
    render(<ActivityLogList entries={[baseEntry]} />);

    const details = screen.getByText('Details').closest('details');
    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute('open');

    fireEvent.click(screen.getByText('Details'));
    expect(details).toHaveAttribute('open');
    expect(screen.getByText(/"token": "\[redacted\]"/)).toBeInTheDocument();
  });

  it('does not render object representation strings for nested details', () => {
    render(<ActivityLogList entries={[baseEntry]} />);

    const renderedText = document.body.textContent ?? '';
    expect(renderedText).not.toContain('[object Object]');
    expect(renderedText).not.toContain('[object: object]');
  });

  it('renders the empty state without errors', () => {
    render(<ActivityLogList entries={[]} />);
    expect(screen.getByText('No activity recorded')).toBeInTheDocument();
  });
});
