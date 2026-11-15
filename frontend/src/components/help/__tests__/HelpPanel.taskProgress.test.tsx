import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { HelpPanel } from '../HelpPanel';

describe('Task progress user guide', () => {
  it('explains event cadence, delivery proof and safe resumption in user language', () => {
    render(<HelpPanel initialSectionId="delivery-progress" onClose={vi.fn()} />);
    const content = screen.getByRole('heading', { name: 'Task progress — What changed and what remains' }).parentElement!;
    expect(content).toHaveTextContent('does not need to post an update every few minutes');
    expect(content).toHaveTextContent('A progress update does not complete a task');
    expect(content).toHaveTextContent('tests and review');
    expect(content).toHaveTextContent('A failed submission saves none of that combined operation');
    expect(content).toHaveTextContent('check access to the actual source');
    expect(content).toHaveTextContent('depend on your permissions');
    expect(content).not.toHaveTextContent('card-delivery-report/v1');
  });
});
