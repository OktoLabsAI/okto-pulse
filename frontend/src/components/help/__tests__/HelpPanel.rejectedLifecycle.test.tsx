import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { HelpPanel } from '../HelpPanel';

describe('HelpPanel Rejected lifecycle guidance', () => {
  it('separates evaluator work from executor rework and preserves Test behavior', () => {
    render(<HelpPanel initialSectionId="tasks" onClose={vi.fn()} />);

    const dialog = screen.getByRole('dialog', { name: 'Help Guide' });
    expect(dialog).toHaveTextContent(/Rejected means rework is required/i);
    expect(dialog).toHaveTextContent(/only manual exit is rejected.*in_progress/i);
    expect(dialog).toHaveTextContent(/cannot be created, manually moved, or dragged into it/i);
    expect(dialog).toHaveTextContent(/new execution report and Current technical traceability/i);
    expect(dialog).toHaveTextContent(/Test Cards.*never receive Rejected/i);
    expect(dialog).not.toHaveTextContent(/fails.*returns to.*not_started/i);
  });
});
